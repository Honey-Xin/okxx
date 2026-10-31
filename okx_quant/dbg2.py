import pandas as pd, numpy as np, config as C
from datafeed import load_bars
from deriv import build_deriv_features
from features import build_features
from exp3 import oos_predict
from backtest import COST, _align_close
bars = load_bars(interval="4h")
tech = build_features(bars); der = build_deriv_features(bars,"4h",verbose=False)
start = der.dropna(thresh=der.shape[1]//2).index[0]; mask = bars.index>=start
X = pd.concat([tech[mask], der[mask]], axis=1)
H=6; net = bars["close"].shift(-H)/bars["close"] - 1 - COST
p,r = oos_predict(X, net[mask], H)
close, idx = _align_close(p, bars)
print("bars index dtype", bars.index.dtype, bars.index[0], bars.index[-1])
print("p index dtype", p.index.dtype, p.index[0], p.index[-1])
print("close len", len(close), "nan", close.isna().sum())
print("close head", close.head(3).to_dict())
print("bars close head", bars["close"].head(3).to_dict())
print("idx.equal(p.index)", idx.equals(p.index))
