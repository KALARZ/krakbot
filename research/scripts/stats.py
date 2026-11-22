"""Empirical market statistics (Phase 0 section C).

For each pair and horizon: autocorrelation (robust t), Lo-MacKinlay variance ratios (robust z*),
DFA Hurst exponent of returns and |returns|, time-series-momentum predictability grid,
regime persistence, extreme-move reversal, hour-of-day seasonality, move size vs cost.
Uses Binance 5m klines as long-history proxy (validated vs Kraken in validate_proxy.py).
"""
import os, sys, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from lib import load5, resample, complete_mask, atr, sma, save_json

SYMS = sys.argv[1].split(",") if len(sys.argv) > 1 else [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "LINKUSDT", "LTCUSDT", "DOGEUSDT", "AVAXUSDT", "DOTUSDT"]
HOR = ["5m", "15m", "1h", "4h", "1d"]
PERIODS = {"2019-2021": (2019, 2022), "2022-2023": (2022, 2024), "2024-2026": (2024, 2027)}

def logret(b, tf):
    c = b["c"]; ok = complete_mask(b, tf)
    r = np.diff(np.log(c)); ok2 = ok[1:] & ok[:-1]
    # also require consecutive timestamps (no gap)
    from lib import MS
    ok2 &= np.diff(b["ts"]) == MS[tf]
    return r[ok2], b["ts"][1:][ok2]

def acf_robust(x, k):
    x = x - x.mean(); a, b = x[k:], x[:-k]
    den = (x * x).sum()
    rho = (a * b).sum() / den
    var = (a * a * b * b).sum() / den ** 2
    return rho, rho / np.sqrt(var)

def vr_robust(x, q):
    x = x - x.mean(); den = (x * x).sum()
    vr, var = 1.0, 0.0
    for k in range(1, q):
        a, b = x[k:], x[:-k]
        rho = (a * b).sum() / den
        w = 2.0 * (q - k) / q
        vr += w * rho
        var += w * w * (a * a * b * b).sum() / den ** 2
    return vr, (vr - 1.0) / np.sqrt(var)

def dfa_hurst(x, smin=16, nscales=12):
    y = np.cumsum(x - x.mean()); N = len(y)
    scales = np.unique(np.logspace(np.log10(smin), np.log10(N // 8), nscales).astype(int))
    F = []
    for s in scales:
        m = N // s
        seg = y[: m * s].reshape(m, s)
        t = np.arange(s)
        A = np.vstack([t, np.ones(s)]).T
        coef, *_ = np.linalg.lstsq(A, seg.T, rcond=None)
        res = seg - (A @ coef).T
        F.append(np.sqrt((res ** 2).mean()))
    return float(np.polyfit(np.log(scales), np.log(F), 1)[0])

def year_of(ts):
    return np.array([dt.datetime.fromtimestamp(t / 1000, dt.UTC).year for t in ts[:: max(1, len(ts) // 5000)]])

def period_mask(ts, y0, y1):
    t0 = dt.datetime(y0, 1, 1, tzinfo=dt.UTC).timestamp() * 1000
    t1 = dt.datetime(y1, 1, 1, tzinfo=dt.UTC).timestamp() * 1000
    return (ts >= t0) & (ts < t1)

def tsmom_grid(b1h):
    """Non-overlapping predictive tests on 1h closes: signal = sign(past L-hour return)."""
    c = b1h["c"]; ts = b1h["ts"]
    lc = np.log(c)
    res = {}
    for L in (4, 12, 24, 72, 168, 336):
        for H in (4, 12, 24, 48):
            idx = np.arange(L, len(c) - H, H)
            # require no gaps over the window
            span_ok = (ts[idx + H] - ts[idx - L]) == (L + H) * 3_600_000
            idx = idx[span_ok]
            past = lc[idx] - lc[idx - L]; fut = lc[idx + H] - lc[idx]
            s = np.sign(past)
            pnl = s * fut
            longonly_edge = fut[past > 0].mean() - fut.mean()
            out = {"n": int(len(idx)), "corr": float(np.corrcoef(past, fut)[0, 1]),
                   "ls_mean_bp": float(pnl.mean() * 1e4), "ls_t": float(pnl.mean() / pnl.std(ddof=1) * np.sqrt(len(pnl))),
                   "long_when_up_bp": float(fut[past > 0].mean() * 1e4), "long_when_down_bp": float(fut[past <= 0].mean() * 1e4),
                   "uncond_bp": float(fut.mean() * 1e4), "longonly_edge_bp": float(longonly_edge * 1e4),
                   "abs_fut_bp": float(np.abs(fut).mean() * 1e4)}
            for pn, (y0, y1) in PERIODS.items():
                m = period_mask(ts[idx], y0, y1)
                if m.sum() > 30:
                    pm = pnl[m]
                    out[f"ls_t_{pn}"] = float(pm.mean() / pm.std(ddof=1) * np.sqrt(m.sum()))
                    out[f"ls_mean_bp_{pn}"] = float(pm.mean() * 1e4)
            res[f"L{L}_H{H}"] = out
    return res

def regimes(b, tf, n_sma):
    c = b["c"]; s = sma(c, n_sma)
    ok = ~np.isnan(s)
    st = (c[ok] > s[ok]).astype(int)
    stay = (st[1:] == st[:-1]).mean()
    # run lengths
    chg = np.flatnonzero(np.diff(st)) + 1
    runs = np.diff(np.concatenate([[0], chg, [len(st)]]))
    return {"p_stay": float(stay), "mean_run_bars": float(runs.mean()), "median_run_bars": float(np.median(runs)),
            "frac_above": float(st.mean())}

def vol_regime(b1h):
    r = np.diff(np.log(b1h["c"]))
    rv = np.sqrt(np.convolve(r * r, np.ones(24), "valid"))  # 24h realised vol
    from lib import rolling_max
    med = np.array([np.nan] * len(rv))
    w = 24 * 90
    # rolling median over 90d, sampled each 24h for speed
    idx = np.arange(w, len(rv), 24)
    hi = np.array([rv[i] > np.median(rv[i - w:i]) for i in idx]).astype(int)
    stay = (hi[1:] == hi[:-1]).mean()
    chg = np.flatnonzero(np.diff(hi)) + 1
    runs = np.diff(np.concatenate([[0], chg, [len(hi)]]))
    # does high vol today predict high vol tomorrow, and returns?
    return {"p_stay_daily": float(stay), "mean_run_days": float(runs.mean())}

def extreme_moves(b1h):
    """Forward returns after extreme 4h moves (z-scored by trailing 30d 1h vol)."""
    lc = np.log(b1h["c"]); ts = b1h["ts"]
    r1 = np.diff(lc)
    out = {}
    W = 24 * 30
    sig = np.sqrt(np.convolve(r1 * r1, np.ones(W), "valid") / W)  # sig[i] uses r1[i..i+W-1]
    for H in (4, 12, 24):
        res = {}
        idx = np.arange(W + 4, len(lc) - H, 4)
        s = sig[idx - 4 - W] if True else None  # vol known before the 4h move window
        s = sig[np.clip(idx - 4 - W + 1, 0, len(sig) - 1)]
        move = (lc[idx] - lc[idx - 4]) / (s * 2.0)  # 4h z-score (sqrt(4)=2)
        fut = lc[idx + H] - lc[idx]
        for z in (-3, -2, 2, 3):
            m = move <= z if z < 0 else move >= z
            if m.sum() >= 20:
                f = fut[m]
                res[f"z{z}"] = {"n": int(m.sum()), "fwd_bp": float(f.mean() * 1e4), "t": float(f.mean() / f.std(ddof=1) * np.sqrt(m.sum())),
                                "hit": float((f > 0).mean()), "uncond_bp": float(fut.mean() * 1e4)}
        out[f"H{H}"] = res
    return out

def hour_of_day(b1h):
    r = np.diff(np.log(b1h["c"])); ts = b1h["ts"][1:]
    hrs = (ts // 3_600_000) % 24
    rows = []
    for h in range(24):
        x = r[hrs == h]
        rows.append({"h": h, "mean_bp": float(x.mean() * 1e4), "t": float(x.mean() / x.std(ddof=1) * np.sqrt(len(x))),
                     "vol_bp": float(x.std() * 1e4)})
    return rows

def move_sizes(b5):
    out = {}
    lc = np.log(b5["c"])
    for name, k in (("1h", 12), ("4h", 48), ("12h", 144), ("24h", 288), ("48h", 576)):
        idx = np.arange(k, len(lc), k)
        d = np.abs(lc[idx] - lc[idx - k])
        recent = b5["ts"][idx] >= dt.datetime(2024, 11, 1, tzinfo=dt.UTC).timestamp() * 1000
        out[name] = {"mean_abs_pct_full": float(d.mean() * 100), "mean_abs_pct_last2y": float(d[recent].mean() * 100),
                     "median_abs_pct_last2y": float(np.median(d[recent]) * 100)}
    return out

def atr_pct(b5):
    out = {}
    for tf in ("15m", "1h", "4h", "1d"):
        b = resample(b5, tf)
        a = atr(b["h"], b["l"], b["c"], 14) / b["c"]
        recent = b["ts"] >= dt.datetime(2024, 11, 1, tzinfo=dt.UTC).timestamp() * 1000
        out[tf] = {"median_atr_pct_full": float(np.nanmedian(a) * 100), "median_atr_pct_last2y": float(np.nanmedian(a[recent]) * 100)}
    return out

results = {}
for s in SYMS:
    b5 = load5(s)
    R = {"first": int(b5["ts"][0]), "last": int(b5["ts"][-1])}
    bars = {tf: resample(b5, tf) for tf in HOR}
    for tf in HOR:
        r, ts = logret(bars[tf], tf)
        st = {"n": int(len(r))}
        for k in (1, 2, 3):
            rho, t = acf_robust(r, k); st[f"acf{k}"] = float(rho); st[f"acf{k}_t"] = float(t)
        for q in (2, 4, 8, 16):
            vr, z = vr_robust(r, q); st[f"vr{q}"] = float(vr); st[f"vr{q}_z"] = float(z)
        st["hurst_dfa_ret"] = dfa_hurst(r)
        st["hurst_dfa_absret"] = dfa_hurst(np.abs(r))
        for pn, (y0, y1) in PERIODS.items():
            m = period_mask(ts, y0, y1)
            if m.sum() > 200:
                rho, t = acf_robust(r[m], 1); st[f"acf1_{pn}"] = float(rho); st[f"acf1_t_{pn}"] = float(t)
                vr, z = vr_robust(r[m], 8); st[f"vr8_{pn}"] = float(vr); st[f"vr8_z_{pn}"] = float(z)
        R[tf] = st
    R["tsmom"] = tsmom_grid(bars["1h"])
    R["regime_4h_sma50"] = regimes(bars["4h"], "4h", 50)
    R["regime_4h_sma200"] = regimes(bars["4h"], "4h", 200)
    R["regime_1d_sma50"] = regimes(bars["1d"], "1d", 50)
    R["vol_regime"] = vol_regime(bars["1h"])
    R["extreme"] = extreme_moves(bars["1h"])
    R["hour_of_day"] = hour_of_day(bars["1h"])
    R["move_sizes"] = move_sizes(b5)
    R["atr_pct"] = atr_pct(b5)
    results[s] = R
    print(s, "done", flush=True)
save_json(results, "stats.json")
