"""Public WebSocket v2 client (book + trade) with heartbeat deadline, jittered reconnect and resync.

* subscribes `book` (depth, snapshot) and `trade` for the given v2 symbols; `heartbeat` and `status` arrive
  automatically once connected;
* any silence longer than `heartbeat_timeout_s` -> the connection is considered dead and is replaced;
* after EVERY (re)connection all books are discarded and rebuilt from the new snapshot (no delta merging across
  a gap); `on_book(symbol, None)` is emitted on disconnect so consumers mark the data stale;
* checksum mismatch -> the book is invalidated and that symbol is resubscribed (fresh snapshot).
* connection budget (docs: Cloudflare allows ~150 connection attempts per IP per rolling 10 minutes and bans the IP
  for 10 minutes beyond that; the ban would hit every process on the VPS): at most `max_attempts_per_window`
  attempts per `attempt_window_s` per process, a floor of `min_backoff_s` between attempts, and the backoff only
  resets after a session that stayed up for `stable_after_s` (a connection that drops at once keeps backing off);
* optional `instrument` channel (reference data: tick, lot, minimums) for REST-free consumers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from decimal import Decimal
from typing import Any

import websockets

from krakbot.kraken_rest.clock import Clock, SystemClock
from krakbot.kraken_ws.book import OrderBook

__all__ = ["WS_URL", "PublicWs"]

WS_URL = "wss://ws.kraken.com/v2"
log = logging.getLogger("krakbot.kraken_ws")

BookCb = Callable[[str, OrderBook | None], None]
TradeCb = Callable[[str, list[dict[str, Any]]], None]
StatusCb = Callable[[dict[str, Any]], None]
AliveCb = Callable[[], None]
InstrumentCb = Callable[[dict[str, Any]], None]
Connect = Callable[[str], Awaitable[Any]]


class PublicWs:
    def __init__(self, symbols: Sequence[str], *, on_book: BookCb, on_trade: TradeCb | None = None,
                 on_status: StatusCb | None = None, url: str = WS_URL, depth: int = 100,
                 heartbeat_timeout_s: float = 5.0, max_backoff_s: float = 60.0, connect: Connect | None = None,
                 clock: Clock | None = None, rng: random.Random | None = None,
                 on_alive: AliveCb | None = None, on_instrument: InstrumentCb | None = None,
                 max_attempts_per_window: int = 30, attempt_window_s: float = 600.0, min_backoff_s: float = 0.0,
                 stable_after_s: float = 60.0) -> None:
        if not 1 <= max_attempts_per_window <= 100:
            raise ValueError("max_attempts_per_window must stay far below Cloudflare's ~150 per 10 min per IP")
        if depth not in (10, 25, 100, 500, 1000):
            raise ValueError("Kraken book depth must be one of 10, 25, 100, 500, 1000")
        self.symbols = list(symbols)
        self._on_book, self._on_trade, self._on_status = on_book, on_trade, on_status
        self._on_alive = on_alive  # every message (heartbeats included) proves the session and its books are live
        self._on_instrument = on_instrument
        self._budget, self._budget_window = max_attempts_per_window, attempt_window_s
        self._min_backoff, self._stable_after = min_backoff_s, stable_after_s
        self._attempts: deque[float] = deque()
        self._url, self._depth = url, depth
        self._hb, self._max_backoff = heartbeat_timeout_s, max_backoff_s
        self._connect = connect or _default_connect
        self._clock = clock or SystemClock()
        self._rng = rng or random.Random()  # noqa: S311 - reconnect jitter, not cryptography
        self.books: dict[str, OrderBook] = {}
        self.connected = False
        self.connections = 0
        self.attempts_total = 0
        self.checksum_failures = 0

    async def _respect_budget(self) -> None:
        while True:
            now = self._clock.monotonic()
            while self._attempts and now - self._attempts[0] >= self._budget_window:
                self._attempts.popleft()
            if len(self._attempts) < self._budget:
                self._attempts.append(now)
                return
            # overshoot by 10 ms: sleeping exactly `wait` can leave now - oldest a float hair below the window, and
            # the loop would then sleep ~0 forever (same trap as the REST limiter in block 2)
            wait = self._attempts[0] + self._budget_window - now + 0.01
            log.warning("ws connection budget reached: waiting", extra={"wait_s": round(wait, 1)})
            await self._clock.sleep(wait)

    async def run(self, stop: asyncio.Event) -> None:
        attempt = 0
        while not stop.is_set():
            started = None
            try:
                await self._respect_budget()
                self.attempts_total += 1
                ws = await self._connect(self._url)
                self.connections += 1
                started = self._clock.monotonic()
                try:
                    await self._session(ws, stop)
                finally:
                    await _close(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("ws disconnected", extra={"error": type(exc).__name__, "detail": str(exc)[:200]})
            self._mark_all_stale()
            if stop.is_set():
                break
            if started is not None and self._clock.monotonic() - started >= self._stable_after:
                attempt = 0  # only a session that really worked resets the backoff
            delay = max(self._min_backoff, min(self._max_backoff, 2.0**attempt) * (0.5 + self._rng.random() / 2))
            attempt += 1
            await self._clock.sleep(delay)

    def _mark_all_stale(self) -> None:
        self.connected = False
        for sym, book in self.books.items():
            book.valid = False
            self._on_book(sym, None)

    async def _subscribe_book(self, ws: Any, symbols: Sequence[str]) -> None:
        await ws.send(json.dumps({"method": "subscribe", "params": {"channel": "book", "symbol": list(symbols),
                                                                   "depth": self._depth, "snapshot": True}}))

    async def _session(self, ws: Any, stop: asyncio.Event) -> None:
        self.books = {s: OrderBook(s, self._depth) for s in self.symbols}
        await self._subscribe_book(ws, self.symbols)
        if self._on_trade is not None:
            await ws.send(json.dumps({"method": "subscribe", "params": {"channel": "trade", "symbol": self.symbols,
                                                                       "snapshot": False}}))
        if self._on_instrument is not None:
            await ws.send(json.dumps({"method": "subscribe", "params": {"channel": "instrument", "snapshot": True}}))
        self.connected = True
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=self._hb)
            except TimeoutError as exc:
                raise ConnectionError(f"no message for {self._hb}s (heartbeat deadline)") from exc
            msg = json.loads(raw, parse_float=Decimal)
            await self._handle(ws, msg)
            if self._on_alive is not None:
                self._on_alive()

    async def _handle(self, ws: Any, msg: dict[str, Any]) -> None:
        ch = msg.get("channel")
        if ch == "book":
            for d in msg.get("data", []):
                sym = d["symbol"]
                book = self.books.get(sym)
                if book is None:
                    continue
                if msg.get("type") == "snapshot":
                    book.snapshot(d.get("bids", []), d.get("asks", []))
                elif book.valid:
                    book.update(d.get("bids", []), d.get("asks", []))
                else:
                    continue  # waiting for the snapshot after a resubscribe
                if "checksum" in d and book.checksum() != int(d["checksum"]):
                    self.checksum_failures += 1
                    book.valid = False
                    self._on_book(sym, None)
                    log.warning("book checksum mismatch: resubscribing", extra={"symbol": sym})
                    await ws.send(json.dumps({"method": "unsubscribe", "params": {"channel": "book", "symbol": [sym],
                                                                                 "depth": self._depth}}))
                    await self._subscribe_book(ws, [sym])
                    continue
                self._on_book(sym, book)
        elif ch == "trade" and self._on_trade is not None:
            by_sym: dict[str, list[dict[str, Any]]] = {}
            for t in msg.get("data", []):
                by_sym.setdefault(t["symbol"], []).append(t)
            for sym, trades in by_sym.items():
                self._on_trade(sym, trades)
        elif ch == "instrument" and self._on_instrument is not None:
            for pair in (msg.get("data") or {}).get("pairs", []) or []:
                self._on_instrument(pair)
        elif ch == "status" and self._on_status is not None:
            for d in msg.get("data", []):
                self._on_status(d)
        elif msg.get("method") == "subscribe" and msg.get("success") is False:
            raise ConnectionError(f"subscription rejected: {msg.get('error')}")


async def _default_connect(url: str) -> Any:
    return await websockets.connect(url, open_timeout=10, ping_interval=20, ping_timeout=20, max_size=2**22)


async def _close(ws: Any) -> None:
    with contextlib.suppress(Exception):  # closing a dead socket may fail; nothing to do
        await ws.close()
