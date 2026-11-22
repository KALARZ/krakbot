"""Render out/wf2_results.json (+ wf2_diag.json if present) into markdown tables (out/wf2_tables.md)."""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import ROOT
R = json.load(open(os.path.join(ROOT, "out", "wf2_results.json")))
D = json.load(open(os.path.join(ROOT, "out", "wf2_diag.json"))) if os.path.exists(os.path.join(ROOT, "out", "wf2_diag.json")) else {}
F = ["donchian", "squeeze", "tsmom", "impulse", "mr_dip"]
L = []; p = L.append
f2 = lambda x, d=2: "n/a" if x is None else (f"{x:+.{d}f}" if isinstance(x, (int, float)) else str(x))
pct = lambda x: "n/a" if x is None else f"{x*100:.1f}%"
import datetime as dt
def kd(k):
    return "—" if not k else f"{k[0]} @ {dt.datetime.fromtimestamp(k[1]/1000, dt.UTC):%Y-%m-%d}"
p("### E.1 Walk-forward OOS (pre-registrato), regole di rischio complete, tier fee dinamico, capitale 10k USD")
p("| Famiglia | trade OOS | win | exp. netta/trade | PF netto (USD) | Sharpe | Sortino | max DD | CAGR | % finestre >0 | exp. lorda | RT medio | lordo/RT (B) | |lordo|/RT (A) | hold medio | uscite maker | kill switch |")
p("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
for f in F:
    b = R[f]["base"]
    p(f"| {f} | {b['n']} | {pct(b['win'])} | {b['exp_net_pct']:+.2f}% | {b['pf_usd']:.2f} | {b['sharpe']:+.2f} | {b['sortino']:+.2f} | {pct(b['max_dd'])} | {pct(b['cagr'])} | {pct(b['frac_windows_pos'])} | {b['exp_gross_pct']:+.2f}% | {b['rt_cost_pct']:.2f}% | {b['edge_ratio_B']:.2f}× | {b['edge_ratio_A']:.1f}× | {b['hold_h']:.0f}h | {pct(b['maker_exit_frac'])} | {kd(b['killed'])} |")
p("")
p("### E.2 Stress sui costi (OOS): expectancy netta per trade, PF, Sharpe")
p("| Famiglia | base (tier dinamico) | Tier 1 fisso | Tier 2 | Tier 3 | fee ×2 | slippage ×2 | fee ×2 + slip ×2 | fee×2+slip×2 a Tier 1 |")
p("|---|---|---|---|---|---|---|---|---|")
for f in F:
    cells = []
    for k in ("base", "tier1", "tier2", "tier3", "fee_x2", "slip_x2", "fee_x2_slip_x2", "fee_x2_slip_x2_tier1"):
        x = R[f][k]
        cells.append("n=0" if not x.get("n") else f"{x['exp_net_pct']:+.2f}% / PF {x['pf_usd']:.2f} / S {x['sharpe']:+.2f}")
    p(f"| {f} | " + " | ".join(cells) + " |")
p("")
p("### E.3 Sensitivity ±20% sui parametri selezionati (OOS): exp. netta/trade, PF")
for f in F:
    s = R[f]["sens"]
    p(f"- **{f}**: " + "; ".join(f"{k} → {v['exp_net_pct']:+.2f}% PF {v['pf_usd']:.2f}" for k, v in s.items() if v.get("n")))
p("")
p("### E.4 Concentrazione: leave-one-pair-out (range) e rimozione del mese migliore")
p("| Famiglia | LOPO exp. min…max | LOPO PF min…max | mese migliore | quota PnL | senza mese migliore: exp / PF / Sharpe |")
p("|---|---|---|---|---|---|")
for f in F:
    lo = R[f]["lopo"]; ex = [v["exp_net_pct"] for v in lo.values() if v.get("n")]; pf = [v["pf_usd"] for v in lo.values() if v.get("n")]
    dbm = R[f].get("drop_best_month", {})
    share = dbm.get("share_of_pnl")
    p(f"| {f} | {min(ex):+.2f}…{max(ex):+.2f}% | {min(pf):.2f}…{max(pf):.2f} | {dbm.get('month')} | {'n/a (PnL totale ≤ 0)' if share is None or share < 0 else f'{share*100:.0f}%'} | {f2(dbm.get('exp_net_pct'))}% / {dbm.get('pf_usd', 0):.2f} / {f2(dbm.get('sharpe'))} |")
p("")
p("### E.5 Sottoperiodi OOS (exp. netta/trade, PF, n)")
p("| Famiglia | 2021 | 2022–2023 | 2024–2026 |"); p("|---|---|---|---|")
for f in F:
    s = R[f].get("subperiods", {})
    p(f"| {f} | " + " | ".join(("— (HALT)" if not s[k]['n'] else f"{f2(s[k]['exp_net_pct'])}% / PF {s[k]['pf_usd']:.2f} / n={s[k]['n']}") for k in ("2021", "2022-2023", "2024-2026")) + " |")
p("")
p("### E.6 Rendimento per finestra semestrale (portafoglio OOS, %)")
p("| Famiglia | " + " | ".join(w["w"] for w in R["mr_dip"]["base"]["windows"]) + " |")
p("|---|" + "---|" * len(R["mr_dip"]["base"]["windows"]))
for f in F:
    ws = {w["w"]: w for w in R[f]["base"]["windows"]}
    k = R[f]["base"].get("killed")
    kt = dt.datetime.fromtimestamp(k[1] / 1000, dt.UTC).strftime("%Y-%m") if k else "9999"
    cell = lambda w: ("HALT" if w["w"] > kt and ws.get(w["w"], {}).get("n", 0) == 0 else f"{ws[w['w']]['ret_pct']:+.1f} ({ws[w['w']]['n']})")
    p(f"| {f} | " + " | ".join(cell(w) for w in R["mr_dip"]["base"]["windows"]) + " |")
p("")
p("### E.7 Parametri scelti dal walk-forward per finestra")
for f in F:
    p(f"- **{f}**: " + "; ".join(f"{c['w']} {json.dumps(c['params'])}" for c in R[f]["chosen"]))
if D:
    p("")
    p("### E.8 Diagnostica (non decisionale): stesso OOS **senza kill switch**")
    p("| Famiglia | n | exp. netta | PF | Sharpe | max DD | CAGR | % finestre >0 | idem a Tier 3 fisso: exp / PF / Sharpe / DD |"); p("|---|---|---|---|---|---|---|---|---|")
    for f in F:
        if f not in D: continue
        a, b = D[f]["no_kill"], D[f]["no_kill_tier3"]
        p(f"| {f} | {a['n']} | {a['exp_net_pct']:+.2f}% | {a['pf_usd']:.2f} | {a['sharpe']:+.2f} | {pct(a['max_dd'])} | {pct(a['cagr'])} | {pct(a['frac_windows_pos'])} | {b['exp_net_pct']:+.2f}% / {b['pf_usd']:.2f} / {b['sharpe']:+.2f} / {pct(b['max_dd'])} |")
open(os.path.join(ROOT, "out", "wf2_tables.md"), "w").write("\n".join(L))
print("\n".join(L))
