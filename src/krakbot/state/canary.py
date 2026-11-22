"""LIVE canary for the DCA (plan §5.3, pre-registered before the first real order).

  * Until CANARY_CLEAN_TRANCHES consecutive canary tranches are clean, every LIVE tranche is a canary: exactly one
    order of Kraken's absolute minimum size (strategies.dca.min_qty).
  * When a canary tranche closes, the DCA halts (reason 'canary'); only `krakbot canary-verify` can clear it, after
    checking fill, ledger, reconciliation and balance against Kraken. `resume` never clears it.
  * A canary tranche is CLEAN when it is 'filled' with qty > 0, has at least one passing verification and no failed
    one. Any non-clean canary tranche (unfilled, partial, a failed check) resets the streak.
  * The phase ends (full size) once the most recent CANARY_CLEAN_TRANCHES canary tranches are all clean.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from krakbot.domain import EventLevel, Mode
from krakbot.numeric import D
from krakbot.state.dca import Tranche
from krakbot.state.repo import Store, tx

__all__ = ["CANARY_CLEAN_TRANCHES", "CanaryCheck", "CanaryRepo"]

CANARY_CLEAN_TRANCHES = 3


@dataclass(frozen=True)
class CanaryCheck:
    fill_ok: bool
    ledger_ok: bool
    recon_ok: bool
    balance_ok: bool
    details: dict[str, Any]

    @property
    def ok(self) -> bool:
        return self.fill_ok and self.ledger_ok and self.recon_ok and self.balance_ok


class CanaryRepo:
    def __init__(self, store: Store) -> None:
        self._s = store
        self._c = store.conn

    # ---------------------------------------------------------------- verifications
    def record(self, tranche_id: str, check: CanaryCheck) -> None:
        with tx(self._c):
            self._c.execute(
                "INSERT INTO dca_canary_checks(tranche_id, checked_at, ok, fill_ok, ledger_ok, recon_ok, balance_ok, "
                "details_json) VALUES (?,?,?,?,?,?,?,?)",
                (tranche_id, self._s.now_ms(), int(check.ok), int(check.fill_ok), int(check.ledger_ok),
                 int(check.recon_ok), int(check.balance_ok), json.dumps(check.details, default=str)))
            self._s.events.append("CANARY_VERIFIED" if check.ok else "CANARY_CHECK_FAILED",
                                  EventLevel.WARNING if check.ok else EventLevel.CRITICAL,
                                  {"tranche_id": tranche_id, **check.details}, strategy="dca", mode=Mode.LIVE,
                                  trade_id=tranche_id)

    def checks(self, tranche_id: str) -> list[bool]:
        return [bool(r[0]) for r in self._c.execute(
            "SELECT ok FROM dca_canary_checks WHERE tranche_id = ? ORDER BY checked_at, id", (tranche_id,))]

    def is_clean(self, t: Tranche) -> bool:
        res = self.checks(t.tranche_id)
        return t.canary and t.status == "filled" and t.qty > 0 and bool(res) and all(res)

    def canary_tranches(self) -> list[Tranche]:
        rows = self._c.execute("SELECT * FROM dca_tranches WHERE mode = 'live' AND canary = 1 "
                               "AND status NOT IN ('scheduled','working') ORDER BY seq DESC").fetchall()
        return [Tranche.from_row(r) for r in rows]

    def streak(self) -> int:
        """Consecutive clean canary tranches, counting back from the most recent closed one."""
        n = 0
        for t in self.canary_tranches():
            if not self.is_clean(t):
                break
            n += 1
        return n

    def phase_complete(self) -> bool:
        return self.streak() >= CANARY_CLEAN_TRANCHES

    # ---------------------------------------------------------------- balance baseline
    def baseline(self, asset: str) -> Decimal | None:
        r = self._c.execute("SELECT balance FROM dca_live_baseline WHERE asset = ?", (asset,)).fetchone()
        return None if r is None else D(r[0])

    def set_baseline_once(self, asset: str, balance: Decimal, run_id: str) -> Decimal:
        """Recorded at the first LIVE start, never overwritten: the check is balance - baseline == bought qty."""
        with tx(self._c):
            cur = self.baseline(asset)
            if cur is not None:
                return cur
            self._c.execute("INSERT INTO dca_live_baseline(asset, balance, recorded_at, run_id) VALUES (?,?,?,?)",
                            (asset, balance, self._s.now_ms(), run_id))
            self._s.events.append("LIVE_BASELINE", EventLevel.WARNING, {"asset": asset, "balance": balance},
                                  strategy="dca", mode=Mode.LIVE)
        return balance
