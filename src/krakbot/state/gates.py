"""Live gate for the DCA strategy: real orders only after N consecutive *clean* UTC days of DRY_RUN (user rule:
"DCA in live solo dopo i 7 giorni di DRY_RUN puliti"). Evaluated by the CLI before `run --live`.

A UTC day is clean when ALL of the following hold:
  1. heartbeat coverage >= min_coverage (healthy main-loop minutes / 1440);
  2. no unclean process exit overlapping the day (a past run with clean_exit=0, or never closed);
  3. every run overlapping the day had dca in dry_run mode;
  4. at least one reconciliation check for (dca, dry_run) that day, and none failed.
Additionally the window must contain at least one DCA dry_run order that reached a terminal status, so the order
path (place -> fill/expire -> record) was actually exercised.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal

__all__ = ["DayHealth", "GateResult", "dca_live_gate"]

DAY_MS = 86_400_000
MIN_PER_DAY = 1440


@dataclass(frozen=True)
class DayHealth:
    day_start_ms: int
    coverage: Decimal
    unclean_runs: int
    non_dry_run_runs: int
    recon_checks: int
    recon_failures: int

    def problems(self, min_coverage: Decimal) -> list[str]:
        p = []
        if self.coverage < min_coverage:
            p.append(f"heartbeat coverage {self.coverage:.4f} < {min_coverage}")
        if self.unclean_runs:
            p.append(f"{self.unclean_runs} unclean process exit(s)")
        if self.non_dry_run_runs:
            p.append(f"{self.non_dry_run_runs} run(s) without dca in dry_run")
        if self.recon_checks == 0:
            p.append("no reconciliation check")
        if self.recon_failures:
            p.append(f"{self.recon_failures} failed reconciliation check(s)")
        return p


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    days: list[DayHealth] = field(default_factory=list)


def _day_health(conn: sqlite3.Connection, d0: int, current_run_id: str | None) -> DayHealth:
    d1 = d0 + DAY_MS
    minutes = conn.execute(
        "SELECT COUNT(*) FROM heartbeats WHERE minute >= ? AND minute < ?", (d0 // 60_000, d1 // 60_000)
    ).fetchone()[0]
    unclean = 0
    non_dry = 0
    for r in conn.execute(
        "SELECT run_id, started_at, ended_at, clean_exit, modes_json FROM runs WHERE started_at < ? "
        "AND (ended_at IS NULL OR ended_at >= ?)", (d1, d0)
    ):
        if r["run_id"] != current_run_id and (r["ended_at"] is None or r["clean_exit"] != 1):
            unclean += 1
        if json.loads(r["modes_json"]).get("dca") != "dry_run":
            non_dry += 1
    rc = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(1 - ok), 0) FROM recon_checks WHERE strategy='dca' AND mode='dry_run' "
        "AND ts >= ? AND ts < ?", (d0, d1)
    ).fetchone()
    return DayHealth(d0, Decimal(minutes) / MIN_PER_DAY, unclean, non_dry, int(rc[0]), int(rc[1]))


def dca_live_gate(conn: sqlite3.Connection, now_ms: int, *, days: int = 7,
                  min_coverage: Decimal = Decimal("0.99"), current_run_id: str | None = None) -> GateResult:
    today0 = now_ms // DAY_MS * DAY_MS
    starts = [today0 - k * DAY_MS for k in range(days, 0, -1)]  # the last `days` COMPLETE UTC days
    health = [_day_health(conn, d0, current_run_id) for d0 in starts]
    reasons = []
    for h in health:
        for p in h.problems(min_coverage):
            reasons.append(f"day {h.day_start_ms // DAY_MS}: {p}")
    exercised = conn.execute(
        "SELECT COUNT(*) FROM orders o JOIN trades t ON t.trade_id = o.trade_id "
        "WHERE t.strategy='dca' AND o.mode='dry_run' AND o.status IN ('filled','canceled','expired') "
        "AND o.updated_at >= ? AND o.updated_at < ?", (starts[0], today0)
    ).fetchone()[0]
    if exercised == 0:
        reasons.append("no DCA dry_run order reached a terminal status in the window (order path not exercised)")
    return GateResult(allowed=not reasons, reasons=reasons, days=health)
