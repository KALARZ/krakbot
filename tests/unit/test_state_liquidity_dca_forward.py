from decimal import Decimal

import pytest

from krakbot.config import Liquidity
from krakbot.domain import Mode, TradeState
from krakbot.state import connect, migrate
from krakbot.state.dca import DcaRepo
from krakbot.state.forward import ForwardTests
from krakbot.state.liquidity import DAY_MS, LiquidityRepo, nearest_rank, spread_bp
from krakbot.state.repo import StateError, Store

DAY0 = 20_400 * DAY_MS
RULES = Liquidity(reference_days=7, sample_seconds=10, spread_percentile=95, depth_bps=25, depth_percentile=5,
                  depth_mult_min=Decimal("10"), min_sample_coverage=Decimal("0.90"))


@pytest.fixture
def store(tmp_path):
    conn = connect(tmp_path / "x.db")
    migrate(conn)
    t = {"v": DAY0 + 8 * DAY_MS}
    yield Store(conn, lambda: t["v"])
    conn.close()


def test_nearest_rank_definition():
    v = [Decimal(i) for i in range(1, 101)]  # 1..100
    assert nearest_rank(v, 50) == 50 and nearest_rank(v, 95) == 95 and nearest_rank(v, 100) == 100
    assert nearest_rank(v, 1) == 1
    small = [Decimal("1"), Decimal("2"), Decimal("3")]
    assert nearest_rank(small, 50) == 2 and nearest_rank(small, 95) == 3 and nearest_rank(small, 5) == 1
    with pytest.raises(ValueError):
        nearest_rank(v, 0)
    with pytest.raises(ValueError):
        nearest_rank([], 50)


def test_spread_bp():
    assert spread_bp(Decimal("55566.8"), Decimal("55597.9")) == Decimal("5.5953")
    with pytest.raises(ValueError):
        spread_bp(Decimal("2"), Decimal("1"))


def _fill_samples(liq, pair, start, end, every_s, spread_fn, depth=Decimal("100000")):
    i = 0
    for ts in range(start, end, every_s * 1000):
        liq.add_sample(pair, ts, mid=Decimal("50000"), spread=spread_fn(i), bid_depth=depth + i,
                       ask_depth=depth + 2 * i)
        i += 1
    return i


def test_daily_rollup_percentiles(store):
    liq = LiquidityRepo(store.conn, store.now_ms)
    # 100 samples with spreads 1..100 bp on one day
    for i in range(100):
        liq.add_sample("XBT/EUR", DAY0 + i * 10_000, mid=Decimal("50000"), spread=Decimal(i + 1),
                       bid_depth=Decimal(1000 + i), ask_depth=Decimal(2000 + i))
    assert not liq.add_sample("XBT/EUR", DAY0, mid=Decimal("1"), spread=Decimal("9"), bid_depth=Decimal("1"),
                              ask_depth=Decimal("1"))  # duplicate timestamp ignored
    r = liq.rollup_day("XBT/EUR", DAY0 // DAY_MS)
    assert r["n"] == 100 and r["spread_p50"] == 50 and r["spread_p95"] == 95 and r["spread_p99"] == 99
    assert r["spread_max"] == 100 and r["bid_depth_p5"] == 1004 and r["ask_depth_p50"] == 2049
    liq.rollup_day("XBT/EUR", DAY0 // DAY_MS)  # idempotent upsert
    assert store.conn.execute("SELECT COUNT(*) FROM spread_daily").fetchone()[0] == 1


def test_threshold_derivation_coverage_and_hash_payload(store):
    liq = LiquidityRepo(store.conn, store.now_ms)
    start, end = DAY0, DAY0 + 7 * DAY_MS
    # XBT/EUR: full coverage at 10 s; spread cycles 1..20 bp
    _fill_samples(liq, "XBT/EUR", start, end, 10, lambda i: Decimal(1 + i % 20))
    # ETH/EUR: only every 20 s -> 50% coverage -> not eligible
    _fill_samples(liq, "ETH/EUR", start, end, 20, lambda i: Decimal(2))
    th = liq.derive("set1", ["XBT/EUR", "ETH/EUR", "SOL/EUR"], start, end, rules=RULES, pause_percentile=99)
    x = th["XBT/EUR"]
    assert x.eligible and x.coverage == Decimal("1.0000")
    assert x.spread_bp_max == 19 and x.spread_bp_pause == 20  # p95 and p99 of a uniform 1..20 cycle
    assert not th["ETH/EUR"].eligible and th["ETH/EUR"].spread_bp_max is None
    assert th["SOL/EUR"].n_samples == 0 and not th["SOL/EUR"].eligible
    assert liq.latest_set_id() == "set1"
    payload = liq.hash_payload("set1")
    assert payload["XBT/EUR"]["spread_bp_max"] == "19" and payload["ETH/EUR"]["eligible"] == "0"
    with pytest.raises(ValueError):
        liq.derive("set2", ["XBT/EUR"], start, start + 3 * DAY_MS, rules=RULES, pause_percentile=99)


def test_dca_tranche_budget_invariants(store):
    dca = DcaRepo(store)
    t = dca.create(tranche_id="dca-1", mode=Mode.DRY_RUN, window_start=DAY0, window_end=DAY0 + DAY_MS,
                   nominal=Decimal("19.23"), carry_in=Decimal("0"), budget=Decimal("19.23"))
    assert t.seq == 1 and t.status == "scheduled" and dca.open_tranche(Mode.DRY_RUN).tranche_id == "dca-1"
    with pytest.raises(StateError):
        dca.add_spend("dca-1", cost=Decimal("1"), fee=Decimal("0"), qty=Decimal("0.00002"))  # not started
    dca.start("dca-1", ref_mid=Decimal("55582.35"))
    dca.add_spend("dca-1", cost=Decimal("15.00"), fee=Decimal("0.06"), qty=Decimal("0.00027"))
    t = dca.close("dca-1", "partial")
    assert t.carry_out == Decimal("4.17") and dca.last_closed(Mode.DRY_RUN).tranche_id == "dca-1"  # derived
    with pytest.raises(StateError):
        dca.close("dca-1", "partial")  # already closed
    assert store.trades.get("dca-1").state == TradeState.HELD
    assert dca.totals(Mode.DRY_RUN) == (Decimal("15.06"), Decimal("0.00027"))


def test_dca_budget_cannot_exceed_due(store):
    dca = DcaRepo(store)
    with pytest.raises(StateError, match="exceeds what is due"):
        dca.create(tranche_id="x", mode=Mode.DRY_RUN, window_start=DAY0, window_end=DAY0 + DAY_MS,
                   nominal=Decimal("10"), carry_in=Decimal("1"), budget=Decimal("11.01"))


def test_dca_one_open_tranche_and_unfilled_close(store):
    dca = DcaRepo(store)
    dca.create(tranche_id="a", mode=Mode.DRY_RUN, window_start=DAY0, window_end=DAY0 + DAY_MS,
               nominal=Decimal("10"), carry_in=Decimal("0"), budget=Decimal("10"))
    with pytest.raises(Exception):  # noqa: B017
        dca.create(tranche_id="b", mode=Mode.DRY_RUN, window_start=DAY0 + 7 * DAY_MS, window_end=DAY0 + 8 * DAY_MS,
                   nominal=Decimal("10"), carry_in=Decimal("0"), budget=Decimal("10"))
    dca.start("a", ref_mid=Decimal("50000"))
    t = dca.close("a", "unfilled")
    assert t.carry_out == Decimal("10")
    assert t.status == "unfilled" and store.trades.get("a").state == TradeState.ENTRY_CANCELLED
    b = dca.create(tranche_id="b", mode=Mode.DRY_RUN, window_start=DAY0 + 7 * DAY_MS, window_end=DAY0 + 8 * DAY_MS,
                   nominal=Decimal("10"), carry_in=Decimal("10"), budget=Decimal("20"))
    assert b.seq == 2
    live = dca.create(tranche_id="L1", mode=Mode.LIVE, window_start=DAY0, window_end=DAY0 + DAY_MS,
                      nominal=Decimal("10"), carry_in=Decimal("0"), budget=Decimal("10"))
    assert live.seq == 1  # live and dry_run books are separate


def test_forward_test_registry(store):
    liq = LiquidityRepo(store.conn, store.now_ms)
    _fill_samples(liq, "XBT/EUR", DAY0, DAY0 + 7 * DAY_MS, 10, lambda i: Decimal(1))
    liq.derive("s1", ["XBT/EUR"], DAY0, DAY0 + 7 * DAY_MS, rules=RULES, pause_percentile=99)
    ft = ForwardTests(store.conn, store.now_ms)
    h = "a" * 64
    ft.start("ft1", h, "s1")
    with pytest.raises(StateError):
        ft.start("ft2", h, "s1")
    assert not ft.close_if_config_changed(h)
    assert ft.close_if_config_changed("b" * 64)
    assert ft.running() is None
    row = store.conn.execute("SELECT status, closed_at FROM forward_tests").fetchone()
    assert row["status"] == "abandoned_config_changed" and row["closed_at"] is not None
    ft.start("ft2", h, "s1")
    ft.close("ft2", "abandoned_pf", checkpoint={"pf": "0.87"})
    with pytest.raises(StateError):
        ft.close("ft2", "abandoned_pf")


def test_dca_real_fill_over_budget_is_recorded_never_refused(store):
    dca = DcaRepo(store)
    dca.create(tranche_id="dca-1", mode=Mode.DRY_RUN, window_start=DAY0, window_end=DAY0 + DAY_MS,
               nominal=Decimal("19.23"), carry_in=Decimal("0"), budget=Decimal("19.23"))
    dca.start("dca-1", ref_mid=Decimal("55582.35"))
    dca.add_spend("dca-1", cost=Decimal("19.15"), fee=Decimal("0.07661"), qty=Decimal("0.00034"))  # 19.22661
    t = dca.add_spend("dca-1", cost=Decimal("0"), fee=Decimal("0.00001"), qty=Decimal("0"))  # rounding: +0.00001
    assert t.spent == Decimal("19.22662")
    t = dca.add_spend("dca-1", cost=Decimal("0.00339"), fee=Decimal("0"), qty=Decimal("0"))  # 19.23001: tiny over
    assert t.spent == Decimal("19.23001")
    assert [r["level"] for r in store.events.by_kind("DCA_OVERSPEND")] == ["warning"]
    assert not store.risk.get("dca", Mode.DRY_RUN).halted          # rounding-sized overspend: logged, no halt
    t = dca.add_spend("dca-1", cost=Decimal("0.50"), fee=Decimal("0.002"), qty=Decimal("0.00001"))  # 2.6% over
    rs = store.risk.get("dca", Mode.DRY_RUN)
    assert rs.halted and rs.halt_reason == "recon" and rs.halt_ref == "overspend:dca-1"
    t = dca.close("dca-1", "filled")
    assert t.carry_out == Decimal(0) and t.spent == Decimal("19.73201")  # recorded as it happened
