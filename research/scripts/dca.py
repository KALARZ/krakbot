"""Honest minimal alternative: weekly DCA into BTC with a post-only limit at the bid (maker fee), Tier 1 fees
(0.40% maker) since small weekly buys rarely reach $2.5k/30d volume on their own. Compared with lump sum.
Metrics on the portfolio value path; drawdown measured on value / invested."""
import os, sys, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from lib import load5, resample, save_json
b = resample(load5("BTCUSDT"), "1d")
ts, c = b["ts"], b["c"]
res = {}
for start in (2019, 2021):
    m = ts >= dt.datetime(start, 1, 1, tzinfo=dt.UTC).timestamp() * 1000
    t, px = ts[m], c[m]
    fee = 0.004; weekly = 100.0
    units = 0.0; invested = 0.0; vals = []; inv = []
    for i in range(len(px)):
        if i % 7 == 0:
            units += weekly * (1 - fee) / (px[i] * (1 - 0.5e-4)); invested += weekly
        vals.append(units * px[i]); inv.append(invested)
    vals = np.array(vals); inv = np.array(inv)
    ratio = vals / inv
    dd = float(-(ratio - 1).min())  # worst unrealised loss on invested capital
    lump_units = 1.0 * (1 - fee) / px[0]
    lump = lump_units * px / 1.0
    res[str(start)] = {"invested": float(inv[-1]), "value": float(vals[-1]), "multiple": float(vals[-1] / inv[-1]),
                       "worst_loss_on_invested": dd, "lump_sum_multiple": float(lump[-1]),
                       "lump_sum_max_dd": float((1 - lump / np.maximum.accumulate(lump)).max()),
                       "lump_cagr": float(lump[-1] ** (365 / len(px)) - 1),
                       "daily_sharpe_lump": float(np.diff(np.log(px)).mean() / np.diff(np.log(px)).std() * np.sqrt(365))}
    print(start, {k: round(v, 3) for k, v in res[str(start)].items()})
save_json(res, "dca.json")
