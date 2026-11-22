"""Point-in-time universe: at the start of each UTC month, the top-K symbols by trailing 90-day quote volume
(Binance USDT books, data available up to that month start only). Delisted coins drop out when their data
ends (LUNA truncated at the 2022-05-12 collapse/delisting; MATIC ends at its 2024-09 migration)."""
import os, sys, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from lib import load5, resample, MS, DATA

CANDIDATES = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "LINKUSDT", "LTCUSDT", "DOGEUSDT", "AVAXUSDT",
              "DOTUSDT", "LUNAUSDT", "EOSUSDT", "BCHUSDT", "TRXUSDT", "XLMUSDT", "ETCUSDT", "ATOMUSDT", "MATICUSDT",
              "UNIUSDT", "FILUSDT"]
CUTOFF = {"LUNAUSDT": int(dt.datetime(2022, 5, 12, tzinfo=dt.UTC).timestamp() * 1000)}

def available():
    return [s for s in CANDIDATES if os.path.exists(os.path.join(DATA, "binance", f"{s}_5m.npz"))]

def load_sym(s):
    d = load5(s)
    if s in CUTOFF:
        m = d["ts"] < CUTOFF[s]
        d = {k: v[m] for k, v in d.items()}
    return d

def month_starts(y0=2019, y1=2027):
    out = []
    for y in range(y0, y1):
        for m in range(1, 13):
            out.append(int(dt.datetime(y, m, 1, tzinfo=dt.UTC).timestamp() * 1000))
    return out

def build(K=8, syms=None):
    syms = syms or available()
    daily = {}
    for s in syms:
        b = resample(load_sym(s), "1d")
        daily[s] = (b["ts"], b["qv"])
    uni = {}
    for t in month_starts():
        vols = {}
        for s, (ts, qv) in daily.items():
            m = (ts >= t - 90 * MS["1d"]) & (ts < t)
            if m.sum() >= 60 and ts[-1] >= t - 2 * MS["1d"]:  # needs 60+ days of history and still trading
                vols[s] = qv[m].sum()
        uni[t] = sorted(vols, key=vols.get, reverse=True)[:K]
    return uni

def member(uni, s, t):
    ms = [m for m in uni if m <= t]
    return bool(ms) and s in uni[max(ms)]

if __name__ == "__main__":
    u = build()
    for t in sorted(u)[::6]:
        print(dt.datetime.fromtimestamp(t / 1000, dt.UTC).strftime("%Y-%m"), [x[:-4] for x in u[t]])
