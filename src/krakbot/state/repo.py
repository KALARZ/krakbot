"""Repositories over the SQLite schema (plan §3). All money/qty values are Decimal in and Decimal out.

Invariants enforced here (on top of the schema constraints):
* trade state changes go through domain.TRANSITIONS and optimistic locking (`version`); every change is logged in
  the append-only `events` table in the same transaction;
* orders are written as INTENT before being sent (write-ahead); status changes follow domain.ORDER_TRANSITIONS;
  filled_qty never decreases and never exceeds qty; a txid, once known, never changes;
* fills are idempotent on exec_id; replaying the same fill is a no-op, a different fill with the same exec_id is an
  error; the order's filled_qty/avg_price are always recomputed from the fills table (single source of truth).
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from krakbot.domain import (
    TERMINAL_ORDER_STATUSES,
    EventLevel,
    Liquidity,
    Mode,
    OrderRole,
    OrderStatus,
    OrderType,
    Side,
    TradeState,
    assert_order_transition,
    assert_transition,
)
from krakbot.numeric import D, from_db

__all__ = [
    "FillConflict", "NotFound", "Order", "OverFill", "PositionAlreadyOpen", "StaleVersion", "StateError", "Store",
    "Trade", "tx",
]

NowMs = Callable[[], int]


class StateError(Exception):
    pass


class StaleVersion(StateError):
    pass


class PositionAlreadyOpen(StateError):
    pass


class NotFound(StateError):
    pass


class FillConflict(StateError):
    pass


class OverFill(StateError):
    pass


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, or a no-op if a transaction is already open (lets repositories compose)."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _dec(row: sqlite3.Row, key: str) -> Decimal | None:
    return from_db(row[key])


@dataclass(frozen=True)
class Trade:
    trade_id: str
    signal_id: str | None
    strategy: str
    mode: Mode
    pair: str
    state: TradeState
    qty_target: Decimal
    qty_filled: Decimal
    avg_entry: Decimal | None
    stop_price: Decimal | None
    tp_price: Decimal | None
    guard_price: Decimal | None
    opened_at: int | None
    closed_at: int | None
    exit_reason: str | None
    realized_pnl: Decimal | None
    fees: Decimal
    version: int
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> Trade:
        return cls(r["trade_id"], r["signal_id"], r["strategy"], Mode(r["mode"]), r["pair"], TradeState(r["state"]),
                   D(r["qty_target"]), D(r["qty_filled"]), _dec(r, "avg_entry"), _dec(r, "stop_price"),
                   _dec(r, "tp_price"), _dec(r, "guard_price"), r["opened_at"], r["closed_at"], r["exit_reason"],
                   _dec(r, "realized_pnl"), D(r["fees"]), r["version"], r["created_at"], r["updated_at"])


@dataclass(frozen=True)
class Order:
    local_id: str
    trade_id: str
    mode: Mode
    role: OrderRole
    cl_ord_id: str
    txid: str | None
    pair: str
    side: Side
    ordertype: OrderType
    price: Decimal | None
    trigger_price: Decimal | None
    qty: Decimal
    filled_qty: Decimal
    avg_price: Decimal | None
    status: OrderStatus
    attempt: int
    post_only: bool
    expire_at: int | None
    cancel_reason: str | None
    created_at: int
    updated_at: int
    last_error: str | None

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> Order:
        return cls(r["local_id"], r["trade_id"], Mode(r["mode"]), OrderRole(r["role"]), r["cl_ord_id"], r["txid"],
                   r["pair"], Side(r["side"]), OrderType(r["ordertype"]), _dec(r, "price"), _dec(r, "trigger_price"),
                   D(r["qty"]), D(r["filled_qty"]), _dec(r, "avg_price"), OrderStatus(r["status"]), r["attempt"],
                   bool(r["post_only"]), r["expire_at"], r["cancel_reason"], r["created_at"], r["updated_at"],
                   r["last_error"])


class Events:
    def __init__(self, conn: sqlite3.Connection, now_ms: NowMs) -> None:
        self._c, self._now = conn, now_ms

    def append(self, kind: str, level: EventLevel = EventLevel.INFO, payload: Mapping[str, Any] | None = None, *,
               strategy: str | None = None, mode: Mode | None = None, signal_id: str | None = None,
               trade_id: str | None = None) -> int:
        cur = self._c.execute(
            "INSERT INTO events(ts, level, kind, strategy, mode, signal_id, trade_id, payload_json) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (self._now(), str(level), kind, strategy, str(mode) if mode else None, signal_id, trade_id,
             json.dumps(dict(payload or {}), default=str, sort_keys=True)),
        )
        return int(cur.lastrowid or 0)

    def by_kind(self, kind: str) -> list[sqlite3.Row]:
        return list(self._c.execute("SELECT * FROM events WHERE kind = ? ORDER BY id", (kind,)))


_TRADE_FIELDS = frozenset({"qty_filled", "avg_entry", "stop_price", "tp_price", "guard_price", "opened_at", "closed_at",
                           "exit_reason", "realized_pnl", "fees"})


class Trades:
    def __init__(self, conn: sqlite3.Connection, now_ms: NowMs, events: Events) -> None:
        self._c, self._now, self._ev = conn, now_ms, events

    def create(self, trade_id: str, *, strategy: str, mode: Mode, pair: str, qty_target: Decimal,
               signal_id: str | None = None, state: TradeState = TradeState.SIGNALLED) -> Trade:
        now = self._now()
        try:
            with tx(self._c):
                self._c.execute(
                    "INSERT INTO trades(trade_id, signal_id, strategy, mode, pair, state, qty_target, created_at, "
                    "updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (trade_id, signal_id, strategy, str(mode), pair, str(state), qty_target, now, now))
                self._ev.append("TRADE_CREATED", payload={"state": str(state), "pair": pair, "qty_target": qty_target},
                                strategy=strategy, mode=mode, signal_id=signal_id, trade_id=trade_id)
        except sqlite3.IntegrityError as exc:
            if "ux_trades_one_open" in str(exc) or "UNIQUE constraint failed: trades.strategy" in str(exc):
                raise PositionAlreadyOpen(f"{strategy}/{mode}/{pair} already has an open trade") from exc
            raise
        return self.get(trade_id)

    def get(self, trade_id: str) -> Trade:
        r = self._c.execute("SELECT * FROM trades WHERE trade_id = ?", (trade_id,)).fetchone()
        if r is None:
            raise NotFound(trade_id)
        return Trade.from_row(r)

    def open(self, strategy: str | None = None, mode: Mode | None = None) -> list[Trade]:
        q = "SELECT * FROM trades WHERE state NOT IN ('ENTRY_CANCELLED','STOPPED','CLOSED','HELD')"
        args: list[Any] = []
        if strategy:
            q += " AND strategy = ?"
            args.append(strategy)
        if mode:
            q += " AND mode = ?"
            args.append(str(mode))
        return [Trade.from_row(r) for r in self._c.execute(q + " ORDER BY created_at", args)]

    def _write(self, trade_id: str, expected_version: int, new_state: TradeState | None, fields: Mapping[str, Any],
               kind: str) -> Trade:
        bad = set(fields) - _TRADE_FIELDS
        if bad:
            raise StateError(f"not updatable: {sorted(bad)}")
        with tx(self._c):
            cur = self.get(trade_id)
            if cur.version != expected_version:
                raise StaleVersion(f"{trade_id}: version {cur.version} != expected {expected_version}")
            if new_state is not None:
                assert_transition(cur.state, new_state)
            sets = ["version = version + 1", "updated_at = ?"]
            args: list[Any] = [self._now()]
            if new_state is not None:
                sets.append("state = ?")
                args.append(str(new_state))
            for k, v in fields.items():
                sets.append(f"{k} = ?")
                args.append(v)
            args += [trade_id, expected_version]
            res = self._c.execute(f"UPDATE trades SET {', '.join(sets)} WHERE trade_id = ? AND version = ?", args)  # noqa: S608
            if res.rowcount != 1:
                raise StaleVersion(trade_id)
            self._ev.append(kind, payload={"from": str(cur.state), "to": str(new_state or cur.state),
                                           "fields": dict(fields), "version": expected_version + 1},
                            strategy=cur.strategy, mode=cur.mode, signal_id=cur.signal_id, trade_id=trade_id)
        return self.get(trade_id)

    def transition(self, trade_id: str, to: TradeState, *, expected_version: int, **fields: Any) -> Trade:
        return self._write(trade_id, expected_version, to, fields, "TRADE_TRANSITION")

    def update(self, trade_id: str, *, expected_version: int, **fields: Any) -> Trade:
        return self._write(trade_id, expected_version, None, fields, "TRADE_UPDATE")


class Orders:
    def __init__(self, conn: sqlite3.Connection, now_ms: NowMs, events: Events) -> None:
        self._c, self._now, self._ev = conn, now_ms, events

    def intent(self, *, local_id: str, trade_id: str, mode: Mode, role: OrderRole, cl_ord_id: str, pair: str,
               side: Side, ordertype: OrderType, qty: Decimal, price: Decimal | None = None,
               trigger_price: Decimal | None = None, post_only: bool = False, expire_at: int | None = None,
               attempt: int = 0) -> Order:
        """Write-ahead: persisted BEFORE the order is sent, so a crash can never leave an order we don't know."""
        now = self._now()
        with tx(self._c):
            self._c.execute(
                "INSERT INTO orders(local_id, trade_id, mode, role, cl_ord_id, pair, side, ordertype, price, "
                "trigger_price, qty, status, attempt, post_only, expire_at, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (local_id, trade_id, str(mode), str(role), cl_ord_id, pair, str(side), str(ordertype), price,
                 trigger_price, qty, str(OrderStatus.INTENT), attempt, int(post_only), expire_at, now, now))
            self._ev.append("ORDER_INTENT", payload={"cl_ord_id": cl_ord_id, "role": str(role), "side": str(side),
                                                     "type": str(ordertype), "qty": qty, "price": price,
                                                     "trigger": trigger_price}, mode=mode, trade_id=trade_id)
        return self.by_cl_ord_id(cl_ord_id)

    def by_cl_ord_id(self, cl_ord_id: str) -> Order:
        r = self._c.execute("SELECT * FROM orders WHERE cl_ord_id = ?", (cl_ord_id,)).fetchone()
        if r is None:
            raise NotFound(cl_ord_id)
        return Order.from_row(r)

    def by_txid(self, txid: str) -> Order | None:
        r = self._c.execute("SELECT * FROM orders WHERE txid = ?", (txid,)).fetchone()
        return None if r is None else Order.from_row(r)

    def for_trade(self, trade_id: str) -> list[Order]:
        return [Order.from_row(r) for r in
                self._c.execute("SELECT * FROM orders WHERE trade_id = ? ORDER BY created_at, local_id", (trade_id,))]

    def unresolved(self, mode: Mode | None = None) -> list[Order]:
        """Orders whose exchange state is not known for sure: must be resolved before new orders (plan §4.5/§4.7)."""
        q = "SELECT * FROM orders WHERE status IN ('intent','sent','unknown')"
        args: list[Any] = []
        if mode:
            q += " AND mode = ?"
            args.append(str(mode))
        return [Order.from_row(r) for r in self._c.execute(q + " ORDER BY created_at", args)]

    def working(self, mode: Mode | None = None) -> list[Order]:
        q = "SELECT * FROM orders WHERE status IN ('open','partially_filled')"
        args: list[Any] = []
        if mode:
            q += " AND mode = ?"
            args.append(str(mode))
        return [Order.from_row(r) for r in self._c.execute(q + " ORDER BY created_at", args)]

    def mark(self, cl_ord_id: str, status: OrderStatus, *, txid: str | None = None, cancel_reason: str | None = None,
             last_error: str | None = None) -> Order:
        with tx(self._c):
            cur = self.by_cl_ord_id(cl_ord_id)
            if status != cur.status:
                assert_order_transition(cur.status, status)
            if txid is not None and cur.txid is not None and txid != cur.txid:
                raise StateError(f"{cl_ord_id}: txid already {cur.txid}, refusing {txid}")
            self._c.execute(
                "UPDATE orders SET status = ?, txid = COALESCE(txid, ?), cancel_reason = COALESCE(?, cancel_reason), "
                "last_error = COALESCE(?, last_error), updated_at = ? WHERE cl_ord_id = ?",
                (str(status), txid, cancel_reason, last_error, self._now(), cl_ord_id))
            level = EventLevel.CRITICAL if status == OrderStatus.UNKNOWN else EventLevel.INFO
            self._ev.append("ORDER_STATUS", level, {"cl_ord_id": cl_ord_id, "from": str(cur.status), "to": str(status),
                                                    "txid": txid, "reason": cancel_reason, "error": last_error},
                            mode=cur.mode, trade_id=cur.trade_id)
        return self.by_cl_ord_id(cl_ord_id)


class Fills:
    def __init__(self, conn: sqlite3.Connection, now_ms: NowMs, events: Events, orders: Orders) -> None:
        self._c, self._now, self._ev, self._orders = conn, now_ms, events, orders

    def record(self, *, exec_id: str, cl_ord_id: str, ts: int, price: Decimal, qty: Decimal, fee: Decimal,
               fee_ccy: str, liquidity: Liquidity, cost: Decimal | None = None) -> tuple[bool, Order]:
        """Idempotent fill ingestion. Returns (inserted, order_after)."""
        cost = price * qty if cost is None else cost
        if qty <= 0 or price <= 0 or fee < 0:
            raise StateError("fill qty/price must be > 0 and fee >= 0")
        with tx(self._c):
            order = self._orders.by_cl_ord_id(cl_ord_id)
            row = self._c.execute("SELECT * FROM fills WHERE exec_id = ?", (exec_id,)).fetchone()
            if row is not None:
                same = (row["local_id"] == order.local_id and D(row["qty"]) == qty and D(row["price"]) == price
                        and D(row["fee"]) == fee and row["ts"] == ts)
                if not same:
                    raise FillConflict(f"exec_id {exec_id} already recorded with different values")
                return False, order
            if order.status == OrderStatus.REJECTED:
                self._ev.append("FILL_ON_REJECTED_ORDER", EventLevel.CRITICAL,
                                {"cl_ord_id": cl_ord_id, "exec_id": exec_id}, mode=order.mode, trade_id=order.trade_id)
                raise StateError(f"fill {exec_id} on order {cl_ord_id} marked rejected: reconciliation required")
            self._c.execute(
                "INSERT INTO fills(exec_id, local_id, txid, trade_id, mode, ts, price, qty, cost, fee, fee_ccy, "
                "liquidity) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (exec_id, order.local_id, order.txid, order.trade_id, str(order.mode), ts, price, qty, cost, fee,
                 fee_ccy, str(liquidity)))
            agg = self._c.execute("SELECT qty, cost FROM fills WHERE local_id = ?", (order.local_id,)).fetchall()
            filled = sum((D(r["qty"]) for r in agg), Decimal(0))
            avg = sum((D(r["cost"]) for r in agg), Decimal(0)) / filled
            if filled > order.qty:
                raise OverFill(f"{cl_ord_id}: filled {filled} > qty {order.qty}")
            new_status = order.status
            if order.status not in TERMINAL_ORDER_STATUSES:
                new_status = OrderStatus.FILLED if filled == order.qty else OrderStatus.PARTIALLY_FILLED
                if new_status != order.status:
                    assert_order_transition(order.status, new_status)
            self._c.execute("UPDATE orders SET filled_qty = ?, avg_price = ?, status = ?, updated_at = ? "
                            "WHERE local_id = ?", (filled, avg, str(new_status), self._now(), order.local_id))
            self._ev.append("FILL", payload={"exec_id": exec_id, "cl_ord_id": cl_ord_id, "qty": qty, "price": price,
                                             "fee": fee, "liquidity": str(liquidity), "filled": filled},
                            mode=order.mode, trade_id=order.trade_id)
        return True, self._orders.by_cl_ord_id(cl_ord_id)

    def for_trade(self, trade_id: str) -> list[sqlite3.Row]:
        return list(self._c.execute("SELECT * FROM fills WHERE trade_id = ? ORDER BY ts, exec_id", (trade_id,)))


@dataclass(frozen=True)
class RiskState:
    strategy: str
    mode: Mode
    consecutive_losses: int
    size_mult: Decimal
    pause_until: int
    halted: bool
    halt_reason: str | None
    halt_ref: str | None


class RiskStates:
    def __init__(self, conn: sqlite3.Connection, now_ms: NowMs, events: Events) -> None:
        self._c, self._now, self._ev = conn, now_ms, events

    def get(self, strategy: str, mode: Mode) -> RiskState:
        self._c.execute("INSERT OR IGNORE INTO risk_state(strategy, mode, updated_at) VALUES (?,?,?)",
                        (strategy, str(mode), self._now()))
        r = self._c.execute("SELECT * FROM risk_state WHERE strategy = ? AND mode = ?",
                            (strategy, str(mode))).fetchone()
        return RiskState(r["strategy"], Mode(r["mode"]), r["consecutive_losses"], D(r["size_mult"]), r["pause_until"],
                         bool(r["halted"]), r["halt_reason"], r["halt_ref"])

    def halt(self, strategy: str, mode: Mode, reason: str, ref: str | None = None) -> RiskState:
        with tx(self._c):
            self.get(strategy, mode)
            self._c.execute("UPDATE risk_state SET halted = 1, halt_reason = ?, halt_ref = ?, updated_at = ? "
                            "WHERE strategy = ? AND mode = ?", (reason, ref, self._now(), strategy, str(mode)))
            self._ev.append("HALT", EventLevel.CRITICAL, {"reason": reason, "ref": ref}, strategy=strategy, mode=mode)
        return self.get(strategy, mode)

    def clear_halt(self, strategy: str, mode: Mode, *, reason: str, ref: str | None = None) -> bool:
        """Clears the halt only if it is the expected one (a different, newer halt is never cleared by mistake)."""
        with tx(self._c):
            cur = self.get(strategy, mode)
            if not cur.halted or cur.halt_reason != reason or (ref is not None and cur.halt_ref != ref):
                return False
            self._c.execute("UPDATE risk_state SET halted = 0, halt_reason = NULL, halt_ref = NULL, updated_at = ? "
                            "WHERE strategy = ? AND mode = ?", (self._now(), strategy, str(mode)))
            self._ev.append("HALT_CLEARED", EventLevel.WARNING, {"reason": reason, "ref": ref}, strategy=strategy,
                            mode=mode)
        return True


class Runs:
    def __init__(self, conn: sqlite3.Connection, now_ms: NowMs) -> None:
        self._c, self._now = conn, now_ms

    def start(self, run_id: str, version: str, modes: Mapping[str, str]) -> None:
        self._c.execute("INSERT INTO runs(run_id, started_at, version, modes_json) VALUES (?,?,?,?)",
                        (run_id, self._now(), version, json.dumps(dict(modes), sort_keys=True)))

    def heartbeat(self, run_id: str) -> None:
        self._c.execute("INSERT OR IGNORE INTO heartbeats(minute, run_id) VALUES (?, ?)",
                        (self._now() // 60_000, run_id))

    def end(self, run_id: str, *, clean: bool, reason: str) -> None:
        self._c.execute("UPDATE runs SET ended_at = ?, clean_exit = ?, exit_reason = ? "
                        "WHERE run_id = ? AND ended_at IS NULL", (self._now(), int(clean), reason, run_id))


class Recon:
    def __init__(self, conn: sqlite3.Connection, now_ms: NowMs) -> None:
        self._c, self._now = conn, now_ms

    def add(self, strategy: str, mode: Mode, ok: bool, details: Mapping[str, Any] | None = None) -> None:
        self._c.execute("INSERT INTO recon_checks(ts, strategy, mode, ok, details_json) VALUES (?,?,?,?,?)",
                        (self._now(), strategy, str(mode), int(ok), json.dumps(dict(details or {}), default=str)))


class Store:
    """Facade over one connection (single process, single writer)."""

    def __init__(self, conn: sqlite3.Connection, now_ms: NowMs | None = None) -> None:
        self.conn = conn
        self.now_ms: NowMs = now_ms or (lambda: time.time_ns() // 1_000_000)
        self.events = Events(conn, self.now_ms)
        self.trades = Trades(conn, self.now_ms, self.events)
        self.orders = Orders(conn, self.now_ms, self.events)
        self.fills = Fills(conn, self.now_ms, self.events, self.orders)
        self.risk = RiskStates(conn, self.now_ms, self.events)
        self.runs = Runs(conn, self.now_ms)
        self.recon = Recon(conn, self.now_ms)

    @contextmanager
    def tx(self) -> Iterator[None]:
        with tx(self.conn):
            yield
