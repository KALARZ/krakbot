from decimal import Decimal

import pytest

from krakbot.kraken_rest.models import BookTop, Instrument
from krakbot.kraken_ws import OrderBook
from krakbot.market_data import LiquiditySampler, MarketData
from krakbot.state import connect, migrate
from krakbot.state.liquidity import DAY_MS, LiquidityRepo

INST = Instrument(rest_key="XXBTZEUR", altname="XBTEUR", wsname="XBT/EUR", base="BTC", quote="EUR", pair_decimals=1,
                  lot_decimals=8, cost_decimals=5, tick_size=Decimal("0.1"), ordermin=Decimal("0.00006"),
                  costmin=Decimal("0.45"), status="online", symbol="BTC/EUR")


def _book(bid="55566.8", ask="55597.9", qty="1.0"):
    b = OrderBook("BTC/EUR", 10)
    b.snapshot([{"price": Decimal(bid), "qty": Decimal(qty)}, {"price": Decimal("55500.0"), "qty": Decimal("2")}],
               [{"price": Decimal(ask), "qty": Decimal(qty)}])
    return b


@pytest.fixture
def clock():
    return {"t": 20_400 * DAY_MS + 5_000}


def test_top_freshness_and_staleness(clock):
    md = MarketData({"XBT/EUR": INST}, depth_bps=25, now_ms=lambda: clock["t"])
    assert md.top("XBT/EUR") is None
    md.on_book("BTC/EUR", _book())
    t = md.top("XBT/EUR")
    assert t.bid == Decimal("55566.8") and t.source == "ws" and t.spread_bp == Decimal("5.5953")
    assert t.bid_depth == Decimal("55566.8") + Decimal("111000.0")  # both bid levels within 25 bp of mid
    clock["t"] += 10_001
    assert md.top("XBT/EUR") is None and md.needs_rest_fallback("XBT/EUR")
    md.on_book("BTC/EUR", _book())
    md.on_book("BTC/EUR", None)  # disconnect / checksum failure -> stale immediately
    assert md.top("XBT/EUR") is None


def test_rest_fallback_never_overrides_fresh_ws(clock):
    md = MarketData({"XBT/EUR": INST}, depth_bps=25, now_ms=lambda: clock["t"])
    md.on_book("BTC/EUR", _book())
    md.on_rest_ticker("XBT/EUR", BookTop(bid=Decimal("1"), ask=Decimal("2"), last=Decimal("1.5")))
    assert md.top("XBT/EUR").source == "ws"
    clock["t"] += 20_000
    md.on_rest_ticker("XBT/EUR", BookTop(bid=Decimal("55000.0"), ask=Decimal("55010.0"), last=Decimal("55005")))
    t = md.top("XBT/EUR")
    assert t.source == "rest" and md.top("XBT/EUR", require_ws=True) is None


def test_trades_dispatched_by_pair(clock):
    md = MarketData({"XBT/EUR": INST}, depth_bps=25, now_ms=lambda: clock["t"])
    seen = []
    md.add_trade_listener(lambda pair, trades: seen.append((pair, len(trades))))
    md.on_trades("BTC/EUR", [{"price": Decimal("1")}, {"price": Decimal("2")}])
    md.on_trades("UNKNOWN/EUR", [{"price": Decimal("1")}])
    assert seen == [("XBT/EUR", 2)]


def test_sampler_fixed_grid_only_fresh_ws_and_daily_rollup(tmp_path, clock):
    conn = connect(tmp_path / "md.db")
    migrate(conn)
    try:
        repo = LiquidityRepo(conn, lambda: clock["t"])
        md = MarketData({"XBT/EUR": INST}, depth_bps=25, now_ms=lambda: clock["t"])
        s = LiquiditySampler(md, repo, ["XBT/EUR"], sample_seconds=10, now_ms=lambda: clock["t"])
        assert s.tick() == 0  # no book yet: no fake sample
        md.on_book("BTC/EUR", _book())
        assert s.tick() == 0  # same 10 s slot already visited
        clock["t"] += 10_000
        md.on_book("BTC/EUR", _book())
        assert s.tick() == 1
        clock["t"] += 3_000
        assert s.tick() == 0  # still the same slot
        md.on_rest_ticker("XBT/EUR", BookTop(bid=Decimal("1"), ask=Decimal("2"), last=Decimal("1")))
        clock["t"] += 30_000  # ws now stale, only REST -> no sample
        md.on_rest_ticker("XBT/EUR", BookTop(bid=Decimal("1"), ask=Decimal("2"), last=Decimal("1")))
        assert s.tick() == 0
        ts = [r[0] for r in conn.execute("SELECT ts FROM book_samples")]
        assert all(t % 10_000 == 0 for t in ts) and len(ts) == 1
        clock["t"] = 20_401 * DAY_MS + 1  # next UTC day: yesterday is rolled up
        s.tick()
        row = conn.execute("SELECT n, spread_p95 FROM spread_daily WHERE day = 20400").fetchone()
        assert row["n"] == 1 and row["spread_p95"] == "5.5953"
    finally:
        conn.close()
