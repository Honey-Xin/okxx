"""把 patterns.py 的每套策略放到历史数据上跑一遍，看它到底赚不赚钱。

判断题先摆在这里：一套「策略」如果不能在事前定义清楚、事后复现，
那它就只是叙事。所以这个文件做三件事：

  1. 信号在 t 根收盘成立，t+1 根收盘进场（不在信号那根立刻成交，避免偷价）
  2. 出场只有三种：止损、止盈、持有满 N 根。同一根内止损与止盈都被触及时，
     按止损算（保守，不做「刚好买在最低点」的假设）
  3. 每笔都扣往返成本（默认 0.13%），并列出去掉成本前的数字

指标里最重要的是「平均每笔净收益」和「样本内下半段是否还成立」——
前者告诉你期望值，后者告诉你是不是只在某一段行情里好用。
"""

from __future__ import annotations

import argparse
import json
import os

import numpy as np
import pandas as pd

import config as C
import datafeed as DF
import patterns as P

COST_DEFAULT = 2 * (C.TAKER_FEE + C.SLIPPAGE_BPS / 10000)  # 往返成本，约 0.13%
REPORT_DIR = C.REPORT_DIR


def run(
    df: pd.DataFrame,
    pattern: dict | None = None,
    side: str = "long",
    sl_pct: float = 2.0,
    tp_pct: float = 6.0,
    max_hold: int = 24,
    cost: float = COST_DEFAULT,
    sig: pd.Series | None = None,
) -> dict:
    """单次回测。返回逐笔明细与汇总。

    sig 可以外部传进来（同一份信号切两段跑，避免重复算指标）。
    """
    if sig is None:
        sig = P.signal_series(df, pattern or {})
    sig = sig.fillna(False).astype(bool)
    close = df["close"].to_numpy(dtype=float)
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    n = len(df)
    long_side = side != "short"

    trades = []
    i = 1
    while i < n:
        if not bool(sig.iloc[i - 1]):
            i += 1
            continue

        entry_i = i
        entry = close[entry_i]
        if long_side:
            stop = entry * (1 - sl_pct / 100.0)
            take = entry * (1 + tp_pct / 100.0)
        else:
            stop = entry * (1 + sl_pct / 100.0)
            take = entry * (1 - tp_pct / 100.0)

        exit_i, exit_px, reason = None, None, None
        j = entry_i + 1
        while j < n:
            if long_side:
                if low[j] <= stop:
                    exit_i, exit_px, reason = j, stop, "stop"
                    break
                if high[j] >= take:
                    exit_i, exit_px, reason = j, take, "take"
                    break
            else:
                if high[j] >= stop:
                    exit_i, exit_px, reason = j, stop, "stop"
                    break
                if low[j] <= take:
                    exit_i, exit_px, reason = j, take, "take"
                    break
            if j - entry_i >= max_hold:
                exit_i, exit_px, reason = j, close[j], "time"
                break
            j += 1

        if exit_i is None:
            # 数据末尾还没出场，用最后一根收盘平掉
            exit_i, exit_px, reason = n - 1, close[n - 1], "eod"

        raw = (exit_px / entry - 1.0) if long_side else (1.0 - exit_px / entry)
        trades.append({
            "entry_time": str(df.index[entry_i]),
            "exit_time": str(df.index[exit_i]),
            "entry": round(entry, 6),
            "exit": round(exit_px, 6),
            "bars": int(exit_i - entry_i),
            "reason": reason,
            "gross_pct": round(raw * 100.0, 4),
            "net_pct": round((raw - cost) * 100.0, 4),
        })
        # 出场后从下一根继续找信号，避免同一个信号重复开仓
        i = exit_i + 1

    return summarize(trades, df, side, sl_pct, tp_pct, max_hold, cost)


def summarize(trades, df, side, sl_pct, tp_pct, max_hold, cost) -> dict:
    out = {
        "side": side,
        "count": len(trades),
        "sl_pct": sl_pct,
        "tp_pct": tp_pct,
        "max_hold": max_hold,
        "cost_pct": round(cost * 100.0, 3),
        "trades": trades,
    }
    if not trades:
        out.update({"win_rate": None, "avg_net": None, "total_net": None,
                    "avg_win": None, "avg_loss": None, "profit_factor": None,
                    "max_dd": None, "expectancy": None})
        return out

    net = np.array([t["net_pct"] for t in trades], dtype=float)
    wins = net[net > 0]
    losses = net[net <= 0]
    equity = np.cumprod(1.0 + net / 100.0)
    peak = np.maximum.accumulate(equity)
    dd = (equity / peak - 1.0) * 100.0

    gross_win = float(wins.sum()) if len(wins) else 0.0
    gross_loss = float(-losses.sum()) if len(losses) else 0.0

    out.update({
        "win_rate": round(len(wins) / len(net) * 100.0, 1),
        "avg_net": round(float(net.mean()), 4),
        "total_net": round(float((equity[-1] - 1.0) * 100.0), 2),
        "avg_win": round(float(wins.mean()), 4) if len(wins) else None,
        "avg_loss": round(float(losses.mean()), 4) if len(losses) else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "max_dd": round(float(dd.min()), 2),
        "expectancy": round(float(net.mean()), 4),
        "stop_hits": sum(1 for t in trades if t["reason"] == "stop"),
        "take_hits": sum(1 for t in trades if t["reason"] == "take"),
        "time_hits": sum(1 for t in trades if t["reason"] in ("time", "eod")),
    })
    return out


def template_pattern(key: str) -> dict:
    tpl = P.TEMPLATES[key]
    return {"side": tpl["side"], "mode": "all", "rules": tpl["rules"]}


def run_all(coins, timeframes, sl_pct, tp_pct, max_hold, cost, bars=6000, save=True) -> pd.DataFrame:
    """跑全部组合，并且把每份数据切成前后两半分别回测。

    为什么要切两半：一套策略如果只是赶上了某段行情，前半段好看、后半段崩掉，
    这两个数字会直接暴露出来。只看全段平均，等于把「运气」和「边际」混成一杯。
    """
    rows = []
    for coin in coins:
        for tf in timeframes:
            try:
                df = DF.load_bars(pair=coin + "_USDT", interval=tf, bars=bars, verbose=False)
            except Exception as e:
                print("跳过 %s %s: %s" % (coin, tf, e))
                continue
            if len(df) < 300:
                continue
            d = P.prepare(df)
            n = len(df)
            half = n // 2
            for key in P.TEMPLATES:
                pat = template_pattern(key)
                sig_full = P.signal_series(df, pat, d=d)
                seg = {
                    "full": run(df, side=pat["side"], sl_pct=sl_pct, tp_pct=tp_pct,
                                max_hold=max_hold, cost=cost, sig=sig_full),
                    "h1": run(df.iloc[:half], side=pat["side"], sl_pct=sl_pct, tp_pct=tp_pct,
                              max_hold=max_hold, cost=cost, sig=sig_full.iloc[:half]),
                    "h2": run(df.iloc[half:], side=pat["side"], sl_pct=sl_pct, tp_pct=tp_pct,
                              max_hold=max_hold, cost=cost, sig=sig_full.iloc[half:]),
                }
                full, h1, h2 = seg["full"], seg["h1"], seg["h2"]
                both_pos = (h1["avg_net"] is not None and h2["avg_net"] is not None
                            and h1["avg_net"] > 0 and h2["avg_net"] > 0)
                rows.append({
                    "coin": coin,
                    "tf": tf,
                    "template": key,
                    "label": P.TEMPLATES[key]["label"],
                    "side": full["side"],
                    "count": full["count"],
                    "win_rate": full["win_rate"],
                    "avg_net": full["avg_net"],
                    "total_net": full["total_net"],
                    "profit_factor": full["profit_factor"],
                    "max_dd": full["max_dd"],
                    "n_h1": h1["count"],
                    "avg_h1": h1["avg_net"],
                    "n_h2": h2["count"],
                    "avg_h2": h2["avg_net"],
                    "stable": bool(both_pos),
                    "bars_from": str(df.index[0])[:16],
                    "bars_to": str(df.index[-1])[:16],
                })
    df_out = pd.DataFrame(rows)
    if save and len(df_out):
        os.makedirs(REPORT_DIR, exist_ok=True)
        path = os.path.join(REPORT_DIR, "pattern_backtest.csv")
        df_out.to_csv(path, index=False, encoding="utf-8-sig")
        print("\n已写入 %s" % path)
    return df_out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", default="BTC")
    ap.add_argument("--tf", default="4h")
    ap.add_argument("--bars", type=int, default=3000)
    ap.add_argument("--sl", type=float, default=2.0)
    ap.add_argument("--tp", type=float, default=6.0)
    ap.add_argument("--hold", type=int, default=24)
    ap.add_argument("--cost", type=float, default=COST_DEFAULT)
    ap.add_argument("--all", action="store_true", help="跑全部币 × 全部周期 × 全部模板")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而不是表格")
    a = ap.parse_args()

    if a.all:
        coins = ["BTC", "ETH", "SOL"]
        tfs = ["1h", "4h"]
        out = run_all(coins, tfs, a.sl, a.tp, a.hold, a.cost, bars=a.bars)
        if not len(out):
            print("没有可用数据")
            return
        pd.set_option("display.width", 220)
        pd.set_option("display.max_rows", 300)
        cols = ["coin", "tf", "template", "side", "count", "win_rate", "avg_net",
                "n_h1", "avg_h1", "n_h2", "avg_h2", "stable"]

        print("\n【1】按全段每笔平均净收益排序（成本已扣，止损 %.1f%% / 止盈 %.1f%% / 最长 %d 根）"
              % (a.sl, a.tp, a.hold))
        print(out.sort_values("avg_net", ascending=False)[cols].to_string(index=False))

        print("\n【2】只保留交易次数 >= 10 的")
        enough = out[out["count"] >= 10]
        if len(enough):
            print(enough.sort_values("avg_net", ascending=False)[cols].to_string(index=False))

        print("\n【3】前后半段都为正（stable=True）的组合")
        stable = out[(out["stable"]) & (out["count"] >= 10)]
        if len(stable):
            print(stable.sort_values("avg_net", ascending=False)[cols].to_string(index=False))
        else:
            print("  一条都没有。")

        print("\n【4】按模板汇总：跨 6 个市场（3 币 × 2 周期）的平均表现")
        agg = (out.groupby(["template", "side"])
               .agg(markets=("avg_net", "size"),
                    pos_markets=("avg_net", lambda s: int((s > 0).sum())),
                    stable_markets=("stable", "sum"),
                    mean_avg_net=("avg_net", "mean"),
                    mean_h1=("avg_h1", "mean"),
                    mean_h2=("avg_h2", "mean"))
               .reset_index()
               .sort_values("mean_avg_net", ascending=False))
        print(agg.to_string(index=False, float_format=lambda v: "%.4f" % v))

        total = len(out[out["count"] >= 10])
        pos = int((out[out["count"] >= 10]["avg_net"] > 0).sum())
        print("\n交易次数 >= 10 的 %d 组里，净收益为正的 %d 组（%.0f%%）；"
              "前后半段都为正的 %d 组（%.0f%%）"
              % (total, pos, pos / max(1, total) * 100,
                 int(out["stable"].sum()), int(out["stable"].sum()) / max(1, len(out)) * 100))
        return

    df = DF.load_bars(pair=a.coin + "_USDT", interval=a.tf, bars=a.bars, verbose=False)
    results = {}
    for key in P.TEMPLATES:
        pat = template_pattern(key)
        results[key] = run(df, pat, side=pat["side"], sl_pct=a.sl, tp_pct=a.tp,
                           max_hold=a.hold, cost=a.cost)
    if a.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return

    print("%s %s  共 %d 根（%s ~ %s）  止损 %.1f%% / 止盈 %.1f%% / 最长 %d 根 / 成本 %.2f%%"
          % (a.coin, a.tf, len(df), str(df.index[0])[:16], str(df.index[-1])[:16],
             a.sl, a.tp, a.hold, a.cost * 100))
    print("")
    print("%-22s %-6s %5s %7s %9s %9s %6s %8s" %
          ("模板", "方向", "笔数", "胜率%", "每笔净%", "累计%", "盈亏比", "回撤%"))
    for key, r in sorted(results.items(), key=lambda kv: (kv[1]["avg_net"] or -999), reverse=True):
        if not r["count"]:
            print("%-22s %-6s %5d %7s %9s %9s %6s %8s" % (key, r["side"], 0, "-", "-", "-", "-", "-"))
            continue
        print("%-22s %-6s %5d %7.1f %9.4f %9.2f %6s %8.2f" %
              (key, r["side"], r["count"], r["win_rate"], r["avg_net"], r["total_net"],
               r["profit_factor"] if r["profit_factor"] else "-", r["max_dd"]))


if __name__ == "__main__":
    main()
