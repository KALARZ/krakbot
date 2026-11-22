"""Rebuild native Kraken 5m OHLC from public /0/public/Trades (paginated by `last`, ns), for execution-fidelity
tests (proxy vs native). Writes data/kraken/trades5m_<PAIR>.npz incrementally per pair. ~1 req / 1.2 s."""
import json, os, sys, time, urllib.request, urllib.parse
import numpy as np
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "kraken")
PAIRS = sys.argv[1].split(",")
DAYS = float(sys.argv[2])
def get(params):
    url = "https://api.kraken.com/0/public/Trades?" + urllib.parse.urlencode(params)
    for i in range(6):
        try:
            d = json.loads(urllib.request.urlopen(url, timeout=30).read())
            if d.get("error"):
                if any("Too many" in e or "Rate" in e for e in d["error"]):
                    time.sleep(5 * (i + 1)); continue
                raise RuntimeError(d["error"])
            return d["result"]
        except Exception as e:
            err = e; time.sleep(2 ** i)
    raise RuntimeError(str(err))
for pair in PAIRS:
    since = int((time.time() - DAYS * 86400) * 1e9)
    buck = {}  # 5m bucket -> [o,h,l,c,v,n,first_t]
    calls = 0
    while True:
        r = get({"pair": pair, "since": since, "count": 1000}); calls += 1
        key = [k for k in r if k != "last"][0]
        rows = r[key]
        for p, v, t, *_ in rows:
            p = float(p); v = float(v); b = int(t // 300) * 300
            x = buck.get(b)
            if x is None: buck[b] = [p, p, p, p, v, 1]
            else:
                x[1] = max(x[1], p); x[2] = min(x[2], p); x[3] = p; x[4] += v; x[5] += 1
        last = int(r["last"])
        if not rows or last <= since or (time.time() * 1e9 - last) < 60e9:
            break
        since = last
        time.sleep(1.2)
    ks = sorted(buck)
    a = np.array([buck[k] for k in ks])
    np.savez_compressed(os.path.join(OUT, f"trades5m_{pair}.npz"), ts=np.array(ks, np.int64) * 1000,
                        o=a[:, 0], h=a[:, 1], l=a[:, 2], c=a[:, 3], v=a[:, 4], n=a[:, 5])
    print(pair, "calls", calls, "bars", len(ks), flush=True)
