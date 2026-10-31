# -*- coding: utf-8 -*-
"""生成市场快照：把行情压成 LLM 能读的结构化 JSON。

这一步是纯计算，不调任何模型。输出的数字就是决策的唯一事实来源，
LLM 不允许自己估算、也不允许引用快照以外的行情。
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

QUANT_ROOT = os.getenv("QUANT_ROOT", r"D:\dshwork\okx_quant")
sys.path.insert(0, QUANT_ROOT)
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(SKILL_DIR, "state")

import pandas as pd  # noqa: E402

import config as C  # noqa: E402
from datafeed import load_bars  # noqa: E402
from features import build_features, atr, rsi  # noqa: E402
import funding_arb_live as FL  # noqa: E402


def quiet_funding_scan(max_apr=300.0, max_basis=0.008, min_price=0.001, limit=12):
    """直接复用执行层的 scan()，只把它的表格输出吞掉。

    这里原先复刻了一份过滤逻辑，结果两边容易走偏：执行层的 scan 修好了
    （批量取费率 + premium 回退 + 重试），快照这份还是老实现，于是快照
    永远显示「没有机会」而实际是网络/字段问题。共用一份实现才不会错位。
    """
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rows = FL.scan(top=limit, max_apr=max_apr,
                       max_basis=max_basis, min_price=min_price)

    out = []
    for r in rows:
        if r["apr"] < FL.MIN_FUNDING_APR:      # scan() 只卡上限，下限在这里补
            continue
        out.append({"coin": r["coin"],
                    "funding_apr_pct": round(r["apr"], 2),
                    "funding_rate": r["rate"],
                    "interval_h": r["interval_h"],
                    "spot_price": r.get("spot"),
                    "mark_price": r.get("mark") or None,
                    "basis_pct": round((r.get("basis") or 0) * 100, 3)})
    return out


def _funding_cache_rows(limit=12):
    """OKX 走代理约有 20% 的瞬时失败率，而 scan() 是"全有或全无"：
    一次 load_markets 失败就让整个费率块变成空列表，看起来像"市场没机会"，
    其实是没拿到数据。失败时退回 dashboard.py 写的那份缓存，并显式标记陈旧。
    返回 (rows, age_minutes)。
    """
    p = os.path.join(QUANT_ROOT, "state", "funding_cache.json")
    if not os.path.exists(p):
        return [], None
    try:
        with open(p, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except Exception:
        return [], None

    age = None
    ts = obj.get("scanned_at")
    if ts:
        try:
            age = round((datetime.now(timezone.utc)
                         - datetime.fromisoformat(ts)).total_seconds() / 60, 1)
        except Exception:
            age = None

    rows = []
    for r in (obj.get("rows") or [])[:limit]:
        rows.append({"coin": r.get("coin"),
                     "funding_apr_pct": r.get("apr"),
                     "funding_rate": r.get("rate"),
                     "interval_h": r.get("interval_h"),
                     "spot_price": r.get("spot"),
                     "mark_price": r.get("mark"),
                     "basis_pct": r.get("basis_pct")})
    return rows, age


def market_state(df, lookback=96):
    """单币技术状态。只给摘要，不给原始 K 线。"""
    f = build_features(df)
    last = f.iloc[-1]
    win = df.tail(lookback)
    px = float(df["close"].iloc[-1])

    def ret(n):
        if len(df) <= n:
            return None
        return round(float(px / df["close"].iloc[-1 - n] - 1) * 100, 3)

    def num(key):
        v = last.get(key)
        return None if v is None or pd.isna(v) else round(float(v), 4)

    return {
        "price": round(px, 4),
        "as_of_bar": str(df.index[-1]),
        "return_pct": {f"{n}b": ret(n) for n in (1, 6, 24, 96) if ret(n) is not None},
        "range_position_pct": {
            "24b": round(float((win["close"].tail(24) < px).mean()) * 100, 1),
            "96b": round(float((win["close"] < px).mean()) * 100, 1),
        },
        "atr_pct": round(float(atr(df).iloc[-1] / px) * 100, 3),
        "rsi14": round(float(rsi(df["close"]).iloc[-1]), 1),
        "ema12_48": num("ema_ratio_12"),
        "ema24_96": num("ema_ratio_24"),
        "vol_ratio_24": num("vol_ratio_24"),
        "macd_hist": num("macd_hist"),
    }


def load_positions():
    p = os.path.join(STATE_DIR, "positions.json")
    if not os.path.exists(p):
        return {"positions": [], "equity_usdt": C.INIT_EQUITY}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as e:
        return {"positions": [], "equity_usdt": C.INIT_EQUITY,
                "error": f"positions.json 读取失败: {e}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", default="BTC,ETH,SOL")
    ap.add_argument("--no-funding", action="store_true", help="跳过费率扫描，只出技术面")
    ap.add_argument("--out", default=None, help="同时写一份到文件")
    a = ap.parse_args()

    coins = [c.strip().upper() for c in a.coins.split(",") if c.strip()]
    snap = {
        "as_of": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "data_source": "gate.io",
        "dry_run": FL.DRY_RUN,
        "constraints": {
            "max_usd_per_coin": FL.MAX_USD_PER_COIN,
            "max_total_usd": FL.MAX_TOTAL_USD,
            "min_funding_apr_pct": FL.MIN_FUNDING_APR,
            "max_basis_pct": 0.8,
            "blacklist": sorted(FL.BLACKLIST),
        },
        "positions": load_positions(),
    }

    market = {}
    for coin in coins:
        try:
            df = load_bars(pair=f"{coin}_USDT", interval=C.TIMEFRAME)
            market[coin] = market_state(df)
        except Exception as e:
            market[coin] = {"error": f"{type(e).__name__}: {e}"}
    snap["market"] = market

    if not a.no_funding:
        try:
            snap["funding_opportunities"] = quiet_funding_scan()
        except Exception as e:
            snap["funding_opportunities"] = []
            snap["funding_error"] = f"{type(e).__name__}: {e}"
            rows, age = _funding_cache_rows()
            if rows:
                snap["funding_opportunities"] = rows
                snap["funding_stale"] = True
                snap["funding_cache_age_min"] = age
                snap["funding_note"] = (
                    "本轮扫描失败，以下为缓存数据，不得当作实时机会开仓"
                )

    txt = json.dumps(snap, ensure_ascii=False, indent=2)
    print(txt)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(txt)
        print(f"\n已写入 {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
