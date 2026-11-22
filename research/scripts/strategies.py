"""Candidate strategy families (long-only, spot). Each returns a list of entry signals on CLOSED bars:
(t_close_ms, close, stop0, manager). manager(t_close, close, high_since_entry, bars_since, stop)
-> (new_stop or None, exit_now, reason). Indicators at bar i use data up to and including bar i only."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from lib import MS, atr, sma, ema, rolling_max, rolling_min, rolling_std

def _complete(b, tf):
    n = b.get("nbar")
    return np.ones(len(b["c"]), bool) if n is None else (n == MS[tf] // MS["5m"])

def _idx_of_close(b, tfms):
    ts = b["ts"]
    def f(t_close):
        k = np.searchsorted(ts, t_close - tfms)
        return k if k < len(ts) and ts[k] == t_close - tfms else None
    return f

def _trail_manager(b, tfms, A, k_tr, exit_fn=None):
    c = b["c"]; idx = _idx_of_close(b, tfms)
    def m(t_close, close, hi, bars_since, stop):
        i = idx(t_close)
        if i is None or np.isnan(A[i]):
            return None, False, ""
        new = (hi - k_tr * A[i]) if k_tr else None  # chandelier on highest high since entry
        if exit_fn is not None and exit_fn(i):
            return new, True, "signal"
        return new, False, ""
    return m

def donchian(b, tf, N=20, k_sl=2.0, k_tr=3.0, regime=200, exitN=None):
    tfms = MS[tf]; c, h, l = b["c"], b["h"], b["l"]
    A = atr(h, l, c, 14)
    up = np.concatenate([[np.nan], rolling_max(h, N)[:-1]])  # prior N bars
    reg = sma(c, regime) if regime else np.full(len(c), -np.inf)
    lo = np.concatenate([[np.nan], rolling_min(l, exitN)[:-1]]) if exitN else None
    sig = (c > up) & ~(np.concatenate([[False], (c > up)[:-1]])) & (c > reg) & ~np.isnan(A) & _complete(b, tf)
    ex = (lambda i: c[i] < lo[i]) if exitN else None
    mgr = _trail_manager(b, tfms, A, k_tr, ex)
    return [(int(b["ts"][i] + tfms), float(c[i]), float(c[i] - k_sl * A[i]), mgr) for i in np.flatnonzero(sig)]

def squeeze(b, tf, n=20, k_bb=2.0, look=120, pct=0.2, k_sl=2.0, k_tr=3.0, regime=200):
    tfms = MS[tf]; c, h, l = b["c"], b["h"], b["l"]
    A = atr(h, l, c, 14)
    mid = sma(c, n); sd = rolling_std(c, n)
    upper = mid + k_bb * sd
    width = (2 * k_bb * sd) / mid
    from numpy.lib.stride_tricks import sliding_window_view
    rank = np.full(len(c), np.nan)
    w = sliding_window_view(np.nan_to_num(width, nan=np.inf), look)
    rank[look - 1:] = (w < w[:, -1:]).mean(axis=1)
    recent_sq = np.zeros(len(c), bool)
    for k in range(1, 6):
        recent_sq[k:] |= (rank[:-k] <= pct)
    reg = sma(c, regime) if regime else np.full(len(c), -np.inf)
    brk = c > upper
    sig = brk & ~np.concatenate([[False], brk[:-1]]) & recent_sq & (c > reg) & ~np.isnan(A) & _complete(b, tf)
    mgr = _trail_manager(b, tfms, A, k_tr)
    return [(int(b["ts"][i] + tfms), float(c[i]), float(c[i] - k_sl * A[i]), mgr) for i in np.flatnonzero(sig)]

def tsmom(b, tf, L=168, thr=1.0, k_sl=2.5, k_tr=0.0, regime=0):
    """Fresh crossing of the vol-scaled L-bar return above thr; time exit at max hold (48h) or trailing."""
    tfms = MS[tf]; c, h, l = b["c"], b["h"], b["l"]
    A = atr(h, l, c, 14)
    lc = np.log(c); r1 = np.concatenate([[0], np.diff(lc)])
    sig1 = rolling_std(r1, 24 * 30 if tf == "1h" else 6 * 30)
    z = np.full(len(c), np.nan); z[L:] = (lc[L:] - lc[:-L]) / (sig1[L:] * np.sqrt(L))
    reg = sma(c, regime) if regime else np.full(len(c), -np.inf)
    above = z > thr
    sig = above & ~np.concatenate([[False], above[:-1]]) & (c > reg) & ~np.isnan(A) & _complete(b, tf)
    ex = lambda i: z[i] < 0  # momentum gone
    mgr = _trail_manager(b, tfms, A, k_tr, ex)
    return [(int(b["ts"][i] + tfms), float(c[i]), float(c[i] - k_sl * A[i]), mgr) for i in np.flatnonzero(sig)]

def impulse(b, tf, n=4, z0=2.0, k_sl=2.0, k_tr=3.0, regime=0):
    """Continuation after an extreme n-bar up-move (z-score vs trailing 30d vol)."""
    tfms = MS[tf]; c, h, l = b["c"], b["h"], b["l"]
    A = atr(h, l, c, 14)
    lc = np.log(c); r1 = np.concatenate([[0], np.diff(lc)])
    W = 24 * 30 if tf == "1h" else 6 * 30
    s1 = np.concatenate([[np.nan], rolling_std(r1, W)[:-1]])
    z = np.full(len(c), np.nan); z[n:] = (lc[n:] - lc[:-n]) / (s1[n:] * np.sqrt(n))
    reg = sma(c, regime) if regime else np.full(len(c), -np.inf)
    hit = z >= z0
    sig = hit & ~np.concatenate([[False], hit[:-1]]) & (c > reg) & ~np.isnan(A) & _complete(b, tf)
    mgr = _trail_manager(b, tfms, A, k_tr)
    return [(int(b["ts"][i] + tfms), float(c[i]), float(c[i] - k_sl * A[i]), mgr) for i in np.flatnonzero(sig)]

def mr_dip(b, tf, n=4, z0=2.5, k_sl=2.0, exit_ma=20, regime=200):
    """Buy an extreme n-bar down-move inside an uptrend regime; exit on reversion to EMA(exit_ma)."""
    tfms = MS[tf]; c, h, l = b["c"], b["h"], b["l"]
    A = atr(h, l, c, 14)
    lc = np.log(c); r1 = np.concatenate([[0], np.diff(lc)])
    W = 24 * 30 if tf == "1h" else 6 * 30
    s1 = np.concatenate([[np.nan], rolling_std(r1, W)[:-1]])
    z = np.full(len(c), np.nan); z[n:] = (lc[n:] - lc[:-n]) / (s1[n:] * np.sqrt(n))
    reg = sma(c, regime) if regime else np.full(len(c), -np.inf)
    e = ema(c, exit_ma)
    hit = z <= -z0
    sig = hit & ~np.concatenate([[False], hit[:-1]]) & (c > reg) & ~np.isnan(A) & _complete(b, tf)
    mgr = _trail_manager(b, tfms, A, 0.0, lambda i: c[i] >= e[i])
    return [(int(b["ts"][i] + tfms), float(c[i]), float(c[i] - k_sl * A[i]), mgr) for i in np.flatnonzero(sig)]

FAMILIES = {"donchian": donchian, "squeeze": squeeze, "tsmom": tsmom, "impulse": impulse, "mr_dip": mr_dip}
