"""Walk-forward portfolio evaluation.

For a family/timeframe/grid and a pair set:
  1. simulate ALL entry signals for every grid point and pair (execution model of bt.simulate_trade)
  2. walk-forward: 6-month test windows; parameters chosen on the preceding 24 months (rolling) by the
     plateau-smoothed per-trade net t-stat (mean of the grid point and its 1-step neighbours) at a
     conservative fee tier; ties -> fewer trades preferred is NOT applied (pure objective)
  3. one continuous portfolio pass over the concatenated OOS candidates (capital, tiers, breakers carry over)
Outputs JSON with OOS trades summary, per-window results, curve stats, and the fixed-parameter
full-sample run of the most-selected parameter set (for sensitivity reporting)."""
import os, sys, json, itertools, math, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from multiprocessing import Pool
from lib import load5, resample, MS, save_json, ROOT
from bt import Bars5, simulate_trade, stats_from_returns, portfolio, curve_stats, FEE_TIERS
import strategies as S
from screen import SPREAD_BP, SLIP

def utc(y, m=1, d=1):
    return int(dt.datetime(y, m, d, tzinfo=dt.UTC).timestamp() * 1000)

def windows(start=(2021, 1), end=(2026, 12)):
    out = []; y, m = start
    while (y, m) < end:
        y2, m2 = (y, m + 6) if m + 6 <= 12 else (y + 1, m - 6)
        out.append((utc(y, m), utc(y2, m2))); y, m = y2, m2
    return out

def grid_points(grid):
    keys = list(grid)
    return keys, [dict(zip(keys, v)) for v in itertools.product(*[grid[k] for k in keys])]

def neighbours(p, grid):
    res = [p]
    for k, vals in grid.items():
        i = vals.index(p[k])
        for j in (i - 1, i + 1):
            if 0 <= j < len(vals):
                q = dict(p); q[k] = vals[j]; res.append(q)
    return res

def key(p):
    return json.dumps(p, sort_keys=True)

def sim_pair(args):
    pair, fam, tf, grid, execcfg, adapt = args
    b5d = load5(pair); b5 = Bars5(b5d); b = resample(b5d, tf)
    _, pts = grid_points(grid)
    res = {}
    for p in pts:
        q = adapt(fam, tf, p) if adapt else p
        trades = []
        for (t, px, st, mgr) in S.FAMILIES[fam](b, tf, **q):
            r = simulate_trade(b5, MS[tf], t, px, st, mgr, SPREAD_BP[pair] * 1e-4, execcfg)
            if r and r["filled"]:
                r["pair"] = pair; trades.append(r)
        res[key(p)] = trades
    c1 = resample(b5d, "1h")
    return pair, res, (c1["ts"], c1["c"])

def net(tr, tier):
    mk, tk = FEE_TIERS[tier - 1][2:]
    fo = mk if tr["exit_liq"] == "M" else tk
    return (1 + tr["gross"]) * (1 - SLIP) * (1 - fo) / ((1 + SLIP) * (1 + mk)) - 1

def objective(trs, tier):
    if len(trs) < 20:
        return -1e9
    r = np.array([net(t, tier) for t in trs])
    return r.mean() / (r.std(ddof=1) + 1e-12) * math.sqrt(len(r))

def run(fam, tf, grid, pairs, pcfg, execcfg, sel_tier=3, train_months=24, adapt=None, procs=4, wins=None):
    with Pool(procs) as pool:
        out = pool.map(sim_pair, [(p, fam, tf, grid, execcfg, adapt) for p in pairs])
    per = {p: r for p, r, _ in out}
    closes = {p: c for p, _, c in out}
    keys, pts = grid_points(grid)
    wins = wins or windows()
    oos = []; chosen = []
    for (t0, t1) in wins:
        tr0 = t0 - train_months * 30 * MS["1d"]
        scores = {}
        for p in pts:
            trs = [t for pair in pairs for t in per[pair][key(p)] if tr0 <= t["t_entry"] < t0]
            scores[key(p)] = objective(trs, sel_tier)
        smooth = {key(p): np.mean([scores[key(q)] for q in neighbours(p, grid)]) for p in pts}
        best = max(pts, key=lambda p: smooth[key(p)])
        chosen.append({"t0": t0, "t1": t1, "params": best, "train_score": float(smooth[key(best)])})
        oos += [t for pair in pairs for t in per[pair][key(best)] if t0 <= t["t_entry"] < t1]
    trades, curve, killed = portfolio(oos, closes, pcfg)
    res = {"fam": fam, "tf": tf, "pairs": pairs, "chosen": chosen, "killed": killed}
    res.update(summarize(trades, curve, wins))
    # fixed-parameter full-period runs for every grid point (sensitivity)
    sens = {}
    for p in pts:
        cand = [t for pair in pairs for t in per[pair][key(p)] if t["t_entry"] >= wins[0][0]]
        tr2, cv2, k2 = portfolio(cand, closes, pcfg)
        s = summarize(tr2, cv2, wins, brief=True); s["killed"] = k2
        sens[key(p)] = s
    res["sensitivity"] = sens
    return res, trades, curve

def summarize(trades, curve, wins, brief=False):
    if not trades or curve is None:
        return {"n": 0}
    r = np.array([t["ret_net"] for t in trades]); g = np.array([t["gross"] for t in trades])
    pnl = np.array([t["pnl_usd"] for t in trades])
    st = stats_from_returns(r)
    pf_usd = pnl[pnl > 0].sum() / -pnl[pnl <= 0].sum() if (pnl <= 0).any() else float("inf")
    cs = curve_stats(*curve)
    out = {"n": int(len(r)), "win": st["win"], "exp_net_pct": st["exp_pct"], "t_net": st["t"], "pf_usd": float(pf_usd),
           "exp_gross_pct": float(g.mean() * 100), "abs_gross_pct": float(np.abs(g).mean() * 100),
           "hold_h_mean": float(np.mean([(t["t_exit"] - t["t_entry"]) / 3.6e6 for t in trades])),
           "fee_rt_mean_pct": float(np.mean([(t["fee_in"] + t["fee_out"]) * 100 for t in trades])),
           **cs}
    if brief:
        return out
    grid, eq = curve
    perw = []
    for (t0, t1) in wins:
        m = (grid >= t0) & (grid < t1)
        if m.sum() < 2:
            perw.append(None); continue
        e = eq[m]; ntr = sum(1 for t in trades if t0 <= t["t_entry"] < t1)
        perw.append({"t0": t0, "ret": float(e[-1] / e[0] - 1), "n": ntr})
    out["windows"] = perw
    valid = [w for w in perw if w is not None and w["n"] > 0]
    out["frac_windows_pos"] = float(np.mean([w["ret"] > 0 for w in valid])) if valid else 0.0
    out["ret_quantiles_pct"] = [float(np.percentile(r, q) * 100) for q in (5, 25, 50, 75, 95)]
    out["exit_reasons"] = {k: int(sum(1 for t in trades if t["reason"] == k)) for k in set(t["reason"] for t in trades)}
    out["maker_exit_frac"] = float(np.mean([t["exit_liq"] == "M" for t in trades]))
    out["tier_fee_maker_mean"] = float(np.mean([t["fee_in"] for t in trades]))
    return out

PCFG = {"capital_usd": 10_000, "risk_pct": 0.005, "max_pos": 3, "max_expo": 0.30, "max_pos_expo": 0.15,
        "slip_bp": 5.0, "impact": {}, "fixed_tier": None, "cooldown_ms": 8 * MS["1h"], "cb_half": 3, "cb_stop": 5,
        "cb_pause_ms": 24 * MS["1h"], "kill_daily": 0.04, "kill_total": 0.20, "ordermin_usd": 10.0}
