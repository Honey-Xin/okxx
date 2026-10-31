# -*- coding: utf-8 -*-
"""执行决策：校验 + 风控 + 下单。这是唯一能动钱的脚本。

设计原则：**不信任决策文件**。LLM 写进去的每一个数字都要重新算一遍，
任何一项越界就整笔拒掉。拒单比错单便宜得多。

默认 dry-run。真实下单必须显式传 --live 并且环境里 DRY_RUN=0，
两道开关是故意的——防止误操作把测试当实盘。
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
POS_FILE = os.path.join(STATE_DIR, "positions.json")
AUDIT_FILE = os.path.join(STATE_DIR, "audit.log")

import config as C  # noqa: E402
import funding_arb_live as FL  # noqa: E402

VALID_REGIMES = {"trend_up", "trend_down", "range", "high_vol", "unclear"}
VALID_OPS = {"open", "close"}


def log_event(evt):
    os.makedirs(STATE_DIR, exist_ok=True)
    evt = dict(evt)
    evt.setdefault("ts", datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"))
    with open(AUDIT_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(evt, ensure_ascii=False) + "\n")


def load_positions():
    if not os.path.exists(POS_FILE):
        return {"positions": [], "equity_usdt": C.INIT_EQUITY}
    with open(POS_FILE, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_positions(p):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = POS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(p, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, POS_FILE)


def validate_decision(d, positions):
    """返回 (是否通过, 拒绝原因列表)。全部检查都是硬性的。"""
    errs = []
    if not isinstance(d, dict):
        return False, ["决策文件不是 JSON 对象"]

    if d.get("regime") not in VALID_REGIMES:
        errs.append(f"regime={d.get('regime')!r} 不在 {sorted(VALID_REGIMES)}")

    c = d.get("confidence")
    if not isinstance(c, (int, float)) or not (0 <= c <= 1):
        errs.append(f"confidence 必须是 0~1，得到 {c!r}")

    if d.get("veto") is True and not str(d.get("veto_reason", "")).strip():
        errs.append("veto=true 但没写 veto_reason")

    acts = d.get("actions")
    if acts is None:
        errs.append("缺 actions 字段（可以为空数组，但不能缺）")
        return False, errs
    if not isinstance(acts, list):
        errs.append("actions 必须是数组")
        return False, errs

    held = {p["coin"] for p in positions.get("positions", [])}
    total = sum(float(p.get("usd", 0)) for p in positions.get("positions", []))
    seen = set()

    for i, a in enumerate(acts):
        if not isinstance(a, dict):
            errs.append(f"actions[{i}] 不是对象")
            continue
        op = a.get("op")
        coin = str(a.get("coin", "")).upper()
        if op not in VALID_OPS:
            errs.append(f"actions[{i}].op={op!r} 不在 {sorted(VALID_OPS)}")
            continue
        if not coin:
            errs.append(f"actions[{i}] 缺 coin")
            continue
        if coin in FL.BLACKLIST:
            errs.append(f"{coin} 在黑名单里（历史费率为负）")
        if coin in seen:
            errs.append(f"{coin} 在同一批决策里出现多次")
        seen.add(coin)

        if op == "open":
            usd = a.get("usd")
            if not isinstance(usd, (int, float)) or usd <= 0:
                errs.append(f"{coin} 开仓金额必须是正数，得到 {usd!r}")
                continue
            if usd > FL.MAX_USD_PER_COIN:
                errs.append(f"{coin} 开仓 {usd} 超过单币上限 {FL.MAX_USD_PER_COIN}")
            if coin in held:
                errs.append(f"{coin} 已有持仓，先平再开，避免叠加成裸敞口")
            if total + usd > FL.MAX_TOTAL_USD:
                errs.append(f"{coin} 开仓后总敞口 {total + usd:.0f} 超过上限 {FL.MAX_TOTAL_USD}")
            total += usd
        else:  # close
            if coin not in held:
                errs.append(f"{coin} 没有持仓，无法平仓")

    return len(errs) == 0, errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--decisions", required=True)
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不执行")
    ap.add_argument("--live", action="store_true", help="真实下单（还需 DRY_RUN=0）")
    a = ap.parse_args()

    with open(a.decisions, "r", encoding="utf-8") as fh:
        d = json.load(fh)

    pos = load_positions()
    ok, errs = validate_decision(d, pos)

    print(f"决策时间: {d.get('as_of')}   regime={d.get('regime')}   "
          f"confidence={d.get('confidence')}   veto={d.get('veto')}")
    if d.get("veto"):
        print(f"否决理由: {d.get('veto_reason')}")
    print(f"待执行动作: {len(d.get('actions', []))} 个")

    if not ok:
        print("\n校验未通过，全部拒单：")
        for e in errs:
            print(f"  ✗ {e}")
        log_event({"event": "reject", "errors": errs, "decision": d})
        return 2

    print("校验通过。")

    if not d.get("actions"):
        print("没有需要执行的动作（空仓也是决策）。")
        log_event({"event": "noop", "decision": d})
        return 0

    really_live = a.live and not FL.DRY_RUN and not a.dry_run
    if not really_live:
        mode = "DRY-RUN"
        why = "未传 --live" if not a.live else "DRY_RUN!=0"
        print(f"\n[{mode}] 不会真实下单，原因：{why}")
    else:
        mode = "LIVE"
        print("\n[LIVE] 即将真实下单，3 秒后开始…")
        import time
        time.sleep(3)

    results = []
    for act in d["actions"]:
        coin, op = act["coin"].upper(), act["op"]
        print(f"\n--- {op.upper()} {coin} ---")
        try:
            if not really_live:
                # 走一遍数量计算但不真正下单，确认精度与张数都对
                if op == "open":
                    sym, amt, px = FL._amount(coin, float(act["usd"]))
                    print(f"  计划买入 {amt} {coin} @ {px:.6f} ≈ {act['usd']} USDT，同时永续开空等量")
                else:
                    print(f"  计划平掉 {coin} 的现货多头与永续空头")
                results.append({"coin": coin, "op": op, "status": "dry_run"})
                continue

            if op == "open":
                r = FL.open_position(coin, float(act["usd"]))
                pos["positions"].append({
                    "coin": coin,
                    "usd": float(act["usd"]),
                    "spot_amt": r.get("spot_amt"),
                    "contracts": r.get("contracts"),
                    "entry_price": r.get("price"),
                    "opened_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                })
                results.append({"coin": coin, "op": op, "status": "filled", "detail": r})
            else:
                FL.close_position(coin)
                pos["positions"] = [p for p in pos["positions"] if p["coin"] != coin]
                results.append({"coin": coin, "op": op, "status": "closed"})
            save_positions(pos)
        except Exception as e:
            print(f"  失败: {type(e).__name__}: {e}")
            results.append({"coin": coin, "op": op, "status": "error",
                            "error": f"{type(e).__name__}: {e}"})

    print(f"\n=== 汇总（{mode}）===")
    for r in results:
        print(f"  {r['op']:<6}{r['coin']:<8}{r['status']}")
    log_event({"event": "execute", "mode": mode, "results": results, "decision": d})
    return 0


if __name__ == "__main__":
    sys.exit(main())
