import re
import sqlite3
from decimal import Decimal
from importlib import resources

import pytest

from krakbot.domain import EventLevel, Liquidity, OrderRole, OrderStatus, OrderType, Side, TradeState
from krakbot.state import MigrationError, connect, migrate, transaction

NOW = 1_795_000_000_000


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    yield conn
    conn.close()


def _schema_sql(conn, table):
    return conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]


def _check_values(sql, column):
    m = re.search(rf"{column}\s+TEXT NOT NULL(?: DEFAULT '[^']*')? CHECK \({column} IN \(([^)]*)\)\)", sql, re.S)
    assert m, column
    return {v.strip().strip("'") for v in m.group(1).replace("\n", " ").split(",")}


def test_wal_fk_synchronous(db):
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert db.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL


def test_migrations_idempotent_and_recorded(tmp_path):
    conn = connect(tmp_path / "x.db")
    try:
        assert migrate(conn) == [1, 2, 3]
        assert migrate(conn) == []
        assert [r[0] for r in conn.execute("SELECT version FROM schema_migrations")] == [1, 2, 3]
    finally:
        conn.close()


def test_checksum_tamper_detected(db):
    db.execute("UPDATE schema_migrations SET checksum = 'x' WHERE version = 1")
    with pytest.raises(MigrationError, match="checksum"):
        migrate(db)


def test_newer_database_rejected(db):
    db.execute("INSERT INTO schema_migrations VALUES (99, 'future', 'c', 0)")
    with pytest.raises(MigrationError, match="newer"):
        migrate(db)


def test_check_constraints_match_domain_enums(db):
    assert _check_values(_schema_sql(db, "trades"), "state") == {s.value for s in TradeState}
    orders = _schema_sql(db, "orders")
    assert _check_values(orders, "status") == {s.value for s in OrderStatus}
    assert _check_values(orders, "role") == {s.value for s in OrderRole}
    assert _check_values(orders, "side") == {s.value for s in Side}
    assert _check_values(orders, "ordertype") == {s.value for s in OrderType}
    assert _check_values(_schema_sql(db, "fills"), "liquidity") == {s.value for s in Liquidity}
    assert _check_values(_schema_sql(db, "events"), "level") == {s.value for s in EventLevel}


def _trade(db, tid, state="ENTRY_WORKING", pair="XBT/EUR", strategy="donchian", mode="dry_run"):
    db.execute("INSERT INTO trades(trade_id, strategy, mode, pair, state, qty_target, created_at, updated_at) "
               "VALUES (?,?,?,?,?,?,?,?)", (tid, strategy, mode, pair, state, Decimal("0.001"), NOW, NOW))


def test_one_open_trade_per_pair(db):
    _trade(db, "t1")
    with pytest.raises(sqlite3.IntegrityError):
        _trade(db, "t2")  # pyramiding blocked at DB level
    _trade(db, "t3", mode="live")  # different mode is a different book
    db.execute("UPDATE trades SET state='CLOSED' WHERE trade_id='t1'")
    _trade(db, "t4")  # after close, a new trade is allowed


def test_dca_tranches_end_held_and_do_not_block(db):
    _trade(db, "d1", state="HELD", strategy="dca")
    _trade(db, "d2", state="HELD", strategy="dca")
    _trade(db, "d3", state="ENTRY_WORKING", strategy="dca")
    with pytest.raises(sqlite3.IntegrityError):
        _trade(db, "d4", state="ENTRY_WORKING", strategy="dca")


def test_invalid_state_and_float_like_values_rejected(db):
    with pytest.raises(sqlite3.IntegrityError):
        _trade(db, "bad", state="OPEN")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO trades(trade_id, strategy, mode, pair, state, qty_target, created_at, updated_at) "
                   "VALUES ('e','dca','dry_run','XBT/EUR','HELD','1e-05',1,1)")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO trades(trade_id, strategy, mode, pair, state, qty_target, created_at, updated_at) "
                   "VALUES ('n','dca','dry_run','XBT/EUR','HELD','nan',1,1)")


def test_python_floats_cannot_be_written(db):
    with pytest.raises((TypeError, sqlite3.ProgrammingError)):
        db.execute("INSERT INTO meta(key, value, updated_at) VALUES ('k', ?, 1)", (0.1,))


def test_decimal_adapter_plain_notation(db):
    _trade(db, "t1")
    db.execute("UPDATE trades SET stop_price=? WHERE trade_id='t1'", (Decimal("1E-8"),))
    assert db.execute("SELECT stop_price FROM trades").fetchone()[0] == "0.00000001"


def test_order_constraints(db):
    _trade(db, "t1")
    def row(lid, tid, role, cl, side, otype, price, trig, post):
        return (lid, tid, "dry_run", role, cl, "XBT/EUR", side, otype, price, trig, "0.1", "intent", post, NOW, NOW)

    base = ("INSERT INTO orders(local_id, trade_id, mode, role, cl_ord_id, pair, side, ordertype, price, trigger_price,"
            " qty, status, post_only, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
    db.execute(base, row("o1", "t1", "entry", "c1", "buy", "limit", "100.0", None, 1))
    with pytest.raises(sqlite3.IntegrityError):  # duplicate cl_ord_id
        db.execute(base, row("o2", "t1", "entry", "c1", "buy", "limit", "100.0", None, 1))
    with pytest.raises(sqlite3.IntegrityError):  # stop-loss without trigger
        db.execute(base, row("o3", "t1", "stop", "c3", "sell", "stop-loss", None, None, 0))
    with pytest.raises(sqlite3.IntegrityError):  # post-only on a market order
        db.execute(base, row("o4", "t1", "exit", "c4", "sell", "market", None, None, 1))
    with pytest.raises(sqlite3.IntegrityError):  # FK: unknown trade
        db.execute(base, row("o5", "nope", "entry", "c5", "buy", "limit", "1", None, 1))


def test_events_append_only(db):
    db.execute("INSERT INTO events(ts, level, kind) VALUES (1, 'info', 'boot')")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE events SET kind='x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM events")


def test_halt_requires_reason(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO risk_state(strategy, mode, halted, updated_at) VALUES ('dca','live',1,1)")
    db.execute("INSERT INTO risk_state(strategy, mode, halted, halt_reason, updated_at) "
               "VALUES ('dca','live',1,'daily',1)")


def test_transaction_rolls_back(db):
    with pytest.raises(RuntimeError), transaction(db):
        db.execute("INSERT INTO meta VALUES ('a','b',1)")
        raise RuntimeError("boom")
    assert db.execute("SELECT COUNT(*) FROM meta").fetchone()[0] == 0


def test_migration_files_are_packaged():
    names = [p.name for p in (resources.files("krakbot.state") / "migrations").iterdir()]
    assert "0001_init.sql" in names
