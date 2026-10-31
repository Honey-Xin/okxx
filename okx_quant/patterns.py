"""信号引擎：把「该不该在这个位置动手」写成可回测的条件组合。

三层的分工（重要，不要混）：

    patterns.py   —— 判断「这个位置值不值得动手」。纯规则、可回测、结果确定。
    strategy.py   —— 管仓位与边界（单笔多大、杠杆几倍、最多几个仓），
                     并调用本文件取信号；它本身不判断行情。
    llm_trade.py  —— 被触发之后，由模型给出具体买点/止损/止盈/仓位系数，
                     只能往更保守方向调。

之前版本把 RSI / 区间位置 / 量比 直接写死在 strategy.py 里当触发条件，
那不叫策略，那只是一组筛子。真正的策略要能表达「回调买」「突破买」
「超跌反弹买」这些不同的入场逻辑，而且能被历史数据检验。

所以这里提供三层表达力：
    1. CONDITIONS —— 语义化的条件库（每个条件是一列布尔值）
    2. TEMPLATES  —— 8 套预设策略（趋势回调/突破/均值回归/动量 × 多空）
    3. 自由组合   —— 从条件库里挑任意几条，用 all / any 连接

模板只是「一组规则的预设」，用户可以逐条增删改参数。
"""

from __future__ import annotations

import pandas as pd

import features as F


# ---------------------------------------------------------------- 指标准备
def prepare(df: pd.DataFrame) -> dict:
    """一次算好所有条件要用到的列，避免每条规则重复计算。

    返回的 dict 里，百分比类指标已经乘过 100（RSI 是 34.9 而不是 0.349），
    因为界面上给人看的、配置里给人填的都是百分数。
    """
    c, h, l = df["close"], df["high"], df["low"]
    f = F.build_features(df)

    d = {
        "close": c,
        "high": h,
        "low": l,
        "volume": df["volume"],
        "ema12": F.ema(c, 12),
        "ema24": F.ema(c, 24),
        "ema48": F.ema(c, 48),
        "ema96": F.ema(c, 96),
        "rsi7": f["rsi_7"] * 100.0,
        "rsi14": f["rsi_14"] * 100.0,
        "pos24": f["pos_24"] * 100.0,
        "pos96": f["pos_96"] * 100.0,
        "atr_pct": f["atr_pct"] * 100.0,
        "atr_slope": f["atr_slope"],
        "vol_ratio": f["vol_ratio_24"],
        "macd": f["macd"] * 100.0,
        "macd_hist": f["macd_hist"] * 100.0,
        "bb_pctb": f["bb_pctb"] * 100.0,
        "bb_width": f["bb_width"] * 100.0,
        "ema12_48": f["ema_fast_slow"] * 100.0,
        "ema_slope": f["ema_slope"] * 100.0,
        "ret_6": f["ret_6"] * 100.0,
        "ret_24": f["ret_24"] * 100.0,
        "upper_wick": f["upper_wick"],
        "lower_wick": f["lower_wick"],
        "body": f["body"],
    }
    # 距 96 根区间上下沿的百分数（正数表示还差多少）
    hi96 = h.rolling(96).max()
    lo96 = l.rolling(96).min()
    d["dist_hi96"] = (hi96 / c - 1.0) * 100.0
    d["dist_lo96"] = (c / lo96 - 1.0) * 100.0
    return d


# ---------------------------------------------------------------- 条件库
# 每个条件：label 给人看、hint 解释含义、args 是可调参数、fn(d, a) 返回布尔列。
CONDITIONS = {
    "trend_up": {
        "label": "趋势向上",
        "hint": "EMA12 在 EMA48 上方，且 EMA24 比 6 根前高",
        "args": {},
        "fn": lambda d, a: (d["ema12"] > d["ema48"]) & (d["ema24"] > d["ema24"].shift(6)),
    },
    "trend_down": {
        "label": "趋势向下",
        "hint": "EMA12 在 EMA48 下方，且 EMA24 比 6 根前低",
        "args": {},
        "fn": lambda d, a: (d["ema12"] < d["ema48"]) & (d["ema24"] < d["ema24"].shift(6)),
    },
    "rsi_below": {
        "label": "RSI 低于",
        "hint": "动量偏弱，用于「跌下来再买」",
        "args": {"v": {"label": "RSI 上限", "default": 45.0, "min": 1, "max": 99, "step": 1}},
        "fn": lambda d, a: d["rsi14"] < a["v"],
    },
    "rsi_above": {
        "label": "RSI 高于",
        "hint": "动量偏强 / 超买",
        "args": {"v": {"label": "RSI 下限", "default": 60.0, "min": 1, "max": 99, "step": 1}},
        "fn": lambda d, a: d["rsi14"] > a["v"],
    },
    "pos_below": {
        "label": "区间位置低于",
        "hint": "价格在最近 24 根的高低区间里的位置（0=最低，100=最高）",
        "args": {"v": {"label": "位置上限 %", "default": 35.0, "min": 0, "max": 100, "step": 5}},
        "fn": lambda d, a: d["pos24"] < a["v"],
    },
    "pos_above": {
        "label": "区间位置高于",
        "hint": "价格在最近 24 根区间的上沿附近",
        "args": {"v": {"label": "位置下限 %", "default": 65.0, "min": 0, "max": 100, "step": 5}},
        "fn": lambda d, a: d["pos24"] > a["v"],
    },
    "near_lo96": {
        "label": "靠近 96 根低点",
        "hint": "现价离最近 96 根最低价只有这么多",
        "args": {"v": {"label": "距离 %", "default": 3.0, "min": 0.1, "max": 50, "step": 0.5}},
        "fn": lambda d, a: d["dist_lo96"] <= a["v"],
    },
    "near_hi96": {
        "label": "靠近 96 根高点",
        "hint": "现价离最近 96 根最高价只有这么多",
        "args": {"v": {"label": "距离 %", "default": 3.0, "min": 0.1, "max": 50, "step": 0.5}},
        "fn": lambda d, a: d["dist_hi96"] <= a["v"],
    },
    "pullback_to_ema": {
        "label": "回踩均线",
        "hint": "价格跌到某条均线附近（含下方），用于「趋势里等回调」",
        "args": {
            "ema": {"label": "均线周期", "default": 24, "min": 12, "max": 96, "step": 12},
            "tol": {"label": "容差 %", "default": 1.5, "min": 0, "max": 10, "step": 0.5},
        },
        "fn": lambda d, a: d["close"] <= d[_ema_key(a["ema"])] * (1 + a["tol"] / 100.0),
    },
    "bounce_to_ema": {
        "label": "反弹到均线",
        "hint": "价格涨到某条均线附近（含上方），用于「下跌趋势里等反弹做空」",
        "args": {
            "ema": {"label": "均线周期", "default": 24, "min": 12, "max": 96, "step": 12},
            "tol": {"label": "容差 %", "default": 1.5, "min": 0, "max": 10, "step": 0.5},
        },
        "fn": lambda d, a: d["close"] >= d[_ema_key(a["ema"])] * (1 - a["tol"] / 100.0),
    },
    "breakout_high": {
        "label": "突破前高",
        "hint": "收盘价高于前 N 根的最高价（不含当根，避免自我引用）",
        "args": {"n": {"label": "回看根数", "default": 24, "min": 5, "max": 200, "step": 1}},
        "fn": lambda d, a: d["close"] > d["high"].rolling(int(a["n"])).max().shift(1),
    },
    "breakdown_low": {
        "label": "跌破前低",
        "hint": "收盘价低于前 N 根的最低价（不含当根）",
        "args": {"n": {"label": "回看根数", "default": 24, "min": 5, "max": 200, "step": 1}},
        "fn": lambda d, a: d["close"] < d["low"].rolling(int(a["n"])).min().shift(1),
    },
    "macd_cross_up": {
        "label": "MACD 金叉",
        "hint": "柱状图由负转正",
        "args": {},
        "fn": lambda d, a: (d["macd_hist"] > 0) & (d["macd_hist"].shift(1) <= 0),
    },
    "macd_cross_dn": {
        "label": "MACD 死叉",
        "hint": "柱状图由正转负",
        "args": {},
        "fn": lambda d, a: (d["macd_hist"] < 0) & (d["macd_hist"].shift(1) >= 0),
    },
    "macd_above": {
        "label": "MACD 柱为正",
        "hint": "动能仍在多头一侧",
        "args": {},
        "fn": lambda d, a: d["macd_hist"] > 0,
    },
    "macd_below": {
        "label": "MACD 柱为负",
        "hint": "动能仍在空头一侧",
        "args": {},
        "fn": lambda d, a: d["macd_hist"] < 0,
    },
    "bb_low": {
        "label": "布林带低位",
        "hint": "价格贴近布林下轨（%B 越低越靠近下轨）",
        "args": {"v": {"label": "%B 上限", "default": 15.0, "min": 0, "max": 100, "step": 5}},
        "fn": lambda d, a: d["bb_pctb"] < a["v"],
    },
    "bb_high": {
        "label": "布林带高位",
        "hint": "价格贴近布林上轨",
        "args": {"v": {"label": "%B 下限", "default": 85.0, "min": 0, "max": 100, "step": 5}},
        "fn": lambda d, a: d["bb_pctb"] > a["v"],
    },
    "vol_expand": {
        "label": "放量",
        "hint": "成交量超过近 24 根均量的倍数",
        "args": {"v": {"label": "量比下限", "default": 1.5, "min": 0.1, "max": 20, "step": 0.1}},
        "fn": lambda d, a: d["vol_ratio"] >= a["v"],
    },
    "vol_dry": {
        "label": "缩量",
        "hint": "成交量不到近 24 根均量的这个倍数",
        "args": {"v": {"label": "量比上限", "default": 0.8, "min": 0.05, "max": 5, "step": 0.05}},
        "fn": lambda d, a: d["vol_ratio"] <= a["v"],
    },
    "rev_up": {
        "label": "下影线反转",
        "hint": "长下影 + 收阳，说明低位有承接",
        "args": {"wick": {"label": "影线占比", "default": 0.5, "min": 0.1, "max": 0.9, "step": 0.05}},
        "fn": lambda d, a: (d["lower_wick"] >= a["wick"]) & (d["body"] > 0),
    },
    "rev_dn": {
        "label": "上影线反转",
        "hint": "长上影 + 收阴，说明高位有人派发",
        "args": {"wick": {"label": "影线占比", "default": 0.5, "min": 0.1, "max": 0.9, "step": 0.05}},
        "fn": lambda d, a: (d["upper_wick"] >= a["wick"]) & (d["body"] < 0),
    },
    "atr_max": {
        "label": "波动不过大",
        "hint": "ATR 占价格的比例上限，太剧烈的时候不进场",
        "args": {"v": {"label": "ATR 上限 %", "default": 5.0, "min": 0.1, "max": 30, "step": 0.5}},
        "fn": lambda d, a: d["atr_pct"] <= a["v"],
    },
    "atr_min": {
        "label": "波动不太小",
        "hint": "ATR 下限，完全没有波动就没有利润空间",
        "args": {"v": {"label": "ATR 下限 %", "default": 0.5, "min": 0.05, "max": 10, "step": 0.05}},
        "fn": lambda d, a: d["atr_pct"] >= a["v"],
    },
}

_EMA_CHOICES = (12, 24, 48, 96)


def _ema_key(n):
    n = int(n)
    return "ema%d" % (n if n in _EMA_CHOICES else 24)


# ---------------------------------------------------------------- 预设模板
# side = long 表示规则描述的是「该买的位置」，short 表示「该卖的位置」。
TEMPLATES = {
    "trend_pullback": {
        "label": "上升趋势回调买",
        "desc": "大方向向上，等回踩均线、动能降温时买，不追高",
        "side": "long",
        "rules": [
            {"cond": "trend_up"},
            {"cond": "pullback_to_ema", "args": {"ema": 24, "tol": 1.5}},
            {"cond": "rsi_below", "args": {"v": 55}},
            {"cond": "atr_max", "args": {"v": 5}},
        ],
    },
    "trend_breakout": {
        "label": "上升趋势突破买",
        "desc": "放量突破前高，顺势跟进",
        "side": "long",
        "rules": [
            {"cond": "trend_up"},
            {"cond": "breakout_high", "args": {"n": 24}},
            {"cond": "vol_expand", "args": {"v": 1.5}},
            {"cond": "atr_max", "args": {"v": 6}},
        ],
    },
    "mean_revert": {
        "label": "超跌反弹买",
        "desc": "跌到区间下沿、布林下轨，出现承接影线再买",
        "side": "long",
        "rules": [
            {"cond": "rsi_below", "args": {"v": 32}},
            {"cond": "bb_low", "args": {"v": 15}},
            {"cond": "near_lo96", "args": {"v": 4}},
            {"cond": "rev_up", "args": {"wick": 0.4}},
        ],
    },
    "momentum": {
        "label": "动量金叉买",
        "desc": "MACD 金叉 + 放量，动能刚转向时进场",
        "side": "long",
        "rules": [
            {"cond": "macd_cross_up"},
            {"cond": "vol_expand", "args": {"v": 1.3}},
            {"cond": "trend_up"},
        ],
    },
    "trend_bounce_short": {
        "label": "下降趋势反弹空",
        "desc": "大方向向下，等反弹到均线、动能过热时做空",
        "side": "short",
        "rules": [
            {"cond": "trend_down"},
            {"cond": "bounce_to_ema", "args": {"ema": 24, "tol": 1.5}},
            {"cond": "rsi_above", "args": {"v": 45}},
            {"cond": "atr_max", "args": {"v": 5}},
        ],
    },
    "trend_breakdown": {
        "label": "下降趋势跌破空",
        "desc": "放量跌破前低，顺势跟空",
        "side": "short",
        "rules": [
            {"cond": "trend_down"},
            {"cond": "breakdown_low", "args": {"n": 24}},
            {"cond": "vol_expand", "args": {"v": 1.5}},
            {"cond": "atr_max", "args": {"v": 6}},
        ],
    },
    "overbought_fade": {
        "label": "超买回落空",
        "desc": "冲到区间上沿、布林上轨，出现派发影线再做空",
        "side": "short",
        "rules": [
            {"cond": "rsi_above", "args": {"v": 68}},
            {"cond": "bb_high", "args": {"v": 85}},
            {"cond": "near_hi96", "args": {"v": 4}},
            {"cond": "rev_dn", "args": {"wick": 0.4}},
        ],
    },
    "momentum_down": {
        "label": "动量死叉空",
        "desc": "MACD 死叉 + 放量，动能刚转弱时进场",
        "side": "short",
        "rules": [
            {"cond": "macd_cross_dn"},
            {"cond": "vol_expand", "args": {"v": 1.3}},
            {"cond": "trend_down"},
        ],
    },
}


# ---------------------------------------------------------------- 规则求值
def resolve_args(cond_name: str, given: dict | None) -> dict:
    """把用户填的参数和默认值合并，并强制转成数字。"""
    spec = CONDITIONS.get(cond_name)
    if not spec:
        return {}
    out = {}
    for k, meta in (spec.get("args") or {}).items():
        raw = (given or {}).get(k, meta.get("default"))
        try:
            out[k] = float(raw)
        except (TypeError, ValueError):
            out[k] = float(meta.get("default"))
    return out


def evaluate_rule(d: dict, rule: dict) -> pd.Series:
    """单条规则 → 布尔列。未知条件返回全 False，不抛异常（配置写错了也要能跑）。"""
    name = rule.get("cond")
    spec = CONDITIONS.get(name)
    if not spec:
        return pd.Series(False, index=len(d["close"]))
    try:
        mask = spec["fn"](d, resolve_args(name, rule.get("args")))
        return mask.fillna(False).astype(bool)
    except Exception:
        return pd.Series(False, index=len(d["close"]), dtype=bool)


def signal_series(df: pd.DataFrame, pattern: dict, d: dict | None = None) -> pd.Series:
    """整段历史的触发信号。mode=all 时全部规则满足，any 时任一条满足。"""
    if not pattern:
        return pd.Series(False, index=df.index)
    rules = [r for r in (pattern.get("rules") or []) if r.get("cond")]
    if not rules:
        return pd.Series(False, index=df.index)
    d = d or prepare(df)
    masks = [evaluate_rule(d, r) for r in rules]
    joined = pd.concat(masks, axis=1)
    mode = (pattern.get("mode") or "all").lower()
    sig = joined.any(axis=1) if mode == "any" else joined.all(axis=1)
    return sig.fillna(False).astype(bool)


def scan_last(df: pd.DataFrame, pattern: dict) -> dict:
    """只看最后一根：每条规则当前的值、阈值、是否通过。

    界面上的「做多条件 ✓✓××」就是拿这个渲染的。数值和阈值都返回，
    是为了让前端只负责显示，不在前端复刻判断逻辑。
    """
    d = prepare(df)
    rules = [r for r in (pattern.get("rules") or []) if r.get("cond")]
    rows = []
    for r in rules:
        name = r.get("cond")
        spec = CONDITIONS.get(name) or {}
        args = resolve_args(name, r.get("args"))
        try:
            mask = spec["fn"](d, args).fillna(False).astype(bool)
            ok = bool(mask.iloc[-1]) if len(mask) else False
        except Exception:
            ok = False
        # 取该规则第一个参数对应的指标当前值，供界面展示
        value = None
        for probe in _probe_value(d, name, args):
            value = probe
            break
        rows.append({
            "cond": name,
            "label": spec.get("label", name),
            "hint": spec.get("hint", ""),
            "ok": ok,
            "args": args,
            "value": value,
        })

    mode = (pattern.get("mode") or "all").lower()
    fired = any(x["ok"] for x in rows) if mode == "any" else all(x["ok"] for x in rows) if rows else False
    return {
        "side": pattern.get("side") or "long",
        "mode": mode,
        "rules": rows,
        "fired": bool(fired),
        "last_close": float(d["close"].iloc[-1]) if len(d["close"]) else None,
    }


def _probe_value(d: dict, name: str, args: dict):
    """给每条规则配一个「当前值」，纯展示用。拿不到就返回空。"""
    try:
        if name in ("rsi_below", "rsi_above"):
            return [round(float(d["rsi14"].iloc[-1]), 2)]
        if name in ("pos_below", "pos_above"):
            return [round(float(d["pos24"].iloc[-1]), 2)]
        if name == "near_lo96":
            return [round(float(d["dist_lo96"].iloc[-1]), 2)]
        if name == "near_hi96":
            return [round(float(d["dist_hi96"].iloc[-1]), 2)]
        if name in ("pullback_to_ema", "bounce_to_ema"):
            key = _ema_key(args.get("ema", 24))
            px = float(d["close"].iloc[-1])
            ema_v = float(d[key].iloc[-1])
            return [round((px / ema_v - 1.0) * 100.0, 2)]
        if name in ("breakout_high", "breakdown_low"):
            n = int(args.get("n", 24))
            ref = d["high"].rolling(n).max().shift(1) if name == "breakout_high" else d["low"].rolling(n).min().shift(1)
            return [round(float(d["close"].iloc[-1]), 4), round(float(ref.iloc[-1]), 4)]
        if name.startswith("macd"):
            return [round(float(d["macd_hist"].iloc[-1]), 4)]
        if name.startswith("bb_"):
            return [round(float(d["bb_pctb"].iloc[-1]), 2)]
        if name.startswith("vol_"):
            return [round(float(d["vol_ratio"].iloc[-1]), 3)]
        if name.startswith("atr_"):
            return [round(float(d["atr_pct"].iloc[-1]), 3)]
        if name == "rev_up":
            return [round(float(d["lower_wick"].iloc[-1]), 3)]
        if name == "rev_dn":
            return [round(float(d["upper_wick"].iloc[-1]), 3)]
        if name in ("trend_up", "trend_down"):
            return [round(float(d["ema12_48"].iloc[-1]), 3)]
    except Exception:
        return []
    return []


def describe(pattern: dict) -> str:
    """一行文字描述，日志和界面提示用。"""
    rules = [r for r in (pattern.get("rules") or []) if r.get("cond")]
    if not rules:
        return "空规则（永不触发）"
    mode = (pattern.get("mode") or "all").lower()
    parts = []
    for r in rules:
        spec = CONDITIONS.get(r.get("cond")) or {}
        args = resolve_args(r.get("cond"), r.get("args"))
        tail = "/".join("%g" % v for v in args.values())
        parts.append(spec.get("label", r.get("cond")) + ("(" + tail + ")" if tail else ""))
    joiner = " 或 " if mode == "any" else " 且 "
    return joiner.join(parts)


if __name__ == "__main__":
    import sys

    import config as C
    import datafeed as DF

    coin = sys.argv[1] if len(sys.argv) > 1 else "BTC"
    tf = sys.argv[2] if len(sys.argv) > 2 else "4h"
    df = DF.load_bars(pair=coin + "_USDT", interval=tf, bars=1200, verbose=False)
    print("数据 %s %s 共 %d 根，最新 %s 收 %s" % (coin, tf, len(df), df.index[-1], df["close"].iloc[-1]))

    for key, tpl in TEMPLATES.items():
        pattern = {"side": tpl["side"], "mode": "all", "rules": tpl["rules"]}
        sig = signal_series(df, pattern)
        hit = int(sig.sum())
        rate = hit / max(1, len(sig)) * 100
        last = scan_last(df, pattern)
        mark = "触发" if last["fired"] else "未触发"
        print("%-22s %-14s 历史触发 %4d 次 (%5.1f%%)  当前 %s   %s"
              % (key, tpl["side"], hit, rate, mark, describe(pattern)))
