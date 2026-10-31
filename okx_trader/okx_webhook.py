#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TradingView 警报 -> OKX 下单 的 webhook 接收器
这是"交易插件"里最常用的一种形态: TV 报警带 JSON 打过来, 本地转成 OKX 订单。

启动:
  set OKX_API_KEY=... & set OKX_API_SECRET=... & set OKX_API_PASSWORD=...
  set WEBHOOK_SECRET=mysecret & set DRY_RUN=0
  python okx_webhook.py --port 8787

TradingView 警报正文(JSON):
  {"secret":"mysecret","symbol":"BTC/USDT:USDT","side":"buy","type":"market","amount":0.01}

TV Webhook URL 填: http://<你的公网地址>:8787/tv
TV 不允许自定义 header，所以鉴权靠 body 里的 secret 或 ?k=mysecret。
"""
import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import ccxt

DRY_RUN = os.environ.get("DRY_RUN", "1") != "0"
SECRET = os.environ.get("WEBHOOK_SECRET", "")
DEFAULT_SYMBOL = os.environ.get("DEFAULT_SYMBOL", "BTC/USDT:USDT")
DEFAULT_SIZE = os.environ.get("DEFAULT_SIZE", "0.01")
MAX_SIZE = float(os.environ.get("MAX_SIZE", "1"))


def client():
    ex = ccxt.okx(
        {
            "apiKey": os.environ["OKX_API_KEY"],
            "secret": os.environ["OKX_API_SECRET"],
            "password": os.environ["OKX_API_PASSWORD"],
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        }
    )
    if os.environ.get("OKX_SANDBOX") == "1":
        ex.set_sandbox_mode(True)
    ex.load_markets()
    return ex


EX = None


class H(BaseHTTPRequestHandler):
    def log_message(self, f, *a):
        print("[%s] %s" % (self.log_date_time_string(), f % a))

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if urlparse(self.path).path == "/health":
            return self._json(200, {"ok": True, "dry_run": DRY_RUN})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path not in ("/tv", "/order"):
            return self._json(404, {"error": "not found"})
        qs = parse_qs(u.query)
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace").strip()

        try:
            p = json.loads(raw) if raw.startswith("{") else self._parse_kv(raw)
        except Exception as e:
            return self._json(400, {"error": f"bad body: {e}"})

        token = str(p.get("secret") or p.get("key") or (qs.get("k") or [""])[0])
        if SECRET and token != SECRET:
            return self._json(403, {"error": "bad secret"})

        symbol = p.get("symbol") or DEFAULT_SYMBOL
        side = str(p.get("side") or "").lower()
        otype = str(p.get("type") or "market").lower()
        try:
            amount = float(p.get("amount") or DEFAULT_SIZE)
        except (TypeError, ValueError):
            return self._json(400, {"error": "bad amount"})
        price = p.get("price")
        price = float(price) if price not in (None, "", 0) else None

        if side not in ("buy", "sell"):
            return self._json(400, {"error": "bad side"})
        if amount <= 0 or amount > MAX_SIZE:
            return self._json(400, {"error": f"amount out of range (max {MAX_SIZE})"})

        print(f"信号: {symbol} {side} {otype} {amount} @ {price}")
        if DRY_RUN:
            return self._json(200, {"dry_run": True, "would": {"symbol": symbol, "side": side, "amount": amount, "price": price}})

        params = {"tdMode": p.get("tdMode", "cross")} if symbol.endswith(":USDT") else {}
        try:
            o = EX.create_order(symbol, otype, side, amount, price, params)
            return self._json(200, {"id": o["id"], "symbol": o["symbol"], "status": o["status"]})
        except Exception as e:
            return self._json(500, {"error": str(e)})

    @staticmethod
    def _parse_kv(raw):
        out = {}
        for k, v in parse_qs(raw).items():
            out[k] = v[0]
        return out


def main():
    global EX
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="0.0.0.0")
    a = ap.parse_args()
    if not DRY_RUN:
        EX = client()
    print(f"webhook 监听 http://{a.host}:{a.port}/tv  DRY_RUN={DRY_RUN}  secret={'on' if SECRET else 'off'}")
    ThreadingHTTPServer((a.host, a.port), H).serve_forever()


if __name__ == "__main__":
    main()
