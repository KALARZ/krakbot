"""Sizing research step 1: OOS trades bucketed by stop distance (% of price = k_sl*ATR/price, i.e. volatility).
Net returns at fee Tier 1 (forward-test reading tier) with 5 bp slippage per side. Monotonicity is the question:
Spearman (trade level) + bucket sequence + stability by subperiod and decomposition cross-sectional vs within-pair."""
import os, sys, json, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from scipy import stats
from lib import ROOT, save_json
from bt import FEE_TIERS

def net(t, tier=1, slip=5e-4):
    mk, tk = FEE_TIERS[tier - 1][2:]
    fo = mk if t["exit_liq"] == "M" else tk
    return (1 + t["gross"]) * (1 - slip) * (1 - fo) / ((1 + slip) * (1 + mk)) - 1

def pf(r):
    return float(r[r > 0].sum() / -r[r <= 0].sum()) if (r <= 0).any() and (r > 0).any() else float("nan")

def bucket_table(trs, nb, label, tier=1):
    sd = np.array([t["stop_dist"] for t in trs]); r = np.array([net(t, tier) for t in trs]); g = np.array([t["gross"] for t in trs])
    edges = np.quantile(sd, np.linspace(0, 1, nb + 1))
    idx = np.clip(np.searchsorted(edges, sd, side="right") - 1, 0, nb - 1)
    rows = []
    for b in range(nb):
        m = idx == b; x = r[m]
        rows.append({"bucket": b + 1, "n": int(m.sum()), "stop_lo": float(edges[b]), "stop_hi": float(edges[b + 1]),
                     "stop_med": float(np.median(sd[m])), "exp_net": float(x.mean()), "t": float(x.mean() / x.std(ddof=1) * np.sqrt(len(x))),
                     "pf": pf(x), "win": float((x > 0).mean()), "exp_gross": float(g[m].mean())})
    rho, p = stats.spearmanr(sd, r)
    seq = [row["exp_net"] for row in rows]
    up_steps = sum(1 for a, b in zip(seq, seq[1:]) if b > a)
    rho_b, _ = stats.spearmanr(range(nb), seq)
    hi, lo = r[idx == nb - 1], r[idx == 0]
    tdiff = stats.ttest_ind(hi, lo, equal_var=False)
    return {"label": label, "n": len(trs), "rows": rows, "spearman_trade": float(rho), "spearman_p": float(p),
            "spearman_buckets": float(rho_b), "up_steps": up_steps, "steps": nb - 1,
            "top_minus_bottom": float(hi.mean() - lo.mean()), "welch_t": float(tdiff.statistic), "welch_p": float(tdiff.pvalue)}

def fmt(T):
    L = [f"**{T['label']}** — N={T['n']}; Spearman(stop%, rend. netto) = {T['spearman_trade']:+.3f} (p={T['spearman_p']:.3g}); "
         f"sequenza crescente in {T['up_steps']}/{T['steps']} passi (ρ sui bucket {T['spearman_buckets']:+.2f}); "
         f"ultimo − primo bucket = {T['top_minus_bottom']*100:+.2f} pp (Welch t={T['welch_t']:+.2f}, p={T['welch_p']:.3g})", "",
         "| Bucket | N | stop % (range) | stop % mediano | expectancy netta T1 | t | PF (T1) | win rate | expectancy lorda |",
         "|---|---|---|---|---|---|---|---|---|"]
    for r in T["rows"]:
        L.append(f"| {r['bucket']} | {r['n']} | {r['stop_lo']*100:.1f}–{r['stop_hi']*100:.1f} | {r['stop_med']*100:.1f} | {r['exp_net']*100:+.2f}% | {r['t']:+.2f} | {r['pf']:.2f} | {r['win']*100:.0f}% | {r['exp_gross']*100:+.2f}% |")
    return "\n".join(L)

def year(t):
    return dt.datetime.fromtimestamp(t["t_entry"] / 1000, dt.UTC).year

out = {}; md = []
J = lambda f: json.load(open(os.path.join(ROOT, "out", f)))
sets = [("K4_exec", "Top-4, trade eseguiti dal portafoglio OOS", J("wf2_donchian_K4_trades.json")),
        ("K4_cand", "Top-4, tutti i segnali OOS (prima dei vincoli di portafoglio)", J("wf2_donchian_K4_candidates.json")),
        ("K8_exec", "Top-8, trade eseguiti (run del §E, per confronto)", J("wf2_donchian_trades.json"))]
for key, label, trs in sets:
    T = bucket_table(trs, 5, label); out[key] = T; md += [f"#### {label}", "", fmt(T), ""]
# stability on the large set (K4 candidates): terciles per subperiod
C = J("wf2_donchian_K4_candidates.json")
md += ["#### Stabilità per sottoperiodo (top-4, tutti i segnali OOS; terzili di stop % ricalcolati nel sottoperiodo)", ""]
for name, (a, b) in {"2021": (2021, 2022), "2022-2023": (2022, 2024), "2024-2026": (2024, 2027)}.items():
    sub = [t for t in C if a <= year(t) < b]
    if len(sub) >= 60:
        T = bucket_table(sub, 3, f"{name}"); out[f"period_{name}"] = T
        md += [fmt(T), ""]
# decomposition: cross-sectional (pair average stop%) vs within-pair (tercile of stop% inside each pair)
pairs = sorted(set(t["pair"] for t in C))
md += ["#### Scomposizione: tra coppie vs dentro la stessa coppia (top-4, tutti i segnali OOS)", "",
       "| Coppia | N | stop % mediano | expectancy netta T1 | PF | terzile basso / medio / alto (expectancy, N) |", "|---|---|---|---|---|---|"]
within = []
for p in pairs:
    tp = [t for t in C if t["pair"] == p]
    if len(tp) < 15: continue
    sd = np.array([t["stop_dist"] for t in tp]); r = np.array([net(t) for t in tp])
    q = np.quantile(sd, [1/3, 2/3]); idx = np.searchsorted(q, sd, side="right")
    cells = []
    for b in range(3):
        m = idx == b
        cells.append(f"{r[m].mean()*100:+.2f}% ({m.sum()})")
        within += [(b, x) for x in r[m]]
    md.append(f"| {p[:-4]} | {len(tp)} | {np.median(sd)*100:.1f} | {r.mean()*100:+.2f}% | {pf(r):.2f} | " + " / ".join(cells) + " |")
wb = np.array([b for b, _ in within]); wr = np.array([x for _, x in within])
rho_w, p_w = stats.spearmanr(wb, wr)
out["within_pair"] = {"spearman": float(rho_w), "p": float(p_w), "exp_by_tercile": [float(wr[wb == b].mean()) for b in range(3)],
                      "n_by_tercile": [int((wb == b).sum()) for b in range(3)], "pf_by_tercile": [pf(wr[wb == b]) for b in range(3)]}
md += ["", f"Effetto **dentro la coppia** (terzili per coppia, aggregati): expectancy T1 basso/medio/alto = " +
       " / ".join(f"{e*100:+.2f}% (N={n}, PF {f:.2f})" for e, n, f in zip(out['within_pair']['exp_by_tercile'], out['within_pair']['n_by_tercile'], out['within_pair']['pf_by_tercile'])) +
       f"; Spearman(terzile, rend.) = {rho_w:+.3f} (p={p_w:.3g})."]
pm = [(np.median([t['stop_dist'] for t in C if t['pair'] == p]), np.mean([net(t) for t in C if t['pair'] == p])) for p in pairs if sum(1 for t in C if t['pair'] == p) >= 15]
rho_x, p_x = stats.spearmanr([a for a, _ in pm], [b for _, b in pm])
out["cross_pair"] = {"spearman": float(rho_x), "p": float(p_x), "n_pairs": len(pm)}
md += [f"Effetto **tra coppie** (stop % mediano della coppia vs expectancy della coppia): Spearman = {rho_x:+.3f} su {len(pm)} coppie (p={p_x:.3g})."]
save_json(out, "sizing_buckets.json")
open(os.path.join(ROOT, "out", "sizing_buckets.md"), "w").write("\n".join(md))
print("\n".join(md))
