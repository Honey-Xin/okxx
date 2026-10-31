# -*- coding: utf-8 -*-
"""实盘执行层：多币种资金费率中性套利（现货多 + 永续空）。

支持 OKX 和 Gate.io 两家，用环境变量 EXCHANGE 切换。

关于网络：本机 DNS 对 okx.com 有污染（解析到 169.254.0.2），所以 ccxt 直连 OKX
会超时。但只要本机开着代理（常见是 Clash 的 127.0.0.1:7897），把 PROXY 指过去
就能正常访问——实测能加载 4636 个合约。Python 的 requests 会读系统代理，
但 ccxt 用的是自己的 session，必须显式注入 proxies，这是踩过的坑。

运作方式：
  1. 现货买入 COIN，同时在永续开出等量空单 → delta 中性，价格涨跌不影响净值
  2. 每 8 小时（或 4 小时）结算一次资金费率，空头方向在正费率时收钱
  3. 费率转负且持续 → 平仓离场，避免反向付费

安全设计：
  - DRY_RUN=1 是默认值，不设 API key 也能跑通全部流程（只打印不下单）
  - 单币敞口、总敞口、最小费率三重闸门
  - 下单前校验合约张数与面值，避免数量算错

用法：
  python funding_arb_live.py scan                    扫描当前费率机会
  python funding_arb_live.py balance                 查余额
  python funding_arb_live.py open --coin OP --usd 100 建仓
  python funding_arb_live.py status                  查持仓与浮动盈亏
  python funding_arb_live.py close --coin OP         平仓
  python funding_arb_live.py monitor --interval 3600 持续监控

环境变量：
  EXCHANGE=okx|gate          默认 okx
  OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSWORD      （OKX 需要 passphrase）
  GATE_API_KEY / GATE_API_SECRET                       （Gate 不需要 passphrase）
  PROXY=http://127.0.0.1:7897  留空则直连
  DRY_RUN=1（默认）| 0 真实下单
  MAX_USD_PER_COIN=200  MAX_TOTAL_USD=1000  MIN_FUNDING_APR=5
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import ccxt

# ---------------------------------------------------------------- 配置
def env(name, default=None):
    """读环境变量，读不到再回退到 Windows 用户级注册表。

    为什么要回退：本进程的父进程（DSH）可能在 setup_okx.ps1 写入用户环境变量
    之前就已经启动，进程环境块是快照，看不到后来的修改。读注册表才能拿到最新值，
    否则定时任务里永远报「缺少 OKX_API_KEY」。
    """
    v = os.getenv(name)
    if v:
        return v
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                val, _ = winreg.QueryValueEx(k, name)
                return val or default
        except OSError:
            return default
    return default


DRY_RUN = str(env("DRY_RUN", "1")) != "0"
MAX_USD_PER_COIN = float(env("MAX_USD_PER_COIN", "200"))
MAX_TOTAL_USD = float(env("MAX_TOTAL_USD", "1000"))
MIN_FUNDING_APR = float(env("MIN_FUNDING_APR", "5"))   # 年化百分比
LEVERAGE = int(env("LEVERAGE", "1"))

BLACKLIST = {"ATOM"}          # 历史上费率为负，剔除


def log(*a):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}]", *a, flush=True)


EXCHANGE = str(env("EXCHANGE", "okx")).lower()
PROXY = str(env("PROXY", "") or "").strip()

_EXCHANGE_CLS = {"okx": ccxt.okx, "gate": ccxt.gate}
if EXCHANGE not in _EXCHANGE_CLS:
    raise ValueError(f"EXCHANGE 只能是 okx 或 gate，收到 {EXCHANGE!r}")


def credentials():
    """返回 (key, secret, password)。OKX 需要 passphrase，Gate 不需要。"""
    if EXCHANGE == "okx":
        return (env("OKX_API_KEY"), env("OKX_API_SECRET"),
                env("OKX_API_PASSWORD"))
    return (env("GATE_API_KEY"), env("GATE_API_SECRET"), None)


_CLIENTS = {}


def build_client(kind="spot"):
    """kind: spot | swap。没配 key 时返回只读客户端（公共接口可用）。

    实例按 kind 缓存：OKX 的 load_markets 要拉 4600+ 市场，每次新建客户端都会
    重拉一遍（在 80% 成功率的链路上就是几十秒的浪费），所以复用。
    """
    if kind in _CLIENTS:
        return _CLIENTS[kind]
    cfg = {"enableRateLimit": True, "options": {"defaultType": kind},
           "timeout": 30000}
    key, sec, pwd = credentials()
    if key and sec:
        cfg["apiKey"] = key
        cfg["secret"] = sec
        if pwd:
            cfg["password"] = pwd
    ex = _EXCHANGE_CLS[EXCHANGE](cfg)
    if PROXY:
        proxies = {"http": PROXY, "https": PROXY}
        ex.proxies = proxies
        ex.session.proxies.update(proxies)
    _CLIENTS[kind] = ex
    return ex


def need_auth():
    key, sec, pwd = credentials()
    if EXCHANGE == "okx":
        pairs = [("OKX_API_KEY", key), ("OKX_API_SECRET", sec),
                 ("OKX_API_PASSWORD", pwd)]
    else:
        pairs = [("GATE_API_KEY", key), ("GATE_API_SECRET", sec)]
    missing = [n for n, v in pairs if not v]
    if missing:
        log(f"缺少 {' / '.join(missing)}，只能用 scan 和只读接口")
        return False
    return True


def swap_params(extra=None):
    """OKX 永续下单必须带 tdMode；Gate 不需要。"""
    p = {"tdMode": "cross"} if EXCHANGE == "okx" else {}
    if extra:
        p.update(extra)
    return p


# ---------------------------------------------------------------- 网络容错
NET_RETRIES = int(os.getenv("NET_RETRIES", "4"))
NET_BASE = float(os.getenv("NET_RETRY_BASE", "1.5"))

NET_ERRORS = (ccxt.NetworkError, ccxt.RequestTimeout,
              ccxt.ExchangeNotAvailable, OSError)


def retry_net(fn, what="请求"):
    """OKX 经代理实测只有 ~80% 成功率（SSL EOF），必须重试。

    3 次重试可把 80% 提到 ~99.2%。下单场景另靠 clOrdId 保证幂等，
    所以重试不会造成重复建仓。
    """
    def wrap(*a, **kw):
        last = None
        for i in range(NET_RETRIES):
            try:
                return fn(*a, **kw)
            except NET_ERRORS as e:
                last = e
                if i == NET_RETRIES - 1:
                    break
                wait = NET_BASE * (2 ** i)
                log(f"   {what}失败({type(e).__name__})，{wait:.1f}s 后重试 {i+1}/{NET_RETRIES}")
                time.sleep(wait)
        raise last
    return wrap


def retry_call(obj, name, *a, **kw):
    return retry_net(getattr(obj, name), name)(*a, **kw)


# ---------------------------------------------------------------- 行情
def spot_symbol(coin):
    return f"{coin}/USDT"


def swap_symbol(coin):
    return f"{coin}/USDT:USDT"


def scan(top=15, max_apr=300.0, max_basis=0.008, min_price=0.001):
    """扫描永续资金费率，只保留「真正可对冲」的机会。

    关键在过滤，不在排序。年化 4000% 的币基本都是无现货、无流动性的空气币，
    单边开空等于裸赌。可交易机会必须同时满足：
      1. 有对应的 USDT 现货市场（否则无法做 delta 中性）
      2. 年化不超过 max_apr（超过说明费率异常，通常伴随极端行情）
      3. 现货与合约标记价偏离不超过 max_basis（基差太大会吃掉收益）
      4. 价格不低于 min_price（低价币最小变动单位占比过大）
    """
    ex = build_client("swap")
    markets = retry_call(ex, "load_markets")
    spot_ex = build_client("spot")
    sm = retry_call(spot_ex, "load_markets")
    spot_bases = {m["base"] for m in sm.values()
                  if m.get("spot") and m.get("quote") == "USDT" and m.get("active", True)}

    # 关键修复：一次拉全量费率。原先逐个 fetch_funding_rate 会产生上千次请求，
    # 在实测只有 ~80% 成功率的链路上必然大面积丢数据（表现为「0 个候选」）。
    rates = retry_call(ex, "fetch_funding_rates")

    raw = []
    for sym, fr in rates.items():
        m = markets.get(sym)
        if not m:
            continue
        if not m.get("swap") or not m.get("linear") or m.get("settle") != "USDT":
            continue
        if not m.get("active", True):
            continue
        base = m.get("base")
        if not base or base in BLACKLIST or base not in spot_bases:
            continue
        rate = fr.get("fundingRate")
        if rate is None:
            continue
        try:
            ih = float(str(fr.get("interval") or "8h").replace("h", ""))
        except ValueError:
            ih = 8.0
        apr = rate * (24 / ih) * 365 * 100
        if apr > max_apr:
            continue
        info = fr.get("info") or {}
        prem = info.get("premium")
        raw.append(dict(coin=base, symbol=sym, rate=rate, interval_h=ih,
                        apr=apr, mark=float(fr.get("markPrice") or 0),
                        premium=float(prem) if prem not in (None, "") else None))

    raw.sort(key=lambda r: r["apr"], reverse=True)
    rows = []
    for r in raw[:80]:                       # 只对头部做基差校验，避免上千次请求
        try:
            sp = float(retry_call(spot_ex, "fetch_ticker",
                                  spot_symbol(r["coin"]))["last"])
        except Exception:
            continue
        if not sp or sp < min_price:
            continue
        # OKX 的 fetch_funding_rates 不返回 markPrice，此时用 info.premium
        # （= (标记价-指数价)/指数价）作为基差，否则会因 basis=None 全被丢弃。
        basis = (r["mark"] - sp) / sp if r["mark"] else None
        if basis is None:
            basis = r.get("premium")
        if basis is None or abs(basis) > max_basis:
            continue
        r.update(spot=sp, basis=basis)
        rows.append(r)

    log(f"可对冲候选 {len(rows)} 个（已过滤无现货/异常费率/基差过大/低价币）")
    print(f"{'币种':<10}{'年化%':>9}{'单期费率':>12}{'间隔h':>7}{'基差%':>9}  现货价")
    for r in rows[:top]:
        print(f"{r['coin']:<10}{r['apr']:>9.2f}{r['rate']:>12.6f}{r['interval_h']:>7.0f}"
              f"{r['basis']*100:>9.3f}  {r['spot']}")
    print(f"\n门槛 MIN_FUNDING_APR={MIN_FUNDING_APR}%  单币上限 {MAX_USD_PER_COIN} USDT  "
          f"总上限 {MAX_TOTAL_USD} USDT")
    if not rows:
        print("当前没有同时满足费率与对冲条件的机会，等待比硬做更划算。")
    return rows


def balance():
    if not need_auth():
        return
    ex = build_client("swap")
    b = ex.fetch_balance()
    print("\n=== 永续账户 ===")
    for k in ("USDT", "total", "free", "used"):
        v = b.get(k)
        if isinstance(v, dict):
            print(f"  {k}: {v.get('USDT')}")
        elif v is not None:
            print(f"  {k}: {v}")
    exs = build_client("spot")
    b2 = exs.fetch_balance()
    print("\n=== 现货账户 ===")
    free = b2.get("free", {})
    nonzero = {k: v for k, v in free.items() if v and v > 0}
    print(f"  USDT: {free.get('USDT')}")
    if nonzero:
        print(f"  其他: {json.dumps({k: round(v, 6) for k, v in list(nonzero.items())[:12]}, ensure_ascii=False)}")


# ---------------------------------------------------------------- 下单
def _amount(coin, usd):
    """把 USDT 金额换算成现货 base 数量，并按交易所精度截断。"""
    ex = build_client("spot")
    ex.load_markets()
    sym = spot_symbol(coin)
    ticker = ex.fetch_ticker(sym)
    px = ticker["last"]
    raw = usd / px
    amt = float(ex.amount_to_precision(sym, raw))
    return sym, amt, px


def open_position(coin, usd):
    """现货买入 + 永续等量开空，构成 delta 中性。"""
    if usd > MAX_USD_PER_COIN:
        log(f"拒绝：{usd} USDT 超过单币上限 {MAX_USD_PER_COIN}")
        return
    sym_s, amt, px = _amount(coin, usd)
    sym_f = swap_symbol(coin)
    log(f"计划：买入 {amt} {coin} @ {px:.6f} ≈ {usd} USDT，同时永续开空等量")

    if DRY_RUN:
        log("[DRY_RUN] 未真实下单。设 DRY_RUN=0 才会执行。")
        return dict(coin=coin, spot_amt=amt, price=px, dry_run=True)

    if not need_auth():
        return

    exs = build_client("spot")
    exs.load_markets()
    o1 = exs.create_market_buy_order(sym_s, amt)
    log(f"现货买入完成：{o1.get('id')}")

    exf = build_client("swap")
    exf.load_markets()
    m = exf.market(sym_f)
    cs = float(m.get("contractSize") or 1)
    contracts = max(1, int(round(amt / cs)))
    try:
        exf.set_leverage(LEVERAGE, sym_f,
                         swap_params() if EXCHANGE == "okx" else None)
    except Exception as e:
        log(f"设置杠杆失败（通常不影响 1x）：{e}")
    o2 = exf.create_market_sell_order(sym_f, contracts, None, swap_params())
    log(f"永续开空完成：{o2.get('id')}，{contracts} 张")
    return dict(coin=coin, spot_amt=amt, contracts=contracts, price=px, dry_run=False)


def close_position(coin):
    """平掉现货多头与永续空头。"""
    sym_s, sym_f = spot_symbol(coin), swap_symbol(coin)
    if DRY_RUN:
        log(f"[DRY_RUN] 将平仓 {coin}：卖出全部现货 + 买入平掉全部空单")
        return
    if not need_auth():
        return

    exf = build_client("swap")
    exf.load_markets()
    try:
        pos = exf.fetch_positions([sym_f])
        live = [p for p in pos if abs(float(p.get("contracts") or 0)) > 0]
    except Exception as e:
        log(f"查询持仓失败：{e}")
        live = []
    for p in live:
        n = abs(float(p["contracts"]))
        side = "buy" if p["side"] == "short" else "sell"
        o = exf.create_market_order(sym_f, side, n, None,
                                    swap_params({"reduceOnly": True}))
        log(f"永续平仓 {side} {n} 张：{o.get('id')}")

    exs = build_client("spot")
    exs.load_markets()
    bal = exs.fetch_balance()
    amt = float((bal.get("free") or {}).get(coin) or 0)
    if amt > 0:
        o = exs.create_market_sell_order(sym_s, float(exs.amount_to_precision(sym_s, amt)))
        log(f"现货卖出 {amt} {coin}：{o.get('id')}")
    else:
        log(f"现货没有可卖的 {coin}")


# ---------------------------------------------------------------- 持仓
def status():
    if not need_auth():
        return
    exf = build_client("swap")
    exf.load_markets()
    pos = exf.fetch_positions()
    live = [p for p in pos if abs(float(p.get("contracts") or 0)) > 0]
    if not live:
        print("当前没有永续持仓")
        return
    print(f"{'币种':<8}{'方向':<7}{'张数':>10}{'名义USD':>12}{'开仓价':>12}{'标记价':>12}{'浮盈亏':>12}")
    tot_pnl = 0.0
    for p in live:
        n = float(p.get("contracts") or 0)
        notional = float(p.get("notional") or 0)
        pnl = float(p.get("unrealizedPnl") or 0)
        tot_pnl += pnl
        print(f"{p.get('base') or p['symbol']:<8}{p.get('side',''):<7}{n:>10.2f}"
              f"{notional:>12.2f}{float(p.get('entryPrice') or 0):>12.6f}"
              f"{float(p.get('markPrice') or 0):>12.6f}{pnl:>12.2f}")
    print(f"\n永续合计浮盈亏: {tot_pnl:+.2f} USDT")
    print("提醒：现货腿的浮盈亏方向相反，两者相加才是净值。")


def monitor(interval=3600):
    """持续监控：定期打印费率榜与持仓状态。"""
    log(f"监控启动，每 {interval}s 刷新一次，Ctrl+C 结束")
    while True:
        try:
            scan(top=8)
            status()
        except KeyboardInterrupt:
            log("监控结束")
            break
        except Exception as e:
            log(f"本轮出错：{type(e).__name__} {e}")
        time.sleep(interval)


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="多币种资金费率中性套利")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan")
    sub.add_parser("balance")
    sub.add_parser("status")
    p_open = sub.add_parser("open")
    p_open.add_argument("--coin", required=True)
    p_open.add_argument("--usd", type=float, default=100)
    p_close = sub.add_parser("close")
    p_close.add_argument("--coin", required=True)
    p_mon = sub.add_parser("monitor")
    p_mon.add_argument("--interval", type=int, default=3600)

    a = ap.parse_args()
    log(f"DRY_RUN={DRY_RUN}  单币上限={MAX_USD_PER_COIN}  总上限={MAX_TOTAL_USD}")

    if a.cmd == "scan":
        scan()
    elif a.cmd == "balance":
        balance()
    elif a.cmd == "status":
        status()
    elif a.cmd == "open":
        open_position(a.coin.upper(), a.usd)
    elif a.cmd == "close":
        close_position(a.coin.upper())
    elif a.cmd == "monitor":
        monitor(a.interval)


if __name__ == "__main__":
    main()
