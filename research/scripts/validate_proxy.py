"""Validate Binance USDT klines as a price proxy for Kraken books: return correlation, beta, range ratio
(high-low size matters for stop/limit fill simulation) on the overlapping windows of Kraken's 720 candles."""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from lib import load5, resample, DATA, save_json, MS
MAP = {"XBTUSD": "BTCUSDT", "XBTEUR": "BTCUSDT", "ETHUSD": "ETHUSDT", "ETHEUR": "ETHUSDT", "SOLUSD": "SOLUSDT",
       "SOLEUR": "SOLUSDT", "XRPUSD": "XRPUSDT", "XRPEUR": "XRPUSDT", "ADAEUR": "ADAUSDT", "LINKEUR": "LINKUSDT",
       "LTCEUR": "LTCUSDT", "XDGEUR": "DOGEUSDT", "AVAXEUR": "AVAXUSDT", "DOTEUR": "DOTUSDT"}
TF = {60: "1h", 240: "4h", 1440: "1d"}
out = {}
print("| Kraken | Binance | TF | n | corr rend. | beta | range H-L Kraken/Binance (mediana) | vol Kraken/Binance (quota volume) |")
print("|---|---|---|---|---|---|---|---|")
cache = {}
for kp, bp in MAP.items():
    if bp not in cache:
        cache[bp] = load5(bp)
    for iv, tf in TF.items():
        f = os.path.join(DATA, "kraken", f"ohlc_{kp}_{iv}.json")
        k = np.array(json.load(open(f)), dtype=float)[:-1]  # drop the open (uncommitted) candle
        kts = (k[:, 0] * 1000).astype(np.int64)
        b = resample(cache[bp], tf)
        common, ik, ib = np.intersect1d(kts, b["ts"], return_indices=True)
        if len(common) < 30:
            continue
        kc, bc = k[ik, 4], b["c"][ib]
        kr, br = np.diff(np.log(kc)), np.diff(np.log(bc))
        ok = np.diff(common) == MS[tf]
        kr, br = kr[ok], br[ok]
        corr = float(np.corrcoef(kr, br)[0, 1]); beta = float(np.polyfit(br, kr, 1)[0])
        rng = float(np.median((k[ik, 2] - k[ik, 3]) / k[ik, 4]) / np.median((b["h"][ib] - b["l"][ib]) / b["c"][ib]))
        volr = float(np.sum(k[ik, 6] * k[ik, 5]) / np.sum(b["qv"][ib]))
        out[f"{kp}_{tf}"] = {"n": int(len(kr)), "corr": corr, "beta": beta, "range_ratio": rng, "vol_ratio": volr}
        print(f"| {kp} | {bp} | {tf} | {len(kr)} | {corr:.3f} | {beta:.2f} | {rng:.2f} | {volr:.3f} |")
save_json(out, "proxy_validation.json")
