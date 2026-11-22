"""Normalized market data hub: latest top of book per pair (from WS v2 books, REST ticker fallback), freshness, and
the fixed-interval liquidity sampler feeding `book_samples` / `spread_daily` (plan §6).

Pairs are identified by the instrument's WS v1 name (e.g. 'XBT/EUR'); WS v2 symbols ('BTC/EUR') are mapped here.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from krakbot.kraken_rest.models import BookTop, Instrument
from krakbot.kraken_ws.book import OrderBook
from krakbot.state.liquidity import DAY_MS, LiquidityRepo, spread_bp

__all__ = ["LiquiditySampler", "MarketData", "Top"]

log = logging.getLogger("krakbot.market_data")


@dataclass(frozen=True)
class Top:
    pair: str
    bid: Decimal
    bid_qty: Decimal | None
    ask: Decimal
    ask_qty: Decimal | None
    ts_ms: int
    source: str                     # "ws" | "rest"
    bid_depth: Decimal | None       # quote within depth_bps (WS only)
    ask_depth: Decimal | None

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread_bp(self) -> Decimal:
        return spread_bp(self.bid, self.ask)


TradeListener = Callable[[str, list[dict[str, Any]]], None]


class MarketData:
    def __init__(self, instruments: Mapping[str, Instrument], *, depth_bps: int, now_ms: Callable[[], int],
                 stale_after_ms: int = 10_000) -> None:
        self._inst = dict(instruments)
        self._by_symbol = {i.ws_symbol: p for p, i in instruments.items()}
        self._depth_bps = depth_bps
        self._now = now_ms
        self._stale = stale_after_ms
        self._tops: dict[str, Top] = {}
        self._trade_listeners: list[TradeListener] = []

    def update_instruments(self, instruments: Mapping[str, Instrument]) -> None:
        """Daily AssetPairs refresh: precisions/minimums only (the WS symbol set is fixed for the process)."""
        for pair, inst in instruments.items():
            if pair in self._inst and inst.ws_symbol == self._inst[pair].ws_symbol:
                self._inst[pair] = inst

    @property
    def ws_symbols(self) -> list[str]:
        return sorted(self._by_symbol)

    def pair_of_symbol(self, symbol: str) -> str | None:
        return self._by_symbol.get(symbol)

    def add_trade_listener(self, fn: TradeListener) -> None:
        self._trade_listeners.append(fn)

    # ---- inputs
    def on_book(self, symbol: str, book: OrderBook | None) -> None:
        pair = self._by_symbol.get(symbol)
        if pair is None:
            return
        if book is None or book.top() is None:
            self._tops.pop(pair, None)  # stale: never serve a book we know is broken
            return
        bp, bq, ap, aq = book.top()  # type: ignore[misc]
        depth = book.depth_within(self._depth_bps)
        self._tops[pair] = Top(pair, bp, bq, ap, aq, self._now(), "ws", depth[0] if depth else None,
                               depth[1] if depth else None)

    def on_rest_ticker(self, pair: str, t: BookTop) -> None:
        cur = self._tops.get(pair)
        if cur is not None and cur.source == "ws" and self._now() - cur.ts_ms < self._stale:
            return  # a fresh WS book always wins over REST
        self._tops[pair] = Top(pair, t.bid, None, t.ask, None, self._now(), "rest", None, None)

    def on_trades(self, symbol: str, trades: list[dict[str, Any]]) -> None:
        pair = self._by_symbol.get(symbol)
        if pair is None:
            return
        for fn in self._trade_listeners:
            fn(pair, trades)

    # ---- outputs
    def top(self, pair: str, *, require_ws: bool = False) -> Top | None:
        t = self._tops.get(pair)
        if t is None or self._now() - t.ts_ms > self._stale:
            return None
        if require_ws and t.source != "ws":
            return None
        return t

    def needs_rest_fallback(self, pair: str) -> bool:
        t = self._tops.get(pair)
        return t is None or self._now() - t.ts_ms > self._stale


class LiquiditySampler:
    """Every `sample_seconds`, store one sample per pair from a FRESH WS book (stale or REST-only data is not sampled,
    so outages lower the coverage instead of being papered over). Timestamps are aligned to the sample grid so a
    restart can never create two samples in the same slot. Rolls up the previous UTC day once it is complete."""

    def __init__(self, md: MarketData, repo: LiquidityRepo, pairs: list[str], *, sample_seconds: int,
                 now_ms: Callable[[], int]) -> None:
        self._md, self._repo, self._pairs = md, repo, pairs
        self._step = sample_seconds * 1000
        self._now = now_ms
        self._last_slot: int | None = None
        self._last_rollup_day: int | None = None

    def tick(self) -> int:
        now = self._now()
        slot = now // self._step * self._step
        written = 0
        if slot != self._last_slot:
            self._last_slot = slot
            for pair in self._pairs:
                t = self._md.top(pair, require_ws=True)
                if t is None or t.bid_depth is None or t.ask_depth is None:
                    continue
                if self._repo.add_sample(pair, slot, mid=t.mid, spread=t.spread_bp, bid_depth=t.bid_depth,
                                         ask_depth=t.ask_depth):
                    written += 1
        day = now // DAY_MS
        if self._last_rollup_day is None:
            self._last_rollup_day = day
        elif day > self._last_rollup_day:
            for d in range(self._last_rollup_day, day):
                for pair in self._pairs:
                    self._repo.rollup_day(pair, d)
            self._last_rollup_day = day
        return written
