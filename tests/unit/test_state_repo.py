import hashlib
import sqlite3
from decimal import Decimal

import pytest

from krakbot.domain import IllegalTransition, Liquidity, Mode, OrderRole, OrderStatus, OrderType, Side, TradeState
from krakbot.state import connect, migrate
from krakbot.state.db import _available, _split_sql
from krakbot.state.repo import FillConflict, OverFill, PositionAlreadyOpen, StaleVersion, StateError, Store

T0 = 1_795_000_000_000


@pytest.fixture
def store(tmp_path):
    conn = connect(tmp_path / "s.db")
    migrate(conn)
    clock = {"t": T0}

    def now():
        clock["t"] += 1
        return clock["t"]
    yield Store(conn, now)
    conn.close()


def _trade(store, tid="t1", pair="XBT/EUR", strategy="donchian", mode=Mode.DRY_RUN):
    return store.trades.create(tid, strategy=strategy, mode=mode, pair=pair, qty_target=Decimal("0.01"))


def _order(store, trade_id="t1", cl="c1", qty="0.01", role=OrderRole.ENTRY, side=Side.BUY):
    return store.orders.intent(local_id="L" + cl, trade_id=trade_id, mode=Mode.DRY_RUN, role=role, cl_ord_id=cl,
                               pair="XBT/EUR", side=side, ordertype=OrderType.LIMIT, qty=Decimal(qty),
                               price=Decimal("50000.0"), post_only=True)


# ------------------------------------------------------------------ migration 0003 on existing data
def test_migration_0003_keeps_halt_ref_and_tranches_and_allows_canary(tmp_path):
    conn = connect(tmp_path / "m3.db")
    try:
        conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                     "checksum TEXT NOT NULL, applied_at INTEGER NOT NULL)")
        for v in (1, 2):
            m = next(x for x in _available() if x[0] == v)
            for stmt in _split_sql(m[2]):
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_migrations VALUES (?, ?, ?, 0)",
                         (v, m[1], hashlib.sha256(m[2].encode()).hexdigest()))
        conn.execute("INSERT INTO risk_state(strategy, mode, halted, halt_reason, halt_ref, updated_at) "
                     "VALUES ('dca','dry_run',1,'unknown_order','cl-9',5)")
        conn.execute("INSERT INTO trades(trade_id, strategy, mode, pair, state, qty_target, created_at, updated_at) "
                     "VALUES ('dca-x','dca','dry_run','XBT/EUR','SIGNALLED','0',1,1)")
        conn.execute("INSERT INTO dca_tranches(tranche_id, seq, mode, window_start, window_end, nominal, carry_in, "
                     "budget, status, created_at, updated_at) VALUES ('dca-x',1,'dry_run',0,1,'19.23','0','19.23',"
                     "'scheduled',1,1)")
        with pytest.raises(sqlite3.IntegrityError):  # 'canary' not allowed before 0003
            conn.execute("UPDATE risk_state SET halt_reason='canary'")
        assert migrate(conn) == [3]
        assert tuple(conn.execute("SELECT halted, halt_reason, halt_ref FROM risk_state").fetchone()) == (
            1, "unknown_order", "cl-9")
        assert conn.execute("SELECT canary FROM dca_tranches").fetchone()[0] == 0
        conn.execute("UPDATE risk_state SET halt_reason='canary', halt_ref='dca-live-1'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE dca_tranches SET canary = 2")
    finally:
        conn.close()


# ------------------------------------------------------------------ migration 0002 on existing data
def test_migration_0002_preserves_existing_risk_state(tmp_path):
    conn = connect(tmp_path / "m.db")
    try:
        conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                     "checksum TEXT NOT NULL, applied_at INTEGER NOT NULL)")
        v1 = next(m for m in _available() if m[0] == 1)
        for stmt in _split_sql(v1[2]):
            conn.execute(stmt)
        conn.execute("INSERT INTO schema_migrations VALUES (1, ?, ?, 0)",
                     (v1[1], hashlib.sha256(v1[2].encode()).hexdigest()))
        conn.execute("INSERT INTO risk_state(strategy, mode, consecutive_losses, halted, halt_reason, updated_at) "
                     "VALUES ('donchian','dry_run',2,1,'daily',5)")
        with pytest.raises(sqlite3.IntegrityError):  # 'unknown_order' not allowed before 0002
            conn.execute("UPDATE risk_state SET halt_reason='unknown_order'")
        assert migrate(conn) == [2, 3]
        r = conn.execute("SELECT consecutive_losses, halted, halt_reason FROM risk_state").fetchone()
        assert tuple(r) == (2, 1, "daily")
        conn.execute("UPDATE risk_state SET halt_reason='unknown_order', halt_ref='cl1'")
    finally:
        conn.close()


# ------------------------------------------------------------------ trades
def test_trade_lifecycle_with_optimistic_locking_and_events(store):
    t = _trade(store)
    assert t.state == TradeState.SIGNALLED and t.version == 0
    t = store.trades.transition("t1", TradeState.ENTRY_WORKING, expected_version=0)
    t = store.trades.transition("t1", TradeState.OPEN_PROTECTED, expected_version=1, qty_filled=Decimal("0.01"),
                                avg_entry=Decimal("50000.0"), stop_price=Decimal("48000.0"), opened_at=T0)
    assert t.version == 2 and t.stop_price == Decimal("48000.0") and isinstance(t.qty_filled, Decimal)
    with pytest.raises(StaleVersion):
        store.trades.update("t1", expected_version=1, stop_price=Decimal("49000.0"))
    with pytest.raises(IllegalTransition):
        store.trades.transition("t1", TradeState.SIGNALLED, expected_version=2)
    with pytest.raises(StateError):
        store.trades.update("t1", expected_version=2, state="CLOSED")  # state only via transition()
    kinds = [r["kind"] for r in store.conn.execute("SELECT kind FROM events WHERE trade_id='t1' ORDER BY id")]
    assert kinds == ["TRADE_CREATED", "TRADE_TRANSITION", "TRADE_TRANSITION"]


def test_failed_transition_leaves_no_trace(store):
    _trade(store)
    n_events = store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    with pytest.raises(IllegalTransition):
        store.trades.transition("t1", TradeState.CLOSED, expected_version=0)
    assert store.trades.get("t1").version == 0
    assert store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == n_events


def test_one_open_position_per_pair(store):
    _trade(store, "t1")
    with pytest.raises(PositionAlreadyOpen):
        _trade(store, "t2")
    _trade(store, "t3", pair="ETH/EUR")
    assert [t.trade_id for t in store.trades.open(strategy="donchian")] == ["t1", "t3"]


# ------------------------------------------------------------------ orders (write-ahead) and fills
def test_order_write_ahead_lifecycle(store):
    _trade(store)
    o = _order(store)
    assert o.status == OrderStatus.INTENT and o.txid is None
    assert [x.cl_ord_id for x in store.orders.unresolved()] == ["c1"]
    store.orders.mark("c1", OrderStatus.SENT)
    o = store.orders.mark("c1", OrderStatus.OPEN, txid="OTX-1")
    assert o.txid == "OTX-1" and store.orders.unresolved() == []
    with pytest.raises(StateError):
        store.orders.mark("c1", OrderStatus.OPEN, txid="OTHER")  # txid never changes
    with pytest.raises(IllegalTransition):
        store.orders.mark("c1", OrderStatus.INTENT)
    assert store.orders.by_txid("OTX-1").cl_ord_id == "c1"


def test_unknown_order_can_only_be_left_with_evidence(store):
    _trade(store)
    _order(store)
    store.orders.mark("c1", OrderStatus.SENT)
    store.orders.mark("c1", OrderStatus.UNKNOWN, last_error="ReadTimeout after send")
    crit = store.conn.execute("SELECT level FROM events WHERE kind='ORDER_STATUS' ORDER BY id DESC LIMIT 1").fetchone()
    assert crit[0] == "critical"
    assert [o.status for o in store.orders.unresolved()] == [OrderStatus.UNKNOWN]
    store.orders.mark("c1", OrderStatus.REJECTED, cancel_reason="absent: not open/closed after deadline+60s")
    with pytest.raises(IllegalTransition):
        store.orders.mark("c1", OrderStatus.OPEN)


def test_fills_idempotent_partial_then_full(store):
    _trade(store)
    _order(store, qty="0.02")
    store.orders.mark("c1", OrderStatus.SENT)
    store.orders.mark("c1", OrderStatus.OPEN, txid="OTX")
    ins, o = store.fills.record(exec_id="E1", cl_ord_id="c1", ts=T0, price=Decimal("50000.0"), qty=Decimal("0.005"),
                                fee=Decimal("1.0"), fee_ccy="EUR", liquidity=Liquidity.MAKER)
    assert ins and o.status == OrderStatus.PARTIALLY_FILLED and o.filled_qty == Decimal("0.005")
    again, o2 = store.fills.record(exec_id="E1", cl_ord_id="c1", ts=T0, price=Decimal("50000.0"),
                                   qty=Decimal("0.005"), fee=Decimal("1.0"), fee_ccy="EUR", liquidity=Liquidity.MAKER)
    assert not again and o2.filled_qty == Decimal("0.005")  # replay = no-op
    with pytest.raises(FillConflict):
        store.fills.record(exec_id="E1", cl_ord_id="c1", ts=T0, price=Decimal("50000.0"), qty=Decimal("0.006"),
                           fee=Decimal("1.0"), fee_ccy="EUR", liquidity=Liquidity.MAKER)
    _, o3 = store.fills.record(exec_id="E2", cl_ord_id="c1", ts=T0 + 1, price=Decimal("50100.0"), qty=Decimal("0.015"),
                               fee=Decimal("3.0"), fee_ccy="EUR", liquidity=Liquidity.MAKER)
    assert o3.status == OrderStatus.FILLED and o3.filled_qty == Decimal("0.020")
    expected_avg = (Decimal("50000.0") * Decimal("0.005") + Decimal("50100.0") * Decimal("0.015")) / Decimal("0.02")
    assert o3.avg_price == expected_avg
    with pytest.raises(OverFill):
        store.fills.record(exec_id="E3", cl_ord_id="c1", ts=T0 + 2, price=Decimal("1.0"), qty=Decimal("0.001"),
                           fee=Decimal("0"), fee_ccy="EUR", liquidity=Liquidity.MAKER)
    assert len(store.fills.for_trade("t1")) == 2  # the overfill was rolled back


def test_late_fill_on_canceled_order_keeps_status(store):
    _trade(store)
    _order(store, qty="0.02")
    store.orders.mark("c1", OrderStatus.SENT)
    store.orders.mark("c1", OrderStatus.CANCELED, cancel_reason="User requested")
    _, o = store.fills.record(exec_id="E1", cl_ord_id="c1", ts=T0, price=Decimal("50000.0"), qty=Decimal("0.004"),
                              fee=Decimal("0.8"), fee_ccy="EUR", liquidity=Liquidity.MAKER)
    assert o.status == OrderStatus.CANCELED and o.filled_qty == Decimal("0.004")


def test_fill_on_rejected_order_escalates(store):
    _trade(store)
    _order(store)
    store.orders.mark("c1", OrderStatus.REJECTED)
    with pytest.raises(StateError, match="reconciliation"):
        store.fills.record(exec_id="E1", cl_ord_id="c1", ts=T0, price=Decimal("1.0"), qty=Decimal("0.001"),
                           fee=Decimal("0"), fee_ccy="EUR", liquidity=Liquidity.TAKER)


def test_store_tx_composes_and_rolls_back_everything(store):
    _trade(store)
    with pytest.raises(RuntimeError), store.tx():
        _order(store, cl="c9")
        store.orders.mark("c9", OrderStatus.SENT)
        raise RuntimeError("crash between intent and send")
    with pytest.raises(Exception):  # noqa: B017
        store.orders.by_cl_ord_id("c9")


# ------------------------------------------------------------------ risk state / halts
def test_halt_and_clear_only_the_expected_halt(store):
    r = store.risk.get("dca", Mode.LIVE)
    assert not r.halted
    store.risk.halt("dca", Mode.LIVE, "unknown_order", ref="c1")
    assert store.risk.get("dca", Mode.LIVE).halt_reason == "unknown_order"
    assert not store.risk.clear_halt("dca", Mode.LIVE, reason="daily")
    assert not store.risk.clear_halt("dca", Mode.LIVE, reason="unknown_order", ref="other")
    assert store.risk.clear_halt("dca", Mode.LIVE, reason="unknown_order", ref="c1")
    assert not store.risk.get("dca", Mode.LIVE).halted
    assert not store.risk.get("donchian", Mode.DRY_RUN).halted  # halts are per strategy


def test_runs_heartbeats_recon(store):
    store.runs.start("r1", "0.1.0", {"dca": "dry_run", "donchian": "dry_run"})
    store.runs.heartbeat("r1")
    store.runs.heartbeat("r1")  # same minute: one row
    store.recon.add("dca", Mode.DRY_RUN, True, {"diff": 0})
    store.runs.end("r1", clean=True, reason="sigterm")
    assert store.conn.execute("SELECT COUNT(*) FROM heartbeats").fetchone()[0] == 1
    assert store.conn.execute("SELECT clean_exit FROM runs").fetchone()[0] == 1
