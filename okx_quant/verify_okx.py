# -*- coding: utf-8 -*-
"""验证 OKX 凭据可用性 —— 只读检查，绝不下单、绝不划转。

检查项：
  1. 网络与时间同步（OKX 对请求时间戳敏感，偏差 >30s 会拒签）
  2. 凭据是否通过签名（fetch_balance）
  3. 账户模式（现货/合约是统一账户还是分开）
  4. 余额分布
  5. 已有持仓与挂单
  6. 下单权限探测（只读接口，不产生订单）

用法：
  $env:PROXY="http://127.0.0.1:7897"   # 若已写入用户环境变量则无需设置
  python verify_okx.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import funding_arb_live as FL


def ok(msg):
    print(f"  [OK]   {msg}")


def bad(msg):
    print(f"  [FAIL] {msg}")


def info(msg):
    print(f"  [..]   {msg}")


def main():
    print("=" * 62)
    print(f"交易所 = {FL.EXCHANGE}    代理 = {FL.PROXY or '直连'}    DRY_RUN = {FL.DRY_RUN}")
    print("=" * 62)

    if not FL.need_auth():
        return 1

    # ---------------------------------------------------------- 1. 时间
    print("\n[1/6] 网络与时间同步")
    try:
        ex = FL.build_client("swap")
        local = int(time.time() * 1000)
        remote = FL.retry_call(ex, "fetch_time")
        drift = (local - remote) / 1000.0
        ok(f"服务器时间 {remote}，本机偏差 {drift:+.1f}s")
        if abs(drift) > 30:
            bad("偏差超过 30s，OKX 会拒绝所有私有请求，请校准系统时间")
        else:
            ok("偏差在安全范围（<30s）")
    except Exception as e:
        bad(f"无法获取服务器时间：{type(e).__name__}: {e}")
        return 1

    # ---------------------------------------------------------- 2. 签名
    print("\n[2/6] 凭据签名验证")
    bal = None
    try:
        bal = FL.retry_call(ex, "fetch_balance")
        ok("签名通过（fetch_balance 成功）")
    except Exception as e:
        msg = str(e)
        bad(f"签名失败：{type(e).__name__}: {msg[:200]}")
        if "50113" in msg or "Invalid Sign" in msg:
            print("        → Secret 不对，或你在创建 key 时填的 passphrase 与这里不一致")
        elif "50111" in msg or "Invalid OK-ACCESS-KEY" in msg:
            print("        → API Key 不存在（可能复制时多了空格/换行）")
        elif "50105" in msg:
            print("        → 该 Key 没有读取权限，去 OKX 后台勾上「读取」")
        elif "Timestamp" in msg or "50102" in msg:
            print("        → 时间戳问题，先校准系统时间")
        return 1

    # ---------------------------------------------------------- 3. 账户模式
    print("\n[3/6] 账户模式")
    try:
        cfg = FL.retry_call(ex, "private_get_account_config")
        d = (cfg.get("data") or [{}])[0]
        acct = d.get("acctLv")
        names = {"1": "简单交易模式", "2": "单币种保证金", "3": "跨币种保证金", "4": "组合保证金"}
        ok(f"acctLv = {acct}（{names.get(str(acct), '未知')}）")
        if d.get("posMode"):
            ok(f"持仓模式 = {d.get('posMode')}（long_short_mode 需要开仓时指定 posSide）")
    except Exception as e:
        info(f"读不到账户配置（不影响交易）：{type(e).__name__}")

    # ---------------------------------------------------------- 4. 余额
    print("\n[4/6] 余额")
    free = (bal.get("free") or {})
    total = (bal.get("total") or {})
    usdt_total = float(total.get("USDT") or 0)
    usdt_free = float(free.get("USDT") or 0)
    ok(f"USDT 总额 {usdt_total:.2f}，可用 {usdt_free:.2f}")
    if usdt_total < 50:
        bad(f"USDT 余额 {usdt_total:.2f} 过低，单币 200 / 总 1000 的配置需要更多资金，"
            f"或把 MAX_USD_PER_COIN / MAX_TOTAL_USD 调小")
    others = {k: round(float(v), 6) for k, v in free.items() if k != "USDT" and v and float(v) > 0}
    if others:
        info(f"其他币种：{others}")

    # ---------------------------------------------------------- 5. 持仓与挂单
    print("\n[5/6] 现有持仓与挂单")
    try:
        pos = FL.retry_call(ex, "fetch_positions")
        live = [p for p in pos if abs(float(p.get("contracts") or 0)) > 0]
        if live:
            for p in live:
                ok(f"{p.get('symbol')} {p.get('side')} {p.get('contracts')} 张 "
                   f"开仓价 {p.get('entryPrice')} 未实现盈亏 {p.get('unrealizedPnl')}")
        else:
            ok("无持仓")
    except Exception as e:
        info(f"查询持仓失败：{type(e).__name__}")
    try:
        orders = FL.retry_call(ex, "fetch_open_orders")
        ok(f"未成交挂单 {len(orders)} 个")
    except Exception as e:
        info(f"查询挂单失败：{type(e).__name__}")

    # ---------------------------------------------------------- 6. 交易权限
    print("\n[6/6] 交易权限")
    try:
        spot = FL.build_client("spot")
        sm = FL.retry_call(spot, "load_markets")
        btc = sm.get("BTC/USDT")
        if btc and btc.get("active"):
            info(f"BTC/USDT 可交易，最小下单量 {btc.get('limits', {}).get('amount', {}).get('min')}")
        sw = FL.retry_call(ex, "load_markets")
        btcsw = sw.get("BTC/USDT:USDT")
        if btcsw:
            ok(f"BTC 永续面值 contractSize = {btcsw.get('contractSize')} BTC/张，"
               f"最小 {btcsw.get('limits', {}).get('amount', {}).get('min')} 张")
        info("注意：这里只探测市场元数据，没有下单。真正的下单权限要在首次实盘时才知道"
             "（缺权限会返回 50113 / 51000 系列错误）")
    except Exception as e:
        info(f"市场元数据探测失败：{type(e).__name__}")

    print("\n" + "=" * 62)
    print("凭据可用。下一步：")
    print("  1. python funding_arb_live.py scan      看当前有没有机会")
    print("  2. python funding_arb_live.py open --coin BTC --usd 50   干跑建仓计划")
    print("  3. DRY_RUN=0 前，务必先跑影子模式 2~4 周")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
