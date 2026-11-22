"""Level-2 order book for the Kraken WS v2 `book` channel, kept in Decimal, with the CRC32 checksum
(docs: exchange/guides/websockets/book-checksum-v2). Prices/quantities must be parsed with Decimal
(json.loads(..., parse_float=Decimal)) so that the checksum formatting is exact."""

from __future__ import annotations

import zlib
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

__all__ = ["OrderBook", "checksum_of"]


def _fmt(d: Decimal) -> str:
    s = format(d, "f").replace(".", "").lstrip("0")
    return s or "0"


def checksum_of(asks: Iterable[tuple[Decimal, Decimal]], bids: Iterable[tuple[Decimal, Decimal]]) -> int:
    """asks sorted low->high, bids high->low; top 10 of each."""
    a = "".join(_fmt(p) + _fmt(q) for p, q in list(asks)[:10])
    b = "".join(_fmt(p) + _fmt(q) for p, q in list(bids)[:10])
    return zlib.crc32((a + b).encode()) & 0xFFFFFFFF


class OrderBook:
    def __init__(self, symbol: str, depth: int) -> None:
        self.symbol = symbol
        self.depth = depth
        self._bids: dict[Decimal, Decimal] = {}
        self._asks: dict[Decimal, Decimal] = {}
        self.valid = False

    def _apply(self, side: dict[Decimal, Decimal], levels: Iterable[Mapping[str, Any]]) -> None:
        for lv in levels:
            if not isinstance(lv["price"], (Decimal, str, int)) or not isinstance(lv["qty"], (Decimal, str, int)):
                raise TypeError("book levels must be parsed as Decimal (parse_float=Decimal)")
            p, q = Decimal(lv["price"]), Decimal(lv["qty"])
            if q == 0:
                side.pop(p, None)
            else:
                side[p] = q

    def _truncate(self) -> None:
        for side, rev in ((self._bids, True), (self._asks, False)):
            keep = sorted(side, reverse=rev)[: self.depth]
            for p in set(side) - set(keep):
                del side[p]

    def snapshot(self, bids: Iterable[Mapping[str, Any]], asks: Iterable[Mapping[str, Any]]) -> None:
        self._bids, self._asks = {}, {}
        self._apply(self._bids, bids)
        self._apply(self._asks, asks)
        self._truncate()
        self.valid = True

    def update(self, bids: Iterable[Mapping[str, Any]], asks: Iterable[Mapping[str, Any]]) -> None:
        if not self.valid:
            raise RuntimeError("update before snapshot")
        self._apply(self._bids, bids)
        self._apply(self._asks, asks)
        self._truncate()

    def bids(self) -> list[tuple[Decimal, Decimal]]:
        return sorted(self._bids.items(), key=lambda x: x[0], reverse=True)

    def asks(self) -> list[tuple[Decimal, Decimal]]:
        return sorted(self._asks.items(), key=lambda x: x[0])

    def checksum(self) -> int:
        return checksum_of(self.asks(), self.bids())

    def top(self) -> tuple[Decimal, Decimal, Decimal, Decimal] | None:
        if not (self.valid and self._bids and self._asks):
            return None
        (bp, bq), (ap, aq) = self.bids()[0], self.asks()[0]
        return bp, bq, ap, aq

    def depth_within(self, bps: int) -> tuple[Decimal, Decimal] | None:
        """Quote notional resting within `bps` of mid on each side (bid side, ask side)."""
        t = self.top()
        if t is None:
            return None
        mid = (t[0] + t[2]) / 2
        lo, hi = mid * (1 - Decimal(bps) / 10_000), mid * (1 + Decimal(bps) / 10_000)
        bid = sum((p * q for p, q in self._bids.items() if p >= lo), Decimal(0))
        ask = sum((p * q for p, q in self._asks.items() if p <= hi), Decimal(0))
        return bid, ask
