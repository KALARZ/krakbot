import os, sys, json, numpy as np
R = json.load(open(sys.argv[1]))
print("rows", len(R))
print("fam      tf  cfgs  n_med fill  gross_med  |g|med  net_t2_med net_t3_med  frac>0_t3  frac>0_t2  rt_t2  rt_t3")
for fam in ['donchian','squeeze','tsmom','impulse','mr_dip']:
    for tf in ['1h','4h']:
        rr=[r for r in R if r['fam']==fam and r['tf']==tf]
        if not rr: continue
        g=np.array([r['gross']['exp_pct'] for r in rr]); n2=np.array([r['net_t2']['exp_pct'] for r in rr]); n3=np.array([r['net_t3']['exp_pct'] for r in rr])
        ab=np.array([r['gross']['abs_mean_pct'] for r in rr]); n=np.array([r['gross']['n'] for r in rr]); fr=np.array([r['fill_rate'] for r in rr])
        rt2=np.median(g-n2); rt3=np.median(g-n3)
        print(f"{fam:8s} {tf} {len(rr):4d} {np.median(n):6.0f} {np.median(fr):.2f}  {np.median(g):+.3f}%   {np.median(ab):.2f}%   {np.median(n2):+.3f}%    {np.median(n3):+.3f}%    {np.mean(n3>0):.2f}      {np.mean(n2>0):.2f}   {rt2:.2f}  {rt3:.2f}")
print()
print("Per pair: median gross exp% (all fams) and best net_t3 config")
for pair in sorted(set(r['pair'] for r in R)):
    rr=[r for r in R if r['pair']==pair]
    g=np.median([r['gross']['exp_pct'] for r in rr])
    print(pair, f"gross_med={g:+.3f}%", "frac net_t3>0:", f"{np.mean([r['net_t3']['exp_pct']>0 for r in rr]):.2f}")
print()
print("Top 25 by net_t3 t-stat with n>=150:")
rr=sorted([r for r in R if r['net_t3']['n']>=150], key=lambda r:-r['net_t3']['t'])[:25]
for r in rr:
    print(f"{r['pair']:9s} {r['tf']} {r['fam']:8s} {json.dumps(r['params']):70s} n={r['net_t3']['n']:4d} g={r['gross']['exp_pct']:+.2f} |g|={r['gross']['abs_mean_pct']:.2f} n3={r['net_t3']['exp_pct']:+.3f} t={r['net_t3']['t']:+.1f} pf={r['net_t3']['pf']:.2f} n2={r['net_t2']['exp_pct']:+.3f} per2={r.get('net_t2_19-21')},{r.get('net_t2_22-23')},{r.get('net_t2_24-26')} hold={r['hold_h']:.0f}h mkx={r['maker_exit_frac']:.2f}")
