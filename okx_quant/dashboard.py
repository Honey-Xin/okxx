# -*- coding: utf-8 -*-
"""量化监控面板的数据层 —— 生成 state/dashboard.json 供 DSH 插件读取。

设计原则：
  1. 任何一块数据失败都不能拖垮整个面板（每块独立 try，失败给 null + error 文本）
  2. 快速路径不碰网络（读 parquet 缓存），慢路径（OKX 实时报价/费率扫描）单独触发
  3. 全程原子写，面板永远读到一个完整的 JSON

用法：
  python dashboard.py               # 快速刷新：行情(缓存) + 持仓 + 决策 + 审计
  python dashboard.py --quote       # 额外拉 OKX 实时报价（3 个币，约 2~5 秒）
  python dashboard.py --scan        # 额外跑资金费率扫描（约 40 秒，会写 funding_cache）
  python dashboard.py --check       # 只做连通性自检，打印结果
"""
import sys
import os
import json
import time
import traceback
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import pandas as pd

import config as C
import datafeed
import funding_arb_live as FL
import strategy as ST

STATE_DIR = os.path.join(ROOT, "state")
OUT = os.path.join(STATE_DIR, "dashboard.json")
FUNDING_CACHE = os.path.join(STATE_DIR, "funding_cache.json")
CANDIDATES = os.path.join(STATE_DIR, "candidates.json")
PENDING = os.path.join(STATE_DIR, "pending.json")
APPROVED = os.path.join(STATE_DIR, "approved.json")
TASK_FILE = os.path.join(STATE_DIR, "llm_task.json")
AUTO_PRICED = os.path.join(STATE_DIR, "auto_priced.json")

# decisions.json / audit.log 是"输入"，可能由 skill 脚本写在别的目录。
# 按顺序找一个存在的，避免面板因为路径不一致而永远显示"还没有决策"。
INPUT_DIRS = [
    os.getenv("QUANT_STATE_DIR") or "",
    STATE_DIR,
    os.path.expanduser(os.path.join("~", ".dsh", "skills", "crypto-quant", "state")),
]


def find_input(name):
    for d in INPUT_DIRS:
        if not d:
            continue
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return None


WATCH = ["BTC", "ETH", "SOL"]
CST = timezone(timedelta(hours=8))


def now_iso():
    return datetime.now(CST).isoformat(timespec="seconds")


def safe(name, fallback=None):
    """把一块数据的计算包起来，任何异常都降级为 fallback + error 文本。"""
    def deco(fn):
        try:
            return fn()
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc(limit=2)}
    return deco


# ---------------------------------------------------------------- 行情
def market_block(quote=False):
    """技术面来自本地 parquet 缓存，可选叠加 OKX 实时报价。"""
    rows = []
    for coin in WATCH:
        pair = f"{coin}_USDT"
        item = {"coin": coin}
        try:
            df = datafeed.load_bars(pair=pair, interval=C.TIMEFRAME, verbose=False)
            if df is None or df.empty:
                item["error"] = "无本地数据"
                rows.append(item)
                continue
            # build_features 只保留特征列，原始价格要在转换前取出
            close = float(df["close"].iloc[-1])
            bar_time = str(df.index[-1])
            import features as F
            f = F.build_features(df)
            last = f.iloc[-1]
            # 迷你走势：最近 60 根收盘价相对首根的变化百分比，给面板画 sparkline
            tail = df["close"].iloc[-60:].astype(float)
            base = float(tail.iloc[0]) or 1.0
            spark = [round((float(v) / base - 1.0) * 100.0, 3) for v in tail]
            item.update(
                price=round(close, 6),
                bar_time=bar_time,
                ret_24h=round(float(last.get("ret_24", 0)) * 100, 2),
                rsi14=round(float(last.get("rsi_14", 0)) * 100, 1),
                atr_pct=round(float(last.get("atr_pct", 0)) * 100, 2),
                range_pos=round(float(last.get("pos_24", 0)) * 100, 1),
                vol_ratio=round(float(last.get("vol_ratio_24", 0)), 2),
                spark=spark,
            )
        except Exception as e:
            item["error"] = f"{type(e).__name__}: {e}"
        rows.append(item)

    if quote:
        try:
            px = FL.PROXY
            ex = FL.build_client("spot")
            proxies = {"http": px, "https": px} if px else None
            for item in rows:
                if "price" not in item:
                    continue
                try:
                    t = FL.retry_call(ex, "fetch_ticker", f"{item['coin']}/USDT")
                    live = float(t["last"])
                    item["live_price"] = round(live, 6)
                    item["live_chg_pct"] = round(float(t.get("percentage") or 0), 2)
                    item["price_lag_pct"] = round((live / item["price"] - 1) * 100, 2)
                except Exception as e:
                    item["quote_error"] = f"{type(e).__name__}"
        except Exception as e:
            for item in rows:
                item.setdefault("quote_error", str(e)[:60])
    return rows


# ---------------------------------------------------------------- 费率
def funding_block(rescan=False):
    """费率候选。扫描很慢（~40s），所以默认读缓存，过期只标记 stale。"""
    cache = None
    if os.path.exists(FUNDING_CACHE):
        try:
            with open(FUNDING_CACHE, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = None

    if rescan or cache is None:
        try:
            rows = FL.scan(top=12)
            cache = {
                "scanned_at": now_iso(),
                "rows": [
                    {
                        "coin": r["coin"],
                        "apr": round(r["apr"], 2),
                        "rate": r["rate"],
                        "interval_h": r["interval_h"],
                        "basis_pct": round(r["basis"] * 100, 3),
                        "spot": r["spot"],
                        "mark": r.get("mark"),
                    }
                    for r in rows
                ],
            }
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = FUNDING_CACHE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
            os.replace(tmp, FUNDING_CACHE)
        except Exception as e:
            if cache is None:
                return {"error": f"{type(e).__name__}: {e}", "rows": []}
            cache.setdefault("error", f"重新扫描失败，用旧数据：{type(e).__name__}")

    # 计算新鲜度
    try:
        t = datetime.fromisoformat(cache["scanned_at"])
        age = (datetime.now(CST) - t).total_seconds() / 60
        cache["age_minutes"] = round(age, 1)
        cache["stale"] = age > 15
    except Exception:
        cache["age_minutes"] = None
        cache["stale"] = True

    rows = cache.get("rows") or []
    # 分级：手续费大约 0.26%/轮，年化 10.95% 要 9 天才回本，所以 10.95% 只是「勉强」。
    for r in rows:
        apr = r.get("apr") or 0
        clean = abs(r.get("basis_pct") or 0) <= 0.8
        if apr >= 30 and clean:
            r["grade"] = "good"
        elif apr >= 12 and clean:
            r["grade"] = "marginal"
        else:
            r["grade"] = "poor"
        r["actionable"] = r["grade"] == "good"
    cache["best_apr"] = max((r["apr"] for r in rows), default=None)
    cache["actionable_count"] = sum(1 for r in rows if r.get("actionable"))
    # 前端只用 as_of / count，不用自己猜 scanned_at 和 len(rows)
    cache["as_of"] = cache.get("scanned_at")
    cache["count"] = len(rows)
    return cache


# ---------------------------------------------------------------- 账户
_BAL_CACHE = {"at": 0.0, "data": None}
BAL_TTL = 120.0
_BAL_CACHE_FILE = os.path.join(STATE_DIR, "cache_balance.json")


def _balance(force=False):
    """USDT 余额，120 秒内复用（含跨进程的磁盘缓存）。

    和持仓一样，这是读操作：OKX 走代理约 20% 瞬时失败率，`retry_call` 默认
    重试 4 次退避 1.5+3+6+12 秒，足以让一次面板刷新卡半分钟。这里压到
    4 秒超时、不重试，失败就退回上一次的数。
    """
    now = time.time()
    cached = _BAL_CACHE["data"]
    at = float(_BAL_CACHE["at"] or 0)
    if cached is None:
        disk = _read_json(_BAL_CACHE_FILE, {})
        if disk.get("data"):
            cached = disk["data"]
            at = float(disk.get("at") or 0)
            _BAL_CACHE.update({"at": at, "data": cached})
    if not force and cached is not None and now - at < BAL_TTL:
        return dict(cached)
    try:
        ex = FL.build_client("swap")
        old = getattr(ex, "timeout", None)
        if old:
            ex.timeout = 4000
        try:
            bal = ex.fetch_balance()
        finally:
            if old:
                ex.timeout = old
        total = bal.get("total") or {}
        free = bal.get("free") or {}
        data = {
            "usdt_total": round(float(total.get("USDT") or 0), 4),
            "usdt_free": round(float(free.get("USDT") or 0), 4),
        }
        _BAL_CACHE.update({"at": now, "data": data})
        try:
            tmp = _BAL_CACHE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"at": now, "data": data}, f, ensure_ascii=False)
            os.replace(tmp, _BAL_CACHE_FILE)
        except Exception:  # noqa: BLE001
            pass
        return dict(data)
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {str(e)[:120]}"
        if cached is not None:
            out = dict(cached)
            out["balance_error"] = msg
            out["stale"] = True
            return out
        return {"balance_error": msg}


def account_block():
    """账户权益与持仓。没配 key 时返回 configured=false，不算错误。

    持仓走 strategy._account_positions()，和规则引擎共用同一份 90 秒缓存 ——
    否则一轮 tick 里 dashboard 和 evaluate 会各查一次，重复踩同样的网络抖动。
    """
    key, sec, pwd = FL.credentials()
    if not (key and sec):
        return {"configured": False, "hint": "未配置 API key，只能看行情和费率"}
    out = {"configured": True}
    out.update(_balance())

    try:
        import strategy as ST
        acct = ST._account_positions()
        out["positions"] = acct.get("positions") or []
        for k in ("error", "stale"):
            if acct.get(k):
                out["positions_" + k] = acct[k]
    except Exception as e:  # noqa: BLE001
        out["positions_error"] = f"{type(e).__name__}: {str(e)[:120]}"
        out["positions"] = []
    return out


# ---------------------------------------------------------------- 决策与审计
def _rule_regime(market, coins=None):
    """用技术面拼一个"规则版行情判断"。

    AI 判断由每小时的 cron 任务写，插件 tick 碰不到它 —— 于是用户点
    「立即跑一轮」时会觉得"行情判断没更新"。这里在 AI 判断过期时给一个
    规则结论兜底，并明确标 source: rule，别让人误以为那是模型写的。

    coins 给出时只分析这些币（面板上启用了方向策略的标的）。
    用户只交易 BTC 时，把 ETH/SOL 的 RSI 一起摆上来是噪音 —— 那不是他要下手的东西。
    """
    rows = market if isinstance(market, list) else list((market or {}).values())
    if coins:
        wanted = {str(c).upper() for c in coins}
        rows = [r for r in rows if str(r.get("coin") or "").upper() in wanted]
    if not rows:
        return None
    r = rows[0] if rows else {}
    rsi = r.get("rsi14")
    atr = r.get("atr_pct")
    ret24 = r.get("ret_24h")
    pos = r.get("range_pos")
    if rsi is None:
        return None

    if atr is not None and atr >= 3.0:
        regime, why = "high_vol", f"ATR {atr:.2f}% 偏高，波动主导"
    elif (ret24 or 0) >= 1.0 and (pos or 0) >= 55:
        regime, why = "trend_up", f"24 根 {ret24:+.2f}% 且处在区间 {pos:.0f}% 分位"
    elif (ret24 or 0) <= -1.0 and (pos or 0) <= 45:
        regime, why = "trend_down", f"24 根 {ret24:+.2f}% 且跌到区间 {pos:.0f}% 分位"
    else:
        regime, why = "range", f"24 根 {ret24 or 0:+.2f}%、区间 {pos or 0:.0f}% 分位，方向不明"

    parts = [f"{r.get('coin', '?')} {why}，RSI {rsi:.1f}"]
    # 只有真的在看多个标的时才把其他币的 RSI 摆出来；
    # 单一标的时那句话对交易决策没有帮助，反而是噪音。
    if len(rows) > 1:
        for x in rows[1:4]:
            if x.get("rsi14") is not None:
                parts.append(f"{x.get('coin', '?')} RSI {x['rsi14']:.1f}")
    return {
        "present": True,
        "source": "rule",
        "regime": regime,
        "confidence": 0.3,
        "actions": [],
        "veto": False,
        "veto_reason": "",
        "notes": "；".join(parts) + "。这条结论由技术指标规则生成，不是模型判断"
                 "（AI 判断由每小时定时任务写入）。",
    }


def decision_block(market=None, coins=None):
    p = find_input("decisions.json")
    age = None
    d = None
    if p:
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            d["present"] = True
            d["source"] = d.get("source") or "agent"
            # 这份判断由 crypto-quant 的定时任务（cron 每小时）由 DSH agent 写，
            # 插件的 tick 完全不碰它。算出年龄，前端才能如实标注"多久没更新"，
            # 否则用户点完「立即跑一轮」发现这里不动，会以为按钮坏了。
            try:
                as_of = d.get("as_of")
                if as_of:
                    t = datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
                    if t.tzinfo is None:
                        t = t.replace(tzinfo=CST)
                    age = round((datetime.now(CST) - t).total_seconds() / 60.0, 1)
                    d["age_minutes"] = age
            except Exception:
                pass
        except Exception as e:
            d = {"present": False, "error": f"{type(e).__name__}: {e}"}

    # AI 判断缺失或超过 90 分钟没更新 -> 用规则版兜底，让面板始终有当下的结论
    if (d is None or not d.get("present") or (age is not None and age > 90)):
        fallback = _rule_regime(market, coins)
        if fallback:
            fallback["as_of"] = datetime.now(CST).isoformat(timespec="seconds")
            fallback["age_minutes"] = 0.0
            fallback["stale_agent"] = age
            return fallback
    return d or {"present": False}


def _read_jsonl(path):
    out = []
    if not path or not os.path.exists(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return out


def _llm_event_text(e):
    """把 llm_trade.py 的审计事件翻成面板能读的一句话。"""
    ev = str(e.get("event") or "")
    dec = e.get("decision") or {}
    name = dec.get("strategy_name") or e.get("strategy_id") or ""
    if ev == "approve":
        side = "做多" if dec.get("side") == "long" else "做空"
        return (
            f"AI 定价通过：{name} → {dec.get('coin', '')} {side} "
            f"{dec.get('usd', '')} USDT {dec.get('leverage', '')}x "
            f"SL {dec.get('stop_loss', '')} TP {dec.get('take_profit', '')}"
        )
    if ev == "reject":
        errs = e.get("errs") or e.get("reason") or ""
        if isinstance(errs, list):
            errs = "；".join(str(x) for x in errs[:3])
        return f"AI 定价被拒：{name} —— {str(errs)[:160]}"
    if ev == "skip":
        return f"AI 放弃开仓：{name} —— {str(e.get('reason') or '')[:160]}"
    return f"AI 定价事件 {ev}：{name}"


def audit_block(limit=12):
    """两本账合并：crypto-quant skill 的执行审计 + llm_trade 的定价审计。

    以前只读 audit.log，所以定价、放行、否决这些事在面板上完全看不见，
    看起来像"定价从来没发生过"。这里按时间戳合并，事件名归一成
    execute / reject / noop 三类，前端不用改。
    """
    events = []
    p = find_input("audit.log")
    for e in _read_jsonl(p):
        events.append({
            "ts": e.get("ts") or e.get("at"),
            "event": str(e.get("event") or "unknown"),
            "decision": e.get("decision"),
            "_src": "skill",
        })

    llm_path = os.path.join(STATE_DIR, "llm_audit.jsonl")
    for e in _read_jsonl(llm_path):
        ev = str(e.get("event") or "")
        if ev == "llm_raw":
            continue  # 原始模型输出太吵，不进时间线
        norm = {"approve": "execute", "reject": "reject"}.get(ev, "noop")
        events.append({
            "ts": e.get("ts"),
            "event": norm,
            "llm_event": ev,
            "text": _llm_event_text(e),
            "_src": "llm",
        })

    events.sort(key=lambda x: str(x.get("ts") or ""))
    counts = {}
    for e in events:
        k = str(e.get("event") or "unknown")
        counts[k] = counts.get(k, 0) + 1
    return {
        "total": len(events),
        "counts": counts,
        "recent": events[-limit:][::-1],
    }


# ---------------------------------------------------------------- 盯盘与策略
def _read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def watch_block():
    """盯盘配置 + 下一次触发倒计时（倒计时在 Python 算，避免前端时区搞错）。"""
    w = ST.load_watch()
    if w.get("enabled") and w.get("last_tick_sec"):
        nxt = int(w["last_tick_sec"]) + int(w.get("interval_min") or 15) * 60
        w["next_tick_sec"] = nxt
        w["seconds_to_next"] = max(0, int(nxt - time.time()))
    else:
        w["seconds_to_next"] = None
    return w


def watchlist_block():
    """面板主视图：按启用中的单币方向策略，实时拉它盯的那个标的。

    指标全部来自 strategy.py 的 `_market_snapshot`，和规则层同一个口径。
    面板只负责把「当前值 vs 阈值」并排摆出来，触发与否仍以规则层的结论为准。
    """
    try:
        items = ST.load_strategies()
    except Exception as exc:
        return {"targets": [], "error": f"读策略失败：{type(exc).__name__}: {exc}"}

    seen, targets = set(), []
    for s in items:
        if s.get("type") != "directional" or not s.get("enabled"):
            continue
        p = s.get("params") or {}
        coin = str(p.get("coin") or "BTC").upper()
        tf = str(p.get("timeframe") or "4h")
        key = f"{coin}|{tf}"
        label = s.get("name") or s.get("id") or ""

        if key in seen:
            # 同一个标的多套策略：合并成一张卡，阈值取最宽松的那套（union）最贴近实况
            for t in targets:
                if t["key"] != key:
                    continue
                t["strategy_names"].append(label)
                t["strategy_ids"].append(s.get("id"))
                gates = t.get("gates") or {}
                lg, sh = gates.get("long") or {}, gates.get("short") or {}
                if _num_or_none(p.get("rsi_below")) is not None:
                    lg["rsi_max"] = _looser(lg.get("rsi_max"), p.get("rsi_below"), high=True)
                if _num_or_none(p.get("pos_below")) is not None:
                    lg["pos_max"] = _looser(lg.get("pos_max"), p.get("pos_below"), high=True)
                if _num_or_none(p.get("rsi_above")) is not None:
                    sh["rsi_min"] = _looser(sh.get("rsi_min"), p.get("rsi_above"), high=False)
                if _num_or_none(p.get("pos_above")) is not None:
                    sh["pos_min"] = _looser(sh.get("pos_min"), p.get("pos_above"), high=False)
                if _num_or_none(p.get("min_vol_ratio")) is not None:
                    gates["vol_min"] = _looser(gates.get("vol_min"), p.get("min_vol_ratio"), high=False)
                if _num_or_none(p.get("max_atr_pct")) is not None:
                    gates["atr_max"] = _looser(gates.get("atr_max"), p.get("max_atr_pct"), high=True)
                gates["long"], gates["short"] = lg, sh
                t["gates"] = gates
            continue
        seen.add(key)

        snap = ST._market_snapshot(coin, tf, with_spark=True)
        base = {
            "key": key,
            "coin": coin,
            "timeframe": tf,
            "strategy_names": [label],
            "strategy_ids": [s.get("id")],
        }
        if snap.get("error"):
            base["error"] = snap["error"]
            targets.append(base)
            continue

        base.update({
            "price": snap.get("price"),
            "bar_time": snap.get("bar_time"),
            "rsi": snap.get("rsi"),
            "pos": snap.get("pos"),
            "pos_long": snap.get("pos_long"),
            "vol_ratio": snap.get("vol_ratio"),
            "atr_pct": snap.get("atr_pct"),
            "ema_fast_slow": snap.get("ema_fast_slow"),
            "macd_hist": snap.get("macd_hist"),
            "ret_24": snap.get("ret_24"),
            "ret_96": snap.get("ret_96"),
            "spark": snap.get("spark") or [],
            "gates": {
                "long": {"rsi_max": _num_or_none(p.get("rsi_below")),
                         "pos_max": _num_or_none(p.get("pos_below"))},
                "short": {"rsi_min": _num_or_none(p.get("rsi_above")),
                          "pos_min": _num_or_none(p.get("pos_above"))},
                "vol_min": _num_or_none(p.get("min_vol_ratio")),
                "atr_max": _num_or_none(p.get("max_atr_pct")),
            },
            "limits": {
                "usd": _num_or_none(p.get("usd")),
                "leverage": _num_or_none(p.get("leverage")),
                "stop_loss_pct": _num_or_none(p.get("stop_loss_pct")),
                "take_profit_pct": _num_or_none(p.get("take_profit_pct")),
                "min_confidence": _num_or_none(p.get("min_confidence")),
            },
        })
        targets.append(base)

    return {"targets": targets, "error": None}


def _num_or_none(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if n == n else None


def _looser(a, b, high):
    """两套阈值合并时取「更容易触发」的那个：上界取大，下界取小。"""
    x, y = _num_or_none(a), _num_or_none(b)
    if x is None:
        return y
    if y is None:
        return x
    return max(x, y) if high else min(x, y)


def strategy_block():
    """策略列表 + 上一轮规则筛的候选（不在这里跑规则，扫描太慢）。

    方向策略的候选带着行情快照，这里压平成前端直接能用的字段，
    免得浏览器端还要知道 snapshot 的嵌套结构。
    """
    items = ST.load_strategies()
    cand = _read_json(CANDIDATES, {})
    # 策略被删掉/停用后，上一轮留下的候选不该继续挂在面板上等复核
    known = {s["id"] for s in items if s.get("enabled")}

    slim = []
    for a in (cand.get("actions") or []):
        if a.get("strategy_id") not in known:
            continue
        snap = a.get("snapshot") or {}
        slim.append({
            "strategy_id": a.get("strategy_id"),
            "strategy_name": a.get("strategy_name"),
            "type": a.get("type") or "funding_arb",
            "coin": a.get("coin"),
            "side": a.get("side"),
            "usd": a.get("usd"),
            "leverage_cap": a.get("leverage_cap"),
            "stop_loss_pct_cap": a.get("stop_loss_pct_cap"),
            "take_profit_pct_cap": a.get("take_profit_pct_cap"),
            "min_confidence": a.get("min_confidence"),
            "timeframe": snap.get("timeframe") or a.get("timeframe"),
            "price": snap.get("price"),
            "rsi": snap.get("rsi"),
            "pos": snap.get("pos"),
            "vol_ratio": snap.get("vol_ratio"),
            "atr_pct": snap.get("atr_pct"),
            "reason": a.get("reason"),
        })

    pend = _read_json(PENDING, {})
    appr = _read_json(APPROVED, {})
    task = _read_json(TASK_FILE, {})
    auto = _read_json(AUTO_PRICED, {})

    # 给每个候选标上「定价走到哪一步」，让前端照着显示。
    # 之前前端硬编码成"等定价"，导致一笔早就复核完（甚至被放弃）的动作，
    # 界面上永远挂着"等定价"，看起来像卡死了。
    state_of = {}
    for i in (pend.get("items") or []):
        if i.get("status") == "awaiting_decision":
            state_of[i.get("strategy_id")] = ("awaiting_agent", "等 agent 定价")
    for i in (auto.get("items") or []):
        if i.get("status") == "awaiting_review":
            state_of.setdefault(i.get("strategy_id"), ("awaiting_review", "已定价，待复核"))
    for o in (appr.get("orders") or []):
        state_of[o.get("strategy_id")] = ("approved", "已通过校验")
    for row in slim:
        code, label = state_of.get(row.get("strategy_id"), ("settled", "已复核：未成交"))
        row["pricing"] = code
        row["pricing_label"] = label
    return {
        "strategies": items,
        "count": len(items),
        "enabled_count": sum(1 for s in items if s.get("enabled")),
        "candidates": {
            "as_of": cand.get("as_of"),
            "needs_review": bool(slim),
            "actions": slim,
            "results": [
                {
                    "name": r.get("name"),
                    "strategy_id": r.get("strategy_id"),
                    "type": r.get("type") or "funding_arb",
                    "matched": len(r.get("matched") or []),
                    "rejected": len(r.get("rejected") or []),
                    "note": r.get("note"),
                    "state": r.get("state"),
                }
                for r in (cand.get("results") or [])
                if r.get("strategy_id") in known
            ],
            "error": cand.get("error"),
        },
        "pending": {
            # 只数「还没定价」的；已经 apply/auto 处理过的仍留在文件里做档案，
            # 但不能再显示成「待定价」，否则面板永远挂着一条已办完的事。
            "count": sum(
                1 for i in (pend.get("items") or [])
                if i.get("status") == "awaiting_decision"
            ),
            "total": len(pend.get("items") or []),
            "as_of": pend.get("as_of"),
        },
        "approved": {
            "count": len(appr.get("orders") or []),
            "as_of": appr.get("as_of"),
            "orders": (appr.get("orders") or [])[:5],
        },
        "task": {
            "count": len(task.get("candidates") or []),
            "as_of": task.get("as_of"),
            "file": TASK_FILE,
        },
        "auto": {
            "as_of": auto.get("as_of"),
            "count": len(auto.get("items") or []),
            "awaiting": sum(1 for i in (auto.get("items") or []) if i.get("status") == "awaiting_review"),
            "items": (auto.get("items") or [])[:8],
        },
        "market": cand.get("market") or {},
    }


# ---------------------------------------------------------------- 组装
def build(quote=False, rescan=False):
    market = safe("market", [])(lambda: market_block(quote=quote))
    # watchlist 要先算：它决定了「行情判断」该分析哪几个币。
    # 用户只交易 BTC 时，把 ETH/SOL 的分析一起塞进结论是噪音。
    watchlist = safe("watchlist", {"targets": [], "error": "unavailable"})(
        watchlist_block)
    coins = sorted({str(t.get("coin") or "").upper()
                    for t in (watchlist.get("targets") or [])
                    if t.get("coin")})
    payload = {
        "as_of": now_iso(),
        "mode": {
            "exchange": FL.EXCHANGE,
            "proxy": FL.PROXY or None,
            "dry_run": bool(FL.DRY_RUN),
            "max_usd_per_coin": FL.MAX_USD_PER_COIN,
            "max_total_usd": FL.MAX_TOTAL_USD,
            "min_funding_apr": FL.MIN_FUNDING_APR,
            "leverage": FL.LEVERAGE,
            "blacklist": sorted(FL.BLACKLIST),
        },
        "market": market,
        "funding": safe("funding", {"error": "unavailable", "rows": []})(
            lambda: funding_block(rescan=rescan)),
        "account": safe("account", {"configured": False, "error": "unavailable"})(
            account_block),
        "decision": safe("decision", {"present": False})(
            lambda: decision_block(market, coins)),
        "audit": safe("audit", {"total": 0, "counts": {}, "recent": []})(
            lambda: audit_block()),
        "watch": safe("watch", {})(watch_block),
        "watchlist": watchlist,
        "strategy": safe("strategy", {"strategies": [], "count": 0})(
            strategy_block),
        "skill_scheduled": True,
    }
    return payload


def write(payload):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, OUT)
    return OUT


def main(argv):
    quote = "--quote" in argv or "--full" in argv
    rescan = "--scan" in argv or "--full" in argv

    if "--check" in argv:
        print(f"EXCHANGE = {FL.EXCHANGE}")
        print(f"PROXY    = {FL.PROXY or '(直连)'}")
        print(f"DRY_RUN  = {FL.DRY_RUN}")
        key, sec, _ = FL.credentials()
        print(f"API key  = {'已配置' if key and sec else '未配置'}")
        try:
            ex = FL.build_client("spot")
            t = FL.retry_call(ex, "fetch_time")
            print(f"OKX 时间  = {t}  偏差 {int(time.time()*1000) - t:+d}ms")
            print("网络     = 通")
        except Exception as e:
            print(f"网络     = 失败 {type(e).__name__}: {str(e)[:100]}")
        return 0

    t0 = time.time()
    payload = build(quote=quote, rescan=rescan)
    path = write(payload)
    dt = time.time() - t0

    m = payload.get("market") or []
    print(f"[{payload['as_of']}] 写入 {path}  耗时 {dt:.1f}s")
    for item in m:
        if isinstance(item, dict) and item.get("price"):
            print(f"  {item['coin']:<4} {item['price']:>12,.2f}  "
                  f"RSI {item.get('rsi14')}  24h {item.get('ret_24h')}%  "
                  f"分位 {item.get('range_pos')}%")
    fb = payload.get("funding") or {}
    if fb.get("rows"):
        best = fb.get("best_apr")
        print(f"  费率候选 {len(fb['rows'])} 个，最高年化 {best}%，"
              f"可做 {fb.get('actionable_count')} 个，数据 {fb.get('age_minutes')} 分钟前")
    else:
        print(f"  费率：{fb.get('error', '无数据')}")
    ab = payload.get("account") or {}
    if ab.get("configured"):
        print(f"  账户 {ab.get('usdt_total')} USDT，持仓 {len(ab.get('positions') or [])} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
