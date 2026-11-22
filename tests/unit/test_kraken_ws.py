import asyncio
import contextlib
import json
from decimal import Decimal

import pytest
import websockets

from krakbot.kraken_rest.clock import ManualClock
from krakbot.kraken_ws import OrderBook, PublicWs, checksum_of

# Official example: docs.kraken.com/exchange/guides/websockets/book-checksum-v2
DOC_BIDS = [("45283.5", "0.10000000"), ("45283.4", "1.54582015"), ("45282.1", "0.10000000"), ("45281.0", "0.10000000"),
            ("45280.3", "1.54592586"), ("45279.0", "0.07990000"), ("45277.6", "0.03310103"), ("45277.5", "0.30000000"),
            ("45277.3", "1.54602737"), ("45276.6", "0.15445238")]
DOC_ASKS = [("45285.2", "0.00100000"), ("45286.4", "1.54571953"), ("45286.6", "1.54571109"), ("45289.6", "1.54560911"),
            ("45290.2", "0.15890660"), ("45291.8", "1.54553491"), ("45294.7", "0.04454749"), ("45296.1", "0.35380000"),
            ("45297.5", "0.09945542"), ("45299.5", "0.18772827")]
DOC_CHECKSUM = 3310070434


def lv(pairs):
    return [{"price": Decimal(p), "qty": Decimal(q)} for p, q in pairs]


def test_checksum_matches_official_example():
    b = OrderBook("BTC/USD", 10)
    b.snapshot(lv(DOC_BIDS), lv(DOC_ASKS))
    assert b.checksum() == DOC_CHECKSUM


def test_book_updates_remove_and_truncate():
    b = OrderBook("BTC/USD", 10)
    b.snapshot(lv(DOC_BIDS), lv(DOC_ASKS))
    b.update([{"price": Decimal("45283.5"), "qty": Decimal("0")}], [{"price": Decimal("45284.0"), "qty": Decimal("2")}])
    assert b.top()[0] == Decimal("45283.4") and b.top()[2] == Decimal("45284.0")
    assert len(b.asks()) == 10 and Decimal("45299.5") not in dict(b.asks())  # truncated to depth
    bid, ask = b.depth_within(25)
    assert bid > 0 and ask > 0


def test_book_rejects_floats_and_updates_before_snapshot():
    b = OrderBook("BTC/USD", 10)
    with pytest.raises(RuntimeError):
        b.update([], [])
    with pytest.raises(TypeError):
        b.snapshot([{"price": 1.5, "qty": 1.0}], [])


def _snapshot_msg(sym, bids, asks):
    return json.dumps({"channel": "book", "type": "snapshot", "data": [{
        "symbol": sym, "bids": [{"price": float(p), "qty": float(q)} for p, q in bids],
        "asks": [{"price": float(p), "qty": float(q)} for p, q in asks],
        "checksum": checksum_of([(Decimal(p), Decimal(q)) for p, q in asks],
                                [(Decimal(p), Decimal(q)) for p, q in bids])}]})


def _raw_snapshot(sym):
    # keep exact decimal text (as Kraken sends it): build JSON by hand
    bids = ",".join(f'{{"price":{p},"qty":{q}}}' for p, q in DOC_BIDS)
    asks = ",".join(f'{{"price":{p},"qty":{q}}}' for p, q in DOC_ASKS)
    return f'{{"channel":"book","type":"snapshot","data":[{{"symbol":"{sym}","bids":[{bids}],"asks":[{asks}],"checksum":{DOC_CHECKSUM}}}]}}'


class FakeKraken:
    """Local WS server scripted per connection."""

    def __init__(self, script):
        self.script = script
        self.connections = 0
        self.received = []

    async def handler(self, ws):
        idx = self.connections
        self.connections += 1
        await self.script(idx, ws, self)

    async def __aenter__(self):
        self.server = await websockets.serve(self.handler, "127.0.0.1", 0)
        self.url = f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *a):
        self.server.close()
        await self.server.wait_closed()


async def _recv_json(ws, server):
    m = json.loads(await ws.recv())
    server.received.append(m)
    return m


async def _run_until(client, cond, timeout=5.0):
    stop = asyncio.Event()
    task = asyncio.create_task(client.run(stop))
    try:
        await asyncio.wait_for(_poll(cond), timeout)
    finally:
        stop.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _poll(cond):
    while not cond():
        await asyncio.sleep(0.01)


async def test_snapshot_update_and_checksum_resubscribe():
    async def script(idx, ws, srv):
        await _recv_json(ws, srv)  # book subscribe
        await _recv_json(ws, srv)  # trade subscribe
        await ws.send(_raw_snapshot("BTC/USD"))
        await ws.send(json.dumps({"channel": "heartbeat"}))
        # bad checksum update
        await ws.send('{"channel":"book","type":"update","data":[{"symbol":"BTC/USD","bids":[],"asks":[{"price":45284.0,"qty":1.0}],"checksum":1}]}')
        unsub = await _recv_json(ws, srv)
        resub = await _recv_json(ws, srv)
        assert unsub["method"] == "unsubscribe" and resub["method"] == "subscribe"
        await ws.send(_raw_snapshot("BTC/USD"))
        await ws.send('{"channel":"trade","type":"update","data":[{"symbol":"BTC/USD","side":"sell","price":45280.1,"qty":0.5,"ord_type":"market","trade_id":1,"timestamp":"2026-11-22T10:00:00.000000Z"}]}')
        await asyncio.sleep(5)

    books, trades = [], []
    async with FakeKraken(script) as srv:
        c = PublicWs(["BTC/USD"], url=srv.url, depth=10, on_book=lambda s, b: books.append(b is not None and b.valid),
                     on_trade=lambda s, t: trades.extend(t), connect=websockets.connect, clock=ManualClock())
        await _run_until(c, lambda: len(trades) == 1)
    assert c.checksum_failures == 1
    assert books[0] is True and False in books and books[-1] is True
    assert trades[0]["price"] == Decimal("45280.1")  # Decimal, not float


async def test_heartbeat_deadline_reconnects_and_resyncs():
    async def script(idx, ws, srv):
        await _recv_json(ws, srv)
        await _recv_json(ws, srv)
        await ws.send(_raw_snapshot("BTC/USD"))
        await asyncio.sleep(10)  # silence: client must drop and reconnect

    events = []
    async with FakeKraken(script) as srv:
        c = PublicWs(["BTC/USD"], url=srv.url, depth=10, heartbeat_timeout_s=0.3,
                     on_book=lambda s, b: events.append(None if b is None else b.checksum()),
                     on_trade=lambda s, t: None, connect=websockets.connect, clock=ManualClock())
        await _run_until(c, lambda: srv.connections >= 2 and events.count(DOC_CHECKSUM) >= 2)
    i = events.index(None)  # stale marker emitted between the two sessions
    assert DOC_CHECKSUM in events[:i] and DOC_CHECKSUM in events[i:]


async def test_server_close_triggers_backoff_and_reconnect():
    async def script(idx, ws, srv):
        await _recv_json(ws, srv)
        if idx == 0:
            await ws.close()
            return
        await _recv_json(ws, srv)
        await ws.send(_raw_snapshot("BTC/USD"))
        await asyncio.sleep(5)

    clock = ManualClock()
    valid = []
    async with FakeKraken(script) as srv:
        c = PublicWs(["BTC/USD"], url=srv.url, depth=10, on_book=lambda s, b: valid.append(b is not None),
                     on_trade=lambda s, t: None, connect=websockets.connect, clock=clock)
        await _run_until(c, lambda: True in valid)
    assert srv.connections >= 2 and clock.sleeps and 0.5 <= clock.sleeps[0] <= 1.0


def test_invalid_depth_rejected():
    with pytest.raises(ValueError):
        PublicWs(["BTC/USD"], on_book=lambda s, b: None, depth=50)
