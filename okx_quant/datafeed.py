# -*- coding: utf-8 -*-
"""数据层：真实 K线抓取 + 本地 parquet 缓存。

两个数据源，分工明确：
  - **Gate.io**：长历史（4h 可回看约 4.5 年），用于训练与回测。
  - **OKX 永续**：短历史（几百根），用于面板实时行情与实盘规则 ——
    看的是哪个市场，就在哪个市场下单，止损价不会因为跨所价差算歪。

本机 www.okx.com 的 DNS 被污染到 169.254.0.2，必须走本机代理（PROXY 环境变量）。
"""
import os
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


# ---------------------------------------------------------------- OKX 行情
# K 线原本取自 Gate.io 现货，但交易在 OKX 永续。两个市场的价格、影线、
# 成交量都不一样 —— 主流币差 0.1%，小币能差 1~2%，止损价会算歪。
# 这里加一条 OKX 永续直连，让面板和规则看到的行情与真正下单的市场一致。
#
# 刻意不用 ccxt：ccxt 每次进程启动都要 load_markets（4600+ 市场，走代理
# 约 18 秒），而这里一个 GET 就够 —— 面板每 5 秒轮询一次，付不起那个钱。
OKX_BASE = "https://www.okx.com"
OKX_BAR = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
           "1h": "1H", "4h": "4H", "1d": "1Dutc"}


def _env(name, default=None):
    """读环境变量。进程环境块可能是旧快照（DSH 先于 setup 脚本启动），
    读不到就回退用户级注册表。"""
    v = os.getenv(name)
    if v:
        return v
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            v, _ = winreg.QueryValueEx(k, name)
        if v:
            return v
    except Exception:
        pass
    return default


def okx_inst(coin, kind="SWAP"):
    return f"{str(coin).upper()}-USDT-{kind}"


_okx_sess = None


def _okx_session():
    s = requests.Session()
    px = (_env("PROXY") or "").strip()
    if px:
        s.proxies.update({"http": px, "https": px})
    s.headers.update({"User-Agent": "okx-quant/1.0"})
    return s


def _okx_get(path, params, retries=3, timeout=12):
    global _okx_sess
    if _okx_sess is None:
        _okx_sess = _okx_session()
    last = None
    for i in range(retries):
        try:
            r = _okx_sess.get(OKX_BASE + path, params=params, timeout=timeout)
            r.raise_for_status()
            j = r.json()
            if j.get("code") not in (None, "0"):
                raise RuntimeError(j.get("msg") or "okx error")
            return j.get("data") or []
        except Exception as e:  # 代理有约 20% 瞬时失败率，重试比放弃划算
            last = e
            time.sleep(0.6 * (i + 1))
    raise last


_OKX_ALIVE = {"ts": 0.0, "ok": None}


def okx_alive(force=False, ttl=60.0):
    """OKX 现在到底通不通？带 60 秒缓存。

    为什么要它：本机访问 OKX 必须走代理（www.okx.com 的 DNS 被污染成
    169.254.0.2，真实 IP 的 443 也被阻断）。代理一停，每一次请求都要等
    超时 + 重试，一轮 tick 从 5 秒变成 45 秒。先花 3 秒探一下，
    不通就整轮改用 Gate —— Gate 可直连，永续价格与 OKX 差不到 0.1%。
    """
    now = time.time()
    if not force and _OKX_ALIVE["ok"] is not None and now - _OKX_ALIVE["ts"] < ttl:
        return _OKX_ALIVE["ok"]
    try:
        d = _okx_get("/api/v5/public/time", {}, retries=1, timeout=3)
        ok = bool(d)
    except Exception:
        ok = False
    _OKX_ALIVE.update(ts=now, ok=ok)
    return ok


def fetch_okx(coin, interval="4h", limit=300, verbose=False):
    """OKX 永续 K 线。返回与 fetch_gateio 同结构：index=UTC，列 open/high/low/close/volume。"""
    if interval not in OKX_BAR:
        raise ValueError(f"不支持的周期: {interval}")
    rows = _okx_get("/api/v5/market/candles",
                    {"instId": okx_inst(coin), "bar": OKX_BAR[interval], "limit": int(limit)},
                    retries=2, timeout=8)
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    # OKX 返回 [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]，且是**倒序**
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close",
                                     "volume", "vol_ccy", "vol_ccy_q", "confirm"])
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ts"] = pd.to_numeric(df["ts"], errors="coerce").astype("int64")
    df = df.dropna(subset=["open", "high", "low", "close"]).sort_values("ts")
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    if verbose:
        print(f"OKX {okx_inst(coin)} {interval}: {len(df)} 根, "
              f"{df.index[0]:%Y-%m-%d %H:%M} ~ {df.index[-1]:%Y-%m-%d %H:%M}")
    return df[["open", "high", "low", "close", "volume"]]


def okx_cache_path(coin, interval):
    return C.DATA_DIR / f"okx_{str(coin).upper()}_{interval}.parquet"


def load_okx_bars(coin, interval="4h", bars=300, ttl=300, force=False, verbose=False):
    """带磁盘缓存的 OKX K 线。

    ttl 秒内直接复用，因为 4h/1h 周期下一条 K 线在几十秒内不会变，
    而面板 5 秒轮询一次 —— 不缓存等于每次轮询都打一次交易所接口。
    """
    path = okx_cache_path(coin, interval)
    if not okx_alive():
        # OKX 不通：直接返回空，让调用方走 Gate。
        # 别在这里用过期缓存顶——那会让策略拿几十小时前的 K 线判断趋势。
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    if path.exists() and not force:
        try:
            if time.time() - path.stat().st_mtime <= ttl:
                old = pd.read_parquet(path)
                if len(old) >= min(bars, 50):
                    return old.tail(bars)
        except Exception:
            pass
    try:
        df = fetch_okx(coin, interval, limit=bars, verbose=verbose)
        if len(df):
            df.to_parquet(path)
            return df
    except Exception as e:
        if verbose:
            print(f"OKX K 线失败({e})，回退缓存")
    # 回退缓存必须判新鲜度：代理断掉时缓存会一直返回旧数据，
    # 而调用方（strategy._load_df）看到"有数据"就不会退到 Gate，
    # 于是策略拿着几十小时前的 K 线判断趋势。宁可返回空，让上层换源。
    if path.exists():
        try:
            old = pd.read_parquet(path)
            step = STEP.get(interval) or 14400
            age = time.time() - float(old.index[-1].timestamp())
            if age <= max(ttl * 2, step * 3):
                return old.tail(bars)
            if verbose:
                print(f"OKX 缓存已过期 {age/3600:.1f} 小时，不再使用")
        except Exception:
            pass
    return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


def okx_ticker(coin, kind="SWAP"):
    """实时成交价 + 24h 涨跌幅。一次 GET，约 0.2 秒。

    timeout 压到 5 秒、只重试 2 次 —— 行情拿不到可以用 K 线收盘价顶住，
    不值得为它把整轮 tick 拖到 30 秒（代理有约 20% 的瞬时失败率）。
    """
    if not okx_alive():
        return None
    d = _okx_get("/api/v5/market/ticker", {"instId": okx_inst(coin, kind)},
                 retries=2, timeout=5)
    if not d:
        return None
    t = d[0]
    last = float(t.get("last") or 0)
    o24 = float(t.get("open24h") or 0)
    return {
        "last": last,
        "chg_pct": ((last / o24 - 1.0) * 100.0) if o24 else None,
        "ts": int(t.get("ts") or 0),
        "inst": okx_inst(coin, kind),
    }


if __name__ == "__main__":
    import sys

    tf = sys.argv[1] if len(sys.argv) > 1 else C.TIMEFRAME
    d = load_bars(interval=tf, bars=MAX_POINTS, force=True)
    print(d.head(3))
    print(d.tail(3))
    print("行数", len(d), "跨度", d.index[-1] - d.index[0])
