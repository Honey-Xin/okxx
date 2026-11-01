# -*- coding: utf-8 -*-
"""LLM 决策层：把规则筛出的"方向候选"交给模型，算买点、仓位、杠杆、止盈止损。

分工很清楚：
  strategy.py  决定"现在值不值得看"  —— 确定性，可回测
  本文件       决定"怎么看、怎么下"  —— 模型给建议
  硬校验       决定"能不能下"        —— 越界就整笔否决，模型说了不算

模型的输出永远只是**建议**。任何一项超出 strategy.py 里设定的边界，
这笔交易就作废，并写明是哪一条不过。这不是不信任模型，是交易系统的基本要求：
人会填错，模型也会。

三种运行方式：
  python llm_trade.py prepare         生成本轮 prompt（state/pending.json + state/prompts/）
  python llm_trade.py run             有 DEEPSEEK_API_KEY 就直接调模型并校验
  python llm_trade.py apply FILE      用外部填好的 JSON 走校验
  python llm_trade.py show            看审批队列

通过校验的写入 state/approved.json，等执行层消费。
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import strategy as ST
from funding_arb_live import env

STATE_DIR = os.path.join(ROOT, "state")
CANDIDATES = os.path.join(STATE_DIR, "candidates.json")
PENDING = os.path.join(STATE_DIR, "pending.json")
APPROVED = os.path.join(STATE_DIR, "approved.json")
PROMPT_DIR = os.path.join(STATE_DIR, "prompts")
LLM_AUDIT = os.path.join(STATE_DIR, "llm_audit.jsonl")
TASK_FILE = os.path.join(STATE_DIR, "llm_task.json")
DECISIONS_PENDING = os.path.join(STATE_DIR, "decisions_pending.json")
AUTO_PRICED = os.path.join(STATE_DIR, "auto_priced.json")
CST = timezone(timedelta(hours=8))

MODEL = env("QUANT_MODEL") or "deepseek-chat"
# 买点允许偏离现价的比例。模型经常吐出几小时前的价格，这里挡一下
ENTRY_TOLERANCE_PCT = 0.5
# 止损止盈的绝对上下限（%），防止给出 0.01% 这种一碰就出的数字
MIN_SL_PCT = 0.3
# 同一策略定价后多久内不再重复入队。规则会每轮重新触发同一个信号，
# 没有冷却期的话，刚放弃的候选下一轮又回来，永远处理不完。
PRICE_COOLDOWN_SEC = float(os.getenv("QUANT_PRICE_COOLDOWN_MIN") or 60) * 60
MIN_TP_PCT = 0.3


def now_iso():
    return datetime.now(CST).isoformat(timespec="seconds")


def _read(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)


def log_event(event):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LLM_AUDIT, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now_iso(), **event}, ensure_ascii=False, default=str) + "\n")


def directional_actions():
    """从 candidates.json 里挑出方向类候选。套利候选不走这条路。"""
    data = _read(CANDIDATES, {})
    return [a for a in (data.get("actions") or [])
            if (a.get("type") or "") == "directional"]


# ------------------------------------------------------------------ prompt
SYSTEM_PROMPT = """你是加密货币合约交易决策模块。你的输出会被程序逐字段校验，
任何一项越过给定边界，整笔交易直接作废，不会有任何补救机会。

规则：
1. 只输出 JSON，不要 markdown、不要解释性前缀。
2. 所有数字必须是数字类型，不要字符串。
3. entry 必须贴近给定现价，偏离超过 0.5% 会被拒。
4. stop_loss 方向必须正确：做多时低于 entry，做空时高于 entry。
5. take_profit 方向同理相反。
6. 如果你认为不该开仓，输出 {"action":"skip","reason":"..."}，不要带其他字段。
7. reason 不超过 80 字，必须引用给定快照里的具体数字，不要空话。
8. 你没有"加仓"的能力：size_mult 只能 ≤ 1.0，杠杆不能超过上限。"""


def build_messages(action):
    snap = action.get("snapshot") or {}
    price = float(snap.get("price") or 0)
    side = action["side"]
    lev_cap = float(action["leverage_cap"])
    sl_cap = float(action["stop_loss_pct_cap"])
    tp_cap = float(action["take_profit_pct_cap"])
    usd = float(action["usd"])
    conf_min = float(action["min_confidence"])

    facts = {
        "coin": snap.get("coin"),
        "timeframe": snap.get("timeframe"),
        "bar_time": snap.get("bar_time"),
        "price": price,
        "rsi14": snap.get("rsi"),
        "range_position_24_pct": snap.get("pos"),
        "range_position_96_pct": snap.get("pos_long"),
        "vol_ratio_24": snap.get("vol_ratio"),
        "atr_pct": snap.get("atr_pct"),
        "ema_12_48_pct": snap.get("ema_fast_slow"),
        "macd_hist_scaled": snap.get("macd_hist"),
        "ret_24_pct": snap.get("ret_24"),
        "ret_96_pct": snap.get("ret_96"),
        "upper_wick": snap.get("upper_wick"),
        "lower_wick": snap.get("lower_wick"),
    }

    user = f"""## 行情快照
{json.dumps(facts, ensure_ascii=False, indent=2)}

## 规则触发
方向：{side}（{'做多' if side == 'long' else '做空'}）
触发原因：{action.get('reason')}

## 硬边界（不可突破）
- 方向必须是 {side}
- 单笔名义价值上限：{usd:.2f} USDT
- 杠杆上限：{lev_cap:g}x
- 止损距离上限：{sl_cap:g}%（离 entry 的百分比）
- 止盈距离上限：{tp_cap:g}%
- 置信度门槛：{conf_min:g}（低于此值会被否决）
- entry 必须落在 {price * (1 - ENTRY_TOLERANCE_PCT / 100):.6f} ~ {price * (1 + ENTRY_TOLERANCE_PCT / 100):.6f} 之间

## 输出格式
{{"action":"open","side":"{side}","entry":<数字>,"confidence":<0~1>,
 "size_mult":<0~1>,"leverage":<1~{lev_cap:g}>,"stop_loss":<数字>,
 "take_profit":<数字>,"reason":"<引用上面的数字>"}}

如果判断不该开仓：{{"action":"skip","reason":"<为什么>"}}"""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


# ------------------------------------------------------------------ 校验
def validate(action, dec):
    """逐条硬校验。返回 (ok, errs, clean)。

    这里的每一条都对应一类真实事故：
      方向错 → 本该做多却开空
      买点离谱 → 模型吐出两小时前的价格，成交即亏
      杠杆超限 → 模型"建议"20x，账户被一根针带走
      止损太远 → 名义 3% 止损变成实际 30% 亏损
      止损方向错 → 多头止损设在 entry 上方，开仓即触发
    """
    errs = []
    if not isinstance(dec, dict):
        return False, ["决策不是 JSON 对象"], None

    act = str(dec.get("action") or "").lower()
    if act == "skip":
        return True, [], {"action": "skip", "reason": str(dec.get("reason") or "")[:200]}
    if act != "open":
        return False, [f"action 只能是 open 或 skip，收到 {dec.get('action')!r}"], None

    snap = action.get("snapshot") or {}
    ref = float(snap.get("price") or 0)
    if ref <= 0:
        return False, ["快照里没有有效现价，无法校验买点"], None
    side = action["side"]

    def num(key, label, required=True):
        v = dec.get(key)
        if v is None:
            if required:
                errs.append(f"缺少 {key}")
            return None
        try:
            return float(v)
        except Exception:
            errs.append(f"{label} 不是数字：{v!r}")
            return None

    if str(dec.get("side") or "") != side:
        errs.append(f"方向必须是 {side}，收到 {dec.get('side')!r}")

    entry = num("entry", "买点")
    if entry is not None:
        dev = abs(entry / ref - 1.0) * 100.0
        if dev > ENTRY_TOLERANCE_PCT:
            errs.append(f"买点 {entry:g} 偏离现价 {ref:g} 达 {dev:.2f}%（上限 {ENTRY_TOLERANCE_PCT}%）")

    conf = num("confidence", "置信度")
    conf_min = float(action.get("min_confidence") or 0.55)
    if conf is not None:
        if not (0.0 <= conf <= 1.0):
            errs.append(f"置信度 {conf} 不在 0~1")
        elif conf < conf_min:
            errs.append(f"置信度 {conf:.2f} 低于门槛 {conf_min:g}")

    size_mult = num("size_mult", "仓位系数", required=False)
    if size_mult is None:
        size_mult = 1.0
    if not (0.0 < size_mult <= 1.0):
        errs.append(f"size_mult 必须落在 (0, 1]，收到 {size_mult}")

    lev = num("leverage", "杠杆", required=False)
    if lev is None:
        lev = 1.0
    lev_cap = float(action.get("leverage_cap") or 1)
    if lev > lev_cap:
        errs.append(f"杠杆 {lev:g}x 超过上限 {lev_cap:g}x")
    if lev < 1.0:
        errs.append(f"杠杆不能低于 1x，收到 {lev:g}")

    sl = num("stop_loss", "止损价")
    sl_cap = float(action.get("stop_loss_pct_cap") or 3)
    if sl is not None and entry is not None:
        d = abs(sl / entry - 1.0) * 100.0
        if side == "long" and sl >= entry:
            errs.append(f"做多的止损 {sl:g} 必须低于买点 {entry:g}")
        if side == "short" and sl <= entry:
            errs.append(f"做空的止损 {sl:g} 必须高于买点 {entry:g}")
        if d > sl_cap:
            errs.append(f"止损距离 {d:.2f}% 超过上限 {sl_cap:g}%")
        if d < MIN_SL_PCT:
            errs.append(f"止损距离 {d:.2f}% 太近（至少 {MIN_SL_PCT}%），一碰就出")

    tp = num("take_profit", "止盈价")
    tp_cap = float(action.get("take_profit_pct_cap") or 9)
    if tp is not None and entry is not None:
        d = abs(tp / entry - 1.0) * 100.0
        if side == "long" and tp <= entry:
            errs.append(f"做多的止盈 {tp:g} 必须高于买点 {entry:g}")
        if side == "short" and tp >= entry:
            errs.append(f"做空的止盈 {tp:g} 必须低于买点 {entry:g}")
        if d > tp_cap:
            errs.append(f"止盈距离 {d:.2f}% 超过上限 {tp_cap:g}%")
        if d < MIN_TP_PCT:
            errs.append(f"止盈距离 {d:.2f}% 太近（至少 {MIN_TP_PCT}%），没有意义")
        if tp is not None and sl is not None:
            rr = d / max(abs(sl / entry - 1.0) * 100.0, 1e-9)
            if rr < 1.0:
                errs.append(f"盈亏比 {rr:.2f} 小于 1（止盈比止损还近），不值得做")

    reason = str(dec.get("reason") or "").strip()
    if not reason:
        errs.append("reason 为空（必须说明依据）")
    elif len(reason) > 200:
        errs.append("reason 超过 200 字")

    if errs:
        return False, errs, None

    usd = round(float(action["usd"]) * size_mult, 2)
    return True, [], {
        "action": "open",
        "strategy_id": action.get("strategy_id"),
        "strategy_name": action.get("strategy_name"),
        "coin": action.get("coin"),
        "side": side,
        "order_side": action.get("order_side") or ("buy" if side == "long" else "sell"),
        "usd": usd,
        "leverage": lev,
        "entry_ref": ref,
        "entry_suggested": entry,
        "stop_loss": sl,
        "take_profit": tp,
        "confidence": conf,
        "size_mult": size_mult,
        "reason": reason,
        "model": MODEL,
        "validated_at": now_iso(),
    }


# ------------------------------------------------------------------ 三种运行方式
def prepare():
    """不调模型，只把 prompt 落盘。适合没有 key、想让 agent 或人来看的场景。"""
    actions = directional_actions()
    if not actions:
        print("没有方向类候选（规则没触发，或只启用了套利策略）")
        return {"pending": 0}

    old = _read(PENDING, {}) or {}
    old_items = old.get("items") or []
    # 同一笔候选只该进队列一次。规则每轮都会重新触发同一个信号 ——
    # 如果每轮都重新排队，用户刚复核完「放弃」，下一轮它又顶着「等定价」
    # 回来，看起来像永远处理不完。这里给已定价的标的加一段冷却期。
    now_ts = time.time()
    cooling = set()
    for i in old_items:
        if i.get("status") != "decided":
            continue
        dts = i.get("decided_at_sec")
        if dts and now_ts - float(dts) < PRICE_COOLDOWN_SEC:
            cooling.add(i.get("strategy_id"))

    os.makedirs(PROMPT_DIR, exist_ok=True)
    items = []
    skipped = 0
    for i, a in enumerate(actions):
        sid = a.get("strategy_id")
        if sid in cooling:
            skipped += 1
            continue
        msgs = build_messages(a)
        name = f"{sid or 'x'}-{int(time.time())}-{i}.json"
        p = os.path.join(PROMPT_DIR, name)
        _write(p, {"action": a, "messages": msgs})
        items.append({
            "id": name,
            "strategy_id": sid,
            "strategy_name": a.get("strategy_name"),
            "coin": a.get("coin"),
            "side": a.get("side"),
            "price": (a.get("snapshot") or {}).get("price"),
            "usd_cap": a.get("usd"),
            "prompt_file": p,
            "status": "awaiting_decision",
        })
    # 保留历史记录（decided），它们不再计数，但要能查「上次为什么放弃」
    kept = [i for i in old_items if i.get("status") != "awaiting_decision"]
    _write(PENDING, {"as_of": now_iso(), "updated_at": now_iso(),
                     "items": items + kept})
    if skipped:
        print(f"{skipped} 条在冷却期内（刚定过价），本轮不重复入队")
    print(f"已生成 {len(items)} 份 prompt -> {PROMPT_DIR}")
    print(f"待复核队列 -> {PENDING}")
    for it in items:
        print(f"  {it['coin']} {it['side']}  @ {it['price']}  上限 {it['usd_cap']} USDT")
    if items:
        print("\n下一步：填好决策后执行  python llm_trade.py apply <文件>")
    return {"pending": len(items)}


def _mark_decided(sids):
    """把 pending.json / auto_priced.json 里已处理的项标成 decided / reviewed。

    不删除条目 —— 留着当档案，但状态必须改，否则面板的「待定价」和
    「待复核定价」会永远挂着一件已经办完的事，看起来像卡住了。
    """
    if not sids:
        return
    now = now_iso()

    pend = _read(PENDING, {}) or {}
    items = pend.get("items") or []
    touched = 0
    for it in items:
        if it.get("status") == "awaiting_decision" and it.get("strategy_id") in sids:
            it["status"] = "decided"
            it["decided_at"] = now
            it["decided_at_sec"] = time.time()
            touched += 1
    if touched:
        pend["updated_at"] = now
        _write(PENDING, pend)

    auto = _read(AUTO_PRICED, {}) or {}
    aitems = auto.get("items") or []
    atouched = 0
    for it in aitems:
        if it.get("status") == "awaiting_review" and it.get("strategy_id") in sids:
            it["status"] = "reviewed"
            it["reviewed_at"] = now
            atouched += 1
    if atouched:
        auto["updated_at"] = now
        _write(AUTO_PRICED, auto)


def apply_file(path):
    """用外部给的决策走校验。文件格式：[{strategy_id, decision}] 或单个 {…}。"""
    raw = _read(path, None)
    if raw is None:
        print(f"读不到 {path}")
        return 1
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        print("文件格式不对，应为数组或对象")
        return 1

    actions = {a.get("strategy_id"): a for a in directional_actions()}
    approved, results = [], []
    for item in raw:
        sid = item.get("strategy_id")
        a = actions.get(sid) or (list(actions.values())[0] if len(actions) == 1 else None)
        if a is None:
            results.append({"strategy_id": sid, "ok": False,
                            "errs": ["找不到对应的候选（规则可能没触发）"]})
            log_event({"event": "reject", "reason": "候选不存在", "strategy_id": sid})
            continue
        ok, errs, clean = validate(a, item.get("decision") or item)
        if ok and clean and clean.get("action") == "open":
            # apply 这条路是 agent 或人手填的，不是直连 API 调的，
            # 标注清楚免得事后查日志以为走了 key
            clean["model"] = item.get("model") or "dsh-agent"
            approved.append(clean)
            results.append({"strategy_id": sid, "ok": True, "decision": clean})
            log_event({"event": "approve", "strategy_id": sid, "decision": clean})
        elif ok:
            results.append({"strategy_id": sid, "ok": True, "decision": clean})
            log_event({"event": "skip", "strategy_id": sid, "reason": (clean or {}).get("reason")})
        else:
            results.append({"strategy_id": sid, "ok": False, "errs": errs})
            log_event({"event": "reject", "strategy_id": sid, "errs": errs})

    _write(APPROVED, {"as_of": now_iso(), "orders": approved, "results": results})
    _mark_decided({r.get("strategy_id") for r in results if r.get("strategy_id")})
    for r in results:
        if r["ok"]:
            d = r.get("decision") or {}
            if d.get("action") == "open":
                print(f"  [通过] {d['coin']} {d['side']}  {d['usd']} USDT  {d['leverage']:g}x  "
                      f"止损 {d['stop_loss']:g}  止盈 {d['take_profit']:g}  置信 {d['confidence']:.2f}")
            else:
                print(f"  [跳过] {d.get('reason')}")
        else:
            print(f"  [拒绝] {r['strategy_id']}")
            for e in r["errs"]:
                print(f"         - {e}")
    print(f"\n通过的 {len(approved)} 笔 -> {APPROVED}")
    return 0


def run_api():
    """有 key 时走这条路：逐个候选调模型、校验、落盘。"""
    import llm_probe as LP
    key = env("DEEPSEEK_API_KEY")
    if not key:
        print("缺少 DEEPSEEK_API_KEY，改用 prepare 模式（生成 prompt 等人工/agent 填）")
        # 注意：prepare() 返回的是统计 dict，不是退出码。
        # 直接 return 它会让 sys.exit(dict) 变成 exit 1，被面板记成"执行报错"。
        prepare()
        return 0

    os.environ["DEEPSEEK_API_KEY"] = key

    actions = directional_actions()
    if not actions:
        print("没有方向类候选")
        return 0

    approved, results = [], []
    for a in actions:
        msgs = build_messages(a)
        try:
            data, dt = LP.call_deepseek(msgs, model=MODEL)
            txt = data["choices"][0]["message"]["content"]
            dec = json.loads(txt)
        except Exception as e:
            results.append({"strategy_id": a.get("strategy_id"), "ok": False,
                            "errs": [f"调用失败：{type(e).__name__}: {str(e)[:120]}"]})
            log_event({"event": "error", "strategy_id": a.get("strategy_id"),
                       "err": f"{type(e).__name__}: {str(e)[:200]}"})
            continue

        ok, errs, clean = validate(a, dec)
        log_event({"event": "llm_raw", "strategy_id": a.get("strategy_id"),
                   "raw": dec, "ok": ok, "errs": errs, "seconds": round(dt, 2)})
        if ok and clean and clean.get("action") == "open":
            approved.append(clean)
            results.append({"strategy_id": a.get("strategy_id"), "ok": True, "decision": clean})
        elif ok:
            results.append({"strategy_id": a.get("strategy_id"), "ok": True, "decision": clean})
        else:
            results.append({"strategy_id": a.get("strategy_id"), "ok": False, "errs": errs})

    _write(APPROVED, {"as_of": now_iso(), "orders": approved, "results": results})
    for r in results:
        if r["ok"]:
            d = r.get("decision") or {}
            if d.get("action") == "open":
                print(f"  [通过] {d['coin']} {d['side']}  {d['usd']} USDT  {d['leverage']:g}x")
            else:
                print(f"  [跳过] {d.get('reason')}")
        else:
            print(f"  [拒绝] {r['strategy_id']}")
            for e in r["errs"]:
                print(f"         - {e}")
    print(f"\n通过的 {len(approved)} 笔 -> {APPROVED}")
    return 0


def auto_price(action):
    """规则定价器：不调模型，用 ATR 与区间位置算出一份「可执行但待复核」的决策。

    为什么要有它：面板的 tick 是纯 Node 进程，它没法启动一个 agent 会话。
    如果定价只能等 agent 在场，那闭环就永远断在「任务书已生成」这一步。
    所以先用确定性算法把钱能怎么下算出来，让流程走完整，再让 agent 复核/否决。

    算法刻意做得笨且可解释：
      止损 = 1.2 × ATR（夹在 [0.3%, 上界]）
      止盈 = 2.5 × ATR（夹在 [0.3%, 上界]，并保证盈亏比 ≥ 1.2）
      仓位 = 越靠近区间边缘越小 —— 在 96 根 10% 分位做空，赔率天然差
      置信度 = 0.55 起步，按 RSI 与量能的配合度加减
    所有结果都还要过 validate()，过不了就跳过，绝不硬塞。
    """
    snap = action.get("snapshot") or {}
    price = float(snap.get("price") or 0)
    if price <= 0:
        return None, ["快照里没有有效现价"]

    side = action["side"]
    atr = float(snap.get("atr_pct") or 1.0)
    sl_cap = float(action.get("stop_loss_pct_cap") or 3.0)
    tp_cap = float(action.get("take_profit_pct_cap") or 9.0)
    conf_min = float(action.get("min_confidence") or 0.55)

    sl_pct = min(max(round(atr * 1.2, 2), MIN_SL_PCT), sl_cap)
    tp_pct = min(max(round(atr * 2.5, 2), MIN_TP_PCT), tp_cap)
    if tp_pct < sl_pct * 1.2:
        tp_pct = min(round(sl_pct * 1.2, 2), tp_cap)

    if side == "long":
        sl = round(price * (1 - sl_pct / 100.0), 1)
        tp = round(price * (1 + tp_pct / 100.0), 1)
    else:
        sl = round(price * (1 + sl_pct / 100.0), 1)
        tp = round(price * (1 - tp_pct / 100.0), 1)

    # 区间位置越极端，说明这笔越像"追尾"，仓位越小
    # 注意字段名：这里是 strategy.py 给的 action["snapshot"]（pos_long/pos），
    # 不是 build_messages 里那套 prompt 字段（range_position_96_pct）。
    # 用错名字不会报错，只会静默 fallback 到 50.0，仓位系数就变成拍脑袋。
    pos = float(snap.get("pos_long") or snap.get("pos") or 50.0)
    if side == "long":
        edge = pos / 100.0           # 越接近顶部越危险
    else:
        edge = 1.0 - pos / 100.0     # 越接近底部越危险
    size_mult = round(min(1.0, max(0.3, 1.0 - edge * 0.7)), 2)

    # 置信度：RSI 与方向配合、量能是否放大
    rsi = float(snap.get("rsi") or 50.0)
    vol = float(snap.get("vol_ratio") or 1.0)
    conf = conf_min + 0.02
    if side == "short" and rsi > 55:
        conf += 0.03
    if side == "long" and rsi < 45:
        conf += 0.03
    if vol >= 1.0:
        conf += 0.03
    elif vol < 0.3:
        conf -= 0.02
    conf = round(min(0.85, max(0.0, conf)), 2)

    dec = {
        "action": "open",
        "side": side,
        "entry": price,
        "confidence": conf,
        "size_mult": size_mult,
        "leverage": min(1.0, float(action.get("leverage_cap") or 1)),
        "stop_loss": sl,
        "take_profit": tp,
        "reason": (
            f"规则定价（未经 AI 复核）：现价 {price:g}，ATR {atr:.2f}% → 止损 {sl_pct:.2f}%、"
            f"止盈 {tp_pct:.2f}%，盈亏比 {tp_pct / max(sl_pct, 1e-9):.2f}；"
            f"96 根区间位置 {pos:.1f}%、RSI {rsi:.1f}、量比 {vol:.2f} → 仓位系数 {size_mult:.2f}。"
        ),
    }
    ok, errs, clean = validate(action, dec)
    if not ok:
        return None, errs
    clean["model"] = "rule-engine"
    return clean, []


def run_auto():
    """给本轮所有待定价候选跑规则定价，写 auto_priced.json（不是 approved，等复核）。"""
    pack = _read(TASK_FILE, {})
    items = []
    for c in (pack.get("candidates") or []):
        a = directional_actions()
        match = next((x for x in a if x.get("strategy_id") == c.get("strategy_id")), None)
        if not match:
            continue
        clean, errs = auto_price(match)
        items.append({
            "strategy_id": c.get("strategy_id"),
            "strategy_name": c.get("strategy_name"),
            "ok": bool(clean),
            "order": clean,
            "errs": errs,
            "status": "awaiting_review" if clean else "pricing_failed",
        })
    _write(AUTO_PRICED, {"as_of": now_iso(), "items": items})
    okn = sum(1 for i in items if i["ok"])
    # 定价这件事必须留痕，否则面板"执行日志"里什么都看不到，
    # 看起来像从没定过价
    for i in items:
        if i["ok"]:
            log_event({"event": "approve", "strategy_id": i["strategy_id"],
                       "decision": i["order"]})
        else:
            log_event({"event": "reject", "strategy_id": i["strategy_id"],
                       "errs": i["errs"], "reason": "规则定价未通过校验"})
    print(f"规则定价 -> {AUTO_PRICED}")
    print(f"成功 {okn} / 共 {len(items)} 条（状态 awaiting_review，等 AI 复核后才进 approved）")
    for i in items:
        if i["ok"]:
            o = i["order"]
            print(f"  {o['coin']} {o['side']}  {o['usd']:g} USDT  {o['leverage']:g}x  "
                  f"SL {o['stop_loss']:g}  TP {o['take_profit']:g}  置信 {o['confidence']:.2f}")
        else:
            print(f"  [跳过] {i['strategy_name']}: {'; '.join(i['errs'])[:120]}")
    return {"auto": len(items), "ok": okn}


def build_task():
    """把待定价候选打包成一份「给 DSH agent 的任务书」。

    为什么需要它：DSH 自己就是模型。面板里点「立即跑一轮」把候选算出来之后，
    不需要再去 platform.deepseek.com 申请 key —— 由 DSH 的 agent 读这份任务书、
    按同样的硬边界给决策，再走 apply 校验就够了。有 key 的人可以用 run 走直连 API，
    没 key 的人走这条路，两条路的校验是同一套。
    """
    pend = _read(PENDING, {})
    items = pend.get("items") or []
    cands = []
    for it in items:
        if it.get("status") != "awaiting_decision":
            continue
        pack = _read(it.get("prompt_file") or "", {})
        a = pack.get("action") or {}
        cands.append({
            "prompt_id": it.get("id"),
            "strategy_id": it.get("strategy_id"),
            "strategy_name": it.get("strategy_name"),
            "coin": it.get("coin"),
            "side": it.get("side"),
            "price": it.get("price"),
            "caps": {
                "usd": a.get("usd"),
                "leverage_max": a.get("leverage_cap"),
                "stop_loss_pct_max": a.get("stop_loss_pct_cap"),
                "take_profit_pct_max": a.get("take_profit_pct_cap"),
                "min_confidence": a.get("min_confidence"),
                "entry_tolerance_pct": ENTRY_TOLERANCE_PCT,
                "min_sl_pct": MIN_SL_PCT,
                "min_tp_pct": MIN_TP_PCT,
            },
            "template": a.get("template"),
            "reason": a.get("reason"),
            "messages": pack.get("messages") or [],
        })

    task = {
        "as_of": now_iso(),
        "count": len(cands),
        "who": "DSH agent（不需要 DEEPSEEK_API_KEY，agent 本身就是模型）",
        "output_file": DECISIONS_PENDING,
        "output_format": "[{\"strategy_id\": \"<strategy_id>\", \"decision\": {...}}]",
        "decision_schema": {
            "action": "open 或 skip",
            "side": "必须等于候选的 side",
            "entry": "买点，与现价偏离不超过 entry_tolerance_pct",
            "confidence": "0~1，且不低于 min_confidence",
            "size_mult": "0~1，只能缩小不能放大",
            "leverage": "≥1 且不超过 leverage_max",
            "stop_loss": "做多必须低于 entry、做空必须高于 entry；距离在 min_sl_pct ~ stop_loss_pct_max",
            "take_profit": "方向相反，距离在 min_tp_pct ~ take_profit_pct_max；盈亏比必须 ≥1",
            "reason": "不超过 200 字，必须引用快照里的具体数字",
        },
        "next_step": f"python llm_trade.py apply {DECISIONS_PENDING}",
        "candidates": cands,
    }
    _write(TASK_FILE, task)
    print(f"任务包 -> {TASK_FILE}")
    print(f"待定价 {len(cands)} 条")
    for c in cands:
        print(f"  {c['coin']} {c['side']}  @ {c['price']}  上限 {c['caps']['usd']} USDT")
    if cands:
        print(f"\n下一步：agent 写 {DECISIONS_PENDING}，然后  {task['next_step']}")
    return {"task_file": TASK_FILE, "count": len(cands)}


def show():
    pend = _read(PENDING, {})
    appr = _read(APPROVED, {})
    print(f"待复核 {len(pend.get('items') or [])} 条（{pend.get('as_of') or '无'}）")
    for it in (pend.get("items") or [])[:10]:
        print(f"  {it['coin']} {it['side']}  @ {it['price']}  {it['status']}")
    print(f"已通过 {len(appr.get('orders') or [])} 笔（{appr.get('as_of') or '无'}）")
    for o in (appr.get("orders") or [])[:10]:
        print(f"  {o['coin']} {o['side']}  {o['usd']} USDT  {o['leverage']:g}x  "
              f"止损 {o['stop_loss']:g}  止盈 {o['take_profit']:g}")
    return 0


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "prepare":
        prepare()
        return 0
    if cmd == "run":
        return run_api()
    if cmd == "apply":
        if len(argv) < 2:
            print("用法: python llm_trade.py apply <json文件>")
            return 1
        return apply_file(argv[1])
    if cmd == "task":
        build_task()
        return 0
    if cmd == "auto":
        run_auto()
        return 0
    if cmd == "show":
        return show()
    print(f"未知命令 {cmd}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
