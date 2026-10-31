# -*- coding: utf-8 -*-
"""DeepSeek 交易决策层探针：验证 LLM 能不能被安全地接进交易系统。

这个脚本只做一件事：把「当前市场状态」压缩成结构化快照，交给 LLM，
要求它返回严格 JSON，然后校验。跑通它，才谈得上接实盘。

核心设计约束（很重要，别改）：
  1. LLM 的输出里没有「加仓」这个选项。size_mult 上限是 1.0，
     意思是它只能在系统给定的仓位上打折扣，不能放大。
     这不是保守，是因为 LLM 的失败模式是「自信地胡说」，
     一旦能放大仓位，一次幻觉就是一次爆仓。
  2. veto 是一票否决权。规则层说要开仓，LLM 可以拦下来。
     拦错了的代价是错过机会，拦对了的代价是避开一次黑天鹅——
     这个不对称性正是 LLM 该待的位置。
  3. 输出必须过 schema 校验，不合格就重试，重试还不行就降级到纯规则。
     LLM 不可用时系统必须继续运行，不能瘫。

用法：
  $env:DEEPSEEK_API_KEY="sk-..."     # 先配 key
  python llm_probe.py
  python llm_probe.py --model deepseek-reasoner
  python llm_probe.py --offline      # 只构造 prompt 不调 API，看输入长什么样
"""
import argparse
import json
import os
import sys
import time

import pandas as pd
import requests

import config as C
from datafeed import load_bars
from features import build_features, atr, rsi

API_URL = "https://api.deepseek.com/chat/completions"

# 输出契约：字段固定，枚举值固定
SCHEMA = {
    "regime": ["trend_up", "trend_down", "range", "high_vol", "unclear"],
    "action": ["long", "short", "flat"],
    "veto": [True, False],
}


def build_snapshot(df, lookback=96):
    """把最近 lookback 根 K 线压成一段紧凑摘要。注意：不是把原始 K 线塞进 prompt。

    原因：LLM 对长数字序列的算术能力很差，200 根 K 线的 OHLC 它读不出趋势，
    还烧掉上万 token。结构化摘要（涨跌幅、波动率、位置、量能）才是它读得懂的。
    """
    f = build_features(df)
    last = f.iloc[-1]
    win = df.tail(lookback)

    def pct(n):
        if len(df) <= n:
            return None
        return round(float(df["close"].iloc[-1] / df["close"].iloc[-1 - n] - 1) * 100, 3)

    snap = {
        "symbol": C.SYMBOL_DATA,
        "timeframe": C.TIMEFRAME,
        "last_close": round(float(df["close"].iloc[-1]), 4),
        "as_of": str(df.index[-1]),
        "returns_pct": {f"{n}根": pct(n) for n in (1, 3, 6, 12, 24, 48, 96) if pct(n) is not None},
        "range_position": {
            "近24根分位": round(float((win["close"].tail(24) < df["close"].iloc[-1]).mean()) * 100, 1),
            "近96根分位": round(float((win["close"] < df["close"].iloc[-1]).mean()) * 100, 1),
        },
        "volatility": {
            "ATR占价格%": round(float(atr(df).iloc[-1] / df["close"].iloc[-1]) * 100, 3),
            "近24根实际波动%": round(float(win["close"].pct_change().tail(24).std() * 100), 3),
        },
        "oscillators": {
            "RSI14": round(float(rsi(df["close"]).iloc[-1]), 1),
            "MACD柱": round(float(last.get("macd_hist", 0)), 4) if pd.notna(last.get("macd_hist")) else None,
        },
        "volume": {
            "近24根量能比": round(float(last.get("vol_ratio_24", 0)), 3) if pd.notna(last.get("vol_ratio_24")) else None,
            "近24根成交额z": round(float(last.get("quote_z", 0)), 2) if pd.notna(last.get("quote_z")) else None,
        },
        "trend_structure": {
            "EMA12/EMA48": round(float(last.get("ema_ratio_12", 0)), 4) if pd.notna(last.get("ema_ratio_12")) else None,
            "EMA24/EMA96": round(float(last.get("ema_ratio_24", 0)), 4) if pd.notna(last.get("ema_ratio_24")) else None,
        },
    }
    return snap


SYSTEM_PROMPT = """你是一个加密货币交易系统的决策辅助模块。你的职责不是预测价格，而是识别当前市场状态、判断规则信号是否存在明显风险。

硬性要求：
1. 只输出 JSON，不要任何解释文字、不要 markdown 代码块。
2. 你必须承认不确定性。信息不足时 action 选 flat、regime 选 unclear，这不算失败。
3. size_mult 是你建议的仓位系数，范围 0.0 到 1.0。你只能缩小仓位，不能放大。
4. veto 为 true 表示你认为当前不应开仓，必须在 veto_reason 里给出具体理由。
5. 不要编造你看到的数据之外的信息。你没有联网，没有实时新闻。

输出格式（严格遵守，字段不可增删）：
{
  "regime": "trend_up|trend_down|range|high_vol|unclear",
  "confidence": 0.0,
  "action": "long|short|flat",
  "size_mult": 1.0,
  "veto": false,
  "veto_reason": "",
  "key_risks": ["最多三条"],
  "reasoning": "不超过 120 字"
}"""


def build_messages(snapshot, news=None):
    user = "当前市场快照：\n" + json.dumps(snapshot, ensure_ascii=False, indent=2)
    if news:
        user += "\n\n近期新闻标题（可能相关，也可能无关）：\n" + "\n".join(f"- {n}" for n in news)
    user += "\n\n请按系统提示的格式输出 JSON。"
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user}]


def call_deepseek(messages, model="deepseek-chat", timeout=60, retries=2):
    key = os.getenv("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("缺少环境变量 DEEPSEEK_API_KEY")
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,          # 交易决策要稳定，不要发散
        "max_tokens": 600,
        "response_format": {"type": "json_object"},   # 强制 JSON，省掉解析清洗
        "stream": False,
    }
    last_err = None
    for i in range(retries + 1):
        t0 = time.time()
        try:
            r = requests.post(API_URL, headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            }, json=payload, timeout=timeout)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()
            return data, time.time() - t0
        except Exception as e:
            last_err = e
            if i < retries:
                time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"调用失败: {last_err}")


def validate(obj):
    """严格校验。不合格就丢弃，绝不把脏数据喂给下单逻辑。"""
    errs = []
    if not isinstance(obj, dict):
        return ["返回不是 JSON 对象"]
    for k, allowed in SCHEMA.items():
        if k not in obj:
            errs.append(f"缺字段 {k}")
        elif obj[k] not in allowed:
            errs.append(f"{k}={obj[k]!r} 不在允许值 {allowed}")
    c = obj.get("confidence")
    if not isinstance(c, (int, float)) or not (0 <= c <= 1):
        errs.append(f"confidence 必须是 0~1 的数字，得到 {c!r}")
    m = obj.get("size_mult")
    if not isinstance(m, (int, float)) or not (0 <= m <= 1):
        errs.append(f"size_mult 必须是 0~1，得到 {m!r}")
    if obj.get("veto") is True and not str(obj.get("veto_reason", "")).strip():
        errs.append("veto=true 但没有给 veto_reason")
    return errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--offline", action="store_true", help="只打印 prompt，不调 API")
    ap.add_argument("--news", nargs="*", default=None, help="可选的新闻标题")
    a = ap.parse_args()

    df = load_bars()
    snap = build_snapshot(df)
    msgs = build_messages(snap, a.news)

    print("=" * 70)
    print("市场快照摘要")
    print(json.dumps(snap, ensure_ascii=False, indent=2))
    print("=" * 70)
    n_chars = sum(len(m["content"]) for m in msgs)
    print(f"prompt 总字符数 {n_chars}（粗估 {n_chars // 2} tokens）")

    if a.offline:
        print("\n--- 完整 prompt ---")
        for m in msgs:
            print(f"\n[{m['role']}]\n{m['content']}")
        return

    if not os.getenv("DEEPSEEK_API_KEY"):
        print("\n未配置 DEEPSEEK_API_KEY，无法调用。")
        print("去 https://platform.deepseek.com 申请后：")
        print('  $env:DEEPSEEK_API_KEY="sk-..."')
        print("然后重跑本脚本。")
        return

    print(f"\n调用 {a.model} ...")
    data, dt = call_deepseek(msgs, a.model)
    content = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    print(f"耗时 {dt:.2f}s   输入 {usage.get('prompt_tokens')} tokens   "
          f"输出 {usage.get('completion_tokens')} tokens   "
          f"缓存命中 {usage.get('prompt_cache_hit_tokens', 0)} tokens")

    try:
        obj = json.loads(content)
    except json.JSONDecodeError as e:
        print(f"\nJSON 解析失败: {e}\n原文:\n{content}")
        return

    print("\n--- 模型输出 ---")
    print(json.dumps(obj, ensure_ascii=False, indent=2))

    errs = validate(obj)
    print("\n--- 校验 ---")
    if errs:
        for e in errs:
            print(f"  ✗ {e}")
        print("结果：不合格，实盘中应丢弃本次输出并降级到纯规则")
    else:
        print("  ✓ 通过全部校验")
        print(f"结果：regime={obj['regime']} action={obj['action']} "
              f"仓位系数={obj['size_mult']} veto={obj['veto']}")


if __name__ == "__main__":
    main()
