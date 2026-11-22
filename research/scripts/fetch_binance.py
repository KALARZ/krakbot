"""Download Binance spot 5m klines (monthly zips) from data.binance.vision.

Used ONLY as long-history price proxy for statistics/backtests (Kraken /0/public/OHLC
returns only the latest 720 candles). Cross-validated against Kraken OHLC in validate_proxy.py.
Output: research/data/binance/<SYMBOL>_5m.npz with int64 ts (ms, UTC open time) and float64 o,h,l,c,v,qv,n.
"""
import io, os, sys, zipfile, time, urllib.request, datetime as dt
import numpy as np

BASE = "https://data.binance.vision/data/spot/monthly/klines/{s}/5m/{s}-5m-{y}-{m:02d}.zip"
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "binance")
SYMS = sys.argv[1].split(",") if len(sys.argv) > 1 else [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "LINKUSDT", "LTCUSDT", "DOGEUSDT", "AVAXUSDT", "DOTUSDT"]
START = (2019, 1)
END = (2026, 10)

def months():
    y, m = START
    while (y, m) <= END:
        yield y, m
        m += 1
        if m == 13:
            y, m = y + 1, 1

def fetch(url, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(2 ** i)
        except Exception:
            time.sleep(2 ** i)
    raise RuntimeError("failed " + url)

os.makedirs(OUT, exist_ok=True)
for s in SYMS:
    path = os.path.join(OUT, f"{s}_5m.npz")
    if os.path.exists(path):
        print("skip", s); continue
    rows = []
    for y, m in months():
        b = fetch(BASE.format(s=s, y=y, m=m))
        if b is None:
            continue
        z = zipfile.ZipFile(io.BytesIO(b))
        txt = z.read(z.namelist()[0]).decode()
        for line in txt.splitlines():
            p = line.split(",")
            if not p[0].isdigit():
                continue  # header
            ts = int(p[0])
            if ts > 10**14:  # Binance switched spot files to microseconds in 2025
                ts //= 1000
            rows.append((ts, float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5]), float(p[7]), float(p[8])))
    a = np.array(rows, dtype=np.float64)
    a = a[np.argsort(a[:, 0], kind="stable")]
    _, idx = np.unique(a[:, 0], return_index=True)
    a = a[idx]
    np.savez_compressed(path, ts=a[:, 0].astype(np.int64), o=a[:, 1], h=a[:, 2], l=a[:, 3], c=a[:, 4], v=a[:, 5], qv=a[:, 6], n=a[:, 7])
    print(s, len(a), dt.datetime.utcfromtimestamp(a[0, 0] / 1000), dt.datetime.utcfromtimestamp(a[-1, 0] / 1000), flush=True)
