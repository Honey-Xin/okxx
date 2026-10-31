# -*- coding: utf-8 -*-
"""绩效与持仓报告：读审计日志和持仓文件，算出真实数字。

报告里的每个数都来自文件，不允许凭印象写。这样复盘时才能对得上账。
"""
import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone

QUANT_ROOT = os.getenv("QUANT_ROOT", r"D:\dshwork\okx_quant")
sys.path.insert(0, QUANT_ROOT)
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(SKILL_DIR, "state")
POS_FILE = os.path.join(STATE_DIR, "positions.json")
AUDIT_FILE = os.path.join(STATE_DIR, "audit.log")

import config as C  # noqa: E402


def read_audit():
    if not os.path.exists(AUDIT_FILE):
        return []
    rows = []
    with open(AUDIT_FILE, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def load_positions():
    if not os.path.exists(POS_FILE):
        return {"positions": [], "equity_usdt": C.INIT_EQUITY}
    with open(POS_FILE, "r", encoding="utf-8") as fh:
        return json.load(fh)


def pnl_snapshot(positions):
    """拉当前价，算每笔套利仓的浮动盈亏与已收资金费。

    注意：这里是「按当前标记价重估」，不是真实已实现盈亏。
    真实到账要以交易所账单为准，回测和实盘的差异通常就藏在这里。
    """
    if not positions:
        return [], 0.0
    import funding_arb_live as FL
    spot_ex = FL.build_client("spot")
    swap_ex = FL.build_client("swap")
    rows, total = [], 0.0
    for p in positions:
        coin = p["coin"]
        try:
            sp = float(spot_ex.fetch_ticker(FL.spot_symbol(coin))["last"])
        except Exception:
            sp = None
        try:
            mp = float(swap_ex.fetch_ticker(FL.swap_symbol(coin))["last"])
        except Exception:
            mp = None
        entry = float(p.get("entry_price") or 0)
        amt = float(p.get("spot_amt") or 0)
        spot_pnl = (sp - entry) * amt if (sp and entry and amt) else 0.0
        swap_pnl = (entry - mp) * amt if (mp and entry and amt) else 0.0
        net = spot_pnl + swap_pnl
        total += net
        rows.append(dict(coin=coin, usd=p.get("usd"), entry=entry,
                         spot=sp, mark=mp, spot_pnl=spot_pnl,
                         swap_pnl=swap_pnl, net=net,
                         opened_at=p.get("opened_at")))
    return rows, total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true", help="打印最近 20 条审计记录")
    a = ap.parse_args()

    pos = load_positions()
    positions = pos.get("positions", [])
    audit = read_audit()

    print("=" * 66)
    print("crypto-quant 运行报告")
    print(f"生成时间 {datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')}")
    print("=" * 66)

    print(f"\n【持仓】{len(positions)} 个套利仓，名义本金 "
          f"{sum(float(p.get('usd', 0)) for p in positions):.2f} USDT")
    if positions:
        rows, total = pnl_snapshot(positions)
        print(f"{'币种':<8}{'本金':>9}{'开仓价':>11}{'现货':>11}{'合约':>11}{'净浮动':>10}")
        for r in rows:
            print(f"{r['coin']:<8}{r['usd']:>9.2f}{r['entry']:>11.6f}"
                  f"{(r['spot'] or 0):>11.6f}{(r['mark'] or 0):>11.6f}{r['net']:>10.2f}")
        print(f"\n合计浮动盈亏 {total:+.2f} USDT（未含资金费到账）")
        print("提醒：浮动盈亏应接近 0，偏离大说明现货与合约数量没对齐。")
    else:
        print("  当前空仓")

    print(f"\n【决策历史】共 {len(audit)} 条审计记录")
    if audit:
        cnt = Counter(r.get("event") for r in audit)
        print(f"  事件分布: {dict(cnt)}")
        rejects = [r for r in audit if r.get("event") == "reject"]
        if rejects:
            print(f"  被拒 {len(rejects)} 次，最近原因：")
            for r in rejects[-3:]:
                for e in r.get("errors", [])[:3]:
                    print(f"    - {e}")
        executes = [r for r in audit if r.get("event") == "execute"]
        if executes:
            last = executes[-1]
            print(f"  最近一次执行: {last.get('ts')}  模式={last.get('mode')}")
            for r in last.get("results", []):
                print(f"    {r['op']:<6}{r['coin']:<8}{r['status']}")

    if a.verbose and audit:
        print("\n【最近 20 条原始记录】")
        for r in audit[-20:]:
            print("  " + json.dumps(r, ensure_ascii=False)[:220])

    print("\n" + "=" * 66)
    print(f"日志文件: {AUDIT_FILE}")
    print(f"持仓文件: {POS_FILE}")


if __name__ == "__main__":
    main()
