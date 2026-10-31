# -*- coding: utf-8 -*-
"""LightGBM + 滚动 walk-forward 验证。

为什么必须 walk-forward：金融序列不能随机切分。随机切分会让同一天的
相邻样本同时出现在训练和测试里，模型只是记住了那段行情，AUC 虚高。
这里用 rolling window + embargo（训练结束到测试开始之间空出 embargo 根
K 线），保证标签窗口不与测试集重叠。

产出：
  models/lgb_wf.pkl     最后一折模型（用于实盘预测）
  models/oos.parquet    全部折拼起来的 out-of-sample 预测（用于回测）
  reports/model_report.txt
"""
import json
import pickle

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import log_loss, roc_auc_score

import config as C
from datafeed import load_bars
from features import make_dataset

PARAMS = dict(
    objective="binary",
    metric="auc",
    learning_rate=0.03,
    num_leaves=15,
    max_depth=4,
    min_child_samples=150,
    feature_fraction=0.7,
    bagging_fraction=0.8,
    bagging_freq=1,
    lambda_l2=5.0,
    verbosity=-1,
    num_threads=4,
    seed=42,
)
N_ROUNDS = 400


def make_splits(n, train_bars, test_bars, embargo):
    """滚动窗口切分，返回 [(tr_start, tr_end, te_start, te_end)]。"""
    out = []
    te_start = train_bars
    while te_start < n:
        te_end = min(te_start + test_bars, n)
        tr_end = te_start - embargo
        tr_start = max(0, tr_end - train_bars)
        if tr_end - tr_start < 500:
            break
        out.append((tr_start, tr_end, te_start, te_end))
        te_start = te_end
    return out


def train_one(Xtr, ytr, Xte):
    dtr = lgb.Dataset(Xtr, label=ytr)
    booster = lgb.train(PARAMS, dtr, num_boost_round=N_ROUNDS)
    return booster, booster.predict(Xte)


def main():
    bars = load_bars()
    X, y, fwd, net = make_dataset(bars)
    print(f"样本 {len(X)}, 特征 {X.shape[1]}, 正样本率 {y.mean():.4f}")

    embargo = C.HORIZON + 20
    splits = make_splits(len(X), C.TRAIN_BARS, C.TEST_BARS, embargo)
    print(f"walk-forward 折数 {len(splits)}  (train_window={C.TRAIN_BARS}, test={C.TEST_BARS}, embargo={embargo})")
    if not splits:
        raise SystemExit("样本不足以做 walk-forward，调小 TRAIN_BARS/TEST_BARS")

    oos = []
    rows = []
    last_booster = None
    for k, (a, b, c, d) in enumerate(splits, 1):
        Xtr, ytr = X.iloc[a:b], y.iloc[a:b]
        Xte = X.iloc[c:d]
        booster, p = train_one(Xtr, ytr, Xte)
        yte = y.iloc[c:d].values
        auc = roc_auc_score(yte, p) if len(np.unique(yte)) > 1 else np.nan
        ll = log_loss(yte, np.clip(p, 1e-6, 1 - 1e-6))
        rows.append(dict(fold=k, tr=(a, b), te=(c, d),
                         auc=auc, logloss=ll, base=float(yte.mean()), n=len(yte),
                         p_mean=float(np.mean(p))))
        print(f"  fold{k}: train[{a}:{b}] test[{c}:{d}] n={len(yte)} "
              f"AUC={auc:.4f} logloss={ll:.4f} base={yte.mean():.3f} p̄={np.mean(p):.3f}")
        oos.append(pd.DataFrame({"proba": p, "y": yte,
                                 "fwd_ret": fwd.iloc[c:d].values,
                                 "net_ret": net.iloc[c:d].values}, index=X.index[c:d]))
        last_booster = booster

    O = pd.concat(oos).sort_index()
    O = O[~O.index.duplicated(keep="last")]
    auc_all = roc_auc_score(O["y"], O["proba"])
    print(f"\n合并 OOS: {len(O)} 行, AUC={auc_all:.4f}")

    # ---- 决定性检验：按预测概率分组的实际净收益 ----
    O = O.sort_values("proba")
    O["decile"] = pd.qcut(O["proba"].rank(method="first"), 10, labels=False) + 1
    g = O.groupby("decile").agg(
        n=("net_ret", "size"),
        p_lo=("proba", "min"),
        p_hi=("proba", "max"),
        mean_net=("net_ret", "mean"),
        win=("net_ret", lambda s: (s > 0).mean()),
    )
    g["mean_net"] = g["mean_net"] * 100
    g["win"] = g["win"] * 100
    print("\n按预测概率十分位分组的真实净收益(%, 已扣手续费+滑点):")
    print(g.round(3).to_string())

    top = O[O["proba"] >= C.PROBA_ENTRY]
    print(f"\n阈值 proba>={C.PROBA_ENTRY}: 触发 {len(top)} 次 "
          f"({len(top)/len(O)*100:.1f}% 的时间), 平均净收益 {top['net_ret'].mean()*100:.3f}%, "
          f"胜率 {(top['net_ret']>0).mean()*100:.1f}%")

    # ---- 保存 ----
    C.MODEL_DIR.mkdir(parents=True, exist_ok=True)
    C.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    with open(C.MODEL_DIR / "lgb_wf.pkl", "wb") as f:
        pickle.dump({"booster": last_booster, "features": list(X.columns),
                     "params": PARAMS, "n_rounds": N_ROUNDS}, f)
    O.to_parquet(C.MODEL_DIR / "oos.parquet")

    # 特征重要性
    imp = pd.Series(last_booster.feature_importance("gain"), index=X.columns).sort_values(ascending=False)
    print("\nTop15 特征(gain):")
    print(imp.head(15).round(1).to_string())

    lines = [
        f"样本 {len(X)} / 特征 {X.shape[1]} / 折数 {len(splits)}",
        f"合并 OOS AUC {auc_all:.4f}",
        "",
        "各折:",
        *[f"  fold{r['fold']} AUC={r['auc']:.4f} base={r['base']:.3f}" for r in rows],
        "",
        "十分位净收益(%):",
        g.round(3).to_string(),
        "",
        f"阈值 {C.PROBA_ENTRY} 触发 {len(top)} 次, 平均净收益 {top['net_ret'].mean()*100:.3f}%, 胜率 {(top['net_ret']>0).mean()*100:.1f}%",
        "",
        "Top20 特征:",
        imp.head(20).round(1).to_string(),
    ]
    (C.REPORT_DIR / "model_report.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n已保存 {C.MODEL_DIR/'lgb_wf.pkl'} 与 {C.MODEL_DIR/'oos.parquet'}")


if __name__ == "__main__":
    main()
