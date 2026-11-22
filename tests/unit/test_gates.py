import json
from decimal import Decimal

import pytest

from krakbot.state import connect, migrate
from krakbot.state.gates import DAY_MS, dca_live_gate

NOW = 20_400 * DAY_MS + 12 * 3_600_000  # some day at 12:00 UTC
DAY0 = NOW // DAY_MS * DAY_MS - 7 * DAY_MS  # first of the 7 complete days before today


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "g.db")
    migrate(conn)
    yield conn
    conn.close()


def _run(db, rid, start, end, clean, dca="dry_run"):
    db.execute("INSERT INTO runs(run_id, started_at, ended_at, clean_exit, version, modes_json) VALUES (?,?,?,?,?,?)",
               (rid, start, end, clean, "0.1.0", json.dumps({"dca": dca, "donchian": "dry_run"})))


def _heartbeats(db, rid, start_ms, end_ms):
    db.executemany("INSERT OR IGNORE INTO heartbeats(minute, run_id) VALUES (?,?)",
                   [(m, rid) for m in range(start_ms // 60_000, end_ms // 60_000)])


def _recon(db, ts, ok=1):
    db.execute("INSERT INTO recon_checks(ts, strategy, mode, ok) VALUES (?,?,?,?)", (ts, "dca", "dry_run", ok))


def _dca_order(db, ts, status="filled"):
    db.execute("INSERT INTO trades(trade_id, strategy, mode, pair, state, qty_target, created_at, updated_at) "
               "VALUES ('d1','dca','dry_run','XBT/EUR','HELD','0.001',?,?)", (ts, ts))
    db.execute("INSERT INTO orders(local_id, trade_id, mode, role, cl_ord_id, pair, side, ordertype, price, qty, "
               "status, post_only, created_at, updated_at) VALUES ('o1','d1','dry_run','dca','c1','XBT/EUR','buy',"
               "'limit','1','0.001',?,1,?,?)", (status, ts, ts))


def _clean_week(db):
    _run(db, "r1", DAY0 - DAY_MS, None, None)  # current run, still alive
    _heartbeats(db, "r1", DAY0, NOW)
    for k in range(7):
        _recon(db, DAY0 + k * DAY_MS + 3_600_000)
    _dca_order(db, DAY0 + 2 * DAY_MS)


def test_clean_week_allows_live(db):
    _clean_week(db)
    g = dca_live_gate(db, NOW, current_run_id="r1")
    assert g.allowed, g.reasons
    assert len(g.days) == 7 and all(d.coverage == 1 for d in g.days)


def test_missing_heartbeats_block(db):
    _clean_week(db)
    db.execute("DELETE FROM heartbeats WHERE minute >= ? AND minute < ?",
               ((DAY0 + 3 * DAY_MS) // 60_000, (DAY0 + 3 * DAY_MS) // 60_000 + 30))  # 30 min gap = 97.9%
    g = dca_live_gate(db, NOW, current_run_id="r1")
    assert not g.allowed and any("heartbeat coverage" in r for r in g.reasons)


def test_crash_blocks(db):
    _clean_week(db)
    _run(db, "r0", DAY0 + DAY_MS, DAY0 + DAY_MS + 60_000, 0)  # a run that exited uncleanly
    g = dca_live_gate(db, NOW, current_run_id="r1")
    assert not g.allowed and any("unclean" in r for r in g.reasons)


def test_dead_previous_run_without_end_is_unclean(db):
    _clean_week(db)
    _run(db, "rx", DAY0 + 4 * DAY_MS, None, None)  # never closed and not the current run => crashed
    assert not dca_live_gate(db, NOW, current_run_id="r1").allowed


def test_failed_or_missing_recon_blocks(db):
    _clean_week(db)
    _recon(db, DAY0 + 5 * DAY_MS + 7_200_000, ok=0)
    g = dca_live_gate(db, NOW, current_run_id="r1")
    assert not g.allowed and any("failed reconciliation" in r for r in g.reasons)


def test_live_mode_during_window_blocks(db):
    _clean_week(db)
    _run(db, "r2", DAY0 + 6 * DAY_MS, DAY0 + 6 * DAY_MS + 1000, 1, dca="live")
    assert any("without dca in dry_run" in r for r in dca_live_gate(db, NOW, current_run_id="r1").reasons)


def test_order_path_must_be_exercised(db):
    _run(db, "r1", DAY0 - DAY_MS, None, None)
    _heartbeats(db, "r1", DAY0, NOW)
    for k in range(7):
        _recon(db, DAY0 + k * DAY_MS + 1)
    g = dca_live_gate(db, NOW, current_run_id="r1")
    assert not g.allowed and any("not exercised" in r for r in g.reasons)


def test_less_than_seven_days_of_history_blocks(db):
    _run(db, "r1", DAY0 + 3 * DAY_MS, None, None)
    _heartbeats(db, "r1", DAY0 + 3 * DAY_MS, NOW)
    assert not dca_live_gate(db, NOW, current_run_id="r1", min_coverage=Decimal("0.99")).allowed
