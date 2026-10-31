# -*- coding: utf-8 -*-
"""A/B 对比：纯技术特征 vs 纯衍生品特征 vs 全部特征。

决定性检验：哪一组能产出单调的十分位净收益 + 足够大的 long-short spread。
LightGBM 原生支持 NaN，所以衍生品特征缺失不需要填补（缺失本身就带信息：
2023-10 之前没有合约统计）。
"""
import warnings

import lightgbm as lgb
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.metrics import roc_auc_score

import config as C
from datafeed import load_bars
from deriv import build_deriv_features
from features import build_features
from model import PARAMS, make_splits

warnings.filterwarnings("ignore")

COST = 2 * (C.TAKER_FEE + C.SLIPPAGE_BPS / 10000)


def evaluate(X, net, H, train_bars, test_bars, rounds=300, label=""):
    y = (net > 0).astype(int)
    ok = net.notna()
    X, y, net = X[ok], y[ok], net[ok]

    splits = make_splits(len(X), train_bars, test_bars, H + 20)
    if len(splits) < 3:
        print(f"{label}: 折数不足 ({len(splits)})")
        return None

    ps, ns = [], []
    for a, b, c, d in splits:
        m = lgb.train(PARAMS, lgb.Dataset(X.iloc[a:b], label=y.iloc[a:b]), num_boost_round=rounds)
        ps.append(m.predict(X.iloc[c:d]))
        ns.append(net.iloc[c:d].values)
    p = np.concatenate(ps)
    r = np.concatenate(ns)

    auc = roc_auc_score((r > 0).astype(int), p)
    rho = spearmanr(p, r).statistic
    q = pd.qcut(pd.Series(p).rank(method="first"), 10, labels=False).values
    top, bot = r[q == 9].mean(), r[q == 0].mean()

    # 五分位单调性
    q5 = pd.qcut(pd.Series(p).rank(method="first"), 5, labels=False).values
    q5m = [r[q5 == i].mean() * 100 for i in range(5)]

    print(f"{label:<22} n={len(p):>5} AUC={auc:.4f} rho={rho:+.4f} "
          f"top={top*100:+.3f}% bot={bot*100:+.3f}% L-S={(top-bot)*100:+.3f}%")
    print(f"{'':<22} 五分位净收益: " + " ".join(f"{v:+.3f}" for v in q5m))
    return dict(label=label, n=len(p), auc=auc, rho=rho,
                top=top * 100, bot=bot * 100, ls=(top - bot) * 100)


def main():
    bars = load_bars(interval="4h")
    print(f"K线 {len(bars)} 根 {bars.index[0]} ~ {bars.index[-1]}")

    tech = build_features(bars)
    der = build_deriv_features(bars, "4h", verbose=False)
    print(f"技术特征 {tech.shape[1]} 个 / 衍生品特征 {der.shape[1]} 个")

    H = 6  # 4h * 6 = 24 小时
    net = bars["close"].shift(-H) / bars["close"] - 1 - COST

    # 只保留衍生品数据可用的区间，保证三组样本完全一致
    start = der.dropna(thresh=der.shape[1] // 2).index[0]
    mask = bars.index >= start
    print(f"对齐区间: {start} 起, {mask.sum()} 根\n")

    sets = {
        "A 纯技术": tech[mask],
        "B 纯衍生品": der[mask],
        "C 技术+衍生品": pd.concat([tech[mask], der[mask]], axis=1),
    }

    rows = []
    for name, X in sets.items():
        out = evaluate(X, net[mask], H, 2000, 600, label=name)
        if out:
            rows.append(out)

    df = pd.DataFrame(rows)
    print("\n===== 汇总 =====")
    print(df.round(4).to_string(index=False))
    C.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(C.REPORT_DIR / "ab_test.csv", index=False, encoding="utf-8-sig")
    print(f"\n往返成本 {COST*100:.3f}% —— L-S 必须显著大于它")


if __name__ == "__main__":
    main()
