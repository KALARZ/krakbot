"""Same pre-registered walk-forward as wf2.py but with a different universe size K (default 4), to give the
forward test a baseline with exactly the live-eligible rule (user constraint: 2-4 pairs)."""
import os, sys, itertools
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wf2, universe as U
from lib import save_json
from bt import portfolio2
fam = sys.argv[1]; K = int(sys.argv[2])
syms = U.available(); uni = U.build(K=K, syms=syms); costs = wf2.build_costs()
grid = wf2.GRIDS[fam]; keys = list(grid)
pts = [dict(zip(keys, v)) for v in itertools.product(*[grid[k] for k in keys])]
per, closes = wf2.simulate(fam, pts, syms, costs)
chosen = wf2.select(per, grid, pts, uni, syms)
cands = wf2.assemble(per, chosen, uni, syms)
out = {"K": K, "chosen": [{"params": p} for _, _, p, _ in chosen]}
from bt import portfolio2 as _p2
_tr, _cv, _k = _p2(cands, closes, dict(wf2.PCFG), costs)
save_json(_tr, f"wf2_{fam}_K{K}_trades.json")
save_json(cands, f"wf2_{fam}_K{K}_candidates.json")
for name, over in (("base", {}), ("tier1", {"fixed_tier": 1}), ("tier3", {"fixed_tier": 3}), ("fee_x2", {"fee_mult": 2.0}), ("no_kill", {"kill_total": None, "kill_daily": None})):
    cfg = dict(wf2.PCFG); cfg.update(over)
    r = wf2.report(*portfolio2(cands, closes, cfg, costs))
    out[name] = {k: r.get(k) for k in ("n", "exp_net_pct", "pf_usd", "sharpe", "max_dd", "cagr", "frac_windows_pos", "killed", "edge_ratio_B")}
    print(name, {k: (round(v, 3) if isinstance(v, float) else v) for k, v in out[name].items()}, flush=True)
print("params per window:", [c["params"] for c in out["chosen"]])
save_json(out, f"wf2_{fam}_K{K}.json")
