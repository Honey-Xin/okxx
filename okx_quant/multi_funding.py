# -*- coding: utf-8 -*-
"""多币种资金费率扫描 + 组合回测。

单币 BTC 的资金费率年化只有 4.65%（1x），但不同币种的资金费率差异巨大。
中性套利的正确做法是：在流动性足够的主流币里，选资金费率最高的几个分散持有，
既提高收益又降低单币风险。

funding_interval 不同（3600/14400/28800 秒），年化必须按各自间隔折算：
  年化 = 每期费率 × (365*24*3600 / interval)
"""
import time

import numpy as np
import pandas as pd

import config as C
from datafeed import load_bars
from deriv import fetch_funding

MAJORS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ADA", "AVAX", "LINK",
          "TON", "SUI", "LTC", "DOT", "TRX", "APT", "ARB", "OP", "PEPE",
          "SHIB", "WIF", "NEAR", "ATOM", "UNI", "FIL", "ETC", "AAVE", "INJ"]

# Gate 的 funding_interval（当前值，历史可能变化，用主力值折算）
INTERVAL = {"BTC": 28800, "ETH": 28800, "SOL": 28800, "XRP": 28800, "DOGE": 28800,
            "BNB": 28800, "ADA": 28800, "AVAX": 28800, "LINK": 28800, "TON": 28800,
            "SUI": 28800, "LTC": 28800, "DOT": 28800, "TRX": 28800, "APT": 28800,
            "ARB": 28800, "OP": 28800, "PEPE": 28800, "SHIB": 28800, "WIF": 28800,
            "NEAR": 28800, "ATOM": 28800, "UNI": 28800, "FIL": 28800, "ETC": 28800,
            "AAVE": 28800, "INJ": 28800}


def collect(coins=None, pages=40, refresh=False):
    coins = coins or MAJORS
    out = {}
    for c in coins:
        sym = f"{c}_USDT"
        try:
            df = fetch_funding(contract=sym, pages=pages, force=refresh)
            if len(df) < 500:
                print(f"  {sym}: 数据太少 ({len(df)})，跳过")
                continue
            out[c] = df["funding"]
        except Exception as e:
            print(f"  {sym}: 失败 {type(e).__name__} {e}")
        time.sleep(0.1)
    return out


def annualized(s, interval_sec):
    """把每期费率序列折成年化百分比。"""
    per_year = 365 * 24 * 3600 / interval_sec
    return s.mean() * per_year * 100


def main():
    print("== 抓取主流币资金费率历史 ==")
    data = collect(pages=40)
    print(f"\n成功 {len(data)} 个币")

    rows = []
    for c, s in data.items():
        iv = INTERVAL.get(c, 28800)
        rows.append(dict(coin=c, n=len(s), mean=s.mean(),
                         annual=annualized(s, iv),
                         pos=(s > 0).mean() * 100,
                         start=str(s.index[0].date()), end=str(s.index[-1].date())))
    df = pd.DataFrame(rows).sort_values("annual", ascending=False)
    print("\n== 资金费率年化排名（不扣成本，1x）==")
    print(df.round(3).to_string(index=False))

    # ---- 组合回测：等权持有多个币的中性头寸 ----
    # 统一到日频求和，避免不同 funding 间隔的对齐问题
    daily = {}
    for c, s in data.items():
        daily[c] = s.resample("1D").sum()
    D = pd.DataFrame(daily)
    # 对齐到所有币都有数据的起点，否则早期区间其他币全 NaN 会被误剔除
    start = max(s.index[0] for s in daily.values())
    D = D.loc[start:]
    D = D.loc[:, D.notna().mean() > 0.9]

    print(f"\n组合可用币种 {D.shape[1]} 个, 区间 {D.index[0].date()} ~ {D.index[-1].date()}")
    # 排名只在组合区间内重算，保证选币不偷看未来
    rank = []
    for c in D.columns:
        s = D[c]
        rank.append(dict(coin=c, annual=s.mean() * 365 * 100, pos=(s > 0).mean() * 100,
                         vol=s.std() * np.sqrt(365) * 100))
    rk = pd.DataFrame(rank).sort_values("annual", ascending=False)
    print("\n== 组合区间内的年化排名（1x）==")
    print(rk.round(3).to_string(index=False))

    def perf(port, lev=1.0):
        r = port * lev
        eq = C.INIT_EQUITY * (1 + r).cumprod()
        total = eq.iloc[-1] / eq.iloc[0] - 1
        years = len(eq) / 365
        ann = (1 + total) ** (1 / years) - 1
        vol = r.std() * np.sqrt(365)
        return dict(总收益=total * 100, 年化=ann * 100, 年化波动=vol * 100,
                    夏普=(r.mean() * 365) / vol if vol > 0 else np.nan,
                    最大回撤=((eq / eq.cummax() - 1).min()) * 100)

    print("\n== 中性套利组合（每日 funding 收入，delta 中性）==")
    res = []
    for name, sel in [("全部币等权", D.columns.tolist()),
                      ("Top5 年化", rk.head(5)["coin"].tolist()),
                      ("Top10 年化", rk.head(10)["coin"].tolist()),
                      ("中位10（剔除极端）", rk.iloc[5:15]["coin"].tolist())]:
        sel = [c for c in sel if c in D.columns]
        if not sel:
            continue
        port = D[sel].mean(axis=1)
        for lev in (1, 2):
            m = perf(port, lev)
            m["组合"] = f"{name} {lev}x"
            m["币数"] = len(sel)
            res.append(m)
            print(f"{name} {lev}x: 年化 {m['年化']:+.2f}%  波动 {m['年化波动']:.2f}%  "
                  f"夏普 {m['夏普']:.2f}  最大回撤 {m['最大回撤']:.2f}%")

    out = pd.DataFrame(res)[["组合", "币数", "总收益", "年化", "年化波动", "夏普", "最大回撤"]]
    C.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(C.REPORT_DIR / "funding_rank.csv", index=False, encoding="utf-8-sig")
    out.to_csv(C.REPORT_DIR / "funding_portfolio.csv", index=False, encoding="utf-8-sig")
    print(f"\n已保存 {C.REPORT_DIR/'funding_rank.csv'} 与 funding_portfolio.csv")


if __name__ == "__main__":
    main()
