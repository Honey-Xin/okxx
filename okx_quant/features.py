# -*- coding: utf-8 -*-
"""特征工程 + 标签。

铁律：第 t 行的所有特征只能用到 <= t 的信息。标签是 t -> t+HORIZON 的
未来净收益，训练时通过 shift(-H) 对齐，切分时靠 embargo 隔离。
"""
import numpy as np
import pandas as pd

import config as C


# ---------------------------------------------------------------- 基础指标
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / (dn + 1e-12)
    return 100 - 100 / (1 + rs)


def atr(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def build_features(df):
    """输入 OHLCV，输出特征 DataFrame（与 df 同索引）。"""
    o, h, l, c, v = df["open"], df["high"], df["low"], df["close"], df["volume"]
    f = pd.DataFrame(index=df.index)

    # ---- 1. 多周期对数收益 ----
    logc = np.log(c)
    for n in (1, 2, 3, 6, 12, 24, 48, 96):
        f[f"ret_{n}"] = logc.diff(n)

    # ---- 2. 已实现波动率（收益的标准差）----
    r1 = logc.diff()
    for n in (12, 24, 48):
        f[f"vol_{n}"] = r1.rolling(n).std()
    # 波动率状态：短/长波动比，>1 说明在放波
    f["vol_ratio"] = f["vol_12"] / (f["vol_48"] + 1e-12)

    # ---- 3. ATR 相关 ----
    a = atr(df, 14)
    f["atr_pct"] = a / c
    f["atr_slope"] = f["atr_pct"] / (f["atr_pct"].rolling(24).mean() + 1e-12)

    # ---- 4. 均线结构 ----
    for n in (12, 24, 48, 96):
        f[f"ema_ratio_{n}"] = ema(c, n) / c - 1
    f["ema_fast_slow"] = ema(c, 12) / ema(c, 48) - 1
    f["ema_slope"] = ema(c, 24).pct_change(6)

    # ---- 5. RSI ----
    for n in (7, 14):
        f[f"rsi_{n}"] = rsi(c, n) / 100.0

    # ---- 6. MACD (归一化到价格) ----
    macd = ema(c, 12) - ema(c, 26)
    sig = ema(macd, 9)
    f["macd"] = macd / c
    f["macd_hist"] = (macd - sig) / c

    # ---- 7. 布林带 ----
    ma20 = c.rolling(20).mean()
    sd20 = c.rolling(20).std()
    f["bb_pctb"] = (c - (ma20 - 2 * sd20)) / (4 * sd20 + 1e-12)
    f["bb_width"] = (4 * sd20) / (ma20 + 1e-12)

    # ---- 8. 价格在近期区间的位置 ----
    for n in (24, 96):
        hi, lo = h.rolling(n).max(), l.rolling(n).min()
        f[f"pos_{n}"] = (c - lo) / (hi - lo + 1e-12)
        f[f"dist_hi_{n}"] = c / hi - 1
        f[f"dist_lo_{n}"] = c / lo - 1

    # ---- 9. 量能 ----
    vma = v.rolling(24).mean()
    f["vol_ratio_24"] = v / (vma + 1e-12)
    f["vol_z"] = (v - vma) / (v.rolling(48).std() + 1e-12)
    f["quote_z"] = (v * c - (v * c).rolling(48).mean()) / ((v * c).rolling(48).std() + 1e-12)

    # ---- 10. 日内/周内周期（crypto 有明显时段效应）----
    hod = df.index.hour.values
    dow = df.index.dayofweek.values
    f["hod_sin"] = np.sin(2 * np.pi * hod / 24)
    f["hod_cos"] = np.cos(2 * np.pi * hod / 24)
    f["dow_sin"] = np.sin(2 * np.pi * dow / 7)
    f["dow_cos"] = np.cos(2 * np.pi * dow / 7)

    # ---- 11. 上影/下影（买卖压力）----
    rng = (h - l).replace(0, np.nan)
    f["upper_wick"] = (h - np.maximum(o, c)) / rng
    f["lower_wick"] = (np.minimum(o, c) - l) / rng
    f["body"] = (c - o) / rng

    return f.replace([np.inf, -np.inf], np.nan)


# ---------------------------------------------------------------- 标签
def build_labels(df, horizon=None, edge=None, cost=None):
    """未来 net_return > edge 记为 1。

    net_return = close[t+H]/close[t] - 1 - 往返成本
    往返成本 = 2*(taker_fee + slippage)
    """
    horizon = horizon or C.HORIZON
    edge = edge if edge is not None else C.LABEL_EDGE
    if cost is None:
        cost = 2 * (C.TAKER_FEE + C.SLIPPAGE_BPS / 10000)
    c = df["close"]
    fwd = c.shift(-horizon) / c - 1
    net = fwd - cost
    y = (net > edge).astype(int)
    return y, fwd, net


def make_dataset(df, horizon=None):
    """返回 (X, y, fwd_ret, net_ret)，已对齐并去掉 NaN 行。"""
    X = build_features(df)
    y, fwd, net = build_labels(df, horizon=horizon)
    d = X.copy()
    d["__y"] = y
    d["__fwd"] = fwd
    d["__net"] = net
    d = d.iloc[: -C.HORIZON] if C.HORIZON > 0 else d
    d = d.dropna()
    return d.drop(columns=["__y", "__fwd", "__net"]), d["__y"].astype(int), d["__fwd"], d["__net"]


if __name__ == "__main__":
    from datafeed import load_bars
    import time as _t

    bars = load_bars()
    t0 = _t.time()
    X, y, fwd, net = make_dataset(bars)
    print(f"特征 {X.shape[1]} 个, 样本 {len(X)} 行, 耗时 {_t.time()-t0:.2f}s")
    print("正样本率", round(y.mean(), 4))
    print(X.tail(3).T.tail(12))
    print("\n未来净收益分布:")
    print(net.describe())
