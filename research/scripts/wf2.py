"""Pre-registered pooled walk-forward (remediation of audit FAILs #3 #6 #7 #11 #13 #14 #16).

Pre-registration (fixed before running; do not edit after seeing results):
- timeframe 4h (1h rejected by the edge budget in the screening: family-median gross expectancy 0.03-0.20%
  per trade vs round trip 0.56-0.70%).
- universe: point-in-time top-8 by trailing 90d quote volume among 20 large caps incl. delisted (universe.py).
- grids below; selection per window on the POOLED universe (never per pair), objective = per-trade net
  t-stat at Tier 2 fees, plateau-smoothed over 1-step grid neighbours.
- windows: 12 semesters 2021H1..2026H2 (last one partial, data end 2026-11-21); rolling 24-month train;
  purge: train trades must exit >= 72h before the test start (embargo); test trades enter inside the window.
- portfolio rules as specified for the live bot (bt.portfolio2), capital 10,000 USD, dynamic fee tier.
- gates (user): OOS PF_net > 1.3, Sharpe > 1, maxDD < 20%, >= 300 OOS trades, >= 80% positive windows,
  expectancy > 0 with fees x2, gross expectancy >= 3x round trip.
"""
import os, sys, json, math, itertools, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from multiprocessing import Pool
from lib import resample, MS, ROOT, DATA, save_json
from bt import Bars5, simulate_trade, portfolio2, curve_stats, FEE_TIERS, stats_from_returns
import strategies as S
import universe as U

GRIDS = {
    "donchian": dict(N=[20, 50], k_sl=[1.5, 2.5], k_tr=[2.0, 3.5], regime=[0, 200]),
    "squeeze": dict(pct=[0.1, 0.25], k_sl=[1.5, 2.5], k_tr=[2.0, 3.5], regime=[0, 200]),
    "tsmom": dict(L=[18, 42, 84], thr=[0.5, 1.0, 1.5], k_sl=[2.0, 3.0], k_tr=[0.0, 3.0]),
    "impulse": dict(n=[1], z0=[2.0, 3.0], k_sl=[1.5, 2.5], k_tr=[2.0, 3.5]),
    "mr_dip": dict(n=[1, 3], z0=[2.0, 3.0], k_sl=[2.0, 3.0], exit_ma=[10, 20], regime=[0, 200]),
}
INT_PARAMS = {"N", "L", "n", "exit_ma", "regime"}
EXEC = {"through_bp": 1.0, "max_chase": 0.0025, "entry_reprices": 3, "exit_reprices": 3, "max_hold_ms": 48 * MS["1h"]}
EMBARGO = 72 * MS["1h"]
SEL_TIER = 2
TF = "4h"
KMAP = {"BTCUSDT": "XBTEUR", "ETHUSDT": "ETHEUR", "SOLUSDT": "SOLEUR", "XRPUSDT": "XRPEUR", "ADAUSDT": "ADAEUR",
        "LINKUSDT": "LINKEUR", "LTCUSDT": "LTCEUR", "DOGEUSDT": "XDGEUR", "AVAXUSDT": "AVAXEUR", "DOTUSDT": "DOTEUR"}
PCFG = {"capital_usd": 10_000, "risk_pct": 0.005, "max_pos": 3, "max_expo": 0.30, "max_pos_expo": 0.15,
        "slip_bp": 5.0, "fixed_tier": None, "cooldown_ms": 8 * MS["1h"], "cb_half": 3, "cb_stop": 5,
        "cb_pause_ms": 24 * MS["1h"], "kill_daily": 0.04, "kill_total": 0.15}

def utc(y, m=1, d=1):
    return int(dt.datetime(y, m, d, tzinfo=dt.UTC).timestamp() * 1000)

WINDOWS = [(utc(y, m), utc(y + (m == 7), 1 if m == 7 else 7)) for y in range(2021, 2027) for m in (1, 7)]

def build_costs():
    bs = json.load(open(os.path.join(ROOT, "out", "book_stats.json")))
    ap = json.load(open(os.path.join(DATA, "assetpairs.json")))["result"]
    alt = {v["altname"]: v for v in ap.values()}
    k_usd = json.load(open(os.path.join(DATA, "kraken", "ohlc_XBTUSD_60.json")))[-2][4]
    k_eur = json.load(open(os.path.join(DATA, "kraken", "ohlc_XBTEUR_60.json")))[-2][4]
    eurusd = float(k_usd) / float(k_eur)
    worst = max(bs.values(), key=lambda v: v["i5k"])
    spread, curves, min_usd = {}, {}, {}
    for s in U.CANDIDATES:
        kp = KMAP.get(s)
        st = bs.get(kp, worst) if kp else worst
        spread[s] = max(st["spread_med"], 1.0) * 1e-4 if kp else 6e-4
        pts = [(1000 * eurusd, st["i1k"]), (5000 * eurusd, st["i5k"]), (20000 * eurusd, st["i20k"] if not math.isnan(st["i20k"]) else st["i5k"] * 4)]
        curves[s] = pts
        if kp and kp in alt:
            px = float(json.load(open(os.path.join(DATA, "kraken", f"ohlc_{kp}_60.json")))[-2][4])
            min_usd[s] = max(float(alt[kp]["ordermin"]) * px, float(alt[kp]["costmin"])) * eurusd
        else:
            min_usd[s] = 5.0
    def impact(pair, notional):
        pts = curves[pair]
        xs = [0.0] + [p[0] for p in pts]; ys = [0.0] + [p[1] for p in pts]
        if notional <= xs[-1]:
            return float(np.interp(notional, xs, ys)) * 1e-4
        return ys[-1] * notional / xs[-1] * 1e-4
    return {"spread": spread, "impact": impact, "min_usd": min_usd, "eurusd": eurusd}

def keyp(p):
    return json.dumps(p, sort_keys=True)

def sim_symbol(args):
    sym, fam, plist, spread = args
    d = U.load_sym(sym); b5 = Bars5(d); b = resample(d, TF)
    out = {}
    for p in plist:
        trs = []
        for (t, px, st, mgr) in S.FAMILIES[fam](b, TF, **p):
            r = simulate_trade(b5, MS[TF], t, px, st, mgr, spread, EXEC)
            if r and r.get("filled"):
                r["pair"] = sym; trs.append(r)
        out[keyp(p)] = trs
    c1 = resample(d, "1h")
    return sym, out, (c1["ts"], c1["c"])

def simulate(fam, plist, syms, costs, procs=4):
    with Pool(procs) as pool:
        res = pool.map(sim_symbol, [(s, fam, plist, costs["spread"][s]) for s in syms])
    return {s: o for s, o, _ in res}, {s: c for s, _, c in res}

def net_trade(t, tier):
    mk, tk = FEE_TIERS[tier - 1][2:]
    fo = mk if t["exit_liq"] == "M" else tk
    return (1 + t["gross"]) * (1 - 5e-4) * (1 - fo) / ((1 + 5e-4) * (1 + mk)) - 1

def neighbours(p, grid):
    res = [p]
    for k, vals in grid.items():
        i = vals.index(p[k])
        for j in (i - 1, i + 1):
            if 0 <= j < len(vals):
                q = dict(p); q[k] = vals[j]; res.append(q)
    return res

def eligible(trs, uni):
    return [t for t in trs if U.member(uni, t["pair"], t["t_sig"])]

def select(per, grid, pts, uni, syms):
    chosen = []
    for (t0, t1) in WINDOWS:
        tr0 = t0 - 730 * MS["1d"]
        score = {}
        for p in pts:
            trs = [t for s in syms for t in per[s][keyp(p)] if t["t_entry"] >= tr0 and t["t_exit"] <= t0 - EMBARGO]
            trs = eligible(trs, uni)
            if len(trs) < 30:
                score[keyp(p)] = -1e9; continue
            r = np.array([net_trade(t, SEL_TIER) for t in trs])
            score[keyp(p)] = r.mean() / (r.std(ddof=1) + 1e-12) * math.sqrt(len(r))
        smooth = {keyp(p): float(np.mean([score[keyp(q)] for q in neighbours(p, grid)])) for p in pts}
        best = max(pts, key=lambda p: smooth[keyp(p)])
        chosen.append((t0, t1, best, smooth[keyp(best)]))
    return chosen

def assemble(per, chosen, uni, syms, override=None, exclude=None):
    oos = []
    for i, (t0, t1, p, _) in enumerate(chosen):
        pp = override[i] if override else p
        for s in syms:
            if exclude and s in exclude: continue
            oos += [t for t in per[s][keyp(pp)] if t0 <= t["t_entry"] < t1]
    return eligible(oos, uni)

def report(trades, curve, killed):
    if not trades:
        return {"n": 0, "killed": killed}
    pnl = np.array([t["pnl_usd"] for t in trades]); r = np.array([t["ret_net"] for t in trades])
    g = np.array([t["gross"] for t in trades])
    st = stats_from_returns(r); cs = curve_stats(*curve)
    rt = np.array([t["fee_in"] + t["fee_out"] + 2 * 5e-4 for t in trades])
    grid, eq = curve
    wins = []
    for (t0, t1) in WINDOWS:
        m = (grid >= t0) & (grid < t1)
        if m.sum() > 1:
            n = sum(1 for t in trades if t0 <= t["t_entry"] < t1)
            k0 = np.flatnonzero(m)[0]
            e0 = eq[k0 - 1] if k0 > 0 else eq[k0]
            wins.append({"w": dt.datetime.fromtimestamp(t0 / 1000, dt.UTC).strftime("%Y-%m"), "ret_pct": float((eq[m][-1] / e0 - 1) * 100), "n": n})
    valid = [w for w in wins if w["n"] > 0]
    return {"n": int(len(r)), "win": st["win"], "exp_net_pct": st["exp_pct"], "t_net": st["t"],
            "pf_usd": float(pnl[pnl > 0].sum() / -pnl[pnl <= 0].sum()) if (pnl <= 0).any() else float("inf"),
            "pnl_usd": float(pnl.sum()), "exp_gross_pct": float(g.mean() * 100), "abs_gross_pct": float(np.abs(g).mean() * 100),
            "rt_cost_pct": float(rt.mean() * 100), "edge_ratio_B": float(g.mean() / rt.mean()), "edge_ratio_A": float(np.abs(g).mean() / rt.mean()),
            "hold_h": float(np.mean([(t["t_exit"] - t["t_entry"]) / 3.6e6 for t in trades])),
            "maker_exit_frac": float(np.mean([t["exit_liq"] == "M" for t in trades])),
            "exit_reasons": {k: int(sum(1 for t in trades if t["reason"] == k)) for k in sorted(set(t["reason"] for t in trades))},
            "ret_q_pct": [float(np.percentile(r, q) * 100) for q in (5, 25, 50, 75, 95)],
            "avg_fee_maker_pct": float(np.mean([t["fee_in"] for t in trades]) * 100),
            "windows": wins, "frac_windows_pos": float(np.mean([w["ret_pct"] > 0 for w in valid])) if valid else 0.0,
            "pairs_traded": sorted(set(t["pair"] for t in trades)), "killed": killed, **cs}

def perturb(p, k, m):
    q = dict(p); v = p[k] * m
    q[k] = max(1, int(round(v))) if k in INT_PARAMS else round(v, 4)
    return q

def run_family(fam, costs, uni, syms):
    grid = GRIDS[fam]
    keys = list(grid); pts = [dict(zip(keys, v)) for v in itertools.product(*[grid[k] for k in keys])]
    per, closes = simulate(fam, pts, syms, costs)
    chosen = select(per, grid, pts, uni, syms)
    out = {"fam": fam, "chosen": [{"w": dt.datetime.fromtimestamp(a / 1000, dt.UTC).strftime("%Y-%m"), "params": p, "score": s} for a, _, p, s in chosen]}
    base_c = assemble(per, chosen, uni, syms)
    def pf_run(cands, **over):
        cfg = dict(PCFG); cfg.update(over)
        return portfolio2(cands, closes, cfg, costs)
    tr, cv, k = pf_run(base_c); out["base"] = report(tr, cv, k)
    base_trades = tr
    for tier in (1, 2, 3):
        out[f"tier{tier}"] = report(*pf_run(base_c, fixed_tier=tier))
    out["fee_x2"] = report(*pf_run(base_c, fee_mult=2.0))
    out["slip_x2"] = report(*pf_run(base_c, slip_bp=10.0))
    out["fee_x2_slip_x2"] = report(*pf_run(base_c, fee_mult=2.0, slip_bp=10.0))
    out["fee_x2_slip_x2_tier1"] = report(*pf_run(base_c, fee_mult=2.0, slip_bp=10.0, fixed_tier=1))
    # +-20% sensitivity on the selected parameters (one parameter at a time, all windows)
    need = {}
    for k_ in keys:
        if all(p[k_] == 0 for _, _, p, _ in chosen):
            continue
        for m in (0.8, 1.2):
            ov = [perturb(p, k_, m) if p[k_] != 0 else p for _, _, p, _ in chosen]
            need[f"{k_}x{m}"] = ov
    plist = {keyp(q): q for ov in need.values() for q in ov if keyp(q) not in per[syms[0]]}
    if plist:
        per2, _ = simulate(fam, list(plist.values()), syms, costs)
        for s in syms:
            per[s].update(per2[s])
    out["sens"] = {}
    for name, ov in need.items():
        rr = report(*pf_run(assemble(per, chosen, uni, syms, override=ov)))
        out["sens"][name] = {x: rr.get(x) for x in ("n", "exp_net_pct", "pf_usd", "sharpe", "max_dd")}
    # leave-one-pair-out
    out["lopo"] = {}
    for s in sorted(set(t["pair"] for t in base_trades)):
        rr = report(*pf_run(assemble(per, chosen, uni, syms, exclude={s})))
        out["lopo"][s] = {x: rr.get(x) for x in ("n", "exp_net_pct", "pf_usd", "sharpe", "max_dd")}
    # best month removal
    if base_trades:
        months = {}
        for t in base_trades:
            mth = dt.datetime.fromtimestamp(t["t_entry"] / 1000, dt.UTC).strftime("%Y-%m")
            months[mth] = months.get(mth, 0) + t["pnl_usd"]
        best = max(months, key=months.get)
        c2 = [c for c in base_c if dt.datetime.fromtimestamp(c["t_entry"] / 1000, dt.UTC).strftime("%Y-%m") != best]
        rr = report(*pf_run(c2))
        tot = sum(months.values())
        out["drop_best_month"] = {"month": best, "share_of_pnl": months[best] / tot if tot else None,
                                  **{x: rr.get(x) for x in ("n", "exp_net_pct", "pf_usd", "sharpe", "max_dd")}}
        # subperiods on base OOS trades
        sub = {}
        for name, (a, b) in {"2021": (2021, 2022), "2022-2023": (2022, 2024), "2024-2026": (2024, 2027)}.items():
            rs = np.array([t["ret_net"] for t in base_trades if a <= dt.datetime.fromtimestamp(t["t_entry"] / 1000, dt.UTC).year < b])
            pn = np.array([t["pnl_usd"] for t in base_trades if a <= dt.datetime.fromtimestamp(t["t_entry"] / 1000, dt.UTC).year < b])
            sub[name] = {"n": int(len(rs)), "exp_net_pct": float(rs.mean() * 100) if len(rs) else None,
                         "pf_usd": float(pn[pn > 0].sum() / -pn[pn <= 0].sum()) if len(pn) and (pn <= 0).any() else None}
        out["subperiods"] = sub
    save_json(base_trades, f"wf2_{fam}_trades.json")
    return out

if __name__ == "__main__":
    fams = sys.argv[1].split(",") if len(sys.argv) > 1 else list(GRIDS)
    syms = U.available()
    uni = U.build(K=8, syms=syms)
    costs = build_costs()
    allres = {"universe_syms": syms, "universe": {dt.datetime.fromtimestamp(t / 1000, dt.UTC).strftime("%Y-%m"): [x[:-4] for x in v] for t, v in uni.items()}}
    for fam in fams:
        res = run_family(fam, costs, uni, syms)
        allres[fam] = res
        b = res["base"]
        print(fam, {x: (round(b[x], 3) if isinstance(b.get(x), float) else b.get(x)) for x in ("n", "exp_net_pct", "pf_usd", "sharpe", "max_dd", "frac_windows_pos", "edge_ratio_B", "killed")}, flush=True)
        save_json(allres, "wf2_results.json")
