import json
from decimal import Decimal

import pytest

from krakbot.state import connect, migrate, transaction
from krakbot.state.gates import CLEAN_DAY_RULE, DAY_MS, dca_live_gate

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
    with transaction(db):
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


PAIRS = ["XBT/EUR", "ETH/EUR"]


def _samples(db, pair, start_ms, end_ms, every_ms=10_000):
    with transaction(db):  # one fsync, not one per row
        db.executemany("INSERT OR IGNORE INTO book_samples(pair, ts, mid, spread_bp, bid_depth, ask_depth) "
                       "VALUES (?,?,'1','1','1','1')", [(pair, t) for t in range(start_ms, end_ms, every_ms)])


def _halt(db, ts, kind="HALT", reason="manual"):
    db.execute("INSERT INTO events(ts, level, kind, strategy, mode, payload_json) VALUES (?,?,?,?,?,?)",
               (ts, "critical", kind, "dca", "dry_run", json.dumps({"reason": reason})))


def _clean_week(db):
    _run(db, "r1", DAY0 - DAY_MS, None, None)  # current run, still alive
    _heartbeats(db, "r1", DAY0, NOW)
    for k in range(7):
        _recon(db, DAY0 + k * DAY_MS + 3_600_000)
    for pair in PAIRS:
        _samples(db, pair, DAY0, NOW)
    _dca_order(db, DAY0 + 2 * DAY_MS)


def gate(db, **kw):
    return dca_live_gate(db, NOW, pairs=PAIRS, current_run_id="r1", **kw)


def test_clean_week_allows_live(db):
    _clean_week(db)
    g = gate(db)
    assert g.allowed, g.reasons
    assert len(g.days) == 7 and all(d.coverage == 1 for d in g.days)


def test_missing_heartbeats_block(db):
    _clean_week(db)
    db.execute("DELETE FROM heartbeats WHERE minute >= ? AND minute < ?",
               ((DAY0 + 3 * DAY_MS) // 60_000, (DAY0 + 3 * DAY_MS) // 60_000 + 30))  # 30 min gap = 97.9%
    g = gate(db)
    assert not g.allowed and any("uptime" in r for r in g.reasons)


def test_crash_blocks(db):
    _clean_week(db)
    _run(db, "r0", DAY0 + DAY_MS, DAY0 + DAY_MS + 60_000, 0)  # a run that exited uncleanly
    g = gate(db)
    assert not g.allowed and any("unclean" in r for r in g.reasons)


def test_dead_previous_run_without_end_is_unclean(db):
    _clean_week(db)
    _run(db, "rx", DAY0 + 4 * DAY_MS, None, None)  # never closed and not the current run => crashed
    assert not gate(db).allowed


def test_failed_or_missing_recon_blocks(db):
    _clean_week(db)
    _recon(db, DAY0 + 5 * DAY_MS + 7_200_000, ok=0)
    g = gate(db)
    assert not g.allowed and any("failed reconciliation" in r for r in g.reasons)


def test_live_mode_during_window_blocks(db):
    _clean_week(db)
    _run(db, "r2", DAY0 + 6 * DAY_MS, DAY0 + 6 * DAY_MS + 1000, 1, dca="live")
    assert any("without dca in dry_run" in r for r in gate(db).reasons)


def test_order_path_must_be_exercised(db):
    _run(db, "r1", DAY0 - DAY_MS, None, None)
    _heartbeats(db, "r1", DAY0, NOW)
    for k in range(7):
        _recon(db, DAY0 + k * DAY_MS + 1)
    g = gate(db)
    assert not g.allowed and any("not exercised" in r for r in g.reasons)


def test_less_than_seven_days_of_history_blocks(db):
    _run(db, "r1", DAY0 + 3 * DAY_MS, None, None)
    _heartbeats(db, "r1", DAY0 + 3 * DAY_MS, NOW)
    assert not gate(db).allowed


def test_registered_rule_values():
    assert (CLEAN_DAY_RULE.days, CLEAN_DAY_RULE.min_uptime, CLEAN_DAY_RULE.min_sample_coverage,
            CLEAN_DAY_RULE.sample_seconds) == (7, Decimal("0.99"), Decimal("0.90"), 10)


def test_uptime_boundary_is_inclusive_at_99_percent(db):
    _clean_week(db)
    d = DAY0 + 3 * DAY_MS
    db.execute("DELETE FROM heartbeats WHERE minute >= ? AND minute < ?", (d // 60_000, d // 60_000 + 14))
    assert gate(db).allowed  # 1426/1440 = 0.99028 >= 0.99
    db.execute("DELETE FROM heartbeats WHERE minute >= ? AND minute < ?", (d // 60_000, d // 60_000 + 15))
    assert not gate(db).allowed  # 1425/1440 = 0.98958


def test_any_halt_during_the_day_makes_it_unclean_even_if_cleared(db):
    _clean_week(db)
    _halt(db, DAY0 + 4 * DAY_MS + 3_600_000)
    _halt(db, DAY0 + 4 * DAY_MS + 3_660_000, kind="HALT_CLEARED")  # one minute later
    g = gate(db)
    bad = [d for d in g.days if not d.clean()]
    assert not g.allowed and len(bad) == 1 and bad[0].day_start_ms == DAY0 + 4 * DAY_MS
    assert any("halted" in r and "manual" in r for r in g.reasons)


def test_halt_carried_over_from_a_previous_day(db):
    _clean_week(db)
    _halt(db, DAY0 - 3_600_000, reason="unknown_order")  # never cleared: every day of the window is unclean
    g = gate(db)
    assert not g.allowed and all(d.halted for d in g.days)
    assert "halted since before the day" in g.days[0].halt_reasons


def test_sample_coverage_below_90_percent_for_any_pair_blocks(db):
    _clean_week(db)
    d = DAY0 + 5 * DAY_MS
    # remove 10.1% of one day of ETH/EUR samples (873 of 8640): 89.9% < 90%
    db.execute("DELETE FROM book_samples WHERE pair = 'ETH/EUR' AND ts >= ? AND ts < ?", (d, d + 873 * 10_000))
    g = gate(db)
    assert not g.allowed and any("ETH/EUR samples 0.8990" in r for r in g.reasons)
    day = next(x for x in g.days if x.day_start_ms == d)
    assert day.sample_coverage["XBT/EUR"] == Decimal("1.0000")


def test_sample_coverage_at_exactly_90_percent_passes(db):
    _clean_week(db)
    d = DAY0 + 5 * DAY_MS
    db.execute("DELETE FROM book_samples WHERE pair = 'ETH/EUR' AND ts >= ? AND ts < ?", (d, d + 864 * 10_000))
    assert gate(db).allowed


def test_window_is_the_last_seven_complete_utc_days(db):
    _clean_week(db)
    assert gate(db).window == (DAY0, DAY0 + 7 * DAY_MS)
