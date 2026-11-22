"""Anti-lookahead test: signals (time, price, stop) emitted up to bar k must be identical whether the
strategy sees data up to bar k or the full series. Run: python3 -I scripts/test_lookahead.py"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from lib import load5, resample
import strategies as S
CASES = [("donchian", {"N": 20, "k_sl": 2.0, "k_tr": 3.0, "regime": 200}), ("squeeze", {"pct": 0.2}),
         ("tsmom", {"L": 42, "thr": 1.0}), ("impulse", {"n": 1, "z0": 2.0}), ("mr_dip", {"n": 1, "z0": 2.0})]
b = resample(load5("SOLUSDT"), "4h")
n = len(b["c"]); fails = 0
for fam, p in CASES:
    full = S.FAMILIES[fam](b, "4h", **p)
    for k in (n // 3, n // 2, (3 * n) // 4, n - 7):
        part = {x: v[:k] for x, v in b.items()}
        cut = int(b["ts"][k - 1] + 4 * 3_600_000)
        a = [(t, round(px, 10), round(st, 10)) for t, px, st, _ in full if t <= cut]
        c = [(t, round(px, 10), round(st, 10)) for t, px, st, _ in S.FAMILIES[fam](part, "4h", **p)]
        if a != c:
            fails += 1; print("LOOKAHEAD", fam, k, len(a), len(c))
    print(fam, "ok" if fails == 0 else "FAIL", len(full), "signals")
sys.exit(1 if fails else 0)
