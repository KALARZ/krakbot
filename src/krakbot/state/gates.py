"""Live gate for the DCA strategy: real orders only after N consecutive *clean* UTC days of DRY_RUN (user rule:
"DCA in live solo dopo i 7 giorni di DRY_RUN puliti"). Evaluated by the CLI and again inside the app before
`run --live`; the liquidity thresholds are derived only from a window that passes this gate.

PRE-REGISTERED definition of a clean UTC day (docs/plan.md §5.1, fixed before the 7-day run started; the values
live in CLEAN_DAY_RULE and the config validator only accepts them). A day is clean when ALL hold:
  1. uptime >= 99%: healthy main-loop minutes (heartbeat written only with a fresh WS book for XBT/EUR) / 1440;
  2. no halt: the DCA (dry_run) was not halted at any moment of the day, for any reason;
  3. samples >= 90% of expected, for EVERY configured sample pair: book samples / (86400 / sample_seconds);
  4. no failed consistency check: >= 1 reconciliation for (dca, dry_run) that day and none failed;
  plus two integrity conditions kept from the original gate:
  5. no unclean process exit overlapping the day (crash, kill, watchdog restart, a run never closed);
  6. every run overlapping the day had the DCA in dry_run.
Over the whole window: at least one DCA dry_run order reached a terminal status (the order path was exercised).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

__all__ = ["CLEAN_DAY_RULE", "CleanDayRule", "DayHealth", "GateResult", "dca_live_gate"]

DAY_MS = 86_400_000
MIN_PER_DAY = 1440


@dataclass(frozen=True)
class CleanDayRule:
    days: int = 7
    min_uptime: Decimal = Decimal("0.99")
    min_sample_coverage: Decimal = Decimal("0.90")
    sample_seconds: int = 10


CLEAN_DAY_RULE = CleanDayRule()


@dataclass(frozen=True)
class DayHealth:
    day_start_ms: int
    coverage: Decimal                       # uptime: healthy minutes / 1440
    unclean_runs: int
    non_dry_run_runs: int
    recon_checks: int
    recon_failures: int
    halted: bool = False
    halt_reasons: tuple[str, ...] = ()
    sample_coverage: dict[str, Decimal] = field(default_factory=dict)

    def problems(self, rule: CleanDayRule) -> list[str]:
        p = []
        if self.coverage < rule.min_uptime:
            p.append(f"uptime (heartbeat coverage) {self.coverage:.4f} < {rule.min_uptime}")
        if self.halted:
            p.append(f"DCA halted during the day ({', '.join(self.halt_reasons) or 'carried over'})")
        for pair, cov in sorted(self.sample_coverage.items()):
            if cov < rule.min_sample_coverage:
                p.append(f"{pair} samples {cov:.4f} of expected < {rule.min_sample_coverage}")
        if self.recon_checks == 0:
            p.append("no reconciliation check")
        if self.recon_failures:
            p.append(f"{self.recon_failures} failed reconciliation check(s)")
        if self.unclean_runs:
            p.append(f"{self.unclean_runs} unclean process exit(s)")
        if self.non_dry_run_runs:
            p.append(f"{self.non_dry_run_runs} run(s) without dca in dry_run")
        return p

    def clean(self, rule: CleanDayRule = CLEAN_DAY_RULE) -> bool:
        return not self.problems(rule)


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    days: list[DayHealth] = field(default_factory=list)
    window: tuple[int, int] = (0, 0)


def _halt_state(conn: sqlite3.Connection, d0: int, d1: int) -> tuple[bool, tuple[str, ...]]:
    """Replay the append-only HALT / HALT_CLEARED events of (dca, dry_run): halted at d0, or any halt inside."""
    halted_at_start = False
    reasons: list[str] = []
    for r in conn.execute(
        "SELECT ts, kind, payload_json FROM events WHERE strategy = 'dca' AND mode = 'dry_run' "
        "AND kind IN ('HALT', 'HALT_CLEARED') AND ts < ? ORDER BY ts, id", (d1,)
    ):
        if r["ts"] < d0:
            halted_at_start = r["kind"] == "HALT"
        elif r["kind"] == "HALT":
            reasons.append(str(json.loads(r["payload_json"]).get("reason")))
    if halted_at_start:
        reasons.insert(0, "halted since before the day")
    return halted_at_start or bool(reasons), tuple(reasons)


def _day_health(conn: sqlite3.Connection, d0: int, current_run_id: str | None, pairs: Sequence[str],
                rule: CleanDayRule) -> DayHealth:
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
    expected = Decimal(DAY_MS // (rule.sample_seconds * 1000))
    coverage = {}
    for pair in pairs:
        n = conn.execute("SELECT COUNT(*) FROM book_samples WHERE pair = ? AND ts >= ? AND ts < ?",
                         (pair, d0, d1)).fetchone()[0]
        coverage[pair] = (Decimal(n) / expected).quantize(Decimal("0.0001"))
    halted, reasons = _halt_state(conn, d0, d1)
    return DayHealth(d0, Decimal(minutes) / MIN_PER_DAY, unclean, non_dry, int(rc[0]), int(rc[1]), halted, reasons,
                     coverage)


def dca_live_gate(conn: sqlite3.Connection, now_ms: int, *, pairs: Sequence[str],
                  rule: CleanDayRule = CLEAN_DAY_RULE, current_run_id: str | None = None) -> GateResult:
    """The last `rule.days` COMPLETE UTC days before `now_ms` must all be clean (module docstring)."""
    if not pairs:
        raise ValueError("the clean-day rule needs the configured sample pairs")
    today0 = now_ms // DAY_MS * DAY_MS
    starts = [today0 - k * DAY_MS for k in range(rule.days, 0, -1)]
    health = [_day_health(conn, d0, current_run_id, pairs, rule) for d0 in starts]
    reasons = []
    for h in health:
        for p in h.problems(rule):
            reasons.append(f"day {h.day_start_ms // DAY_MS}: {p}")
    exercised = conn.execute(
        "SELECT COUNT(*) FROM orders o JOIN trades t ON t.trade_id = o.trade_id "
        "WHERE t.strategy='dca' AND o.mode='dry_run' AND o.status IN ('filled','canceled','expired') "
        "AND o.updated_at >= ? AND o.updated_at < ?", (starts[0], today0)
    ).fetchone()[0]
    if exercised == 0:
        reasons.append("no DCA dry_run order reached a terminal status in the window (order path not exercised)")
    return GateResult(allowed=not reasons, reasons=reasons, days=health, window=(starts[0], today0))
