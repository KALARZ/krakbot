"""Shadow run (plan §5.4) and the WebSocket connection budget that protects the shared IP."""

import asyncio
import inspect
import io
import itertools
import json
import random
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import respx

import krakbot.shadow as shadow_mod
from krakbot.cli import main
from krakbot.config import ConfigError, load_settings
from krakbot.domain import Mode
from krakbot.kraken_rest.clock import ManualClock
from krakbot.kraken_rest.models import instrument_from_ws_v2
from krakbot.kraken_ws import OrderBook, PublicWs
from krakbot.shadow import DCA_WS_SYMBOL, ShadowApp
from krakbot.shadow_report import shadow_report
from krakbot.strategies.dca import periodic_window
from tests.unit.dca_harness import MONDAY_10

# WS v2 `instrument` pair as Kraken documents it (numbers arrive as Decimal: the client parses floats as Decimal)
WS_PAIR = json.loads('{"symbol": "BTC/EUR", "base": "BTC", "quote": "EUR", "status": "online", "qty_precision": 8, '
                     '"qty_increment": 1e-08, "price_precision": 1, "cost_precision": 5, "marginable": false, '
                     '"has_index": true, "cost_min": 0.45, "tick_size": 0.1, "price_increment": 0.1, '
                     '"qty_min": 0.00006}', parse_float=Decimal)


# ---------------------------------------------------------------------------------------------------- WS budget
class _DeadWs:
    def __init__(self, clock, lifetime_s):
        self.clock, self.lifetime = clock, lifetime_s

    async def send(self, _):
        return None

    async def recv(self):
        self.clock.advance(self.lifetime)
        raise ConnectionError("dropped")

    async def close(self):
        return None


async def _run_ws(clock, *, attempts, lifetime_s=None, **kw):
    stop = asyncio.Event()
    times = []

    async def connect(_url):
        times.append(clock.monotonic())
        if len(times) >= attempts:
            stop.set()
        if lifetime_s is None:
            raise OSError("refused")
        return _DeadWs(clock, lifetime_s)

    ws = PublicWs(["BTC/EUR"], on_book=lambda s, b: None, connect=connect, clock=clock, **kw)
    await asyncio.wait_for(ws.run(stop), 5)
    return times


async def test_connection_budget_never_exceeded_in_any_10_minute_window():
    clock = ManualClock()
    times = await _run_ws(clock, attempts=40, max_attempts_per_window=6, max_backoff_s=1.0)
    for i, t in enumerate(times):
        assert sum(1 for u in times[i:] if u - t < 600) <= 6  # sliding window, never more than the budget
    assert times[-1] - times[0] >= 6 * 600 - 600  # 40 attempts at 6 / 10 min take > 50 minutes


@pytest.mark.parametrize("seed", range(25))
async def test_budget_wait_terminates_despite_float_rounding(seed):
    clock = ManualClock()
    clock.advance(1e6 + seed * 0.1234567)  # large monotonic values make the rounding residue likely
    stop = asyncio.Event()
    n = []

    async def connect(_url):
        n.append(1)
        if len(n) >= 14:
            stop.set()
        raise OSError("refused")

    ws = PublicWs(["BTC/EUR"], on_book=lambda s, b: None, connect=connect, clock=clock, rng=random.Random(seed),
                  max_attempts_per_window=3, max_backoff_s=0.7)
    await asyncio.wait_for(ws.run(stop), 5)
    assert len(n) == 14


async def test_min_backoff_floor_between_attempts():
    clock = ManualClock()
    times = await _run_ws(clock, attempts=8, min_backoff_s=30.0, max_backoff_s=1.0)
    assert all(b - a >= 30 for a, b in itertools.pairwise(times))


async def test_flapping_connection_keeps_backing_off_and_stable_one_resets():
    clock = ManualClock()
    await _run_ws(clock, attempts=8, lifetime_s=0.1, max_backoff_s=64.0)
    flap = clock.sleeps[:7]
    assert all(s >= 0.5 * 2 ** k for k, s in enumerate(flap))  # connect-then-drop does NOT reset the backoff
    clock2 = ManualClock()
    await _run_ws(clock2, attempts=6, lifetime_s=61.0, max_backoff_s=64.0)
    assert all(s <= 1.0 for s in clock2.sleeps[:5])  # each session lasted > 60 s: backoff reset every time


def test_budget_must_stay_far_below_cloudflare_limit():
    with pytest.raises(ValueError):
        PublicWs(["BTC/EUR"], on_book=lambda s, b: None, max_attempts_per_window=150)


async def test_instrument_channel_subscription_and_callback():
    clock = ManualClock()
    sent, seen = [], []
    msgs = [json.dumps({"channel": "instrument", "type": "snapshot",
                        "data": {"assets": [], "pairs": [{**{k: str(v) if isinstance(v, Decimal) else v
                                                             for k, v in WS_PAIR.items()}}]}})]
    stop = asyncio.Event()

    class Ws:
        async def send(self, m):
            sent.append(json.loads(m))

        async def recv(self):
            if msgs:
                return msgs.pop()
            stop.set()
            raise ConnectionError("end")

        async def close(self):
            return None

    async def connect(_url):
        return Ws()

    c = PublicWs(["BTC/EUR"], on_book=lambda s, b: None, on_instrument=seen.append, connect=connect, clock=clock)
    await asyncio.wait_for(c.run(stop), 5)
    assert {"method": "subscribe", "params": {"channel": "instrument", "snapshot": True}} in sent
    assert seen and seen[0]["symbol"] == "BTC/EUR"


def test_instrument_from_ws_v2():
    i = instrument_from_ws_v2(WS_PAIR, "XBT/EUR")
    assert (i.wsname, i.symbol, i.tick_size, i.lot_decimals, i.cost_decimals) == ("XBT/EUR", "BTC/EUR",
                                                                                  Decimal("0.1"), 8, 5)
    assert i.ordermin == Decimal("0.00006") and i.costmin == Decimal("0.45") and i.min_cost(Decimal("55000")) == \
        Decimal("3.30000")


def test_periodic_window():
    h = 3_600_000
    assert periodic_window(5 * h + 1, period_ms=h, window_ms=45 * 60_000) == (5 * h, 5 * h + 45 * 60_000)
    with pytest.raises(ValueError):
        periodic_window(0, period_ms=h, window_ms=2 * h)


# ---------------------------------------------------------------------------------------------------- config
def test_shadow_must_not_share_the_main_data_dir(cfg_dir):
    p = cfg_dir / "config.toml"
    p.write_text(p.read_text().replace('data_dir = "data-shadow"', 'data_dir = "data"', 1))
    with pytest.raises(ConfigError, match=r"shadow\.data_dir must differ"):
        load_settings(p, cfg_dir / ".env")


def test_shadow_window_must_leave_room_for_gtd_expiry(cfg_dir):
    p = cfg_dir / "config.toml"
    p.write_text(p.read_text().replace("window_minutes = 45 ", "window_minutes = 55 ", 1))
    with pytest.raises(ConfigError, match="must fit in the period"):
        load_settings(p, cfg_dir / ".env")


# ---------------------------------------------------------------------------------------------------- the app
def test_shadow_module_cannot_make_rest_calls():
    src = inspect.getsource(shadow_mod)
    assert "KrakenRestClient(" not in src and "httpx" not in src and "LiquiditySampler" not in src


class _Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def _book(app, bid="55566.8", ask="55597.9"):
    b = OrderBook(DCA_WS_SYMBOL, 10)
    b.snapshot([{"price": Decimal(bid), "qty": Decimal("1")}], [{"price": Decimal(ask), "qty": Decimal("1")}])
    app.md.on_book(DCA_WS_SYMBOL, b)


def _trade(app, clock, price, qty):
    ts = datetime.fromtimestamp(clock.t / 1000, tz=UTC).isoformat().replace("+00:00", "Z")
    app.md.on_trades(DCA_WS_SYMBOL, [{"price": Decimal(price), "qty": Decimal(qty), "timestamp": ts}])


@respx.mock  # any HTTP request from the shadow would fail here: it must make none
async def test_shadow_hourly_windows_exercise_repricing_gtd_partials_and_carry(cfg_dir):
    settings = load_settings(cfg_dir / "config.toml", cfg_dir / ".env")
    clock = _Clock(MONDAY_10 + 30_000)
    app = ShadowApp(settings, now_ms=clock)
    await app.start()
    try:
        _book(app)
        await app.step()
        assert app.engine is None  # no instrument yet: nothing is built from placeholders
        app._on_instrument(WS_PAIR)
        _book(app)
        await app.step()
        assert app.engine is not None and app.plan.nominal == Decimal("19.23") and app.plan.tranches == 240

        async def run_for(seconds, every=5, trade=None):
            for _ in range(seconds // every):
                clock.t += every * 1000
                _book(app)
                if trade is not None:
                    trade()
                    trade = None
                await app.step()

        async def until(t_ms, trade=None):
            await run_for((t_ms - clock.t) // 1000, trade=trade)

        h = 3_600_000
        # hour 0 (10:00): a small print below the bid -> partial fill, then GTD expiries and re-placements
        await until(MONDAY_10 + 3 * 60_000)
        await until(MONDAY_10 + 4 * 60_000, trade=lambda: _trade(app, clock, "55566.0", "0.0001"))
        await until(MONDAY_10 + h)  # through the end of the 45-minute window and its GTD expiry
        # hour 1 (11:00): no prints at all -> unfilled, the budget carries into hour 2
        await until(MONDAY_10 + 2 * h)
        # hour 2 (12:00): a big print -> filled with the carried budget (capped at 2 x nominal)
        await until(MONDAY_10 + 2 * h + 2 * 60_000)
        await until(MONDAY_10 + 2 * h + 3 * 60_000, trade=lambda: _trade(app, clock, "55560.0", "1"))
        await until(MONDAY_10 + 3 * h - 60_000)
        r = shadow_report(app.conn, clock.t)
        cov = r["coverage"]
        assert cov["repricing (attempt>=1)"] >= 3 and cov["GTD expiry"] >= 3
        assert cov["partial fill (order)"] >= 1 and cov["partial tranche"] == 1
        assert cov["unfilled tranche"] == 1 and cov["carry to next window"] >= 2
        statuses = [t.status for t in app.dca.all(Mode.DRY_RUN)]
        assert statuses[:3] == ["partial", "unfilled", "filled"]
        t3 = app.dca.all(Mode.DRY_RUN)[2]
        assert t3.budget == Decimal("38.46") and t3.carry_in > 0
        assert r["recon"] >= 2 and r["recon_ok"] == r["recon"] and r["halts"] == 0
    finally:
        await app.close(clean=True, reason="test")
    assert not settings.db_path.exists()  # the main database (and so the gate) was never touched
    assert settings.shadow_db_path.exists() and len(respx.calls) == 0
    out = io.StringIO()
    code = main(["--config", str(cfg_dir / "config.toml"), "--env", str(cfg_dir / ".env"), "shadow-report"], out=out,
                now_ms=clock)
    text = out.getvalue()
    assert code == 0 and "carry to next window   COVERED" in text and "GTD expiry             COVERED" in text
    assert not settings.db_path.exists()
