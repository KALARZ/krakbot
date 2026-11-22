"""Diagnostic (not used for any decision): same pre-registered selection as wf2.py, portfolio WITHOUT kill
switches, to show whether the OOS failure is caused by the kill switch or by the absence of edge."""
import os, sys, itertools, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wf2, universe as U
from lib import save_json
from bt import portfolio2
syms = U.available(); uni = U.build(K=8, syms=syms); costs = wf2.build_costs()
out = {}
for fam in sys.argv[1].split(","):
    grid = wf2.GRIDS[fam]; keys = list(grid)
    pts = [dict(zip(keys, v)) for v in itertools.product(*[grid[k] for k in keys])]
    per, closes = wf2.simulate(fam, pts, syms, costs)
    chosen = wf2.select(per, grid, pts, uni, syms)
    cands = wf2.assemble(per, chosen, uni, syms)
    cfg = dict(wf2.PCFG); cfg.update(kill_total=None, kill_daily=None)
    r = wf2.report(*portfolio2(cands, closes, cfg, costs))
    cfg3 = dict(cfg); cfg3["fixed_tier"] = 3
    r3 = wf2.report(*portfolio2(cands, closes, cfg3, costs))
    out[fam] = {"no_kill": {k: r.get(k) for k in ("n", "exp_net_pct", "pf_usd", "sharpe", "max_dd", "cagr", "frac_windows_pos", "windows")},
                "no_kill_tier3": {k: r3.get(k) for k in ("n", "exp_net_pct", "pf_usd", "sharpe", "max_dd", "cagr", "frac_windows_pos")}}
    print(fam, {k: (round(v, 3) if isinstance(v, float) else v) for k, v in out[fam]["no_kill"].items() if k != "windows"},
          "| T3:", {k: (round(v, 3) if isinstance(v, float) else v) for k, v in out[fam]["no_kill_tier3"].items()}, flush=True)
    save_json(out, "wf2_diag.json")
