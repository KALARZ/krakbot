"""KrakenBroker over a mocked Kraken REST API (respx) and the dead man's switch controller. No real requests."""

import urllib.parse
from decimal import Decimal

import httpx
import pytest
import respx
from pydantic import SecretStr

from krakbot.domain import OrderStatus
from krakbot.execution.dms import DmsController
from krakbot.execution.live import KrakenBroker, map_kraken_status
from krakbot.execution.types import LookupKind, PlaceRequest
from krakbot.kraken_rest import InMemoryNonceStore, KrakenRestClient, NonceSource, OutcomeUnknownError, RetryPolicy
from krakbot.kraken_rest.clock import ManualClock
from krakbot.kraken_rest.errors import KrakenError
from tests.unit.dca_harness import INST

BASE = "https://api.kraken.com/0/private/"
SECRET = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
CL = "6f1d2c3b-4a5e-4f60-8a7b-9c0d1e2f3a4b"


def ok(result):
    return httpx.Response(200, json={"error": [], "result": result})


def form(request) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(request.content.decode()))


@pytest.fixture
async def client():
    clock = ManualClock()
    async with httpx.AsyncClient() as http:
        yield KrakenRestClient(http=http, api_key=SecretStr("TESTKEY-NOT-REAL-0123456789"),
                               api_secret=SecretStr(SECRET), nonce=NonceSource(InMemoryNonceStore()),
                               trading_enabled=True, clock=clock, retry=RetryPolicy(max_attempts=1))


@pytest.fixture
def now():
    return {"t": 1_795_428_030_000}


def broker(client, now):
    return KrakenBroker(client, {"XBT/EUR": INST}, now_ms=lambda: now["t"])


def req(**kw):
    base = dict(cl_ord_id=CL, pair="XBT/EUR", price=Decimal("55566.8"), qty=Decimal("0.00034470"), expire_after_s=300,
                deadline_s=10)
    base.update(kw)
    return PlaceRequest(**base)


@pytest.mark.parametrize("info,expected", [
    ({"status": "pending", "vol": "1", "vol_exec": "0"}, OrderStatus.SENT),
    ({"status": "open", "vol": "1", "vol_exec": "0"}, OrderStatus.OPEN),
    ({"status": "open", "vol": "1", "vol_exec": "0.4"}, OrderStatus.PARTIALLY_FILLED),
    ({"status": "closed", "vol": "1", "vol_exec": "1"}, OrderStatus.FILLED),
    ({"status": "canceled", "vol": "1", "vol_exec": "0.2"}, OrderStatus.CANCELED),
    ({"status": "expired", "vol": "1", "vol_exec": "0"}, OrderStatus.EXPIRED),
])
def test_status_mapping(info, expected):
    assert map_kraken_status(info) == expected


def test_unknown_status_is_an_error_not_a_guess():
    with pytest.raises(KrakenError):
        map_kraken_status({"status": "weird"})


@respx.mock
async def test_place_is_post_only_gtd_with_deadline_and_client_id(client, now):
    route = respx.post(BASE + "AddOrder").mock(return_value=ok({"descr": {"order": "buy"}, "txid": ["OABC-1"]}))
    upd = await broker(client, now).place_post_only_buy(req())
    assert upd.status == OrderStatus.OPEN and upd.txid == "OABC-1"
    f = form(route.calls.last.request)
    assert f["oflags"] == "post" and f["timeinforce"] == "GTD" and f["expiretm"] == "+300"
    assert f["cl_ord_id"] == CL and f["type"] == "buy" and f["ordertype"] == "limit" and "deadline" in f
    assert f["pair"] == "XBTEUR" and f["volume"] == "0.00034470" and f["price"] == "55566.8"


@respx.mock
async def test_place_definite_rejections_vs_ambiguous(client, now):
    respx.post(BASE + "AddOrder").mock(return_value=httpx.Response(200, json={"error": ["EOrder:Post only order"]}))
    upd = await broker(client, now).place_post_only_buy(req())
    assert upd.status == OrderStatus.REJECTED
    respx.post(BASE + "AddOrder").mock(return_value=httpx.Response(200, json={"error": ["EOrder:Insufficient funds"]}))
    assert (await broker(client, now).place_post_only_buy(req())).status == OrderStatus.REJECTED
    respx.post(BASE + "AddOrder").mock(side_effect=httpx.ReadTimeout("timeout"))
    with pytest.raises(OutcomeUnknownError):  # the request may have reached the engine: never resent
        await broker(client, now).place_post_only_buy(req())
    respx.post(BASE + "AddOrder").mock(return_value=ok({"descr": {}}))  # success without txid: ambiguous
    with pytest.raises(OutcomeUnknownError):
        await broker(client, now).place_post_only_buy(req())


@respx.mock
async def test_lookup_found_with_fills_filters_by_client_id(client, now):
    o_route = respx.post(BASE + "OpenOrders").mock(return_value=ok({"open": {}}))
    c_route = respx.post(BASE + "ClosedOrders").mock(return_value=ok({"closed": {"OABC-1": {
        "cl_ord_id": CL, "status": "closed", "vol": "0.0003447", "vol_exec": "0.0003447", "trades": ["TX1"]}},
        "count": 1}))
    respx.post(BASE + "QueryTrades").mock(return_value=ok({"TX1": {
        "time": 1795428040.5, "price": "55566.8", "vol": "0.0003447", "fee": "0.07662", "pair": "XXBTZEUR",
        "maker": True}}))
    look = await broker(client, now).lookup(CL, sent_at_ms=now["t"] - 5_000, deadline_ms=now["t"] + 5_000)
    assert look.kind == LookupKind.FOUND and look.update.status == OrderStatus.FILLED and look.update.txid == "OABC-1"
    f = look.update.fills[0]
    assert f.exec_id == "TX1" and f.fee == Decimal("0.07662") and f.fee_ccy == "EUR" and f.ts_ms == 1_795_428_040_500
    assert form(o_route.calls.last.request)["cl_ord_id"] == CL
    cf = form(c_route.calls.last.request)
    assert cf["cl_ord_id"] == CL and cf["closetime"] == "open" and int(cf["start"]) == (now["t"] - 65_000) // 1000


@respx.mock
async def test_absence_only_after_deadline_plus_margin_and_only_on_successful_queries(client, now):
    respx.post(BASE + "OpenOrders").mock(return_value=ok({"open": {}}))
    respx.post(BASE + "ClosedOrders").mock(return_value=ok({"closed": {}, "count": 0}))
    b = broker(client, now)
    sent = now["t"] - 30_000
    look = await b.lookup(CL, sent_at_ms=sent, deadline_ms=sent + 10_000)
    assert look.kind == LookupKind.UNAVAILABLE  # 30 s after send: too early to prove anything
    now["t"] = sent + 10_000 + 60_001
    look = await b.lookup(CL, sent_at_ms=sent, deadline_ms=sent + 10_000)
    assert look.kind == LookupKind.ABSENT
    respx.post(BASE + "ClosedOrders").mock(return_value=ok({"closed": {}, "count": 3}))  # truncated page
    assert (await b.lookup(CL, sent_at_ms=sent, deadline_ms=sent + 10_000)).kind == LookupKind.UNAVAILABLE
    respx.post(BASE + "OpenOrders").mock(side_effect=httpx.ConnectError("down"))
    look = await b.lookup(CL, sent_at_ms=sent, deadline_ms=sent + 10_000)
    assert look.kind == LookupKind.UNAVAILABLE  # unreachable is never evidence of absence
    respx.post(BASE + "OpenOrders").mock(return_value=httpx.Response(200, json={"error": ["EService:Unavailable"]}))
    assert (await b.lookup(CL, sent_at_ms=sent, deadline_ms=sent + 10_000)).kind == LookupKind.UNAVAILABLE


@respx.mock
async def test_refresh_by_txid_and_cancel_by_client_id(client, now):
    respx.post(BASE + "QueryOrders").mock(return_value=ok({"OABC-1": {
        "status": "expired", "vol": "0.0003447", "vol_exec": "0", "trades": []}}))
    upd = await broker(client, now).refresh(CL, "OABC-1")
    assert upd.status == OrderStatus.EXPIRED and upd.fills == []
    route = respx.post(BASE + "CancelOrder").mock(return_value=ok({"count": 1}))
    await broker(client, now).cancel(CL, "OABC-1", pair="XBT/EUR", order_age_s=400.0)
    assert form(route.calls.last.request)["cl_ord_id"] == CL


@respx.mock
async def test_dms_entry_only_arms_while_orders_work_and_disarms(client, now):
    calls = []
    respx.post(BASE + "CancelAllOrdersAfter").mock(
        side_effect=lambda r: (calls.append(form(r)["timeout"]), ok({"currentTime": "x", "triggerTime": "y"}))[1])
    d = DmsController(client, mode="entry_only", timeout_s=60, refresh_s=20, now_ms=lambda: now["t"])
    await d.disarm()
    await d.maintain(entries_working=False, stops_resting=False)
    await d.maintain(entries_working=True, stops_resting=False)
    now["t"] += 10_000
    await d.maintain(entries_working=True, stops_resting=False)  # within refresh: no call
    now["t"] += 10_000
    await d.maintain(entries_working=True, stops_resting=False)  # refresh
    await d.maintain(entries_working=True, stops_resting=True)   # a resting stop must never be at risk: disarm
    assert calls == ["0", "60", "60", "0"] and not d.armed


@respx.mock
async def test_dms_failure_is_counted_not_raised(client, now):
    respx.post(BASE + "CancelAllOrdersAfter").mock(
        return_value=httpx.Response(200, json={"error": ["EGeneral:Permission denied"]}))
    d = DmsController(client, mode="always", timeout_s=60, refresh_s=20, now_ms=lambda: now["t"])
    await d.maintain(entries_working=False, stops_resting=False)
    assert d.failures == 1 and not d.armed
