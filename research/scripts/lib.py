"""Shared helpers for the research scripts (numpy only, float64 is fine for research;
the production bot uses Decimal for prices/quantities)."""
import os, json
import numpy as np

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DATA = os.path.join(ROOT, "data")
MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}

def load5(sym):
    z = np.load(os.path.join(DATA, "binance", f"{sym}_5m.npz"))
    return {k: z[k] for k in z.files}

def resample(d, tf):
    """Aggregate 5m bars into UTC-aligned bars of timeframe tf. Returns dict of arrays.
    Bars with no 5m data are absent (gaps preserved, not forward-filled)."""
    if tf == "5m":
        return dict(d)
    step = MS[tf]
    key = d["ts"] // step
    brk = np.flatnonzero(np.diff(key)) + 1
    starts = np.concatenate([[0], brk])
    ends = np.concatenate([brk, [len(key)]]) - 1
    out = {
        "ts": key[starts] * step,
        "o": d["o"][starts],
        "h": np.maximum.reduceat(d["h"], starts),
        "l": np.minimum.reduceat(d["l"], starts),
        "c": d["c"][ends],
        "v": np.add.reduceat(d["v"], starts),
        "qv": np.add.reduceat(d["qv"], starts),
        "nbar": np.diff(np.concatenate([starts, [len(key)]])),
    }
    return out

def complete_mask(b, tf):
    """True for bars built from the full number of 5m sub-bars."""
    return b.get("nbar", np.full(len(b["ts"]), MS[tf] // 300_000)) == MS[tf] // 300_000

def atr(h, l, c, n=14):
    pc = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    out = np.full_like(tr, np.nan)
    if len(tr) < n:
        return out
    out[n - 1] = tr[:n].mean()
    a = 1.0 / n
    for i in range(n, len(tr)):  # Wilder smoothing
        out[i] = out[i - 1] + a * (tr[i] - out[i - 1])
    return out

def sma(x, n):
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        cs = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out

def ema(x, n):
    out = np.empty(len(x)); a = 2.0 / (n + 1); out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = out[i - 1] + a * (x[i] - out[i - 1])
    return out

def rolling_max(x, n):
    from numpy.lib.stride_tricks import sliding_window_view
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        out[n - 1:] = sliding_window_view(x, n).max(axis=1)
    return out

def rolling_min(x, n):
    from numpy.lib.stride_tricks import sliding_window_view
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        out[n - 1:] = sliding_window_view(x, n).min(axis=1)
    return out

def rolling_std(x, n):
    from numpy.lib.stride_tricks import sliding_window_view
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        out[n - 1:] = sliding_window_view(x, n).std(axis=1, ddof=1)
    return out

def save_json(obj, name):
    os.makedirs(os.path.join(ROOT, "out"), exist_ok=True)
    with open(os.path.join(ROOT, "out", name), "w") as f:
        json.dump(obj, f, indent=1, default=float)
