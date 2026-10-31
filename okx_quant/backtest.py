# -*- coding: utf-8 -*-
"""回测引擎：把 OOS 预测变成真实资金曲线。

关键防未来函数设计：
  - 阈值用「过去 500 根的滚动分位」再 shift(1)，绝不用全样本分位
  - 信号在 t 根收盘产生，按 t 根收盘价成交
  - 收益按 t -> t+H 计算，成本按往返扣

两个策略并排回测：
  S1 方向策略   — 极端分位入场，H 根后平仓
  S2 资金费率   — 现货多 + 永续空，delta 中性收 funding

S2 的正确回测方式：funding 是 8 小时结算一次（UTC 00/08/16），4h 网格
包含这些时点，所以可以精确 reindex。策略是「持续持有不择时」，成本只在
建仓/平仓各扣一次。把 funding 转负当成平仓信号会导致几千次来回切换，
手续费直接吃光 5% 的年化——那是实现错误，不是策略特征。
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import config as C
from datafeed import load_bars
from deriv import build_deriv_features, fetch_funding
from features import build_features
from exp3 import oos_predict

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

COST = 2 * (C.TAKER_FEE + C.SLIPPAGE_BPS / 10000)
FREQ = 365 * 24 / 4          # 每年 4h 根数


def metrics(eq, freq_per_year=FREQ):
    eq = pd.Series(eq).dropna()
    if len(eq) < 3:
        return {}
    ret = eq.pct_change().dropna()
    ret = ret[np.isfinite(ret)]
    total = eq.iloc[-1] / eq.iloc[0] - 1
    years = len(eq) / freq_per_year
    ann = (1 + total) ** (1 / years) - 1 if years > 0 and total > -1 else np.nan
    vol = ret.std() * np.sqrt(freq_per_year)
    sharpe = (ret.mean() * freq_per_year) / vol if vol > 0 else np.nan
    dd = (eq / eq.cummax() - 1).min()
    return dict(总收益=total * 100, 年化=ann * 100, 年化波动=vol * 100,
                夏普=sharpe, 最大回撤=dd * 100, 期末资金=eq.iloc[-1])


def _align_close(p, bars):
    """把 K 线收盘价严格对齐到预测索引上（自动处理时区）。"""
    idx = pd.DatetimeIndex(p.index)
    if bars.index.tz is not None and idx.tz is None:
        idx = idx.tz_localize(bars.index.tz)
    elif bars.index.tz is None and idx.tz is not None:
        idx = idx.tz_convert(None)
    return bars["close"].reindex(idx), idx


def backtest_direction(p, bars, H=6, entry_q=0.90, short_q=0.10, allow_short=False,
                       init=None):
    """p=预测概率（DatetimeIndex）, bars=K线。极端分位入场，持有 H 根。"""
    init = init or C.INIT_EQUITY
    close, idx = _align_close(p, bars)

    # 滚动分位（用过去 500 根，shift(1) 保证只用历史）
    rank = p.rolling(500, min_periods=200).rank(pct=True).shift(1)

    pos, entry_i, direction = 0, None, 0
    eq = init
    equity, trades = [], []
    i = 0
    n = len(idx)
    while i < n:
        if pos == 0:
            q = rank.iloc[i]
            if pd.notna(q) and q >= entry_q:
                pos, entry_i, direction = 1, i, 1
            elif allow_short and pd.notna(q) and q <= short_q:
                pos, entry_i, direction = 1, i, -1

        if pos == 1 and close.iloc[i] is not np.nan:
            # 到点或已到末尾就强制平仓
            done = (i - entry_i >= H) or (i == n - 1)
            if done:
                e0, e1 = close.iloc[entry_i], close.iloc[i]
                if pd.notna(e0) and pd.notna(e1) and e0 > 0:
                    gross = direction * (e1 / e0 - 1)
                    ret = gross - COST
                    eq *= (1 + ret)
                    trades.append(dict(entry=idx[entry_i], exit=idx[i], dir=direction,
                                       gross=gross * 100, ret=ret * 100, equity=eq))
                pos, entry_i, direction = 0, None, 0
        equity.append(eq)
        i += 1

    return pd.Series(equity, index=idx[:len(equity)]), pd.DataFrame(trades)


def backtest_funding(bars, funding, leverage=1.0, init=None, stop_on_negative=False,
                     neg_threshold=-5e-5):
    """S2 资金费率中性：现货多 + 永续空。

    收益 = 每 8 小时收到的 funding × 杠杆（funding>0 时空头收钱）
    成本 = 建仓 + 平仓各一次，两条腿各 0.05% + 滑点
    stop_on_negative=True 时，funding 低于 neg_threshold 才临时撤出（带阈值滞回，
    避免在 0 附近来回摩擦）。
    """
    init = init or C.INIT_EQUITY
    idx = bars.index
    f = funding.reindex(idx.union(funding.index)).ffill().reindex(idx)
    f_on_grid = funding.reindex(idx)                     # 只在结算时点有值
    settle = f_on_grid.fillna(0.0)

    if stop_on_negative:
        # 滞回状态机：funding < 阈值 撤出，funding > 0 重新进入
        state, states = 1, []
        for v in f.fillna(0):
            if state == 1 and v < neg_threshold:
                state = 0
            elif state == 0 and v > 0:
                state = 1
            states.append(state)
        s = pd.Series(states, index=idx)
        earned = settle * s
        switch = s.diff().abs().fillna(0)
    else:
        s = pd.Series(1.0, index=idx)
        earned = settle
        switch = pd.Series(0.0, index=idx)

    cost = switch * COST * 2                             # 两条腿进出
    port_ret = earned * leverage * s - cost
    if not stop_on_negative:
        port_ret.iloc[0] -= COST * 2                     # 建仓一次
        port_ret.iloc[-1] -= COST * 2                    # 平仓一次
    eq = init * (1 + port_ret).cumprod()
    return eq, port_ret, s


def main():
    bars = load_bars(interval="4h")
    tech = build_features(bars)
    der = build_deriv_features(bars, "4h", verbose=False)
    start = der.dropna(thresh=der.shape[1] // 2).index[0]
    mask = bars.index >= start
    X = pd.concat([tech[mask], der[mask]], axis=1)

    H = 6
    net = bars["close"].shift(-H) / bars["close"] - 1 - COST
    p, r = oos_predict(X, net[mask], H)
    print(f"OOS 预测 {len(p)} 根, {p.index[0]} ~ {p.index[-1]}")

    rows = []
    # ---- S1 方向策略 ----
    print("\n===== S1 方向策略（4h / 24h 持仓 / 极端分位）=====")
    for tag, short, eq_th in [("只做多", False, 0.90), ("多空都做", True, 0.90),
                              ("只做多 top5%", False, 0.95)]:
        eqs, tr = backtest_direction(p, bars, H=H, allow_short=short, entry_q=eq_th)
        m = metrics(eqs)
        m["策略"] = f"S1 {tag}"
        m["交易次数"] = len(tr)
        m["胜率"] = (tr["ret"] > 0).mean() * 100 if len(tr) else np.nan
        m["单笔均值%"] = tr["ret"].mean() if len(tr) else np.nan
        rows.append(m)
        print(f"\n{tag}: {len(tr)} 笔, 胜率 {m['胜率']:.1f}%, 单笔均值 {m['单笔均值%']:+.3f}%")
        print(f"   总收益 {m['总收益']:+.2f}%  年化 {m['年化']:+.2f}%  "
              f"夏普 {m['夏普']:.2f}  最大回撤 {m['最大回撤']:.2f}%")

    # ---- S2 资金费率策略 ----
    print("\n===== S2 资金费率中性（现货多 + 永续空）=====")
    fu = fetch_funding()
    eqf_best = None
    for lev in (1, 2, 3):
        eqf, pr, s = backtest_funding(bars, fu["funding"], leverage=lev)
        m = metrics(eqf)
        m["策略"] = f"S2 资金费率 {lev}x"
        m["交易次数"] = 1
        m["胜率"] = np.nan
        m["单笔均值%"] = np.nan
        rows.append(m)
        print(f"{lev}x: 总收益 {m['总收益']:+.2f}%  年化 {m['年化']:+.2f}%  "
              f"波动 {m['年化波动']:.2f}%  夏普 {m['夏普']:.2f}  最大回撤 {m['最大回撤']:.2f}%")
        if lev == 2:
            eqf_best = eqf

    # 带择时的版本
    eqf2, pr2, s2 = backtest_funding(bars, fu["funding"], leverage=2, stop_on_negative=True)
    m = metrics(eqf2)
    m.update({"策略": "S2 资金费率 2x+择时", "交易次数": int(s2.diff().abs().fillna(0).sum()),
              "胜率": np.nan, "单笔均值%": np.nan})
    rows.append(m)
    print(f"2x+择时: 总收益 {m['总收益']:+.2f}%  年化 {m['年化']:+.2f}%  夏普 {m['夏普']:.2f}  "
          f"换手 {m['交易次数']} 次  最大回撤 {m['最大回撤']:.2f}%")

    df = pd.DataFrame(rows)[["策略", "总收益", "年化", "年化波动", "夏普", "最大回撤",
                             "期末资金", "交易次数", "胜率", "单笔均值%"]]
    print("\n===== 汇总 =====")
    print(df.round(2).to_string(index=False))
    C.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(C.REPORT_DIR / "backtest.csv", index=False, encoding="utf-8-sig")

    # ---- 画图 ----
    fig, axes = plt.subplots(2, 1, figsize=(13, 9))
    eq_long, _ = backtest_direction(p, bars, H=H, allow_short=False)
    eq_both, _ = backtest_direction(p, bars, H=H, allow_short=True)
    bh_idx = pd.DatetimeIndex(eq_long.index)
    c0 = bars["close"].reindex(bh_idx)
    bh = C.INIT_EQUITY * (c0 / c0.iloc[0])          # 归一化到 10000，才能对比

    # 多币种组合（最终策略）
    from multi_funding import collect
    data = collect(pages=40)
    daily = {c: s.resample("1D").sum() for c, s in data.items()}
    D = pd.DataFrame(daily)
    st = max(s.index[0] for s in daily.values())
    D = D.loc[st:]
    D = D.loc[:, D.notna().mean() > 0.9]
    rk = D.mean().sort_values(ascending=False)
    sel = [c for c in D.columns if c in set(rk.head(10).index)]
    port_multi = D[sel].mean(axis=1)
    eqm = C.INIT_EQUITY * (1 + port_multi * 2).cumprod()

    ax = axes[0]
    ax.plot(eq_long.index, eq_long, label="S1 方向 只做多", lw=1.2, alpha=.85)
    ax.plot(eq_both.index, eq_both, label="S1 方向 多空", lw=1.2, alpha=.85)
    ax.plot(eqf_best.index, eqf_best, label="S2 资金费率 BTC 2x", lw=1.5)
    ax.plot(eqm.index, eqm, label="S2 资金费率 Top10 币 2x", lw=2.0, color="crimson")
    ax.plot(bh.index, bh, label="买入持有 BTC", lw=1.0, ls="--", color="gray")
    ax.set_title("净值曲线（初始 10000 USDT，log 刻度）")
    ax.legend(loc="lower left"); ax.grid(alpha=.3); ax.set_yscale("log")

    ax = axes[1]
    for e, lb in [(eq_long, "S1 方向 只做多"), (eq_both, "S1 方向 多空"),
                  (eqf_best, "S2 BTC 2x"), (eqm, "S2 Top10 币 2x")]:
        ax.plot(e.index, (e / e.cummax() - 1) * 100, label=lb, lw=1.2)
    ax.set_title("回撤 (%)"); ax.legend(); ax.grid(alpha=.3)
    plt.tight_layout()
    out = C.REPORT_DIR / "equity.png"
    plt.savefig(out, dpi=130)
    print(f"\n图已保存 {out}")


if __name__ == "__main__":
    main()
