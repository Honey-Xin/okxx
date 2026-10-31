# -*- coding: utf-8 -*-
"""衍生品数据：资金费率 + 合约统计（持仓量/多空比/爆仓/大户持仓）。

这些是加密市场最有效的中低频信息源，比纯 K 线技术指标强得多：
  - funding_rate 高 → 多头拥挤 → 后续均值回归向下
  - OI 上升 + 价格下跌 → 空头加仓 → 趋势延续
  - lsr_account（散户账户多空比）是反向指标
  - top_lsr_account（大户多空比）是正向指标
  - 爆仓量突增 → 局部插针后的反转

数据覆盖（实测）：
  contract_stats 4h : 2023-10-08 起，约 6700 根
  funding_rate 8h   : 每页仅返回 90 条，必须多翻页

铁律：没有数据的地方保持 NaN，绝不 fillna(0) 冒充「零爆仓」。
"""
import time

import numpy as np
import pandas as pd
import requests

import config as C

BASE = "https://api.gateio.ws/api/v4"
S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})

STEP = {"1h": 3600, "4h": 14400, "1d": 86400}


def _get(path, params=None, tries=4):
    for i in range(tries):
        try:
            r = S.get(BASE + path, params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(1.5 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if i == tries - 1:
                raise
            time.sleep(1.0 * (i + 1))
    return []


def fetch_funding(contract=None, pages=60, force=False):
    """资金费率，8 小时一个点。Gate 每页只返回 90 条，必须多翻页。"""
    contract = contract or C.SYMBOL_DATA
    p = C.DATA_DIR / f"funding_{contract}.parquet"
    if p.exists() and not force:
        df = pd.read_parquet(p)
        print(f"缓存命中 {p.name}: {len(df)} 行 {df.index[0]} ~ {df.index[-1]}")
        return df

    rows, cur_to, page = {}, int(time.time()), 0
    while page < pages:
        data = _get("/futures/usdt/funding_rate",
                    {"contract": contract, "limit": 1000, "to": cur_to})
        if not data:
            break
        got = 0
        for d in data:
            t = int(d["t"])
            if t not in rows:
                rows[t] = float(d["r"])
                got += 1
        page += 1
        if got == 0:
            break
        cur_to = min(int(d["t"]) for d in data) - 1
        time.sleep(0.12)

    df = pd.DataFrame(sorted(rows.items()), columns=["ts", "funding"])
    df["ts"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    df = df.set_index("ts").sort_index()
    C.DATA_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(p)
    print(f"资金费率 {len(df)} 行, {df.index[0]} ~ {df.index[-1]}")
    return df


STAT_COLS = ["open_interest", "open_interest_usd", "lsr_taker", "lsr_account",
             "top_lsr_account", "top_lsr_size", "long_liq_usd", "short_liq_usd",
             "long_taker_size", "short_taker_size", "mark_price"]


def fetch_stats(contract=None, interval="4h", bars=10000, force=False):
    """合约统计（OI/多空比/爆仓）。返回 DataFrame 索引为 UTC 时间。"""
    contract = contract or C.SYMBOL_DATA
    p = C.DATA_DIR / f"stats_{contract}_{interval}.parquet"
    if p.exists() and not force:
        df = pd.read_parquet(p)
        print(f"缓存命中 {p.name}: {len(df)} 行 {df.index[0]} ~ {df.index[-1]}")
        return df

    step = STEP[interval]
    cur_to = int(time.time()) // step * step
    rows, page = {}, 0
    while page < bars // 900 + 12:
        data = _get("/futures/usdt/contract_stats",
                    {"contract": contract, "interval": interval, "limit": 1000, "to": cur_to})
        if not data:
            break
        got = 0
        for d in data:
            t = int(d["time"])
            if t not in rows:
                rows[t] = d
                got += 1
        page += 1
        if got == 0:
            break
        cur_to = min(int(d["time"]) for d in data) - step
        time.sleep(0.12)

    recs = []
    for t, d in sorted(rows.items()):
        r = {"ts": t}
        for k in STAT_COLS:
            v = d.get(k)
            try:
                r[k] = float(v) if v is not None else np.nan
            except (TypeError, ValueError):
                r[k] = np.nan
        recs.append(r)
    df = pd.DataFrame(recs)
    df["ts"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    df = df.set_index("ts").sort_index()
    C.DATA_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(p)
    print(f"合约统计 {interval}: {len(df)} 行, {df.index[0]} ~ {df.index[-1]}")
    return df


def build_deriv_features(bars, interval="4h", contract=None, verbose=True):
    """把衍生品数据对齐到 K 线索引上，产出衍生品特征。缺失保持 NaN。"""
    contract = contract or C.SYMBOL_DATA
    idx = bars.index

    st = fetch_stats(contract=contract, interval=interval, bars=10000)
    st = st.reindex(idx.union(st.index)).ffill(limit=2).reindex(idx)

    fu = fetch_funding(contract=contract, pages=60)
    fu = fu.reindex(idx.union(fu.index)).ffill().reindex(idx)

    f = pd.DataFrame(index=idx)

    # ---- 资金费率 ----
    fr = fu["funding"]
    f["funding"] = fr
    f["funding_ma3"] = fr.rolling(3).mean()
    f["funding_ma_3d"] = fr.rolling(18).mean()          # 4h*18 = 3 天
    f["funding_z"] = (fr - fr.rolling(180).mean()) / (fr.rolling(180).std() + 1e-12)
    f["funding_chg"] = fr.diff()

    # ---- 持仓量 ----
    oi = st["open_interest_usd"]
    f["oi_chg_1"] = oi.pct_change(1)
    f["oi_chg_6"] = oi.pct_change(6)                    # 24 小时
    f["oi_chg_24"] = oi.pct_change(24)                  # 4 天
    f["oi_z"] = (oi - oi.rolling(180).mean()) / (oi.rolling(180).std() + 1e-12)

    # ---- 多空比 ----
    f["lsr_taker"] = st["lsr_taker"]
    f["lsr_account"] = st["lsr_account"]
    f["lsr_acc_z"] = (st["lsr_account"] - st["lsr_account"].rolling(180).mean()) / (
        st["lsr_account"].rolling(180).std() + 1e-12)
    f["top_lsr_acct"] = st["top_lsr_account"]
    f["top_lsr_size"] = st["top_lsr_size"]
    # 大户相对散户的方向倾斜，>1 说明大户比散户更偏多
    f["smart_dumb"] = st["top_lsr_account"] / (st["lsr_account"] + 1e-12)

    # ---- 爆仓 ----
    tl, sk = st["long_liq_usd"], st["short_liq_usd"]
    tot = tl + sk
    f["liq_ratio"] = tl / tot.where(tot > 0)
    f["liq_usd_log"] = np.log1p(tot)
    f["liq_z"] = (tot - tot.rolling(180).mean()) / (tot.rolling(180).std() + 1e-12)

    # ---- 吃单不平衡 ----
    lt, sht = st["long_taker_size"], st["short_taker_size"]
    f["taker_imb"] = (lt - sht) / (lt + sht).where((lt + sht) > 0)

    # ---- OI 与价格背离：OI 涨而价格跌 = 空头在加仓 ----
    ret6 = bars["close"].pct_change(6)
    f["oi_price_div"] = f["oi_chg_6"] - ret6
    f["oi_x_ret"] = f["oi_chg_6"] * ret6

    # ---- 基差 ----
    f["mark_prem"] = st["mark_price"] / bars["close"] - 1

    f = f.replace([np.inf, -np.inf], np.nan).astype("float64")

    if verbose:
        cov = f.notna().mean().round(3)
        print(f"\n衍生品特征 {f.shape[1]} 个，覆盖率:")
        print(cov.to_string())
        first = f.dropna(thresh=f.shape[1] // 2).index
        if len(first):
            print(f"有效起点(半数以上特征非空): {first[0]}")

    return f


if __name__ == "__main__":
    bars = pd.read_parquet(C.DATA_DIR / "BTC_USDT_4h.parquet")
    print(f"K线 {len(bars)} 根 {bars.index[0]} ~ {bars.index[-1]}")
    fd = build_deriv_features(bars, "4h")
    print(f"\n最后 3 行:")
    print(fd.tail(3).T.to_string())
