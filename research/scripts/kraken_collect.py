"""Collect Kraken public market data for the study.

mode ohlc : last 720 candles for every interval/pair (validation of the Binance proxy, recent regime)
mode book : periodic /0/public/Depth snapshots -> spread & depth statistics (JSONL)
All timestamps UTC. Public endpoints only, ~1 req/s.
"""
import json, os, sys, time, urllib.request, urllib.parse

API = "https://api.kraken.com/0/public/"
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "kraken")
PAIRS = ["XBTEUR", "XBTUSD", "ETHEUR", "ETHUSD", "SOLEUR", "SOLUSD", "XRPEUR", "XRPUSD",
         "ADAEUR", "LINKEUR", "LTCEUR", "XDGEUR", "AVAXEUR", "DOTEUR", "XBTUSDC", "ETHUSDC"]

def get(ep, **params):
    url = API + ep + "?" + urllib.parse.urlencode(params)
    for i in range(5):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                d = json.loads(r.read())
            if d.get("error"):
                raise RuntimeError(d["error"])
            return d["result"]
        except Exception as e:
            err = e
            time.sleep(2 ** i)
    raise RuntimeError(f"{url}: {err}")

def ohlc():
    os.makedirs(OUT, exist_ok=True)
    for p in PAIRS:
        for iv in (5, 15, 60, 240, 1440):
            try:
                r = get("OHLC", pair=p, interval=iv)
            except Exception as e:
                print("ERR", p, iv, e, flush=True); continue
            key = [k for k in r if k != "last"][0]
            json.dump(r[key], open(os.path.join(OUT, f"ohlc_{p}_{iv}.json"), "w"))
            print(p, iv, len(r[key]), flush=True)
            time.sleep(1.1)

def book(duration_s, period_s):
    os.makedirs(OUT, exist_ok=True)
    f = open(os.path.join(OUT, "book_snapshots.jsonl"), "a")
    t_end = time.time() + duration_s
    while time.time() < t_end:
        t0 = time.time()
        for p in PAIRS:
            try:
                r = get("Depth", pair=p, count=200)
            except Exception as e:
                print("ERR", p, e, flush=True); continue
            key = list(r)[0]
            bids = [(float(x[0]), float(x[1])) for x in r[key]["bids"]]
            asks = [(float(x[0]), float(x[1])) for x in r[key]["asks"]]
            f.write(json.dumps({"t": time.time(), "pair": p, "bids": bids, "asks": asks}) + "\n")
            f.flush()
            time.sleep(1.1)
        time.sleep(max(0, period_s - (time.time() - t0)))

if __name__ == "__main__":
    if sys.argv[1] == "ohlc":
        ohlc()
    else:
        book(float(sys.argv[2]), float(sys.argv[3]))
