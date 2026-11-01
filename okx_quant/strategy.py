# -*- coding: utf-8 -*-
"""策略层：参数化的资金费率中性套利 + 规则筛选引擎。

这里只做"规则筛"，不调模型、不下单：
  strategies.json  用户定义的策略（标的池、门槛、仓位上限、杠杆、止损）
  watch.json       自动盯盘的开关与频率
  candidates.json  规则筛出的候选动作，等 LLM 复核

分工：
  规则引擎（本文件）负责"能不能做"—— 硬条件，确定性、可复现
  LLM 复核（DSH 会话）负责"还要不要做"—— 读新闻、看上下文，可否决但不能加仓

CLI:
  python strategy.py list                     列出策略
  python strategy.py add --name X [参数]       新建策略
  python strategy.py toggle --id X            启用/停用
  python strategy.py rm --id X                删除
  python strategy.py eval                     用最新数据跑一遍规则
  python strategy.py watch --interval 15 --on  设置盯盘
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import funding_arb_live as FL
import patterns as PT
import datafeed as DF

_DF_CACHE = {}

STATE_DIR = os.path.join(ROOT, "state")
STRATEGIES = os.path.join(STATE_DIR, "strategies.json")
WATCH = os.path.join(STATE_DIR, "watch.json")
CANDIDATES = os.path.join(STATE_DIR, "candidates.json")
CST = timezone(timedelta(hours=8))

# 参数默认值：新策略从这套开始，也是面板"新建策略"表单的初始值
DEFAULT_PARAMS = {
    "coins": [],              # 空 = 所有可对冲标的
    "min_apr": 30.0,          # 年化低于此不做（10.95% 的基准费率被 0.26% 手续费吃掉）
    "max_basis": 0.8,         # 现货/永续价差绝对值上限 %
    "max_usd_per_coin": 200.0,
    "max_total_usd": 1000.0,
    "max_positions": 5,
    "leverage": 1.0,
    "stop_loss_pct": 5.0,     # 单腿浮亏超过这个比例触发复核
}

DEFAULT_WATCH = {
    "enabled": False,
    "interval_min": 15,
    "llm_review": True,       # 规则筛完后是否要 LLM 复核才执行
    "scan_funding": False,    # 是否顺带扫全市场资金费率（40~60s）；只做单币合约时关掉
    "last_tick": None,
    "last_tick_sec": None,
    "next_tick": None,
    "ticks": 0,
}

# 单币合约方向交易的参数。与套利策略完全不同的一套：没有费率、没有两腿，
# 只有一个币、一个方向、一个止损止盈。
#
# 注意这里的两层：
#   pattern  —— 「该不该在这个位置动手」（patterns.py 里的一套条件组合）
#   下面几项 —— 「动手时最多能有多大」（仓位、杠杆、止损止盈的上限）
# 早期版本把 RSI/区间位置 直接当触发条件，那只是一种筛子，表达不了
# 「回调买」「突破买」「超跌反弹买」这些不同的入场逻辑，所以抽成了模板。
DEFAULT_DIRECTIONAL = {
    "coin": "BTC",
    "timeframe": "4h",        # 自定义周期：1m/5m/15m/30m/1h/4h/1d
    "side": "both",           # long / short / both
    "usd": 100.0,             # 单笔名义上限（LLM 只能在这个数内缩）
    "leverage": 3.0,          # 杠杆上限（LLM 不能超过）
    "stop_loss_pct": 3.0,     # 止损距离上限，LLM 给的止损不能比这个更远
    "take_profit_pct": 9.0,   # 止盈距离上限
    "min_confidence": 0.55,   # LLM 置信度低于这个就否决
    "template": "trend_pullback",   # 逻辑模板名（patterns.TEMPLATES 的键）
    "mode": "all",                  # all=全部条件满足 / any=任一满足
    "rules": [],                    # 具体规则，默认由 template 展开
}

TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]


def _tpl_rules(key):
    """把模板展开成可编辑的规则列表（深拷贝，防止改一处污染模板）。"""
    tpl = PT.TEMPLATES.get(key) or PT.TEMPLATES["trend_pullback"]
    return [{"cond": r["cond"], "args": dict(r.get("args") or {})} for r in tpl["rules"]]


DEFAULT_DIRECTIONAL["rules"] = _tpl_rules(DEFAULT_DIRECTIONAL["template"])


def _pattern_of(p):
    """从参数里取出信号定义。旧策略没有 rules 时按 template 兜底。"""
    rules = p.get("rules")
    if not isinstance(rules, list) or not rules:
        rules = _tpl_rules(p.get("template") or DEFAULT_DIRECTIONAL["template"])
    return {
        "side": p.get("side") or "long",
        "mode": p.get("mode") or "all",
        "rules": rules,
    }


def now_iso():
    return datetime.now(CST).isoformat(timespec="seconds")


def _read(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)


# ------------------------------------------------------------------ 策略
def load_strategies():
    raw = _read(STRATEGIES, {"strategies": []})
    if isinstance(raw, list):        # 容错：手写成数组也能读
        raw = {"strategies": raw}
    return raw.get("strategies") or []


def save_strategies(items):
    _write(STRATEGIES, {"updated_at": now_iso(), "strategies": items})


def make_strategy(name, params=None, sid=None, stype="funding_arb"):
    """建一个策略。stype 决定用哪套默认参数和哪套评估逻辑。"""
    if stype not in ("funding_arb", "directional"):
        raise ValueError(f"未知策略类型 {stype}")
    base = DEFAULT_DIRECTIONAL if stype == "directional" else DEFAULT_PARAMS
    p = dict(base)
    p.update(params or {})
    # 上限不能超过执行层的硬约束，否则规则放行、执行层拒单，两边打架
    if stype == "directional":
        p["usd"] = min(float(p["usd"]), float(FL.MAX_USD_PER_COIN))
        if str(p.get("timeframe")) not in TIMEFRAMES:
            p["timeframe"] = "4h"
        if str(p.get("side")) not in ("long", "short", "both"):
            p["side"] = "both"
        if str(p.get("mode")) not in ("all", "any"):
            p["mode"] = "all"
        tpl = str(p.get("template") or "")
        if tpl not in PT.TEMPLATES:
            tpl = DEFAULT_DIRECTIONAL["template"]
            p["template"] = tpl
        # rules 必须是真的列表；从模板复制一份，避免多套策略共享同一个对象
        if not isinstance(p.get("rules"), list) or not p["rules"]:
            p["rules"] = _tpl_rules(tpl)
        else:
            p["rules"] = [
                {"cond": str(r.get("cond")), "args": dict(r.get("args") or {})}
                for r in p["rules"] if isinstance(r, dict) and r.get("cond") in PT.CONDITIONS
            ]
        if not p["rules"]:
            p["rules"] = _tpl_rules(tpl)
    else:
        p["max_usd_per_coin"] = min(float(p["max_usd_per_coin"]), float(FL.MAX_USD_PER_COIN))
        p["max_total_usd"] = min(float(p["max_total_usd"]), float(FL.MAX_TOTAL_USD))
    return {
        "id": sid or f"strat-{int(time.time())}-{os.urandom(2).hex()}",
        "name": name or ("单币方向" if stype == "directional" else "未命名策略"),
        "type": stype,
        "enabled": True,
        "params": p,
        "created_at": now_iso(),
    }


def upsert(strategy):
    items = load_strategies()
    for i, s in enumerate(items):
        if s["id"] == strategy["id"]:
            strategy["created_at"] = s.get("created_at", strategy["created_at"])
            strategy["updated_at"] = now_iso()
            items[i] = strategy
            break
    else:
        items.append(strategy)
    save_strategies(items)
    return strategy


def toggle(sid, enabled=None):
    items = load_strategies()
    for s in items:
        if s["id"] == sid:
            s["enabled"] = (not s.get("enabled")) if enabled is None else bool(enabled)
            s["updated_at"] = now_iso()
            save_strategies(items)
            return s
    return None


def remove(sid):
    items = load_strategies()
    keep = [s for s in items if s["id"] != sid]
    if len(keep) == len(items):
        return False
    save_strategies(keep)
    return True


# ------------------------------------------------------------------ 盯盘
def load_watch():
    w = dict(DEFAULT_WATCH)
    w.update(_read(WATCH, {}))
    return w


def save_watch(w):
    _write(WATCH, w)
    return w


# ------------------------------------------------------------------ 规则引擎
def _market_snapshot(coin, timeframe, with_spark=False):
    """拿某个币某个周期的 K 线与指标，供方向策略判断。

    返回的字典既给规则用（rsi/pos/vol_ratio/atr_pct），也原样塞进候选动作给 LLM 看。

    数据源**只有 OKX 永续**：面板和规则看到的必须是真正下单的那个市场。
    以前用 Gate 现货 K 线，主流币差 0.1% 无所谓，小币能差 1~2%，止损价会算歪；
    后来改成「OKX 失败回退 Gate」，更糟 —— 拿 Gate 的价去算 OKX 的止损位，
    表面有信号其实位置是错的。现在取不到就返回 error，宁可不给信号。
    """
    try:
        import datafeed as DF
        import features as F
    except Exception as e:
        return {"error": f"数据模块导入失败：{type(e).__name__}: {e}"}

    pair = f"{str(coin).upper()}_USDT"
    # 只认 OKX。这里以前会在 OKX 失败时悄悄回退 Gate 现货，
    # 结果是「用 Gate 的价格算止损、去 OKX 下单」——两个交易所永续差了
    # 0.1%~0.5%，止损位就是错的。宁可不给信号，也不给错的价格。
    source = "okx"
    try:
        df = DF.load_okx_bars(str(coin).upper(), timeframe, bars=300, ttl=300, verbose=False)
    except Exception as e:
        return {"error": f"OKX K 线不可用：{type(e).__name__}: {str(e)[:80]}"}
    if df is None or len(df) < 60:
        n = 0 if df is None else len(df)
        return {"error": f"OKX {pair} {timeframe} 只有 {n} 根 K 线"
                        f"（代理没开？OKX 需要 http://127.0.0.1:7897）"}

    close = float(df["close"].iloc[-1])
    try:
        f = F.build_features(df)
        row = f.iloc[-1]
    except Exception as e:
        return {"error": f"算指标失败：{type(e).__name__}: {str(e)[:80]}"}

    def val(key, mult=1.0, default=None):
        v = row.get(key)
        if v is None or pd_isna(v):
            return default
        try:
            return round(float(v) * mult, 4)
        except Exception:
            return default

    out = {
        "coin": str(coin).upper(),
        "timeframe": timeframe,
        "price": round(close, 6),
        "bar_time": str(df.index[-1]),
        "bars": len(df),
        "source": source,
        "rsi": val("rsi_14", 100.0),
        "pos": val("pos_24", 100.0),
        "pos_long": val("pos_96", 100.0),
        "vol_ratio": val("vol_ratio_24"),
        "atr_pct": val("atr_pct", 100.0),
        "ema_fast_slow": val("ema_fast_slow", 100.0),
        "macd_hist": val("macd_hist", 10000.0),
        "ret_24": val("ret_24", 100.0),
        "ret_96": val("ret_96", 100.0),
        "upper_wick": val("upper_wick"),
        "lower_wick": val("lower_wick"),
    }

    if with_spark:
        try:
            tail = df["close"].iloc[-60:].astype(float)
            base = float(tail.iloc[0]) or 1.0
            out["spark"] = [round((float(v) / base - 1.0) * 100.0, 3) for v in tail]
        except Exception:
            out["spark"] = []

    return out


def pd_isna(v):
    """不引入 pandas 的 NaN 判断（features 里全是 float/nan）。"""
    try:
        return v != v          # NaN != NaN
    except Exception:
        return False


def _load_df(coin, timeframe):
    """按 (币, 周期) 缓存 K 线。同一轮评估里多套策略用同一份数据时不重复拉。

    只用 OKX 永续（与下单市场一致），取不到返回 None，不回退 Gate。
    模板最多回看 96 根，300 根足够，不必拉 1200 —— 多拉是纯浪费。
    """
    key = "%s|%s" % (coin, timeframe)
    if key in _DF_CACHE:
        return _DF_CACHE[key]
    # 同 _market_snapshot：只认 OKX，不回退 Gate
    try:
        df = DF.load_okx_bars(str(coin).upper(), timeframe, bars=300, ttl=300, verbose=False)
    except Exception:
        df = None
    if df is None or len(df) < 60:
        df = None
    _DF_CACHE[key] = df
    return df


def evaluate(strategy, funding_rows, account, market=None):
    """按策略类型分派。规则层只回答"条件成立吗"，不做任何主观判断。"""
    stype = strategy.get("type") or "funding_arb"
    if stype == "directional":
        return _eval_directional(strategy, market, account)
    return _eval_funding_arb(strategy, funding_rows, account)


def _eval_funding_arb(strategy, funding_rows, account):
    """资金费率中性套利：从费率表筛出能两腿对冲的标的。"""
    p = strategy.get("params") or DEFAULT_PARAMS
    res = {
        "strategy_id": strategy.get("id"),
        "name": strategy.get("name"),
        "enabled": bool(strategy.get("enabled")),
        "matched": [],
        "rejected": [],
        "actions": [],
        "note": None,
    }
    if not res["enabled"]:
        res["note"] = "策略未启用"
        return res

    positions = account.get("positions") or []
    held = {str(x.get("symbol") or "").split("/")[0].upper() for x in positions}
    held.discard("")
    # 新开仓两条腿都吃保证金，用名义价值近似占用
    used = 0.0
    for x in positions:
        try:
            used += abs(float(x.get("notional") or 0))
        except Exception:
            pass
    remaining = max(0.0, float(p.get("max_total_usd") or 0) - used)
    slots = max(0, int(p.get("max_positions") or 0) - len(held))

    whitelist = {str(c).upper() for c in (p.get("coins") or [])}
    for r in funding_rows:
        coin = str(r.get("coin") or "").upper()
        apr = float(r.get("apr") or 0)
        basis = float(r.get("basis_pct") or 0)

        reasons = []
        if coin in FL.BLACKLIST:
            reasons.append("在执行层黑名单里")
        if whitelist and coin not in whitelist:
            reasons.append("不在策略标的池")
        if apr < float(p.get("min_apr") or 0):
            reasons.append(f"年化 {apr:.2f}% 低于门槛 {p.get('min_apr')}%")
        if abs(basis) > float(p.get("max_basis") or 0):
            reasons.append(f"基差 {basis:.2f}% 超过 {p.get('max_basis')}%")
        if coin in held:
            reasons.append("已持仓")
        if not reasons and slots <= 0:
            reasons.append(f"已达持仓数上限 {p.get('max_positions')}")
        if not reasons and remaining < 10:
            reasons.append(f"总敞口已用尽（剩余 {remaining:.0f} USDT）")

        if reasons:
            res["rejected"].append({"coin": coin, "apr": round(apr, 2),
                                    "basis_pct": round(basis, 2), "why": reasons})
            continue

        usd = min(float(p.get("max_usd_per_coin") or 0), remaining)
        res["matched"].append({"coin": coin, "apr": round(apr, 2),
                               "basis_pct": round(basis, 2), "usd": round(usd, 2)})
        res["actions"].append({
            "strategy_id": strategy.get("id"),
            "op": "open",
            "coin": coin,
            "usd": round(usd, 2),
            "leverage": p.get("leverage"),
            "reason": (f"策略「{strategy.get('name')}」命中：年化 {apr:.2f}% "
                       f">= {p.get('min_apr')}%，基差 {basis:+.2f}% 在 ±{p.get('max_basis')}% 内"),
        })
        slots -= 1
        remaining -= usd

    if not res["matched"]:
        res["note"] = "本轮没有标的满足策略条件"
    return res


def _eval_directional(strategy, market, account):
    """单币合约方向交易：先判断该不该让 LLM 看一眼，再产候选。

    规则层只做一件事：把"值得分析"的时刻筛出来。真正的买点、仓位、杠杆、
    止盈止损由 LLM 在下游计算，再由 llm_trade.py 的硬校验兜底。
    """
    p = dict(DEFAULT_DIRECTIONAL)
    p.update(strategy.get("params") or {})
    res = {
        "strategy_id": strategy.get("id"),
        "name": strategy.get("name"),
        "type": "directional",
        "enabled": bool(strategy.get("enabled")),
        "matched": [],
        "rejected": [],
        "actions": [],
        "note": None,
    }
    if not res["enabled"]:
        res["note"] = "策略未启用"
        return res

    coin = str(p.get("coin") or "BTC").upper()
    tf = str(p.get("timeframe") or "4h")
    side = str(p.get("side") or "both")

    snap = (market or {}).get(f"{coin}|{tf}")
    if not snap:
        res["note"] = f"没有 {coin} {tf} 的行情快照"
        return res
    if snap.get("error"):
        res["note"] = snap["error"]
        return res

    tpl = str(p.get("template") or DEFAULT_DIRECTIONAL["template"])
    tpl_info = PT.TEMPLATES.get(tpl) or {}
    # 模板自带一个方向（回调买就是 long、反弹空就是 short）。
    # 策略里显式指定了 long/short 就以策略为准，选 both 就听模板的。
    trig_side = side if side in ("long", "short") else (tpl_info.get("side") or "long")

    df = _load_df(coin, tf)
    if df is None or len(df) < 120:
        res["note"] = "%s %s 的 K 线不足（%d 根），跳过本轮" % (coin, tf, 0 if df is None else len(df))
        return res

    pattern = _pattern_of(p)
    pattern["side"] = trig_side
    try:
        scan = PT.scan_last(df, pattern)
    except Exception as e:
        res["note"] = "规则求值失败：%s" % e
        return res

    # 把每条规则的当前值/阈值/是否通过原样带出去。前端只负责显示，
    # 判断逻辑只有 patterns.py 一份，别处复刻必然走偏。
    res["state"] = {
        "coin": coin, "timeframe": tf, "side": trig_side,
        "price": scan.get("last_close") or snap.get("price"),
        "bar_time": snap.get("bar_time"),
        "bars": len(df),
        "template": tpl,
        "template_label": tpl_info.get("label") or tpl,
        "mode": scan.get("mode") or "all",
        "rules": scan.get("rules") or [],
        "fired": bool(scan.get("fired")),
        "desc": PT.describe(pattern),
        "rsi": snap.get("rsi"), "pos": snap.get("pos"), "pos_long": snap.get("pos_long"),
        "vol_ratio": snap.get("vol_ratio"), "atr_pct": snap.get("atr_pct"),
    }

    if not scan.get("fired"):
        failed = [r for r in (scan.get("rules") or []) if not r.get("ok")]
        brief = "、".join(r["label"] for r in failed[:3]) or "条件不满足"
        res["note"] = "未触发 · 卡在 " + brief
        return res

    if coin in FL.BLACKLIST:
        res["note"] = f"{coin} 在执行层黑名单里"
        return res

    positions = account.get("positions") or []
    held = {str(x.get("symbol") or "").split("/")[0].upper() for x in positions}
    held.discard("")
    used = 0.0
    for x in positions:
        try:
            used += abs(float(x.get("notional") or 0))
        except Exception:
            pass
    usd = float(p.get("usd") or 0)
    remaining = max(0.0, float(FL.MAX_TOTAL_USD) - used)

    if coin in held:
        res["note"] = f"{coin} 已有持仓，不重复开（先平再加）"
        return res
    if remaining < usd * 0.5:
        res["note"] = f"总敞口只剩 {remaining:.0f} USDT，不够开 {usd:.0f}"
        return res

    usd = min(usd, remaining)
    order = "buy" if trig_side == "long" else "sell"
    res["matched"].append({
        "coin": coin, "side": trig_side, "price": scan.get("last_close") or snap.get("price"),
        "usd": round(usd, 2), "template": tpl,
    })
    res["actions"].append({
        "strategy_id": strategy.get("id"),
        "strategy_name": strategy.get("name"),
        "type": "directional",
        "op": "open",
        "coin": coin,
        "side": trig_side,
        "order_side": order,
        "timeframe": tf,
        "usd": round(usd, 2),
        # 下面这几个是"上限"，LLM 只能往更保守的方向调
        "leverage_cap": float(p.get("leverage") or 1),
        "stop_loss_pct_cap": float(p.get("stop_loss_pct") or 3),
        "take_profit_pct_cap": float(p.get("take_profit_pct") or 9),
        "min_confidence": float(p.get("min_confidence") or 0.55),
        "template": tpl,
        "snapshot": snap,
        "reason": "策略「%s」触发%s（%s）：%s" % (
            strategy.get("name"), "做多" if trig_side == "long" else "做空",
            tpl_info.get("label") or tpl, PT.describe(pattern)),
    })
    return res


def cache_rows():
    """读 dashboard.py 写的 funding_cache.json，避免 tick 时重复扫一遍（省 ~60 秒）。"""
    p = os.path.join(STATE_DIR, "funding_cache.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            c = json.load(f)
    except Exception:
        return None
    rows = c.get("rows") or []
    if not rows:
        return None
    return [{
        "coin": r.get("coin"),
        "apr": float(r.get("apr") or 0),
        "basis_pct": float(r.get("basis_pct") or 0),
        "interval_h": r.get("interval_h"),
        "spot": r.get("spot"),
    } for r in rows]


_ACCT_CACHE = {"at": 0.0, "data": None}
ACCT_TTL = 90.0
_POS_CACHE_FILE = os.path.join(STATE_DIR, "cache_positions.json")


def _read_json_file(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _write_json_file(path, payload):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        pass


def _account_positions(force=False):
    """读账户持仓，90 秒内复用同一次结果。

    这是盯盘慢的主犯：OKX 走代理大约 20% 瞬时失败率，`retry_call` 默认重试 4 次
    退避 1.5+3+6+12 秒，一个读操作能把一轮拖到 30 秒以上 —— 而持仓几秒内根本不会变。

    所以这里自己重试（2 次、间隔 0.6 秒），并且失败时退回上一次成功的结果；
    连上次的都没有就返回空列表并带上 error。**绝不为了一个读操作卡住整轮**。
    """
    now = time.time()
    cached = _ACCT_CACHE["data"]
    at = float(_ACCT_CACHE["at"] or 0)
    if cached is None:
        # 进程内缓存挡不住「每次 tick 都是新进程」这件事 —— runPython 每轮
        # 起一个新的 python，内存缓存全丢。所以落一份到磁盘，90 秒内跨进程复用。
        disk = _read_json_file(_POS_CACHE_FILE, {})
        if disk.get("data"):
            cached = disk["data"]
            at = float(disk.get("at") or 0)
            _ACCT_CACHE.update({"at": at, "data": cached})
    if not force and cached is not None and now - at < ACCT_TTL:
        return dict(cached)
    if not DF.okx_alive():
        # 代理没开时 build_client 会走 load_markets（几千个市场），
        # 一轮盯盘能因此多花几十秒。先探测，不通就直接降级。
        out = dict(cached) if cached is not None else {"positions": []}
        out.update({"positions": out.get("positions") or [],
                    "error": "OKX 不可达（代理未运行？）", "stale": True})
        return out

    try:
        ex = FL.build_client("swap")
        pos = None
        last = None
        # 读持仓不需要等 10 秒 —— ccxt 默认 timeout 10s，两次都超时就是 20 秒。
        # 这里压到 4 秒：拿不到就降级用缓存，比让整轮盯盘卡死强。
        old_timeout = getattr(ex, "timeout", None)
        if old_timeout:
            ex.timeout = 4000
        try:
            for attempt in (1, 2):
                try:
                    pos = ex.fetch_positions() or []
                    break
                except Exception as e:  # noqa: BLE001
                    last = e
                    if attempt == 2:
                        raise
                    time.sleep(0.6)
        finally:
            if old_timeout:
                ex.timeout = old_timeout
        live = []
        for x in (pos or []):
            n = abs(float(x.get("contracts") or 0))
            if n <= 0:
                continue
            # 字段要够面板直接用：账户卡要开仓价/标记价/强平价/杠杆/盈亏比例，
            # 只给 symbol+notional 的话 dashboard 还得再查一次 API。
            live.append({
                "symbol": x.get("symbol"),
                "side": x.get("side"),
                "contracts": n,
                "entry": x.get("entryPrice"),
                "mark": x.get("markPrice"),
                "notional": x.get("notional"),
                "upnl": round(float(x.get("unrealizedPnl") or 0), 4),
                "pct": round(float(x.get("percentage") or 0), 1),
                "liq": x.get("liquidationPrice"),
                "leverage": x.get("leverage"),
            })
        data = {"positions": live}
        _ACCT_CACHE.update({"at": now, "data": data})
        _write_json_file(_POS_CACHE_FILE, {"at": now, "data": data})
        return dict(data)
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {str(e)[:100]}"
        if cached is not None:
            out = dict(cached)
            out["error"] = msg
            out["stale"] = True
            return out
        return {"positions": [], "error": msg}


def evaluate_all(funding_rows=None, account=None, use_cache=False, skip_funding=False):
    """对全部策略跑一遍。按类型决定要准备什么数据 —— 只有套利策略才扫费率，
    只有方向策略才拉 K 线，免得为没用到的数据白等一分钟。

    skip_funding=True 时连缓存费率都不读（用户在面板上关掉了费率扫描，
    只想做单币合约），套利策略会如实报「没有候选」，而不是拿旧数据假装在评估。
    """
    strategies = load_strategies()
    types = {s.get("type") or "funding_arb" for s in strategies}
    market = {}

    error = None
    if skip_funding:
        funding_rows = []
    elif "funding_arb" in types and funding_rows is None:
        if use_cache:
            funding_rows = cache_rows()
        if funding_rows is None:
            try:
                funding_rows = []
                for r in FL.scan(top=60):
                    funding_rows.append({
                        "coin": r["coin"],
                        "apr": round(r["apr"], 2),
                        "basis_pct": round((r.get("basis") or 0) * 100, 2),
                        "interval_h": r["interval_h"],
                        "spot": r.get("spot"),
                    })
            except Exception as e:
                funding_rows = []
                error = f"费率扫描失败：{type(e).__name__}: {e}"
    if funding_rows is None:
        funding_rows = []

    if "directional" in types:
        for s in strategies:
            if (s.get("type") or "funding_arb") != "directional":
                continue
            p = s.get("params") or {}
            coin = str(p.get("coin") or "BTC").upper()
            tf = str(p.get("timeframe") or "4h")
            key = f"{coin}|{tf}"
            if key in market:
                continue
            market[key] = _market_snapshot(coin, tf)

    if account is None:
        account = _account_positions()

    results, actions = [], []
    for s in strategies:
        r = evaluate(s, funding_rows, account, market=market)
        results.append(r)
        actions.extend(r["actions"])

    out = {
        "as_of": now_iso(),
        "funding_count": len(funding_rows),
        "rules_checked": len(results),
        "types": sorted(types),
        "market": {k: {kk: vv for kk, vv in (v or {}).items() if kk != "spark"}
                   for k, v in market.items()},
        "actions": actions,
        "results": results,
        "needs_review": bool(actions),
        "account": account,
    }
    if error:
        out["error"] = error
    _write(CANDIDATES, out)
    return out


# ------------------------------------------------------------------ CLI
def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]

    if cmd == "list":
        items = load_strategies()
        for s in items:
            p = s.get("params") or {}
            typ = s.get("type") or "funding_arb"
            flag = "启用" if s.get("enabled") else "停用"
            print(f"{s['id']}  [{flag}] {s['name']}  <{typ}>")
            if typ == "directional":
                print(f"   {p.get('coin')} {p.get('timeframe')} {p.get('side')}  "
                      f"单笔 {p.get('usd')}  杠杆≤{p.get('leverage')}x  "
                      f"止损≤{p.get('stop_loss_pct')}%  止盈≤{p.get('take_profit_pct')}%  "
                      f"置信度≥{p.get('min_confidence')}")
                print(f"   信号 {p.get('template') or DEFAULT_DIRECTIONAL['template']} "
                      f"({p.get('mode') or 'all'})：{PT.describe(_pattern_of(p))}")
            else:
                print(f"   门槛 {p.get('min_apr')}%  基差 ±{p.get('max_basis')}%  "
                      f"单币 {p.get('max_usd_per_coin')}  总 {p.get('max_total_usd')}  "
                      f"持仓数 {p.get('max_positions')}  杠杆 {p.get('leverage')}x")
                print(f"   标的池 {p.get('coins') or '全部可对冲'}")
        if not items:
            print("（还没有策略）")
        return 0

    if cmd == "add":
        ap = argparse.ArgumentParser()
        ap.add_argument("--type", default="funding_arb", choices=["funding_arb", "directional"])
        ap.add_argument("--name", default=None)
        # 套利参数
        ap.add_argument("--coins", default="")
        ap.add_argument("--min-apr", type=float, default=DEFAULT_PARAMS["min_apr"])
        ap.add_argument("--max-basis", type=float, default=DEFAULT_PARAMS["max_basis"])
        ap.add_argument("--max-usd-per-coin", type=float, default=DEFAULT_PARAMS["max_usd_per_coin"])
        ap.add_argument("--max-total-usd", type=float, default=DEFAULT_PARAMS["max_total_usd"])
        ap.add_argument("--max-positions", type=int, default=DEFAULT_PARAMS["max_positions"])
        ap.add_argument("--leverage", type=float, default=None)
        ap.add_argument("--stop-loss-pct", type=float, default=None)
        # 方向参数
        ap.add_argument("--coin", default=DEFAULT_DIRECTIONAL["coin"])
        ap.add_argument("--timeframe", default=DEFAULT_DIRECTIONAL["timeframe"], choices=TIMEFRAMES)
        ap.add_argument("--side", default=DEFAULT_DIRECTIONAL["side"], choices=["long", "short", "both"])
        ap.add_argument("--usd", type=float, default=DEFAULT_DIRECTIONAL["usd"])
        ap.add_argument("--take-profit-pct", type=float, default=DEFAULT_DIRECTIONAL["take_profit_pct"])
        ap.add_argument("--min-confidence", type=float, default=DEFAULT_DIRECTIONAL["min_confidence"])
        ap.add_argument("--template", default=DEFAULT_DIRECTIONAL["template"],
                        choices=list(PT.TEMPLATES.keys()),
                        help="信号模板：决定「什么位置算入场机会」")
        ap.add_argument("--mode", default="all", choices=["all", "any"],
                        help="all=所有规则同时满足 / any=任一满足")
        a = ap.parse_args(argv[1:])

        if a.type == "directional":
            p = {
                "coin": a.coin.upper(), "timeframe": a.timeframe, "side": a.side,
                "usd": a.usd,
                "leverage": a.leverage if a.leverage is not None else DEFAULT_DIRECTIONAL["leverage"],
                "stop_loss_pct": a.stop_loss_pct if a.stop_loss_pct is not None else DEFAULT_DIRECTIONAL["stop_loss_pct"],
                "take_profit_pct": a.take_profit_pct,
                "min_confidence": a.min_confidence,
                "template": a.template, "mode": a.mode,
                "rules": _tpl_rules(a.template),
            }
            s = make_strategy(a.name or f"{a.coin.upper()} {a.timeframe} 方向", p,
                              stype="directional")
        else:
            p = {
                "coins": [c.strip().upper() for c in a.coins.split(",") if c.strip()],
                "min_apr": a.min_apr, "max_basis": a.max_basis,
                "max_usd_per_coin": a.max_usd_per_coin, "max_total_usd": a.max_total_usd,
                "max_positions": a.max_positions,
                "leverage": a.leverage if a.leverage is not None else DEFAULT_PARAMS["leverage"],
                "stop_loss_pct": a.stop_loss_pct if a.stop_loss_pct is not None else DEFAULT_PARAMS["stop_loss_pct"],
            }
            s = make_strategy(a.name or "资金费率套利", p, stype="funding_arb")
        upsert(s)
        print(f"已创建 {s['id']}  {s['name']}  <{s['type']}>")
        return 0

    if cmd == "toggle":
        s = toggle(argv[1])
        print(f"{s['id']} -> {'启用' if s['enabled'] else '停用'}" if s else "没找到")
        return 0 if s else 1

    if cmd == "rm":
        ok = remove(argv[1])
        print("已删除" if ok else "没找到")
        return 0 if ok else 1

    if cmd == "eval":
        ap = argparse.ArgumentParser()
        ap.add_argument("--from-cache", action="store_true",
                        help="用 funding_cache.json 而不是重新扫市场（tick 用，省 60 秒）")
        a = ap.parse_args(argv[1:])
        out = evaluate_all(use_cache=a.from_cache)
        print(f"[{out['as_of']}] 检查 {out.get('rules_checked')} 个策略，"
              f"候选标的 {out.get('funding_count')} 个")
        if out.get("error"):
            print("错误:", out["error"])
        for r in out.get("results") or []:
            tag = r.get("type") or "funding_arb"
            print(f"  {r['name']} <{tag}>: 命中 {len(r['matched'])} 个"
                  + (f"  ({r['note']})" if r.get("note") else ""))
            for m in r["matched"]:
                if tag == "directional":
                    print(f"    -> {m['coin']} {m['side']}  @ {m['price']}  {m['usd']} USDT"
                          f"  RSI {m.get('rsi')}  区间 {m.get('pos')}%")
                else:
                    print(f"    -> {m['coin']}  年化 {m['apr']}%  基差 {m['basis_pct']}%  {m['usd']} USDT")
        if out.get("actions"):
            print(f"  共 {len(out['actions'])} 个动作待 LLM 复核 -> {CANDIDATES}")
        return 0

    if cmd == "watch":
        ap = argparse.ArgumentParser()
        ap.add_argument("--interval", type=int, default=None)
        ap.add_argument("--on", action="store_true")
        ap.add_argument("--off", action="store_true")
        a = ap.parse_args(argv[1:])
        w = load_watch()
        if a.interval:
            w["interval_min"] = max(1, a.interval)
        if a.on:
            w["enabled"] = True
        if a.off:
            w["enabled"] = False
        save_watch(w)
        print(f"盯盘 {'开' if w['enabled'] else '关'}  每 {w['interval_min']} 分钟")
        return 0

    print(f"未知命令 {cmd}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
