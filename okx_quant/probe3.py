import pandas as pd, numpy as np, config as C
fu = pd.read_parquet(C.DATA_DIR/"funding_BTC_USDT.parquet")
print("funding 行数", len(fu), fu.index[0], fu.index[-1])
print("每期均值 %.6f  中位数 %.6f  年化(3次/天) %.2f%%" % (fu.funding.mean(), fu.funding.median(), fu.funding.mean()*3*365*100))
print("正值占比 %.3f" % (fu.funding>0).mean())
yr = fu.groupby(fu.index.year).agg(n=("funding","size"), mean=("funding","mean"), pos=("funding", lambda s:(s>0).mean()))
yr["年化%"] = yr["mean"]*3*365*100
print(yr.round(4).to_string())
# 现货 vs 合约基差
b = pd.read_parquet(C.DATA_DIR/"BTC_USDT_4h.parquet")
st = pd.read_parquet(C.DATA_DIR/"stats_BTC_USDT_4h.parquet")
m = st["mark_price"].reindex(b.index.union(st.index)).ffill().reindex(b.index)
prem = (m/b["close"]-1).dropna()
print("\n基差: 均值 %.5f%% 标准差 %.5f%% 最大 %.4f%% 最小 %.4f%%" % (prem.mean()*100, prem.std()*100, prem.max()*100, prem.min()*100))
