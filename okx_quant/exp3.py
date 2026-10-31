# -*- coding: utf-8 -*-
"""严格验证 C 组（技术+衍生品）信号是否真实、是否稳定。

三道检验：
  1. 阈值扫描：只交易最极端的分位，看净收益怎么变
  2. 按年切分：2024 / 2025 / 2026 是否都为正（防止单一行情拟合）
  3. 预测期扫描：H=1/2/3/6，找交易频率与单笔边际的最优平衡
只有三道都过的配置才值得上实盘。
"""
import warnings

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

import config as C
from datafeed import load_bars
from deriv import build_deriv_features
from features import build_features
from model import PARAMS, make_splits

warnings.filterwarnings("ignore")

COST = 2 * (C.TAKER_FEE + C.SLIPPAGE_BPS / 10000)


def oos_predict(X, net, H, train_bars=2000, test_bars=600, rounds=300):
    y = (net > 0).astype(int)
    ok = net.notna()
    X, y, net = X[ok], y[ok], net[ok]
    splits = make_splits(len(X), train_bars, test_bars, H + 20)
    ps, ns, idx = [], [], []
    for a, b, c, d in splits:
        m = lgb.train(PARAMS, lgb.Dataset(X.iloc[a:b], label=y.iloc[a:b]), num_boost_round=rounds)
        ps.append(m.predict(X.iloc[c:d]))
        ns.append(net.iloc[c:d].values)
        idx.append(X.index[c:d])
    # 用 concat 拼，保住 UTC 时区；np.concatenate + pd.Index 会退化成 tz-naive，
    # 后续 reindex 到 tz-aware 的 K 线索引会全部变 NaN
    P = pd.concat([pd.Series(v, index=i) for v, i in zip(ps, idx)])
    N = pd.concat([pd.Series(v, index=i) for v, i in zip(ns, idx)])
    return P, N


def report(p, r, tag=""):
    """p=预测概率, r=净收益"""
    auc = roc_auc_score((r > 0).astype(int), p)
    rows = []
    for name, lo, hi in [("top2%", 0.98, 1.0), ("top5%", 0.95, 1.0),
                         ("top10%", 0.90, 1.0), ("top20%", 0.80, 1.0)]:
        q = p.rank(pct=True)
        sel = (q >= lo) & (q <= hi)
        rows.append(dict(bucket=name, n=int(sel.sum()),
                         mean_net=r[sel].mean() * 100,
                         win=(r[sel] > 0).mean() * 100))
    for name, lo, hi in [("bot5%", 0.0, 0.05), ("bot10%", 0.0, 0.10)]:
        q = p.rank(pct=True)
        sel = (q >= lo) & (q <= hi)
        rows.append(dict(bucket=name, n=int(sel.sum()),
                         mean_net=-r[sel].mean() * 100,   # 做空，收益取反
                         win=(r[sel] < 0).mean() * 100))
    d = pd.DataFrame(rows)
    print(f"\n{tag}  AUC={auc:.4f}")
    print(d.round(3).to_string(index=False))
    return auc, d


def main():
    bars = load_bars(interval="4h")
    tech = build_features(bars)
    der = build_deriv_features(bars, "4h", verbose=False)
    start = der.dropna(thresh=der.shape[1] // 2).index[0]
    mask = bars.index >= start
    X = pd.concat([tech[mask], der[mask]], axis=1)
    print(f"组合特征 {X.shape[1]} 个, 样本区间 {X.index[0]} ~ {X.index[-1]} ({len(X)} 根)\n")

    # ---- 检验 1+2: H=6, 阈值扫描 + 按年拆分 ----
    H = 6
    net = bars["close"].shift(-H) / bars["close"] - 1 - COST
    p, r = oos_predict(X, net[mask], H)
    auc, tab = report(p, r, "H=6 (24小时)")

    print("\n按年稳定性（top10% 做多 / bot10% 做空）:")
    yr_rows = []
    for yr in sorted(p.index.year.unique()):
        s = p.index.year == yr
        pp, rr = p[s], r[s]
        if len(pp) < 100:
            continue
        q = pp.rank(pct=True)
        top = rr[q >= 0.90]
        bot = rr[q <= 0.10]
        yr_rows.append(dict(year=int(yr), n=len(pp),
                            top=top.mean() * 100, top_n=len(top),
                            bot=-bot.mean() * 100, bot_n=len(bot),
                            all=r[s].mean() * 100))
    print(pd.DataFrame(yr_rows).round(3).to_string(index=False))

    # ---- 检验 3: 预测期扫描 ----
    print("\n预测期扫描 (top10% 做多净收益 / 交易次数):")
    for H2 in (1, 2, 3, 6):
        net2 = bars["close"].shift(-H2) / bars["close"] - 1 - COST
        p2, r2 = oos_predict(X, net2[mask], H2)
        q = p2.rank(pct=True)
        top = r2[q >= 0.90]
        bot = -r2[q <= 0.10]
        print(f"  H={H2} ({H2*4}h 持仓)  top10% {top.mean()*100:+.3f}% (n={len(top)})  "
              f"bot10% {bot.mean()*100:+.3f}%  L-S {(top.mean()-(-bot.mean()))*100:+.3f}%  "
              f"年化机会 {len(top)/len(p2)*365*24/4/(H2*4)*100:.0f} 次当量")

    C.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    tab.to_csv(C.REPORT_DIR / "threshold_scan.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(yr_rows).to_csv(C.REPORT_DIR / "by_year.csv", index=False, encoding="utf-8-sig")


if __name__ == "__main__":
    main()
