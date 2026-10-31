# -*- coding: utf-8 -*-
"""参数扫描：找什么样的周期/预测期/标签门槛能让模型真的有钱赚。

判断标准不是 AUC，而是：
  - 排序能力 spearman(proba, net_ret)
  - top decile 做多净收益 / bottom decile 做空净收益
  - long-short spread（这个才是可交易的真信号）
所有净收益已扣往返手续费+滑点。
"""
import itertools
import warnings

import lightgbm as lgb
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.metrics import roc_auc_score

import config as C
from datafeed import load_bars
from features import build_features
from model import PARAMS, make_splits

warnings.filterwarnings("ignore")

COST = 2 * (C.TAKER_FEE + C.SLIPPAGE_BPS / 10000)  # 往返成本


def run(bars, H, edge, train_bars, test_bars, rounds=200):
    X = build_features(bars)
    c = bars["close"]
    fwd = c.shift(-H) / c - 1
    net = fwd - COST
    y = (net > edge).astype(int)

    d = X.copy()
    d["y"], d["net"] = y, net
    d = d.iloc[:-H].dropna()
    if len(d) < train_bars + test_bars + 200:
        return None
    Xd, yd, netd = d.drop(columns=["y", "net"]), d["y"], d["net"]

    splits = make_splits(len(d), train_bars, test_bars, H + 20)
    if len(splits) < 3:
        return None

    ps, ns = [], []
    for a, b, cc, dd in splits:
        m = lgb.train(PARAMS, lgb.Dataset(Xd.iloc[a:b], label=yd.iloc[a:b]), num_boost_round=rounds)
        ps.append(m.predict(Xd.iloc[cc:dd]))
        ns.append(netd.iloc[cc:dd].values)
    p = np.concatenate(ps)
    r = np.concatenate(ns)
    yy = (r > edge).astype(int)

    auc = roc_auc_score(yy, p) if len(np.unique(yy)) > 1 else np.nan
    rho = spearmanr(p, r).statistic
    q = pd.qcut(pd.Series(p).rank(method="first"), 10, labels=False)
    top = r[q == 9].mean()
    bot = r[q == 0].mean()

    return dict(H=H, edge=edge, n=len(p), auc=auc, rho=rho,
                top=top * 100, bot=bot * 100, ls=(top - bot) * 100,
                base=r.mean() * 100)


def main():
    rows = []
    for tf, train_bars, test_bars in [("1h", 4000, 800), ("4h", 2000, 600)]:
        bars = load_bars(interval=tf)
        hs = [1, 2, 4, 6, 12] if tf == "1h" else [1, 2, 3, 6]
        for H, edge in itertools.product(hs, [0.0, 0.002]):
            out = run(bars, H, edge, train_bars, test_bars)
            if out:
                out["tf"] = tf
                rows.append(out)
                print(f"{tf} H={H:>2} edge={edge:.3f} n={out['n']:>5} "
                      f"AUC={out['auc']:.4f} rho={out['rho']:+.4f} "
                      f"top={out['top']:+.3f}% bot={out['bot']:+.3f}% "
                      f"L-S={out['ls']:+.3f}% base={out['base']:+.3f}%")

    df = pd.DataFrame(rows)
    print("\n===== 按 long-short spread 排序 =====")
    print(df.sort_values("ls", ascending=False).round(4).to_string(index=False))
    C.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(C.REPORT_DIR / "sweep.csv", index=False, encoding="utf-8-sig")
    print(f"\n往返成本 = {COST*100:.3f}%  —— L-S spread 必须明显大于成本才算有信号")


if __name__ == "__main__":
    main()
