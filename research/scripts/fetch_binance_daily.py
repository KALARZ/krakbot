"""Fetch Binance daily 5m kline files for Nov 2026 (after the last monthly file) and append to the npz."""
import io, os, sys, zipfile, urllib.request, datetime as dt
import numpy as np
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "binance")
SYMS = sys.argv[1].split(",")
d0, d1 = dt.date(2026, 11, 1), dt.date(2026, 11, 21)
for s in SYMS:
    path = os.path.join(OUT, f"{s}_5m.npz")
    z0 = dict(np.load(path))
    rows = []
    d = d0
    while d <= d1:
        url = f"https://data.binance.vision/data/spot/daily/klines/{s}/5m/{s}-5m-{d.isoformat()}.zip"
        try:
            b = urllib.request.urlopen(url, timeout=60).read()
        except Exception as e:
            print("miss", s, d, e); d += dt.timedelta(days=1); continue
        txt = zipfile.ZipFile(io.BytesIO(b)).read(zipfile.ZipFile(io.BytesIO(b)).namelist()[0]).decode()
        for line in txt.splitlines():
            p = line.split(",")
            if not p[0].isdigit(): continue
            ts = int(p[0]); ts = ts // 1000 if ts > 10**14 else ts
            rows.append((ts, *map(float, (p[1], p[2], p[3], p[4], p[5], p[7], p[8]))))
        d += dt.timedelta(days=1)
    if not rows: continue
    a = np.array(rows)
    keys = ["o", "h", "l", "c", "v", "qv", "n"]
    ts = np.concatenate([z0["ts"], a[:, 0].astype(np.int64)])
    cols = {k: np.concatenate([z0[k], a[:, i + 1]]) for i, k in enumerate(keys)}
    _, idx = np.unique(ts, return_index=True)
    np.savez_compressed(path, ts=ts[idx], **{k: v[idx] for k, v in cols.items()})
    print(s, "now", len(idx), "last", dt.datetime.fromtimestamp(ts[idx][-1] / 1000, dt.UTC))
