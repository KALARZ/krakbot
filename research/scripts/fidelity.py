"""Execution fidelity: same signals (from Binance 4h bars, long warm-up), executed by bt.simulate_trade on
(a) Binance 5m bars and (b) Kraken NATIVE 5m bars rebuilt from /0/public/Trades. Native bars are rescaled to
USDT units with a 1-day rolling median of the Binance/Kraken close ratio (removes FX/basis level, keeps Kraken's
own ranges, gaps and trade-through behaviour). 5m buckets without trades become flat bars (no fills possible).
Reports, per Kraken pair, fill rates and paired gross-return differences."""
import os, sys, json, itertools
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from lib import load5, resample, MS, DATA, save_json
from bt import Bars5, simulate_trade
import strategies as S
from wf2 import GRIDS, EXEC, KMAP, build_costs

INV = {v: k for k, v in KMAP.items()}

def native_bars(kp, bin5):
    z = np.load(os.path.join(DATA, "kraken", f"trades5m_{kp}.npz"))
    ts = np.arange(z["ts"][0], z["ts"][-1] + 1, MS["5m"])
    idx = np.searchsorted(z["ts"], ts)
    have = (idx < len(z["ts"])) & (z["ts"][np.clip(idx, 0, len(z["ts"]) - 1)] == ts)
    c = np.empty(len(ts)); o = np.empty(len(ts)); h = np.empty(len(ts)); l = np.empty(len(ts))
    last = z["o"][0]
    for i in range(len(ts)):
        if have[i]:
            j = idx[i]; o[i], h[i], l[i], c[i] = z["o"][j], z["h"][j], z["l"][j], z["c"][j]; last = c[i]
        else:
            o[i] = h[i] = l[i] = c[i] = last
    # FX/basis rescale to Binance USDT units
    bi = np.searchsorted(bin5["ts"], ts)
    ok = (bi < len(bin5["ts"])) & (bin5["ts"][np.clip(bi, 0, len(bin5["ts"]) - 1)] == ts)
    ratio = np.full(len(ts), np.nan); ratio[ok] = bin5["c"][bi[ok]] / c[ok]
    from numpy.lib.stride_tricks import sliding_window_view
    w = 288
    rr = np.copy(ratio)
    fill = np.nanmedian(ratio)
    rr[np.isnan(rr)] = fill
    med = np.concatenate([np.full(w - 1, np.median(rr[:w])), np.median(sliding_window_view(rr, w), axis=1)])
    return {"ts": ts, "o": o * med, "h": h * med, "l": l * med, "c": c * med, "have": have}

if __name__ == "__main__":
    costs = build_costs()
    out = {}
    for kp, sym in INV.items():
        f = os.path.join(DATA, "kraken", f"trades5m_{kp}.npz")
        if not os.path.exists(f):
            continue
        d = load5(sym); b4 = resample(d, "4h")
        nb = native_bars(kp, d)
        t_lo, t_hi = nb["ts"][0] + MS["1d"], nb["ts"][-1] - 3 * MS["1d"]
        m = (d["ts"] >= nb["ts"][0]) & (d["ts"] <= nb["ts"][-1])
        pb = Bars5({k: d[k][m] for k in ("ts", "o", "h", "l", "c")})
        kb = Bars5(nb)
        sp = costs["spread"][sym]
        rows = []
        for fam, grid in GRIDS.items():
            keys = list(grid)
            for vals in itertools.product(*[grid[k] for k in keys]):
                p = dict(zip(keys, vals))
                for (t, px, st, mgr) in S.FAMILIES[fam](b4, "4h", **p):
                    if not (t_lo <= t <= t_hi):
                        continue
                    a = simulate_trade(pb, MS["4h"], t, px, st, mgr, sp, EXEC)
                    b = simulate_trade(kb, MS["4h"], t, px, st, mgr, sp, EXEC)
                    if a is None or b is None:
                        continue
                    rows.append((fam, a.get("filled", False), b.get("filled", False), a.get("gross"), b.get("gross"),
                                 a.get("exit_liq"), b.get("exit_liq"), t))
        if not rows:
            continue
        uniq = {}
        for r in rows:  # de-duplicate identical signals produced by several grid points
            uniq.setdefault((r[0], r[7], r[3], r[4]), r)
        R = list(uniq.values())
        fa = np.array([r[1] for r in R]); fb = np.array([r[2] for r in R])
        both = [r for r in R if r[1] and r[2]]
        dg = np.array([r[4] - r[3] for r in both]) if both else np.array([np.nan])
        mx_a = np.mean([r[5] == "M" for r in both]) if both else np.nan
        mx_b = np.mean([r[6] == "M" for r in both]) if both else np.nan
        only_a = [r for r in R if r[1] and not r[2]]
        res = {"signals": len(R), "fill_proxy": float(fa.mean()), "fill_native": float(fb.mean()), "both": len(both),
               "gross_proxy_pct": float(np.mean([r[3] for r in both]) * 100) if both else None,
               "gross_native_pct": float(np.mean([r[4] for r in both]) * 100) if both else None,
               "delta_gross_bp_mean": float(np.nanmean(dg) * 1e4), "delta_gross_bp_median": float(np.nanmedian(dg) * 1e4),
               "maker_exit_proxy": float(mx_a), "maker_exit_native": float(mx_b),
               "gross_of_missed_on_native_pct": float(np.mean([r[3] for r in only_a]) * 100) if only_a else None,
               "native_bar_coverage": float(nb["have"].mean()), "days": float((nb["ts"][-1] - nb["ts"][0]) / 8.64e7)}
        out[kp] = res
        print(kp, {k: (round(v, 3) if isinstance(v, float) else v) for k, v in res.items()}, flush=True)
    save_json(out, "fidelity.json")
