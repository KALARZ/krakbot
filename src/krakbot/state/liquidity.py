"""Book sampling, daily spread/depth percentiles and the pre-registered threshold derivation (plan §6).

Percentile definition (the only one used anywhere): nearest-rank. For n sorted samples, the p-th percentile
(0 < p <= 100) is the sample at 1-based rank ceil(p * n / 100).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from krakbot.config import Liquidity
from krakbot.numeric import D, from_db
from krakbot.state.repo import tx

__all__ = ["LiquidityRepo", "PairThreshold", "nearest_rank", "spread_bp"]

DAY_MS = 86_400_000
_BP_Q = Decimal("0.0001")


def nearest_rank(sorted_values: Sequence[Decimal], p: int) -> Decimal:
    if not 0 < p <= 100:
        raise ValueError("percentile must be in (0, 100]")
    n = len(sorted_values)
    if n == 0:
        raise ValueError("no samples")
    rank = -(-p * n // 100)  # ceil(p*n/100) in integers
    return sorted_values[rank - 1]


def spread_bp(bid: Decimal, ask: Decimal) -> Decimal:
    if bid <= 0 or ask < bid:
        raise ValueError(f"invalid book top bid={bid} ask={ask}")
    mid = (bid + ask) / 2
    return ((ask - bid) / mid * 10_000).quantize(_BP_Q, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class PairThreshold:
    pair: str
    n_samples: int
    coverage: Decimal
    eligible: bool
    spread_bp_max: Decimal | None
    spread_bp_pause: Decimal | None
    bid_depth_min: Decimal | None
    ask_depth_min: Decimal | None

    def hash_view(self) -> dict[str, str]:
        """What goes into the forward-test freeze hash (strings, canonical)."""
        return {"eligible": str(int(self.eligible)), "spread_bp_max": str(self.spread_bp_max),
                "bid_depth_min": str(self.bid_depth_min), "ask_depth_min": str(self.ask_depth_min)}


class LiquidityRepo:
    def __init__(self, conn: sqlite3.Connection, now_ms: Callable[[], int]) -> None:
        self._c, self._now = conn, now_ms

    def add_sample(self, pair: str, ts: int, *, mid: Decimal, spread: Decimal, bid_depth: Decimal,
                   ask_depth: Decimal) -> bool:
        cur = self._c.execute(
            "INSERT OR IGNORE INTO book_samples(pair, ts, mid, spread_bp, bid_depth, ask_depth) VALUES (?,?,?,?,?,?)",
            (pair, ts, mid, spread, bid_depth, ask_depth))
        return cur.rowcount == 1

    def _samples(self, pair: str, start: int, end: int) -> tuple[list[Decimal], list[Decimal], list[Decimal]]:
        rows = self._c.execute("SELECT spread_bp, bid_depth, ask_depth FROM book_samples WHERE pair = ? AND ts >= ? "
                               "AND ts < ?", (pair, start, end)).fetchall()
        return (sorted(D(r[0]) for r in rows), sorted(D(r[1]) for r in rows), sorted(D(r[2]) for r in rows))

    def rollup_day(self, pair: str, day: int) -> dict[str, Decimal | int | None]:
        s, b, a = self._samples(pair, day * DAY_MS, (day + 1) * DAY_MS)
        n = len(s)
        out: dict[str, Decimal | int | None] = {"n": n}
        for p in (50, 90, 95, 99):
            out[f"spread_p{p}"] = nearest_rank(s, p) if n else None
        out["spread_max"] = s[-1] if n else None
        for name, vals in (("bid_depth", b), ("ask_depth", a)):
            out[f"{name}_p5"] = nearest_rank(vals, 5) if n else None
            out[f"{name}_p50"] = nearest_rank(vals, 50) if n else None
        self._c.execute(
            "INSERT INTO spread_daily(pair, day, n, spread_p50, spread_p90, spread_p95, spread_p99, spread_max, "
            "bid_depth_p5, bid_depth_p50, ask_depth_p5, ask_depth_p50, computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(pair, day) DO UPDATE SET n=excluded.n, spread_p50=excluded.spread_p50, "
            "spread_p90=excluded.spread_p90, spread_p95=excluded.spread_p95, spread_p99=excluded.spread_p99, "
            "spread_max=excluded.spread_max, bid_depth_p5=excluded.bid_depth_p5, bid_depth_p50=excluded.bid_depth_p50, "
            "ask_depth_p5=excluded.ask_depth_p5, ask_depth_p50=excluded.ask_depth_p50, "
            "computed_at=excluded.computed_at",
            (pair, day, n, out["spread_p50"], out["spread_p90"], out["spread_p95"], out["spread_p99"],
             out["spread_max"], out["bid_depth_p5"], out["bid_depth_p50"], out["ask_depth_p5"], out["ask_depth_p50"],
             self._now()))
        return out

    def derive(self, set_id: str, pairs: Sequence[str], ref_start: int, ref_end: int, *, rules: Liquidity,
               pause_percentile: int) -> dict[str, PairThreshold]:
        """Pre-registered derivation over [ref_start, ref_end) (the 7 clean DRY_RUN days):
        spread_bp_max = p{spread_percentile} of the spread samples; spread_bp_pause (DCA) = p{pause_percentile};
        bid/ask_depth_min = p{depth_percentile} of the depth samples; a pair with coverage < min_sample_coverage
        (samples / expected samples at `sample_seconds`) is not eligible and gets no thresholds."""
        if ref_end - ref_start < rules.reference_days * DAY_MS:
            raise ValueError(f"reference window shorter than {rules.reference_days} days")
        expected = (ref_end - ref_start) // (rules.sample_seconds * 1000)
        out: dict[str, PairThreshold] = {}
        rule = {"spread_percentile": rules.spread_percentile, "depth_percentile": rules.depth_percentile,
                "pause_percentile": pause_percentile, "sample_seconds": rules.sample_seconds,
                "min_sample_coverage": str(rules.min_sample_coverage), "depth_bps": rules.depth_bps,
                "percentile_method": "nearest_rank"}
        with tx(self._c):
            self._c.execute("INSERT INTO liquidity_threshold_sets(set_id, ref_start, ref_end, rule_json, derived_at) "
                            "VALUES (?,?,?,?,?)",
                            (set_id, ref_start, ref_end, json.dumps(rule, sort_keys=True), self._now()))
            for pair in pairs:
                s, b, a = self._samples(pair, ref_start, ref_end)
                n = len(s)
                coverage = (Decimal(n) / Decimal(expected)).quantize(_BP_Q) if expected else Decimal(0)
                ok = n > 0 and coverage >= rules.min_sample_coverage
                th = PairThreshold(
                    pair, n, coverage, ok,
                    nearest_rank(s, rules.spread_percentile) if ok else None,
                    nearest_rank(s, pause_percentile) if ok else None,
                    nearest_rank(b, rules.depth_percentile) if ok else None,
                    nearest_rank(a, rules.depth_percentile) if ok else None)
                self._c.execute(
                    "INSERT INTO liquidity_thresholds(set_id, pair, n_samples, coverage, eligible, spread_bp_max, "
                    "spread_bp_pause, bid_depth_min, ask_depth_min) VALUES (?,?,?,?,?,?,?,?,?)",
                    (set_id, pair, n, coverage, int(ok), th.spread_bp_max, th.spread_bp_pause, th.bid_depth_min,
                     th.ask_depth_min))
                out[pair] = th
        return out

    def latest_set_id(self) -> str | None:
        r = self._c.execute("SELECT set_id FROM liquidity_threshold_sets ORDER BY derived_at DESC, rowid DESC "
                            "LIMIT 1").fetchone()
        return None if r is None else str(r[0])

    def thresholds(self, set_id: str) -> dict[str, PairThreshold]:
        out = {}
        for r in self._c.execute("SELECT * FROM liquidity_thresholds WHERE set_id = ?", (set_id,)):
            out[r["pair"]] = PairThreshold(r["pair"], r["n_samples"], D(r["coverage"]), bool(r["eligible"]),
                                           from_db(r["spread_bp_max"]), from_db(r["spread_bp_pause"]),
                                           from_db(r["bid_depth_min"]), from_db(r["ask_depth_min"]))
        return out

    def hash_payload(self, set_id: str) -> dict[str, dict[str, str]]:
        return {p: t.hash_view() for p, t in self.thresholds(set_id).items()}
