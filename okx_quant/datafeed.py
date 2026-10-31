# -*- coding: utf-8 -*-
"""数据层：真实 K线抓取 + 本地 parquet 缓存。

数据源选择理由：
  本机 www.okx.com 被解析到 169.254.0.2（污染），BN/Bybit/KuCoin/Coinbase 同样不可达；
  api.gateio.ws 直连 200。BTC/USDT 是同一全球标的，跨所价差在 bps 级，
  所以用 Gate 的数据做训练与回测，用 OKX 做真实下单，两者成立。
"""
import time
import requests
import pandas as pd

import config as C

BASE = "https://api.gateio.ws/api/v4"
HEAD = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# Gate 的 K线字段顺序（官方文档）：
# [t, quote_volume, close, high, low, open, base_volume, window_closed]
COLS = ["ts", "quote_vol", "close", "high", "low", "open", "base_vol", "closed"]

STEP = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}

# Gate 硬限制: 只能回看最近 10000 根 (1h ≈ 416 天, 4h ≈ 4.5 年)
MAX_POINTS = 10000


def _fetch_chunk(pair, interval, frm, to):
    url = f"{BASE}/spot/candlesticks"
    params = {"currency_pair": pair, "interval": interval, "limit": C.BARS_LIMIT, "from": frm, "to": to}
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, headers=HEAD, timeout=20)
            if r.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))
    return []


def fetch_gateio(pair, interval, start_ts, end_ts, verbose=True):
    """抓 [start_ts, end_ts) 的 K线，返回 DataFrame(index=UTC datetime)。

    注意 Gate 的两个硬约束：
      1) (to-from)/step 必须 < limit，否则 "range too broad"
      2) from 不能早于 now - 10000*step，否则 "too long ago"
    因此这里从 end 往 start **反向翻页**，天然不会掉进第 2 条。
    """
    step = STEP[interval]
    band = step * (C.BARS_LIMIT - 1)
    cur_to = int(end_ts)
    out = []
    page = 0
    while cur_to > int(start_ts) and page < MAX_POINTS // (C.BARS_LIMIT - 1) + 2:
        frm = max(cur_to - band, int(start_ts))
        if frm >= cur_to:
            break
        rows = []
        try:
            rows = _fetch_chunk(pair, interval, frm, cur_to)
        except requests.HTTPError as e:
            body = ""
            if getattr(e, "response", None) is not None:
                body = e.response.text
            if "too long ago" in body:
                if verbose:
                    print(f"  已到 Gate 回看上限({pd.to_datetime(frm, unit='s')}), 停止翻页")
                break
            raise
        page += 1
        if rows:
            out.extend(rows)
        if verbose and page % 3 == 0:
            print(f"  ...已抓 {len(out)} 根, 回溯到 {pd.to_datetime(frm, unit='s')}")
        cur_to = frm
        if cur_to <= int(start_ts):
            break
        time.sleep(0.2)

    if not out:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = pd.DataFrame(out, columns=COLS)
    df["ts"] = df["ts"].astype("int64")
    for c in ("open", "high", "low", "close", "base_vol"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.rename(columns={"base_vol": "volume"})
    df = df[["ts", "open", "high", "low", "close", "volume"]]
    df = df.dropna().drop_duplicates(subset="ts").sort_values("ts")
    df.index = pd.to_datetime(df["ts"], unit="s", utc=True)
    return df[["open", "high", "low", "close", "volume"]]


def cache_path(pair, interval):
    return C.DATA_DIR / f"{pair}_{interval}.parquet"


def load_bars(pair=None, interval=None, bars=None, force=False, verbose=True):
    """优先读缓存；缓存不够长就补抓。bars=需要多少根，受 MAX_POINTS 限制。"""
    pair = pair or C.SYMBOL_DATA
    interval = interval or C.TIMEFRAME
    if bars is None:
        bars = C.TRAIN_BARS + C.N_FOLDS * C.TEST_BARS + 200
    bars = min(bars, MAX_POINTS)
    path = cache_path(pair, interval)

    step = STEP[interval]
    end = int(time.time()) // step * step
    # 留 100 根余量: Gate 的 10000 点上限是含边界的, 卡死会 400
    bars = min(bars, MAX_POINTS - 100)
    start = end - bars * step
    floor = end - MAX_POINTS * step
    if start < floor:
        start = floor
        if verbose:
            print(f"[提示] {interval} 只能回看 {MAX_POINTS} 根, 起点提前到 {pd.to_datetime(start, unit='s')}")

    # 缓存命中要同时满足两个条件：历史够长（起点够早）**且**数据够新（终点够晚）。
    # 原实现只检查起点，导致 parquet 一旦写成就永不更新——每小时跑的盯盘任务
    # 会一直拿到同一份旧 K 线，静默地把几小时前的行情当现价用。
    old = None
    t0 = start
    if path.exists() and not force:
        old = pd.read_parquet(path)
        if len(old):
            start_ok = int(old.index[0].timestamp()) <= start + step * 5
            gap = (end - int(old.index[-1].timestamp())) // step
            if start_ok and gap <= 1:
                if verbose:
                    print(f"缓存命中 {path.name}: {len(old)} 根, {old.index[0]} ~ {old.index[-1]}")
                return old.tail(bars)
            if start_ok:
                if verbose:
                    print(f"缓存 {path.name} 落后 {gap} 根, 增量补抓")
                t0 = int(old.index[-1].timestamp()) + step
            else:
                if verbose:
                    print(f"缓存 {path.name} 历史不足, 从 {pd.to_datetime(start, unit='s')} 重抓")

    t0 = max(t0, floor)
    if t0 > end:
        return old.tail(bars) if old is not None and len(old) else pd.DataFrame()
    if verbose:
        print(f"抓取 {pair} {interval}: {pd.to_datetime(t0, unit='s')} ~ {pd.to_datetime(end, unit='s')}")
    new = fetch_gateio(pair, interval, t0, end, verbose=verbose)
    df = pd.concat([old, new]) if old is not None and len(old) else new
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.to_parquet(path)
    if verbose:
        print(f"已存 {path}  共 {len(df)} 根  {df.index[0]} ~ {df.index[-1]}")
    return df.tail(bars)


if __name__ == "__main__":
    import sys

    tf = sys.argv[1] if len(sys.argv) > 1 else C.TIMEFRAME
    d = load_bars(interval=tf, bars=MAX_POINTS, force=True)
    print(d.head(3))
    print(d.tail(3))
    print("行数", len(d), "跨度", d.index[-1] - d.index[0])
