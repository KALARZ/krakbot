"""Screen strategy families x small parameter grids x pairs x timeframes with the execution simulator.
Per-trade statistics (no portfolio constraints): gross, and net at fee tiers 1/2/3 with 5 bp slippage per side.
Output: out/screen.json (one row per family/params/pair/tf)."""
import os, sys, json, itertools, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from multiprocessing import Pool
from lib import load5, resample, MS, save_json, ROOT
from bt import Bars5, simulate_trade, stats_from_returns, FEE_TIERS
import strategies as S

PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "LINKUSDT", "LTCUSDT", "DOGEUSDT", "AVAXUSDT", "DOTUSDT"]
# median Kraken spreads (bp) for the corresponding EUR/USD books, from book_stats (conservative: the wider of EUR/USD)
SPREAD_BP = {"BTCUSDT": 1.0, "ETHUSDT": 1.0, "SOLUSDT": 2.0, "XRPUSDT": 2.5, "ADAUSDT": 4.5, "LINKUSDT": 2.0,
             "LTCUSDT": 6.0, "DOGEUSDT": 3.0, "AVAXUSDT": 4.0, "DOTUSDT": 6.0}
EXEC = {"through_bp": 1.0, "max_chase": 0.0025, "entry_reprices": 3, "exit_reprices": 3, "max_hold_ms": 48 * MS["1h"]}
SLIP = 0.0005
PERIODS = {"19-21": (2019, 2022), "22-23": (2022, 2024), "24-26": (2024, 2027)}

GRIDS = {
    "donchian": dict(N=[20, 50], k_sl=[1.5, 2.5], k_tr=[2.0, 3.5], regime=[0, 200]),
    "squeeze": dict(pct=[0.1, 0.25], k_sl=[1.5, 2.5], k_tr=[2.0, 3.5], regime=[0, 200]),
    "tsmom": dict(L=[72, 168, 336], thr=[0.5, 1.0, 1.5], k_sl=[2.0, 3.0], k_tr=[0.0, 3.0]),
    "impulse": dict(n=[1, 4], z0=[2.0, 3.0], k_sl=[1.5, 2.5], k_tr=[2.0, 3.5]),
    "mr_dip": dict(n=[1, 4, 12], z0=[2.0, 3.0], k_sl=[2.0, 3.0], exit_ma=[10, 20], regime=[0, 200]),
}

def net_ret(g, exit_liq, tier):
    mk, tk = FEE_TIERS[tier - 1][2:]
    fo = mk if exit_liq == "M" else tk
    return (1 + g) * (1 - SLIP) * (1 - fo) / ((1 + SLIP) * (1 + mk)) - 1

def yr(t):
    return dt.datetime.fromtimestamp(t / 1000, dt.UTC).year

def run_pair(pair):
    b5d = load5(pair); b5 = Bars5(b5d)
    rows = []
    for tf in ("1h", "4h"):
        b = resample(b5d, tf)
        for fam, grid in GRIDS.items():
            keys = list(grid)
            for vals in itertools.product(*[grid[k] for k in keys]):
                p = dict(zip(keys, vals))
                q = dict(p)
                if fam == "tsmom" and tf == "4h":
                    q["L"] = p["L"] // 4
                if fam in ("impulse", "mr_dip") and tf == "4h":
                    q["n"] = max(1, p["n"] // 4)
                sigs = S.FAMILIES[fam](b, tf, **q)
                trades = []; last_exit = -1; nsig = 0; nmiss = 0
                for (t, px, st, mgr) in sigs:
                    if t < last_exit:  # one position per pair (sequential, pair-level)
                        continue
                    nsig += 1
                    r = simulate_trade(b5, MS[tf], t, px, st, mgr, SPREAD_BP[pair] * 1e-4, EXEC)
                    if r is None: continue
                    if not r["filled"]:
                        nmiss += 1; continue
                    trades.append(r); last_exit = r["t_exit"]
                if not trades:
                    continue
                g = np.array([x["gross"] for x in trades])
                row = {"pair": pair, "tf": tf, "fam": fam, "params": p, "n_sig": nsig, "fill_rate": len(trades) / max(nsig, 1),
                       "gross": stats_from_returns(g),
                       "hold_h": float(np.mean([(x["t_exit"] - x["t_entry"]) / 3.6e6 for x in trades])),
                       "maker_exit_frac": float(np.mean([x["exit_liq"] == "M" for x in trades])),
                       "stop_frac": float(np.mean(["stop" in x["reason"] for x in trades])),
                       "stop_dist": float(np.median([x["stop_dist"] for x in trades]))}
                for tier in (1, 2, 3):
                    nr = np.array([net_ret(x["gross"], x["exit_liq"], tier) for x in trades])
                    row[f"net_t{tier}"] = stats_from_returns(nr)
                    if tier == 2:
                        ys = np.array([yr(x["t_entry"]) for x in trades])
                        for pn, (y0, y1) in PERIODS.items():
                            m = (ys >= y0) & (ys < y1)
                            row[f"net_t2_{pn}"] = float(nr[m].mean() * 100) if m.sum() >= 10 else None
                            row[f"n_{pn}"] = int(m.sum())
                rows.append(row)
    print(pair, "done", len(rows), flush=True)
    return rows

if __name__ == "__main__":
    pairs = sys.argv[1].split(",") if len(sys.argv) > 1 else PAIRS
    with Pool(4) as pool:
        res = pool.map(run_pair, pairs)
    allrows = [r for rr in res for r in rr]
    save_json(allrows, "screen.json" if len(sys.argv) <= 2 else sys.argv[2])
    print("rows", len(allrows))
