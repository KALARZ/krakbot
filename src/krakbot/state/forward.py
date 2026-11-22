"""Forward-test registry (plan §0.2): one running test at most; a changed freeze hash closes it."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any

from krakbot.state.repo import StateError, tx

__all__ = ["ForwardTests"]

_CLOSING = frozenset({"abandoned_pf", "abandoned_kill", "abandoned_config_changed", "unlock_eligible", "failed_gates",
                      "closed_manual"})


class ForwardTests:
    def __init__(self, conn: sqlite3.Connection, now_ms: Callable[[], int]) -> None:
        self._c, self._now = conn, now_ms

    def start(self, run_id: str, freeze_hash: str, threshold_set_id: str) -> None:
        if self.running() is not None:
            raise StateError("a forward test is already running")
        self._c.execute("INSERT INTO forward_tests(run_id, started_at, freeze_hash, threshold_set_id, status) "
                        "VALUES (?,?,?,?, 'running')", (run_id, self._now(), freeze_hash, threshold_set_id))

    def running(self) -> sqlite3.Row | None:
        r: sqlite3.Row | None = self._c.execute("SELECT * FROM forward_tests WHERE status = 'running'").fetchone()
        return r

    def close(self, run_id: str, status: str, *, checkpoint: Mapping[str, Any] | None = None,
              secondary: Mapping[str, Any] | None = None) -> None:
        if status not in _CLOSING:
            raise StateError(f"invalid closing status {status}")
        with tx(self._c):
            cur = self._c.execute(
                "UPDATE forward_tests SET status = ?, closed_at = ?, checkpoint_json = ?, secondary_json = ? "
                "WHERE run_id = ? AND status = 'running'",
                (status, self._now(), json.dumps(dict(checkpoint), default=str) if checkpoint else None,
                 json.dumps(dict(secondary), default=str) if secondary else None, run_id))
            if cur.rowcount != 1:
                raise StateError(f"forward test {run_id} is not running")

    def close_if_config_changed(self, current_hash: str) -> bool:
        r = self.running()
        if r is None or r["freeze_hash"] == current_hash:
            return False
        self.close(r["run_id"], "abandoned_config_changed",
                   checkpoint={"frozen": r["freeze_hash"], "now": current_hash})
        return True
