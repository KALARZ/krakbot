"""DCA tranche persistence (plan §0.1). The budget/carry logic lives in the DCA strategy; this layer stores it.

Budget rule: orders are SIZED so that cost + fee (fee rounded up) fits the tranche budget (strategies.dca.order_qty).
A real fill is a fact and is ALWAYS recorded, even if it exceeds the budget (fee rounding, a fee tier that got
worse between placement and fill): refusing it would make the ledger disagree with the exchange. An overspend is
logged (DCA_OVERSPEND); beyond OVERSPEND_HALT_FRACTION of the budget it means a bug or a fee surprise, so new
purchases are halted (reason 'recon') until the operator checks. The due-based ledger absorbs any overspend in the
next tranche automatically (spendable = due - cumulative spent)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from krakbot.domain import EventLevel, Mode, TradeState
from krakbot.numeric import D, from_db
from krakbot.state.repo import StateError, Store, tx

__all__ = ["OVERSPEND_HALT_FRACTION", "DcaRepo", "Tranche"]

_CLOSED = frozenset({"filled", "partial", "unfilled", "skipped_min"})
OVERSPEND_HALT_FRACTION = Decimal("0.01")


@dataclass(frozen=True)
class Tranche:
    tranche_id: str
    seq: int
    mode: Mode
    window_start: int
    window_end: int
    nominal: Decimal
    carry_in: Decimal
    budget: Decimal
    spent: Decimal
    qty: Decimal
    fees: Decimal
    ref_mid: Decimal | None
    status: str
    carry_out: Decimal | None
    canary: bool = False

    @property
    def remaining(self) -> Decimal:
        return self.budget - self.spent

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> Tranche:
        return cls(r["tranche_id"], r["seq"], Mode(r["mode"]), r["window_start"], r["window_end"], D(r["nominal"]),
                   D(r["carry_in"]), D(r["budget"]), D(r["spent"]), D(r["qty"]), D(r["fees"]), from_db(r["ref_mid"]),
                   r["status"], from_db(r["carry_out"]), bool(r["canary"]))


class DcaRepo:
    def __init__(self, store: Store, pair: str = "XBT/EUR") -> None:
        self._s = store
        self._c = store.conn
        self._pair = pair

    def create(self, *, tranche_id: str, mode: Mode, window_start: int, window_end: int, nominal: Decimal,
               carry_in: Decimal, budget: Decimal, canary: bool = False) -> Tranche:
        if budget <= 0 or nominal < 0 or carry_in < 0:
            raise StateError("budget must be > 0, nominal and carry_in >= 0")
        if budget > nominal + carry_in:
            raise StateError(f"budget {budget} exceeds what is due (nominal {nominal} + carry {carry_in})")
        now = self._s.now_ms()
        with tx(self._c):
            seq = self.next_seq(mode)
            self._s.trades.create(tranche_id, strategy="dca", mode=mode, pair=self._pair, qty_target=Decimal(0))
            self._c.execute(
                "INSERT INTO dca_tranches(tranche_id, seq, mode, window_start, window_end, nominal, carry_in, budget, "
                "status, created_at, updated_at, canary) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (tranche_id, seq, str(mode), window_start, window_end, nominal, carry_in, budget, "scheduled", now,
                 now, int(canary)))
        return self.get(tranche_id)

    def get(self, tranche_id: str) -> Tranche:
        r = self._c.execute("SELECT * FROM dca_tranches WHERE tranche_id = ?", (tranche_id,)).fetchone()
        if r is None:
            raise StateError(f"unknown tranche {tranche_id}")
        return Tranche.from_row(r)

    def next_seq(self, mode: Mode) -> int:
        r = self._c.execute("SELECT COALESCE(MAX(seq), 0) FROM dca_tranches WHERE mode = ?", (str(mode),)).fetchone()
        return int(r[0]) + 1

    def latest(self, mode: Mode) -> Tranche | None:
        r = self._c.execute("SELECT * FROM dca_tranches WHERE mode = ? ORDER BY seq DESC LIMIT 1",
                            (str(mode),)).fetchone()
        return None if r is None else Tranche.from_row(r)

    def recent_closed(self, mode: Mode, n: int) -> list[Tranche]:
        rows = self._c.execute("SELECT * FROM dca_tranches WHERE mode = ? AND status IN "
                               "('filled','partial','unfilled','skipped_min') ORDER BY seq DESC LIMIT ?",
                               (str(mode), n)).fetchall()
        return [Tranche.from_row(r) for r in rows]

    def all(self, mode: Mode) -> list[Tranche]:
        return [Tranche.from_row(r) for r in
                self._c.execute("SELECT * FROM dca_tranches WHERE mode = ? ORDER BY seq", (str(mode),))]

    def open_tranche(self, mode: Mode) -> Tranche | None:
        r = self._c.execute("SELECT * FROM dca_tranches WHERE mode = ? AND status IN ('scheduled','working')",
                            (str(mode),)).fetchone()
        return None if r is None else Tranche.from_row(r)

    def last_closed(self, mode: Mode) -> Tranche | None:
        r = self._c.execute("SELECT * FROM dca_tranches WHERE mode = ? AND status IN "
                            "('filled','partial','unfilled','skipped_min') ORDER BY seq DESC LIMIT 1",
                            (str(mode),)).fetchone()
        return None if r is None else Tranche.from_row(r)

    def start(self, tranche_id: str, ref_mid: Decimal) -> Tranche:
        with tx(self._c):
            t = self.get(tranche_id)
            if t.status != "scheduled":
                raise StateError(f"tranche {tranche_id} is {t.status}")
            self._c.execute("UPDATE dca_tranches SET status='working', ref_mid=?, updated_at=? WHERE tranche_id=?",
                            (ref_mid, self._s.now_ms(), tranche_id))
            tr = self._s.trades.get(tranche_id)
            self._s.trades.transition(tranche_id, TradeState.ENTRY_WORKING, expected_version=tr.version)
        return self.get(tranche_id)

    def add_spend(self, tranche_id: str, *, cost: Decimal, fee: Decimal, qty: Decimal) -> Tranche:
        """cost = quote paid for the base (price*qty), fee = quote fee; budget covers cost + fee."""
        with tx(self._c):
            t = self.get(tranche_id)
            if t.status != "working":
                raise StateError(f"tranche {tranche_id} is {t.status}")
            new_spent = t.spent + cost + fee
            if new_spent > t.budget:
                over = new_spent - t.budget
                severe = over > t.budget * OVERSPEND_HALT_FRACTION
                self._s.events.append("DCA_OVERSPEND", EventLevel.CRITICAL if severe else EventLevel.WARNING,
                                      {"tranche_id": tranche_id, "spent": new_spent, "budget": t.budget,
                                       "over": over}, strategy="dca", mode=t.mode, trade_id=tranche_id)
                if severe:
                    self._s.risk.halt("dca", t.mode, "recon", ref=f"overspend:{tranche_id}")
            self._c.execute("UPDATE dca_tranches SET spent=?, qty=?, fees=?, updated_at=? WHERE tranche_id=?",
                            (new_spent, t.qty + qty, t.fees + fee, self._s.now_ms(), tranche_id))
        return self.get(tranche_id)

    def close(self, tranche_id: str, status: str) -> Tranche:
        """carry_out is DERIVED (nominal + carry_in - spent): it can never be invented or double counted."""
        if status not in _CLOSED:
            raise StateError(f"invalid closing status {status}")
        with tx(self._c):
            t = self.get(tranche_id)
            if t.status not in ("scheduled", "working"):
                raise StateError(f"tranche {tranche_id} already {t.status}")
            carry_out = max(Decimal(0), t.nominal + t.carry_in - t.spent)  # overspend: carried by the ledger
            self._c.execute("UPDATE dca_tranches SET status=?, carry_out=?, updated_at=? WHERE tranche_id=?",
                            (status, carry_out, self._s.now_ms(), tranche_id))
            tr = self._s.trades.get(tranche_id)
            target = TradeState.HELD if t.qty > 0 else TradeState.ENTRY_CANCELLED
            if tr.state == TradeState.SIGNALLED and target == TradeState.HELD:
                raise StateError("a tranche with fills must have been started")
            self._s.trades.transition(tranche_id, target, expected_version=tr.version, qty_filled=t.qty,
                                      fees=t.fees, closed_at=self._s.now_ms())
            payload = {"tranche_id": tranche_id, "status": status, "spent": t.spent, "qty": t.qty,
                       "carry_out": carry_out}
            self._s.events.append("DCA_TRANCHE_CLOSED", payload=payload, strategy="dca", mode=t.mode,
                                  trade_id=tranche_id)
        return self.get(tranche_id)

    def totals(self, mode: Mode) -> tuple[Decimal, Decimal]:
        rows = self._c.execute("SELECT spent, qty FROM dca_tranches WHERE mode = ?", (str(mode),)).fetchall()
        return (sum((D(r[0]) for r in rows), Decimal(0)), sum((D(r[1]) for r in rows), Decimal(0)))
