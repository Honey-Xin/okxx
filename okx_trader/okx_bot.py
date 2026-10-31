#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OKX 交易 CLI 骨架 (ccxt 版)
默认 DRY_RUN=1，只打印不下单；设 DRY_RUN=0 才真正发单。

环境变量:
  OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSWORD
  OKX_SANDBOX=1   -> 走 OKX 模拟盘(需在 OKX 后台开模拟盘 API Key)
  DRY_RUN=1       -> 默认值, 只计算不下单

用法示例:
  python okx_bot.py price --symbol BTC/USDT:USDT
  python okx_bot.py balance
  python okx_bot.py positions --symbol BTC/USDT:USDT
  python okx_bot.py cancel-all --symbol BTC/USDT:USDT
  python okx_bot.py grid --symbol BTC/USDT:USDT --lower 60000 --upper 70000 --grids 10 --size 0.01
  python okx_bot.py order --symbol BTC/USDT:USDT --side buy --type limit --amount 0.01 --price 60000
"""
import argparse
import json
import os
import sys
import time
from decimal import Decimal

try:
    import ccxt
except ImportError:
    sys.exit("pip install ccxt")


def env(name, default=None):
    v = os.environ.get(name, default)
    return v.strip() if isinstance(v, str) else v


DRY_RUN = env("DRY_RUN", "1") != "0"


def apply_proxy(url):
    """OKX 域名在部分网络下被污染(解析到 169.254.0.2)，必须走代理。"""
    if not url:
        return
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        os.environ[k] = url
    print(f"使用代理 {url}")


def build_client(need_auth=True):
    key = env("OKX_API_KEY")
    secret = env("OKX_API_SECRET")
    pwd = env("OKX_API_PASSWORD")
    auth = bool(key and secret and pwd)
    if need_auth and not auth:
        sys.exit("缺少 OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSWORD")
    ex = ccxt.okx(
        {
            "apiKey": key,
            "secret": secret,
            "password": pwd,
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},  # swap=永续, spot=现货
        }
    )
    if env("OKX_SANDBOX") == "1":
        ex.set_sandbox_mode(True)
    ex.load_markets()
    return ex


def fmt(x):
    return float(Decimal(str(x)).normalize()) if x is not None else None


# ---------------------------------------------------------------- commands
def cmd_price(ex, a):
    t = ex.fetch_ticker(a.symbol)
    print(
        f"{a.symbol}  last={t['last']}  bid={t['bid']}  ask={t['ask']}  "
        f"24h%={t['percentage']}  vol={t['quoteVolume']}"
    )


def cmd_balance(ex, a):
    b = ex.fetch_balance()
    for cur, amount in b["total"].items():
        if amount and amount > 0:
            print(f"{cur:>8}  free={b['free'].get(cur, 0):<18} total={amount}")


def cmd_positions(ex, a):
    ps = ex.fetch_positions([a.symbol] if a.symbol else None)
    if not ps:
        print("无持仓")
        return
    for p in ps:
        print(
            f"{p['symbol']:<22} {p['side']:<5} contracts={p['contracts']} "
            f"entry={p['entryPrice']} mark={p['markPrice']} "
            f"uPnl={p['unrealizedPnl']} lev={p['leverage']}"
        )


def cmd_cancel_all(ex, a):
    if DRY_RUN:
        print(f"[DRY] 撤销 {a.symbol} 全部挂单")
        return
    print(ex.cancel_all_orders(a.symbol))


def cmd_order(ex, a):
    amount = float(a.amount)
    price = float(a.price) if a.price else None
    if a.type == "market":
        price = None
    params = {}
    if a.symbol.endswith(":USDT") and a.type == "limit":
        params["tdMode"] = "cross"
    print(f"{'[DRY] ' if DRY_RUN else ''}下单 {a.side} {a.type} {amount} {a.symbol} @ {price or 'MKT'}")
    if DRY_RUN:
        return
    o = ex.create_order(a.symbol, a.type, a.side, amount, price, params)
    print(json.dumps({"id": o["id"], "status": o["status"], "price": o["price"]}, ensure_ascii=False))


def cmd_grid(ex, a):
    """极简网格：区间均分，价格上穿挂卖、下穿挂买。循环刷新。"""
    sym, lower, upper, n, size = a.symbol, float(a.lower), float(a.upper), int(a.grids), float(a.size)
    if lower >= upper:
        sys.exit("--lower 必须小于 --upper")
    step = (upper - lower) / n
    lines = [round(lower + step * i, 8) for i in range(n + 1)]
    print(f"网格 {sym} 区间 [{lower}, {upper}] {n} 格 步长 {step:.4f} 每格 {size}")
    print("网格线:", lines)

    if a.offline:
        # 离线自测: 不连交易所, 用正弦价格序列验证网格算路
        import math
        mid = (lower + upper) / 2
        amp = (upper - lower) / 2 * 0.9
        for tick in range(a.ticks):
            last = mid + amp * math.sin(tick / 3.0)
            print(f"\n[offline tick {tick}] last={last:.2f}")
            for lv in lines[1:]:
                if lv < last:
                    place(ex, sym, "buy", lv, size)
                elif lv > last:
                    place(ex, sym, "sell", lv, size)
        print("\n离线自测结束: 网格算路正常")
        return

    while True:
        try:
            last = float(ex.fetch_ticker(sym)["last"])
            print(f"\n[{time.strftime('%H:%M:%S')}] last={last}")
            open_orders = ex.fetch_open_orders(sym)
            bid_prices = {float(o["price"]) for o in open_orders if o["side"] == "buy"}
            ask_prices = {float(o["price"]) for o in open_orders if o["side"] == "sell"}

            for i, lv in enumerate(lines[1:], start=1):
                # 网格线下方: 该价位挂买单
                if lv < last and not any(abs(lv - p) < step * 0.2 for p in bid_prices):
                    place(ex, sym, "buy", lv, size)
                # 网格线上方: 该价位挂卖单
                if lv > last and not any(abs(lv - p) < step * 0.2 for p in ask_prices):
                    place(ex, sym, "sell", lv, size)

            if a.once:
                return
            time.sleep(a.interval)
        except KeyboardInterrupt:
            print("\n退出网格")
            return
        except Exception as e:
            print("错误:", e)
            time.sleep(5)


def place(ex, sym, side, price, size):
    params = {"tdMode": "cross"} if sym.endswith(":USDT") else {}
    msg = f"  {'[DRY]' if DRY_RUN else '>>'} {side.upper():<4} {size} @ {price}"
    print(msg)
    if DRY_RUN:
        return
    try:
        ex.create_order(sym, "limit", side, size, price, params)
    except Exception as e:
        print(f"    失败: {e}")


# ---------------------------------------------------------------- argparse
PROXY_HELP = "如 http://127.0.0.1:7890 或 socks5h://127.0.0.1:1080；也可设 HTTPS_PROXY"


def main():
    p = argparse.ArgumentParser(description="OKX 交易 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--proxy", default=None, help=PROXY_HELP)

    def add(name, fn, need_symbol=True):
        s = sub.add_parser(name, parents=[common])
        if need_symbol:
            s.add_argument("--symbol", default="BTC/USDT:USDT")
        s.set_defaults(fn=fn)

    add("price", cmd_price)
    add("balance", cmd_balance, need_symbol=False)
    add("positions", cmd_positions)
    add("cancel-all", cmd_cancel_all)

    s = sub.add_parser("order", parents=[common])
    s.add_argument("--symbol", default="BTC/USDT:USDT")
    s.add_argument("--side", choices=["buy", "sell"], required=True)
    s.add_argument("--type", choices=["limit", "market"], default="limit")
    s.add_argument("--amount", required=True)
    s.add_argument("--price")
    s.set_defaults(fn=cmd_order)

    s = sub.add_parser("grid", parents=[common])
    s.add_argument("--symbol", default="BTC/USDT:USDT")
    s.add_argument("--lower", required=True)
    s.add_argument("--upper", required=True)
    s.add_argument("--grids", type=int, default=10)
    s.add_argument("--size", required=True, help="每格下单数量(币)")
    s.add_argument("--interval", type=float, default=15, help="轮询秒数")
    s.add_argument("--once", action="store_true", help="只跑一轮")
    s.add_argument("--offline", action="store_true", help="离线自测网格算路, 不连交易所")
    s.add_argument("--ticks", type=int, default=8, help="离线自测的价格点数量")
    s.set_defaults(fn=cmd_grid)

    a = p.parse_args()
    apply_proxy(a.proxy)
    print(f"DRY_RUN={DRY_RUN}  SANDBOX={env('OKX_SANDBOX', '0')}")
    if getattr(a, "offline", False):
        a.fn(None, a)
        return
    ex = build_client(need_auth=a.cmd != "price")
    a.fn(ex, a)


if __name__ == "__main__":
    main()
