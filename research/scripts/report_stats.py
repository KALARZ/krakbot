"""Render out/stats.json into markdown tables (out/stats_tables.md)."""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import ROOT
S = json.load(open(os.path.join(ROOT, "out", "stats.json")))
L = []
def p(x=""): L.append(x)
short = lambda s: s.replace("USDT", "")
p("### C.1 Autocorrelazione lag-1 (t robusto) e variance ratio VR(8) (z* robusto), campione intero")
p("| Coppia | " + " | ".join(f"ρ1 {tf}" for tf in ("5m","15m","1h","4h","1d")) + " | " + " | ".join(f"VR8 {tf}" for tf in ("5m","1h","4h","1d")) + " |")
p("|---|" + "---|" * 9)
for s, R in S.items():
    a = " | ".join(f"{R[tf]['acf1']:+.3f} ({R[tf]['acf1_t']:+.1f})" for tf in ("5m","15m","1h","4h","1d"))
    v = " | ".join(f"{R[tf]['vr8']:.2f} ({R[tf]['vr8_z']:+.1f})" for tf in ("5m","1h","4h","1d"))
    p(f"| {short(s)} | {a} | {v} |")
p()
p("### C.2 ρ1 per sottoperiodo (t robusto)")
p("| Coppia | TF | 2019-2021 | 2022-2023 | 2024-2026 |"); p("|---|---|---|---|---|")
for s, R in S.items():
    for tf in ("5m", "1h", "4h"):
        cells = []
        for pn in ("2019-2021", "2022-2023", "2024-2026"):
            k = f"acf1_{pn}"
            cells.append(f"{R[tf][k]:+.3f} ({R[tf]['acf1_t_'+pn]:+.1f})" if k in R[tf] else "n/a")
        p(f"| {short(s)} | {tf} | " + " | ".join(cells) + " |")
p()
p("### C.3 Esponente di Hurst (DFA) su rendimenti e |rendimenti|")
p("| Coppia | " + " | ".join(f"H {tf}" for tf in ("5m","1h","4h","1d")) + " | " + " | ".join(f"H|r| {tf}" for tf in ("5m","1h","4h")) + " |")
p("|---|" + "---|" * 7)
for s, R in S.items():
    p(f"| {short(s)} | " + " | ".join(f"{R[tf]['hurst_dfa_ret']:.3f}" for tf in ("5m","1h","4h","1d")) + " | " + " | ".join(f"{R[tf]['hurst_dfa_absret']:.2f}" for tf in ("5m","1h","4h")) + " |")
p()
p("### C.4 Time-series momentum (test non sovrapposti su chiusure 1h): t-stat della strategia sign(rend. passato L) × rend. futuro H")
keys = [f"L{L}_H{H}" for L in (12, 24, 72, 168, 336) for H in (12, 24, 48)]
p("| Coppia | " + " | ".join(k for k in keys) + " |"); p("|---|" + "---|" * len(keys))
for s, R in S.items():
    p(f"| {short(s)} | " + " | ".join(f"{R['tsmom'][k]['ls_t']:+.1f}" for k in keys) + " |")
p()
p("### C.5 TSMOM long-only: rendimento medio futuro (bp) quando il passato è positivo vs negativo, L=168h (7g), per H")
p("| Coppia | H=12h up/down | H=24h up/down | H=48h up/down | t L168H48 per periodo 19-21 / 22-23 / 24-26 |"); p("|---|---|---|---|---|")
for s, R in S.items():
    T = R["tsmom"]
    cells = [f"{T[f'L168_H{H}']['long_when_up_bp']:+.0f} / {T[f'L168_H{H}']['long_when_down_bp']:+.0f}" for H in (12, 24, 48)]
    per = " / ".join(f"{T['L168_H48'].get('ls_t_'+pn, float('nan')):+.1f}" for pn in ("2019-2021","2022-2023","2024-2026"))
    p(f"| {short(s)} | " + " | ".join(cells) + f" | {per} |")
p()
p("### C.6 Persistenza dei regimi")
p("| Coppia | 4h close>SMA50: P(stay), durata media (barre) | 4h close>SMA200: P(stay), durata | 1d >SMA50: P(stay), durata (giorni) | Vol 24h > mediana 90g: P(stay giornaliera), durata (gg) |"); p("|---|---|---|---|---|")
for s, R in S.items():
    a, b, c, v = R["regime_4h_sma50"], R["regime_4h_sma200"], R["regime_1d_sma50"], R["vol_regime"]
    p(f"| {short(s)} | {a['p_stay']:.3f}, {a['mean_run_bars']:.1f} | {b['p_stay']:.3f}, {b['mean_run_bars']:.1f} | {c['p_stay']:.3f}, {c['mean_run_bars']:.1f} | {v['p_stay_daily']:.3f}, {v['mean_run_days']:.1f} |")
p()
p("### C.7 Movimenti estremi a 4h (z-score su vol 30g): rendimento successivo medio in bp (t), H=12h e H=24h")
p("| Coppia | z≤-3 H12 | z≤-2 H12 | z≥+2 H12 | z≥+3 H12 | z≤-3 H24 | z≤-2 H24 | z≥+2 H24 | uncond H24 |"); p("|---|---|---|---|---|---|---|---|---|")
for s, R in S.items():
    E = R["extreme"]
    def c(H, z):
        x = E[f"H{H}"].get(f"z{z}"); return f"{x['fwd_bp']:+.0f} ({x['t']:+.1f}, n={x['n']})" if x else "n/a"
    unc = E["H24"].get("z-2", {}).get("uncond_bp", float("nan"))
    p(f"| {short(s)} | {c(12,-3)} | {c(12,-2)} | {c(12,2)} | {c(12,3)} | {c(24,-3)} | {c(24,-2)} | {c(24,2)} | {unc:+.1f} |")
p()
p("### C.8 Stagionalità oraria (UTC): ore con |t|>3 sul rendimento medio 1h")
for s, R in S.items():
    hits = [f"{h['h']:02d}h {h['mean_bp']:+.1f}bp (t={h['t']:+.1f})" for h in R["hour_of_day"] if abs(h["t"]) > 3]
    p(f"- {short(s)}: " + (", ".join(hits) if hits else "nessuna"))
p()
p("### C.9 Ampiezza dei movimenti vs costi: |Δlog prezzo| medio (%) e ATR14 mediano (%) — ultimi 2 anni")
p("| Coppia | E|r| 1h | 4h | 12h | 24h | 48h | ATR% 15m | 1h | 4h | 1d |"); p("|---|---|---|---|---|---|---|---|---|---|")
for s, R in S.items():
    m, a = R["move_sizes"], R["atr_pct"]
    p(f"| {short(s)} | " + " | ".join(f"{m[k]['mean_abs_pct_last2y']:.2f}" for k in ("1h","4h","12h","24h","48h")) + " | " + " | ".join(f"{a[k]['median_atr_pct_last2y']:.2f}" for k in ("15m","1h","4h","1d")) + " |")
open(os.path.join(ROOT, "out", "stats_tables.md"), "w").write("\n".join(L))
print("\n".join(L))
