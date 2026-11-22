"""Fee-tier sensitivity of the pre-registered walk-forward (same selection as wf2.py; only the portfolio fee
tier changes). Answers: would a lower tier (e.g. Tier 7 0.10/0.22 via $400k assets) make the candidate viable?"""
import os, sys, itertools
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wf2, universe as U
from lib import save_json
from bt import portfolio2, FEE_TIERS
fam = sys.argv[1] if len(sys.argv) > 1 else "donchian"
syms = U.available(); uni = U.build(K=8, syms=syms); costs = wf2.build_costs()
grid = wf2.GRIDS[fam]; keys = list(grid)
pts = [dict(zip(keys, v)) for v in itertools.product(*[grid[k] for k in keys])]
per, closes = wf2.simulate(fam, pts, syms, costs)
chosen = wf2.select(per, grid, pts, uni, syms)
cands = wf2.assemble(per, chosen, uni, syms)
out = {}
for tier in range(1, 10):
    cfg = dict(wf2.PCFG); cfg["fixed_tier"] = tier
    r = wf2.report(*portfolio2(cands, closes, cfg, costs))
    out[tier] = {k: r.get(k) for k in ("n", "exp_net_pct", "pf_usd", "sharpe", "max_dd", "cagr", "frac_windows_pos", "killed", "edge_ratio_B", "rt_cost_pct")}
    mk, tk = FEE_TIERS[tier - 1][2:]
    print(f"T{tier} ({mk*100:.2f}/{tk*100:.2f}) n={r['n']} exp={r['exp_net_pct']:+.3f}% PF={r['pf_usd']:.3f} Sharpe={r['sharpe']:+.3f} DD={r['max_dd']*100:.1f}% CAGR={r['cagr']*100:+.2f}% win_w={r['frac_windows_pos']*100:.0f}% B={r['edge_ratio_B']:.2f} killed={r['killed']}", flush=True)
save_json(out, f"wf2_tiers_{fam}.json")
