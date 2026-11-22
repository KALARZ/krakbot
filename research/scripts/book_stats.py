"""Spread & depth statistics from Kraken /0/public/Depth snapshots (data/kraken/book_snapshots.jsonl)."""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from lib import DATA, save_json

def impact_bp(levels, notional, mid):
    """VWAP slippage (bp vs mid) of a market order consuming `notional` quote on one side."""
    rem = notional; cost = 0.0; qty = 0.0
    for p, q in levels:
        take = min(rem, p * q)
        cost += take; qty += take / p; rem -= take
        if rem <= 1e-9: break
    if rem > 1e-9: return float("nan")
    vwap = cost / qty
    return abs(vwap / mid - 1) * 1e4

rows = {}
for line in open(os.path.join(DATA, "kraken", "book_snapshots.jsonl")):
    d = json.loads(line)
    if not d["bids"] or not d["asks"]: continue
    bb, ba = d["bids"][0][0], d["asks"][0][0]
    mid = (bb + ba) / 2
    r = rows.setdefault(d["pair"], {"spread": [], "d10": [], "d25": [], "d50": [], "i1k": [], "i5k": [], "i20k": [], "t": []})
    r["t"].append(d["t"])
    r["spread"].append((ba - bb) / mid * 1e4)
    for bp, k in ((10, "d10"), (25, "d25"), (50, "d50")):
        lo = mid * (1 - bp / 1e4)
        r[k].append(sum(p * q for p, q in d["bids"] if p >= lo))
    for n, k in ((1000, "i1k"), (5000, "i5k"), (20000, "i20k")):
        r[k].append(impact_bp(d["bids"], n, mid))
out = {}
print("| Pair | snapshots | spread mediano (bp) | spread p90 (bp) | depth bid ≤10bp | ≤25bp | ≤50bp (quote, mediana) | impatto vendita mkt 1k / 5k / 20k (bp da mid, mediana) |")
print("|---|---|---|---|---|---|---|---|")
for p, r in rows.items():
    a = {k: np.array(v, float) for k, v in r.items()}
    o = {"n": len(a["spread"]), "spread_med": float(np.median(a["spread"])), "spread_p90": float(np.percentile(a["spread"], 90)),
         "d10": float(np.median(a["d10"])), "d25": float(np.median(a["d25"])), "d50": float(np.median(a["d50"])),
         "i1k": float(np.nanmedian(a["i1k"])), "i5k": float(np.nanmedian(a["i5k"])), "i20k": float(np.nanmedian(a["i20k"])),
         "hours": float((a["t"].max() - a["t"].min()) / 3600)}
    out[p] = o
    print(f"| {p} | {o['n']} | {o['spread_med']:.2f} | {o['spread_p90']:.2f} | {o['d10']:,.0f} | {o['d25']:,.0f} | {o['d50']:,.0f} | {o['i1k']:.1f} / {o['i5k']:.1f} / {o['i20k']:.1f} |")
save_json(out, "book_stats.json")
