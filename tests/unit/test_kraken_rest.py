import asyncio
import base64
import hashlib
import hmac
import random
import urllib.parse
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import respx
from pydantic import SecretStr

from krakbot.domain import OrderType, Side
from krakbot.kraken_rest import (
    AddOrderRequest,
    ErrorCategory,
    InMemoryNonceStore,
    KrakenRestClient,
    LocalValidationError,
    NonceSource,
    OutcomeUnknownError,
    RetryPolicy,
    SqliteNonceStore,
    TradingDisabledError,
    classify,
)
from krakbot.kraken_rest.clock import ManualClock
from krakbot.kraken_rest.errors import AuthError, InsufficientFundsError, InvalidNonceError, RateLimitError
from krakbot.kraken_rest.models import parse_asset_pairs
from krakbot.kraken_rest.ratelimit import EngineCounter, PublicLimiter, RestCallCounter, engine_cost, rest_cost
from krakbot.kraken_rest.signing import decode_secret, sign
from krakbot.state import connect, migrate

BASE = "https://api.kraken.com"
# Official example from docs.kraken.com/exchange/guides/rest/authentication
DOC_SECRET = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
DOC_BODY = "nonce=1616492376594&ordertype=limit&pair=XBTUSD&price=37500&type=buy&volume=1.25"
DOC_SIGN = "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ=="
API_KEY = "TESTKEY-NOT-REAL-0123456789"
PAIRS = {"XXBTZEUR": {"altname": "XBTEUR", "wsname": "XBT/EUR", "base": "XXBT", "quote": "ZEUR", "pair_decimals": 1,
                      "lot_decimals": 8, "cost_decimals": 5, "tick_size": "0.1", "ordermin": "0.00006",
                      "costmin": "0.45", "status": "online"},
         "XBTEUR.d": {"altname": "XBTEUR.d", "base": "XXBT", "quote": "ZEUR"}}  # dark pool entry without wsname
OK = {"error": [], "result": {"ZEUR": {"balance": "100.0", "hold_trade": "0"}}}


def ok(result):
    return httpx.Response(200, json={"error": [], "result": result})


def err(*e):
    return httpx.Response(200, json={"error": list(e)})


@pytest.fixture
def clock():
    return ManualClock()


@pytest.fixture
def inst():
    return parse_asset_pairs(PAIRS)["XBT/EUR"]


async def make(clock, *, trading=False, store=None, max_attempts=5):
    http = httpx.AsyncClient(base_url=BASE)
    nonce = NonceSource(store or InMemoryNonceStore(), clock_ms=lambda: int(clock.time() * 1000))
    c = KrakenRestClient(http=http, api_key=SecretStr(API_KEY), api_secret=SecretStr(DOC_SECRET), nonce=nonce,
                         trading_enabled=trading, clock=clock,
                         retry=RetryPolicy(max_attempts=max_attempts, rng=random.Random(7)))
    return c, http


def body_of(call):
    return dict(urllib.parse.parse_qsl(call.request.content.decode()))


def verify_signature(call):
    req = call.request
    body = req.content.decode()
    nonce = dict(urllib.parse.parse_qsl(body))["nonce"]
    sha = hashlib.sha256((nonce + body).encode()).digest()
    mac = hmac.new(base64.b64decode(DOC_SECRET), req.url.path.encode() + sha, hashlib.sha512)
    expected = base64.b64encode(mac.digest())
    return req.headers["API-Sign"] == expected.decode()


# ---------------------------------------------------------------- signing / nonce
def test_signature_matches_official_test_vector():
    assert sign("/0/private/AddOrder", 1616492376594, DOC_BODY, decode_secret(DOC_SECRET)) == DOC_SIGN


@pytest.mark.parametrize("bad", ["not base64!!", base64.b64encode(b"short").decode()])
def test_decode_secret_rejects_bad_secrets(bad):
    with pytest.raises(ValueError):
        decode_secret(bad)


def test_nonce_monotonic_even_if_clock_goes_back_and_after_restart():
    t = [2_000]
    store = InMemoryNonceStore()
    n = NonceSource(store, clock_ms=lambda: t[0])
    a, b = n.next(), n.next()
    t[0] = 1_000  # clock stepped back
    c = n.next()
    assert a < b < c and store.value == c
    restarted = NonceSource(store, clock_ms=lambda: t[0])
    assert restarted.next() > c


def test_sqlite_nonce_store_persists_and_never_decreases(tmp_path):
    conn = connect(tmp_path / "n.db")
    try:
        migrate(conn)
        s = SqliteNonceStore(conn)
        assert s.load() == 0
        s.save(500)
        s.save(400)  # a lower value never overwrites the high-water mark
        assert s.load() == 500
        assert NonceSource(SqliteNonceStore(conn), clock_ms=lambda: 1).next() == 501
    finally:
        conn.close()


# ---------------------------------------------------------------- private requests
@respx.mock(base_url=BASE)
async def test_private_call_signed_and_nonces_increase(respx_mock, clock):
    route = respx_mock.post("/0/private/BalanceEx").mock(return_value=httpx.Response(200, json=OK))
    store = InMemoryNonceStore()
    c, http = await make(clock, store=store)
    async with http:
        for _ in range(3):
            bal = await c.balance_ex()
    assert bal["ZEUR"]["balance"] == Decimal("100.0")
    nonces = [int(body_of(call)["nonce"]) for call in route.calls]
    assert nonces == sorted(set(nonces)) and store.value == nonces[-1]
    for call in route.calls:
        assert call.request.headers["API-Key"] == API_KEY
        assert call.request.headers["Content-Type"].startswith("application/x-www-form-urlencoded")
        assert verify_signature(call)


@respx.mock(base_url=BASE)
async def test_concurrent_private_calls_send_nonces_in_order(respx_mock, clock):
    route = respx_mock.post("/0/private/OpenOrders").mock(return_value=ok({"open": {}}))
    c, http = await make(clock)
    async with http:
        await asyncio.gather(*(c.open_orders() for _ in range(6)))
    nonces = [int(body_of(call)["nonce"]) for call in route.calls]
    assert len(nonces) == 6 and nonces == sorted(nonces) and len(set(nonces)) == 6


@respx.mock(base_url=BASE, assert_all_called=False)
async def test_trading_disabled_never_touches_the_network(respx_mock, clock, inst):
    c, http = await make(clock, trading=False)
    req = AddOrderRequest(pair="XBTEUR", side=Side.BUY, ordertype=OrderType.LIMIT, volume=Decimal("0.001"),
                          price=Decimal("50000.0"), cl_ord_id="t1", post_only=True)
    async with http:
        with pytest.raises(TradingDisabledError):
            await c.add_order(req, inst)
        with pytest.raises(TradingDisabledError):
            await c.amend_order(instrument=inst, order_age_s=10, txid="X", limit_price=Decimal("1.0"))
        with pytest.raises(TradingDisabledError):
            await c.cancel_order(pair_altname="XBTEUR", order_age_s=10, cl_ord_id="t1")
        with pytest.raises(TradingDisabledError):
            await c.cancel_all()
        with pytest.raises(TradingDisabledError):
            await c.cancel_all_orders_after(60)
    assert len(respx_mock.calls) == 0
    assert c.trading_enabled is False


def test_trading_requires_credentials_and_nonce():
    http = httpx.AsyncClient(base_url=BASE)
    with pytest.raises(ValueError):
        KrakenRestClient(http=http, trading_enabled=True)


def test_repr_and_errors_hide_credentials(clock):
    http = httpx.AsyncClient(base_url=BASE)
    c = KrakenRestClient(http=http, api_key=SecretStr(API_KEY), api_secret=SecretStr(DOC_SECRET),
                         nonce=NonceSource(InMemoryNonceStore()))
    assert API_KEY not in repr(c) and DOC_SECRET not in repr(c)


# ---------------------------------------------------------------- retries & classification
@respx.mock(base_url=BASE)
async def test_public_get_retries_on_5xx_with_bounded_jitter(respx_mock, clock):
    route = respx_mock.get("/0/public/Time").mock(side_effect=[httpx.Response(500), httpx.Response(502),
                                                                ok({"unixtime": 1})])
    c, http = await make(clock)
    async with http:
        assert (await c.server_time())["unixtime"] == 1
    assert route.call_count == 3
    retry_sleeps = [s for s in clock.sleeps if s > 0]
    assert all(0 <= s <= 60 for s in retry_sleeps)


@respx.mock(base_url=BASE)
async def test_idempotent_private_retries_service_unavailable(respx_mock, clock):
    route = respx_mock.post("/0/private/BalanceEx").mock(side_effect=[err("EService:Unavailable"),
                                                                       httpx.Response(200, json=OK)])
    c, http = await make(clock)
    async with http:
        await c.balance_ex()
    assert route.call_count == 2


@pytest.mark.parametrize("effect,exc", [
    (httpx.ReadTimeout("timeout"), OutcomeUnknownError),
    (err("EService:Deadline elapsed"), OutcomeUnknownError),
    (httpx.Response(502), OutcomeUnknownError),
    (err("EService:Unavailable"), OutcomeUnknownError),
    (err("EOrder:Insufficient funds"), InsufficientFundsError),
    (err("EAPI:Invalid key"), AuthError),
])
@respx.mock(base_url=BASE)
async def test_add_order_is_never_blindly_resent(respx_mock, clock, inst, effect, exc):
    route = respx_mock.post("/0/private/AddOrder").mock(side_effect=[effect, ok({"txid": ["OX"]})])
    c, http = await make(clock, trading=True)
    req = AddOrderRequest(pair="XBTEUR", side=Side.BUY, ordertype=OrderType.LIMIT, volume=Decimal("0.001"),
                          price=Decimal("50000.0"), cl_ord_id="t1", post_only=True)
    async with http:
        with pytest.raises(exc) as ei:
            await c.add_order(req, inst)
    assert route.call_count == 1
    assert API_KEY not in str(ei.value) and DOC_SECRET not in str(ei.value)


@respx.mock(base_url=BASE)
async def test_add_order_connect_error_is_safe_to_retry(respx_mock, clock, inst):
    route = respx_mock.post("/0/private/AddOrder").mock(side_effect=[httpx.ConnectError("refused"),
                                                                      ok({"txid": ["OX"]})])
    c, http = await make(clock, trading=True)
    req = AddOrderRequest(pair="XBTEUR", side=Side.BUY, ordertype=OrderType.LIMIT, volume=Decimal("0.001"),
                          price=Decimal("50000.0"), cl_ord_id="t1", post_only=True)
    async with http:
        assert (await c.add_order(req, inst))["txid"] == ["OX"]
    assert route.call_count == 2


@respx.mock(base_url=BASE)
async def test_invalid_nonce_retried_once_with_a_fresh_nonce(respx_mock, clock):
    route = respx_mock.post("/0/private/BalanceEx").mock(side_effect=[err("EAPI:Invalid nonce"),
                                                                       httpx.Response(200, json=OK)])
    c, http = await make(clock)
    async with http:
        await c.balance_ex()
    n = [body_of(x)["nonce"] for x in route.calls]
    assert len(n) == 2 and n[0] != n[1]
    route.side_effect = [err("EAPI:Invalid nonce"), err("EAPI:Invalid nonce")]
    async with httpx.AsyncClient(base_url=BASE) as http2:
        c2 = KrakenRestClient(http=http2, api_key=SecretStr(API_KEY), api_secret=SecretStr(DOC_SECRET),
                              nonce=NonceSource(InMemoryNonceStore()), clock=clock)
        with pytest.raises(InvalidNonceError):
            await c2.balance_ex()


@respx.mock(base_url=BASE)
async def test_rate_limit_and_throttled_wait(respx_mock, clock):
    until = int(clock.time()) + 7
    route = respx_mock.post("/0/private/OpenOrders").mock(side_effect=[err(f"EService:Throttled:[{until}]"),
                                                                        err("EAPI:Rate limit exceeded"),
                                                                        ok({"open": {}})])
    c, http = await make(clock)
    async with http:
        await c.open_orders()
    assert route.call_count == 3
    assert max(clock.sleeps) >= 7.0 - 1e-9  # honoured the Throttled timestamp


@respx.mock(base_url=BASE)
async def test_gives_up_after_max_attempts(respx_mock, clock):
    route = respx_mock.post("/0/private/OpenOrders").mock(return_value=err("EAPI:Rate limit exceeded"))
    c, http = await make(clock, max_attempts=4)
    async with http:
        with pytest.raises(RateLimitError):
            await c.open_orders()
    assert route.call_count == 4


@respx.mock(base_url=BASE)
async def test_warnings_do_not_fail(respx_mock, clock):
    respx_mock.post("/0/private/OpenOrders").mock(return_value=httpx.Response(200, json={
        "error": ["WGeneral:Deprecated"], "result": {"open": {}}}))
    c, http = await make(clock)
    async with http:
        assert await c.open_orders() == {"open": {}}


@pytest.mark.parametrize("errors,cat", [
    (["EService:Deadline elapsed"], ErrorCategory.UNKNOWN_OUTCOME),
    (["EOrder:Rate limit exceeded"], ErrorCategory.RATE_LIMIT),
    (["EService:Throttled: 1700000000"], ErrorCategory.RATE_LIMIT),
    (["EAPI:Invalid signature"], ErrorCategory.AUTH),
    (["EGeneral:Permission denied"], ErrorCategory.PERMISSION),
    (["EOrder:Insufficient funds"], ErrorCategory.INSUFFICIENT_FUNDS),
    (["EOrder:Cost minimum not met"], ErrorCategory.ORDER_REJECTED),
    (["EOrder:Tick size check failed"], ErrorCategory.ORDER_REJECTED),
    (["EService:Market in cancel_only mode"], ErrorCategory.MARKET_STATE),
    (["EOrder:Unknown order"], ErrorCategory.UNKNOWN_ORDER),
    (["EGeneral:Internal error:5"], ErrorCategory.RETRYABLE),
    (["EOrder:Insufficient funds", "EService:Deadline elapsed"], ErrorCategory.UNKNOWN_OUTCOME),
    (["ESomething:New"], ErrorCategory.OTHER),
])
def test_classification(errors, cat):
    assert classify(errors) == cat


# ---------------------------------------------------------------- rate limiters
async def test_rest_counter_starter_tier(clock):
    rc = RestCallCounter("starter", clock)  # 15 * 0.8 = 12 budget, decay 0.33/s
    for _ in range(12):
        await rc.acquire("OpenOrders")
    assert clock.sleeps == []
    await rc.acquire("OpenOrders")
    assert clock.sleeps and abs(clock.sleeps[-1] - 1 / 0.33) < 1e-4
    assert rest_cost("TradesHistory") == 2 and rest_cost("AddOrder") == 0 and rest_cost("BalanceEx") == 1


def test_engine_costs_by_age():
    assert engine_cost("add") == 1
    assert engine_cost("cancel", 3) == 8 and engine_cost("cancel", 30) == 4
    assert engine_cost("cancel", 100) == 1 and engine_cost("cancel", 400) == 0
    assert engine_cost("cancel", None) == 8  # unknown age -> worst case
    assert engine_cost("amend", 3) == 4 and engine_cost("amend", 20) == 1


async def test_engine_counter_waits_per_pair(clock):
    ec = EngineCounter("starter", clock)  # 60 * 0.8 = 48 budget per pair, decay 1/s
    for _ in range(6):
        await ec.acquire("XBTEUR", 8)
    assert clock.sleeps == []
    await ec.acquire("ETHEUR", 8)  # other pair: independent counter
    assert clock.sleeps == []
    await ec.acquire("XBTEUR", 8)
    assert clock.sleeps and abs(clock.sleeps[-1] - 8.0) < 1e-4


async def test_public_limiter_spacing(clock):
    pl = PublicLimiter(clock)
    await pl.acquire()
    await pl.acquire()
    assert clock.sleeps == [1.0]


# ---------------------------------------------------------------- models / validation
def test_parse_asset_pairs(inst):
    assert inst.altname == "XBTEUR" and inst.tick_size == Decimal("0.1") and inst.lot_step == Decimal("0.00000001")
    assert isinstance(inst.ordermin, Decimal)
    assert inst.min_cost(Decimal("54557.1")) == Decimal("0.00006") * Decimal("54557.1")
    assert set(parse_asset_pairs(PAIRS)) == {"XBT/EUR"}  # pair without wsname skipped
    bad = {"X": dict(PAIRS["XXBTZEUR"], tick_size="0")}
    with pytest.raises(ValueError):
        parse_asset_pairs(bad)


def test_add_order_params_encoding(clock):
    req = AddOrderRequest(pair="XBTEUR", side=Side.BUY, ordertype=OrderType.LIMIT, volume=Decimal("1E-5"),
                          price=Decimal("54557.1"), cl_ord_id="6d1b345e-2821-40e2-ad83-4ecb18a06876", post_only=True,
                          timeinforce="GTD", expire_after_s=90, deadline_s=10, close_ordertype=OrderType.STOP_LOSS,
                          close_price=Decimal("52000.0"))
    p = dict(req.to_params(datetime(2026, 11, 22, 10, 0, 0, 123000, tzinfo=UTC)))
    assert p["volume"] == "0.00001" and p["price"] == "54557.1" and p["oflags"] == "post"
    assert p["timeinforce"] == "GTD" and p["expiretm"] == "+90"
    assert p["deadline"] == "2026-11-22T10:00:10.123Z"
    assert p["close[ordertype]"] == "stop-loss" and p["close[price]"] == "52000.0"
    encoded = urllib.parse.urlencode(req.to_params(datetime(2026, 1, 1, tzinfo=UTC)))
    assert "expiretm=%2B90" in encoded and "close%5Bordertype%5D=stop-loss" in encoded


@pytest.mark.parametrize("kwargs,needle", [
    ({"price": Decimal("54557.15")}, "tick_size"),
    ({"volume": Decimal("0.000123456")}, "lot step"),
    ({"volume": Decimal("0.00005")}, "ordermin"),
    ({"price": Decimal("1.0"), "volume": Decimal("0.0001")}, "costmin"),
    ({"ordertype": OrderType.MARKET, "price": None}, "post_only is only valid"),
    ({"timeinforce": "GTD"}, "GTD requires"),
    ({"cl_ord_id": "has space in it"}, "cl_ord_id"),
    ({"cl_ord_id": "x" * 19}, "cl_ord_id"),
    ({"close_ordertype": OrderType.STOP_LOSS, "close_price": Decimal("60000.0")}, "below the entry"),
    ({"deadline_s": 61}, "deadline_s"),
    ({"volume": 0.001}, "finite Decimal"),
])
def test_add_order_local_validation(inst, kwargs, needle):
    base = dict(pair="XBTEUR", side=Side.BUY, ordertype=OrderType.LIMIT, volume=Decimal("0.001"),
                price=Decimal("54557.1"), cl_ord_id="t1", post_only=True)
    base.update(kwargs)
    problems = AddOrderRequest(**base).problems(inst)
    assert any(needle in x for x in problems), problems


@respx.mock(base_url=BASE, assert_all_called=False)
async def test_add_order_rejected_locally_before_io(respx_mock, clock, inst):
    c, http = await make(clock, trading=True)
    req = AddOrderRequest(pair="XBTEUR", side=Side.BUY, ordertype=OrderType.LIMIT, volume=Decimal("0.00001"),
                          price=Decimal("50000.0"), cl_ord_id="t1", post_only=True)
    async with http:
        with pytest.raises(LocalValidationError, match="ordermin"):
            await c.add_order(req, inst)
    assert len(respx_mock.calls) == 0


@respx.mock(base_url=BASE)
async def test_add_order_happy_path_body(respx_mock, clock, inst):
    route = respx_mock.post("/0/private/AddOrder").mock(return_value=ok({"txid": ["OUF4EM-FRGI2-MQMWZD"]}))
    c, http = await make(clock, trading=True)
    req = AddOrderRequest(pair="XBTEUR", side=Side.BUY, ordertype=OrderType.LIMIT, volume=Decimal("0.00100000"),
                          price=Decimal("54557.1"), cl_ord_id="t1", post_only=True, timeinforce="GTD",
                          expire_after_s=90, close_ordertype=OrderType.STOP_LOSS, close_price=Decimal("52000.0"))
    async with http:
        r = await c.add_order(req, inst)
    assert r["txid"] == ["OUF4EM-FRGI2-MQMWZD"]
    b = body_of(route.calls[0])
    assert b["ordertype"] == "limit" and b["type"] == "buy" and b["volume"] == "0.00100000"
    assert b["oflags"] == "post" and b["expiretm"] == "+90" and b["close[price]"] == "52000.0"
    assert verify_signature(route.calls[0])


@respx.mock(base_url=BASE, assert_all_called=False)
async def test_dead_mans_switch_timeout_validation(respx_mock, clock):
    c, http = await make(clock, trading=True)
    async with http:
        with pytest.raises(LocalValidationError):
            await c.cancel_all_orders_after(86400)
    assert len(respx_mock.calls) == 0


async def test_counter_terminates_on_float_residuals():
    """Regression: a large monotonic clock value used to make the acquire loop spin forever."""
    big = ManualClock()
    big.advance(10_000_000.0)
    rc = RestCallCounter("starter", big)
    await asyncio.wait_for(asyncio.gather(*(rc.acquire("OpenOrders") for _ in range(40))), timeout=5)
    assert rc.value <= 12 + 1e-6


@respx.mock(base_url=BASE)
async def test_asset_pairs_merges_ws_v2_symbol(respx_mock, clock):
    legacy = PAIRS
    display = {"BTC/EUR": dict(PAIRS["XXBTZEUR"], base="BTC", quote="EUR")}
    route = respx_mock.get("/0/public/AssetPairs").mock(side_effect=[ok(legacy), ok(display)])
    c, http = await make(clock)
    async with http:
        inst = await c.asset_pairs(["XBTEUR"])
    x = inst["XBT/EUR"]
    assert x.symbol == "BTC/EUR" and x.ws_symbol == "BTC/EUR" and x.rest_key == "XXBTZEUR"
    assert "assetVersion=1" in str(route.calls[1].request.url)


def test_ws_symbol_missing_is_an_error(inst):
    with pytest.raises(ValueError):
        _ = inst.ws_symbol
