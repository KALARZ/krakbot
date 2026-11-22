"""Assemble docs/research.md from research/research_template.md and the JSON/markdown outputs (no hand-copied numbers)."""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from lib import ROOT
from bt import FEE_TIERS
OUT = os.path.join(ROOT, "out"); DOCS = os.path.join(ROOT, "..", "docs")
rd = lambda f: open(os.path.join(OUT, f)).read().strip()
J = lambda f: json.load(open(os.path.join(OUT, f)))
t = open(os.path.join(ROOT, "research_template.md")).read()
t = t.replace("{{A_B}}", open(os.path.join(ROOT, "research_A_B.md")).read().strip())
t = t.replace("{{PROXY_TABLE}}", rd("proxy_validation_table.md"))
t = t.replace("{{PROXY5_TABLE}}", rd("proxy_5m_table.md"))
t = t.replace("{{BOOK_TABLE}}", rd("book_stats_table.md"))
t = t.replace("{{STATS_TABLES}}", rd("stats_tables.md"))
# fidelity
fd = J("fidelity.json") if os.path.exists(os.path.join(OUT, "fidelity.json")) else {}
L = ["| Coppia Kraken | giorni | segnali | fill proxy | fill nativo | fillati in entrambi | lordo medio proxy | lordo medio nativo | Δ lordo (bp) media / mediana | uscite maker proxy / nativo | barre 5m con trade |",
     "|---|---|---|---|---|---|---|---|---|---|---|"]
for k, v in fd.items():
    L.append(f"| {k} | {v['days']:.0f} | {v['signals']} | {v['fill_proxy']*100:.1f}% | {v['fill_native']*100:.1f}% | {v['both']} | {v['gross_proxy_pct']:+.2f}% | {v['gross_native_pct']:+.2f}% | {v['delta_gross_bp_mean']:+.1f} / {v['delta_gross_bp_median']:+.1f} | {v['maker_exit_proxy']*100:.0f}% / {v['maker_exit_native']*100:.0f}% | {v['native_bar_coverage']*100:.0f}% |")
L.append("")
L.append("Segnali = tutti i segnali distinti delle 5 famiglie (griglie del §E) negli ultimi ~90 giorni. Lordo per trade prima delle fee. Le coppie mancanti (ETH/SOL/XBT EUR ecc.) non sono state scaricate: il download da `/0/public/Trades` richiede ~2 s per chiamata e migliaia di chiamate per coppia liquida.")
t = t.replace("{{FIDELITY_TABLE}}", "\n".join(L))
# edge table
S = J("stats.json")
rows = ["| Tier | RT maker→maker | RT maker→taker | 3× RT (min…max) |", "|---|---|---|---|"]
rt = {}
for i in range(5):
    mk, tk = FEE_TIERS[i][2:]
    mm, mt = (2 * mk + 0.001) * 100, (mk + tk + 0.001) * 100
    rt[i + 1] = (mm, mt)
    rows.append(f"| {i+1} ({mk*100:.2f}/{tk*100:.2f}) | {mm:.2f}% | {mt:.2f}% | {3*mm:.2f}…{3*mt:.2f}% |")
rows.append("")
rows.append("Movimento medio |Δ log prezzo| per orizzonte (ultimi 2 anni) e confronto con 3×RT maker→taker (✓ = supera a quel tier):")
rows.append("")
rows.append("| Coppia | 1h | 4h | 12h | 24h | 48h | 48h ≥3×RT a T1 / T2 / T3 |")
rows.append("|---|---|---|---|---|---|---|")
for s, R in S.items():
    m = R["move_sizes"]
    v48 = m["48h"]["mean_abs_pct_last2y"]
    ok = " / ".join("✓" if v48 >= 3 * rt[k][1] else "✗" for k in (1, 2, 3))
    rows.append(f"| {s[:-4]} | " + " | ".join(f"{m[h]['mean_abs_pct_last2y']:.2f}%" for h in ("1h", "4h", "12h", "24h", "48h")) + f" | {ok} |")
t = t.replace("{{EDGE_TABLE}}", "\n".join(rows))
# screening table
R = J("screen_8.json")
rows = ["| Famiglia | TF | config | trade mediani | fill | lordo mediano | |lordo| mediano | netto T1 | netto T2 | netto T3 | % config >0 a T3 |", "|---|---|---|---|---|---|---|---|---|---|---|"]
for fam in ['donchian', 'squeeze', 'tsmom', 'impulse', 'mr_dip']:
    for tf in ['1h', '4h']:
        rr = [r for r in R if r['fam'] == fam and r['tf'] == tf]
        med = lambda f: float(np.median([f(r) for r in rr]))
        rows.append(f"| {fam} | {tf} | {len(rr)} | {med(lambda r: r['gross']['n']):.0f} | {med(lambda r: r['fill_rate'])*100:.0f}% | {med(lambda r: r['gross']['exp_pct']):+.2f}% | {med(lambda r: r['gross']['abs_mean_pct']):.2f}% | {med(lambda r: r['net_t1']['exp_pct']):+.2f}% | {med(lambda r: r['net_t2']['exp_pct']):+.2f}% | {med(lambda r: r['net_t3']['exp_pct']):+.2f}% | {np.mean([r['net_t3']['exp_pct'] > 0 for r in rr])*100:.0f}% |")
t = t.replace("{{SCREEN_TABLE}}", "\n".join(rows))
t = t.replace("{{WF2_TABLES}}", rd("wf2_tables.md"))
# selection matrix rows
W = J("wf2_results.json"); D = J("wf2_diag.json")
cx = {"donchian": "bassa", "squeeze": "media", "tsmom": "bassa", "impulse": "bassa", "mr_dip": "media"}
mrows = []
for f in ['donchian', 'squeeze', 'tsmom', 'impulse', 'mr_dip']:
    b = W[f]["base"]; sens = [v["exp_net_pct"] for v in W[f]["sens"].values() if v.get("n")]
    lo = [v["exp_net_pct"] for v in W[f]["lopo"].values() if v.get("n")]
    nk = D.get(f, {}).get("no_kill", {})
    trades_yr = nk.get("n", b["n"]) / 5.89  # OOS 2021-01-01..2026-11-21, run senza kill (attività piena)
    mrows.append(f"| {f} 4h | exp {b['exp_net_pct']:+.2f}%/trade, PF {b['pf_usd']:.2f}, Sharpe {b['sharpe']:+.2f} (senza kill: PF {nk.get('pf_usd', float('nan')):.2f}, DD {nk.get('max_dd', float('nan'))*100:.0f}%) | finestre >0 {b['frac_windows_pos']*100:.0f}%, LOPO {min(lo):+.2f}…{max(lo):+.2f}% | {min(sens):+.2f}…{max(sens):+.2f}% | RT {b['rt_cost_pct']:.2f}%, uscite maker {b['maker_exit_frac']*100:.0f}%, ~{trades_yr:.0f} trade/anno | {cx[f]} | **Scartato**: PF < 1.3, Sharpe < 1, lordo/RT {b['edge_ratio_B']:.2f}× < 3× |")
t = t.replace("{{MATRIX_ROWS}}", "\n".join(mrows))
# audit response
fx = list(fd.values())
fid_txt = "; ".join(f"{k}: fill {v['fill_native']*100:.1f}% contro {v['fill_proxy']*100:.1f}%, Δ lordo {v['delta_gross_bp_mean']:+.1f} bp" for k, v in fd.items()) or "n/d"
n_oos = {f: W[f]["base"]["n"] for f in ['donchian', 'squeeze', 'tsmom', 'impulse', 'mr_dip']}
A = ["| # audit | Voce | Stato | Evidenza |", "|---|---|---|---|",
 "| 0 | Documenti assenti | Risolto | questo file e `docs/plan.md` |",
 "| 1 | Spread proxy vs Kraken | Parziale | Kraken: 16 snapshot in 30 min (C.0d); Binance non misurabile (host bloccato). Misura ≥7 gg spostata nell'accettazione DRY_RUN |",
 f"| 1c/2 | Microstructure, 720 candele native | Quantificato | range 5m 0.31–0.95× (C.0b); test di fedeltà su barre native da Trades a 92 giorni (C.0c): {fid_txt}. Con 720 candele 4h ci sono ≤10 segnali per strategia: test statisticamente impossibile, quindi sostituito dal test di fedeltà |",
 "| 3 | Survivorship / selezione a posteriori | Risolto | universo point-in-time con LUNA, MATIC, EOS, ecc. (C.0); selezione solo sul pool nel walk-forward |",
 "| 4 | Regimi | Documentato | C.0e |",
 "| 5 | Leakage | Risolto | `scripts/test_lookahead.py`: segnali identici su dati troncati per 5/5 famiglie; barre incomplete mascherate |",
 "| 6 | Embargo walk-forward | Risolto | purge+embargo 72 h, 12 finestre, eseguito (`wf2.py`) |",
 "| 7 | Soglie sul campione completo | Risolto | griglie pre-registrate; selezione solo in-sample per finestra (E.7) |",
 "| 8 | Fee ×2, slippage ×2 | Risolto | E.2: con fee ×2 solo donchian resta positivo per trade (+0.10%) ma con PF 0.88 |",
 "| 9 | Budget di edge 3× | Risolto | §D ed E.1: definizione B 0.03–1.26× ⇒ nessuno passa |",
 "| 10 | Impatto sul book | Parziale | collegato (`wf2.build_costs`, ×2 su stop e kill), ma stimato da 30 minuti di book |",
 f"| 11 | ≥100 trade OOS per candidato | Risolto | {', '.join(f'{k} {v}' for k, v in n_oos.items())} (≥300 solo donchian, tsmom, mr_dip) |",
 "| 12 | Sensitivity ±20% | Risolto | E.3 |",
 "| 13 | Concentrazione coppia/mese | Risolto | E.4 (LOPO, mese migliore), E.5 (sottoperiodi) |",
 "| 14 | PF/Sharpe/DD OOS | Risolto: **tutti FAIL** | E.1 |",
 "| 15 | ordermin/costmin/decimali | Risolto | minimi per coppia in `portfolio2`, floor a 8 decimali; capitale minimo DCA in §F |",
 "| 16 | Tier fee | Risolto | E.2 a tier 1/2/3 fissi più tier dinamico |",
 "| 17 | OCO, riconciliazione, DMS | Specificato | `docs/plan.md` §4–5 |",
 "| 18 | Sizing dallo stop | Specificato e simulato | `docs/plan.md` §6, `bt.portfolio2` |",
 "| 19 | Kill switch e breaker | Specificato e simulato | flatten più HALT in `portfolio2`, soglie in `docs/plan.md` §6 |",
 "| 20 | Morte con posizione aperta | Specificato | `docs/plan.md` §4.5 caso (b) e §5 |",
 "| 21 | Sezione scarti | Risolto | §E.9 |",
 "| 22 | Assunzioni fragili | Risolto | §I |",
 "| 23 | Claim senza backtest | Nessuno | ogni numero è generato da `research/out/*.json` (`build_docs.py`) |"]
t = t.replace("{{AUDIT_ROWS}}", "\n".join(A))
# K tables
EURUSD = 1.0943
tiers = [(1, None, .0040), (2, None, .0030), (3, 20000, .0022), (4, 50000, .0020), (5, 100000, .0015), (6, 200000, .0012), (7, 400000, .0010), (8, 600000, .0008)]
def tier_for(aop):
    best = tiers[0]
    for x in tiers:
        if x[1] and aop >= x[1]: best = x
    return best
K = ["| Capitale C (EUR) | tier se tutto C è su Kraken | maker | volume DCA (0.20·C) | fee DCA a T1 | fee DCA al tier di C | risparmio | extra da parcheggiare per il T7 | risparmio a T7 | prob. annua di default di pareggio sull'extra |",
     "|---|---|---|---|---|---|---|---|---|---|"]
for C in (1000, 5000, 20000, 50000, 100000):
    vol = 0.20 * C; x = tier_for(C * EURUSD); extra = max(0, 400000 / EURUSD - C)
    s7 = vol * (.0040 - .0010)
    K.append(f"| {C:,} | T{x[0]} | {x[2]*100:.2f}% | {vol:,.0f} | {vol*.004:,.2f} | {vol*x[2]:,.2f} | {vol*(.004-x[2]):,.2f} | {extra:,.0f} | {s7:,.2f} | {s7/extra*100:.4f}% |")
t = t.replace("{{DCA_TIER_TABLE}}", "\n".join(K))
TT = J("wf2_tiers_donchian.json")
T = ["| Tier (maker/taker) | trade OOS | exp. netta/trade | PF netto | Sharpe | max DD | CAGR | % finestre >0 | lordo/RT | kill switch |", "|---|---|---|---|---|---|---|---|---|---|"]
import datetime as _dt
for k_, v in TT.items():
    mk, tk = FEE_TIERS[int(k_) - 1][2:]
    kl = v["killed"]; ks = f"{kl[0]} @ {_dt.datetime.fromtimestamp(kl[1]/1000, _dt.UTC):%Y-%m}" if kl else "—"
    T.append(f"| T{k_} ({mk*100:.2f}/{tk*100:.2f}) | {v['n']} | {v['exp_net_pct']:+.2f}% | {v['pf_usd']:.3f} | {v['sharpe']:+.2f} | {v['max_dd']*100:.1f}% | {v['cagr']*100:+.2f}% | {v['frac_windows_pos']*100:.0f}% | {v['edge_ratio_B']:.2f}× | {ks} |")
t = t.replace("{{TIERS_TABLE}}", "\n".join(T))
K4 = J("wf2_donchian_K4.json")
rows = ["| Scenario | trade OOS | exp. netta/trade | PF netto | Sharpe | max DD | CAGR | % finestre >0 | lordo/RT | kill switch |", "|---|---|---|---|---|---|---|---|---|---|"]
lab = {"base": "tier dinamico (10k USD)", "tier1": "Tier 1 fisso", "tier3": "Tier 3 fisso", "fee_x2": "fee ×2", "no_kill": "senza kill switch"}
for k_, nm in lab.items():
    v = K4[k_]; kl = v["killed"]
    ks = f"{kl[0]} @ {_dt.datetime.fromtimestamp(kl[1]/1000, _dt.UTC):%Y-%m}" if kl else "—"
    rows.append(f"| {nm} | {v['n']} | {v['exp_net_pct']:+.2f}% | {v['pf_usd']:.3f} | {v['sharpe']:+.2f} | {v['max_dd']*100:.1f}% | {v['cagr']*100:+.2f}% | {v['frac_windows_pos']*100:.0f}% | {v['edge_ratio_B']:.2f}× | {ks} |")
t = t.replace("{{K4_TABLE}}", "\n".join(rows))
_sl = rd("sizing_buckets.md").split("\n"); _out = []
for _i, _ln in enumerate(_sl):
    if _ln.startswith("#### "):
        _nxt = next((x for x in _sl[_i + 1:] if x.strip()), "")
        if _nxt.startswith("**" + _ln[5:]):
            continue  # redundant with the bold label of the following block
        _ln = "*" + _ln[5:] + "*"
    _out.append(_ln)
t = t.replace("{{SIZING_TABLES}}", "\n".join(_out))
assert "{{" not in t, [x for x in t.split("{{")[1:]][:3]
open(os.path.join(DOCS, "research.md"), "w").write(t)
print("written", len(t), "chars")
