import pandas as pd, numpy as np, config as C
from datafeed import load_bars
from deriv import build_deriv_features
from features import build_features
from exp3 import oos_predict
from backtest import COST
bars = load_bars(interval="4h")
tech = build_features(bars); der = build_deriv_features(bars,"4h",verbose=False)
start = der.dropna(thresh=der.shape[1]//2).index[0]; mask = bars.index>=start
X = pd.concat([tech[mask], der[mask]], axis=1)
H=6; net = bars["close"].shift(-H)/bars["close"] - 1 - COST
p,r = oos_predict(X, net[mask], H)
print("p type", type(p.index), len(p), "nan", p.isna().sum(), "min/max", p.min(), p.max())
rk = p.rolling(500, min_periods=200).rank(pct=True).shift(1)
print("rank nan:", rk.isna().sum(), "of", len(rk))
print(rk.describe())
print(">=0.9 次数", (rk>=0.9).sum(), "  <=0.1 次数", (rk<=0.1).sum())
