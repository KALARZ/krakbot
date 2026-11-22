"""5m microstructure: Kraken native (last 720 x 5m candles) vs Binance on the overlap. Saves out/proxy_5m.json."""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from lib import load5, DATA, save_json
MAP = {"XBTUSD": "BTCUSDT", "XBTEUR": "BTCUSDT", "ETHEUR": "ETHUSDT", "SOLEUR": "SOLUSDT", "SOLUSD": "SOLUSDT", "XRPEUR": "XRPUSDT",
       "XDGEUR": "DOGEUSDT", "ADAEUR": "ADAUSDT", "LINKEUR": "LINKUSDT", "AVAXEUR": "AVAXUSDT", "LTCEUR": "LTCUSDT", "DOTEUR": "DOTUSDT"}
def rho1(x):
    x = x - x.mean(); return float((x[1:] * x[:-1]).sum() / (x * x).sum())
out = {}; cache = {}
print("| Kraken | n | corr 5m | ρ1 Kraken | ρ1 Binance | sd Kraken/Binance | range H-L Kraken/Binance | trade Kraken per 5m (mediana) |")
print("|---|---|---|---|---|---|---|---|")
for kp, bp in MAP.items():
    k = np.array(json.load(open(os.path.join(DATA, "kraken", f"ohlc_{kp}_5.json"))), float)[:-1]
    kts = (k[:, 0] * 1000).astype(np.int64)
    if bp not in cache: cache[bp] = load5(bp)
    B = cache[bp]
    common, ik, ib = np.intersect1d(kts, B["ts"], return_indices=True)
    kr = np.diff(np.log(k[ik, 4])); br = np.diff(np.log(B["c"][ib]))
    o = {"n": int(len(kr)), "corr": float(np.corrcoef(kr, br)[0, 1]), "rho1_k": rho1(kr), "rho1_b": rho1(br),
         "sd_ratio": float(kr.std() / br.std()),
         "range_ratio": float(np.median((k[ik, 2] - k[ik, 3]) / k[ik, 4]) / np.median((B["h"][ib] - B["l"][ib]) / B["c"][ib])),
         "trades_per_5m": float(np.median(k[ik, 7]))}
    out[kp] = o
    print(f"| {kp} | {o['n']} | {o['corr']:.3f} | {o['rho1_k']:+.3f} | {o['rho1_b']:+.3f} | {o['sd_ratio']:.2f} | {o['range_ratio']:.2f} | {o['trades_per_5m']:.0f} |")
save_json(out, "proxy_5m.json")
