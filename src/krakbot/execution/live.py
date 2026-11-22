"""LIVE broker over the Kraken REST client. Used only after the live gate; the REST client itself refuses trading
calls unless it was built with trading enabled."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

from krakbot.domain import Liquidity, OrderStatus, OrderType, Side
from krakbot.execution.types import FillEvent, Lookup, LookupKind, OrderUpdate, PlaceRequest
from krakbot.kraken_rest.client import KrakenRestClient
from krakbot.kraken_rest.errors import (
    InsufficientFundsError,
    KrakenError,
    MarketStateError,
    OrderRejectedError,
    OutcomeUnknownError,
)
from krakbot.kraken_rest.models import AddOrderRequest, Instrument

__all__ = ["KrakenBroker", "map_kraken_status"]

ABSENCE_MARGIN_MS = 60_000


def map_kraken_status(info: Mapping[str, Any]) -> OrderStatus:
    st = str(info.get("status"))
    vol, done = Decimal(str(info.get("vol", "0"))), Decimal(str(info.get("vol_exec", "0")))
    if st == "pending":
        return OrderStatus.SENT
    if st == "open":
        return OrderStatus.PARTIALLY_FILLED if done > 0 else OrderStatus.OPEN
    if st == "closed":
        return OrderStatus.FILLED if done >= vol else OrderStatus.CANCELED
    if st == "canceled":
        return OrderStatus.CANCELED
    if st == "expired":
        return OrderStatus.EXPIRED
    raise KrakenError("QueryOrders", [f"ECLIENT:unknown order status {st!r}"])


class KrakenBroker:
    def __init__(self, rest: KrakenRestClient, instruments: Mapping[str, Instrument], *,
                 now_ms: Callable[[], int]) -> None:
        self._rest = rest
        self._inst = dict(instruments)
        self._now = now_ms

    def update_instruments(self, instruments: Mapping[str, Instrument]) -> None:
        """Daily AssetPairs refresh (plan §0.1): new tick/lot/minimums apply to the next order."""
        self._inst.update(instruments)

    async def place_post_only_buy(self, req: PlaceRequest) -> OrderUpdate:
        inst = self._inst[req.pair]
        add = AddOrderRequest(pair=inst.altname, side=Side.BUY, ordertype=OrderType.LIMIT, volume=req.qty,
                              price=req.price, cl_ord_id=req.cl_ord_id, post_only=True, timeinforce="GTD",
                              expire_after_s=req.expire_after_s, deadline_s=req.deadline_s)
        try:
            r = await self._rest.add_order(add, inst)
        except (OrderRejectedError, InsufficientFundsError, MarketStateError) as exc:
            return OrderUpdate(OrderStatus.REJECTED, reason=str(exc))  # definitely not placed
        txids = r.get("txid") or []
        if not txids:
            raise OutcomeUnknownError("AddOrder", ["ECLIENT:success without txid"])
        return OrderUpdate(OrderStatus.OPEN, txid=str(txids[0]))

    async def _fills(self, info: Mapping[str, Any]) -> list[FillEvent]:
        ids = [str(t) for t in info.get("trades", []) or []]
        if not ids:
            return []
        trades = await self._rest.query_trades(ids)
        out = []
        for tid, t in trades.items():
            out.append(FillEvent(tid, int(Decimal(str(t["time"])) * 1000), Decimal(str(t["price"])),
                                 Decimal(str(t["vol"])), Decimal(str(t["fee"])),
                                 self._quote_of(str(t.get("pair", ""))),
                                 Liquidity.TAKER if t.get("maker") is False else Liquidity.MAKER))
        return out

    def _quote_of(self, rest_pair: str) -> str:
        for inst in self._inst.values():
            if rest_pair in (inst.rest_key, inst.altname):
                return inst.quote
        return "EUR"

    async def refresh(self, cl_ord_id: str, txid: str | None) -> OrderUpdate:
        if txid is None:
            look = await self.lookup(cl_ord_id, sent_at_ms=self._now(), deadline_ms=self._now())
            return look.update if look.update is not None else OrderUpdate(OrderStatus.UNKNOWN, reason=look.detail)
        r = await self._rest.query_orders([txid])
        info = r[txid]
        return OrderUpdate(map_kraken_status(info), txid=txid, fills=await self._fills(info),
                           reason=info.get("reason"))

    async def cancel(self, cl_ord_id: str, txid: str | None, *, pair: str, order_age_s: float | None) -> None:
        await self._rest.cancel_order(pair_altname=self._inst[pair].altname, order_age_s=order_age_s,
                                      cl_ord_id=cl_ord_id)

    async def lookup(self, cl_ord_id: str, *, sent_at_ms: int, deadline_ms: int) -> Lookup:
        try:
            opened = await self._rest.open_orders(cl_ord_id=cl_ord_id)
            closed = await self._rest.closed_orders(start=max(0, (sent_at_ms - 60_000) // 1000), cl_ord_id=cl_ord_id,
                                                    closetime="open")
        except (KrakenError, OSError) as exc:
            return Lookup(LookupKind.UNAVAILABLE, detail=f"{type(exc).__name__}: {exc}")
        closed_book = closed.get("closed", {}) or {}
        truncated = int(closed.get("count", 0) or 0) > len(closed_book)
        for book in (opened.get("open", {}) or {}, closed_book):
            for txid, info in book.items():
                if info.get("cl_ord_id") == cl_ord_id:
                    try:
                        fills = await self._fills(info)
                    except (KrakenError, OSError) as exc:
                        return Lookup(LookupKind.UNAVAILABLE, detail=f"found {txid} but fills unreadable: {exc}")
                    return Lookup(LookupKind.FOUND, OrderUpdate(map_kraken_status(info), txid=txid, fills=fills,
                                                                reason=info.get("reason")), detail=f"found {txid}")
        if truncated:  # a partial page is never proof of absence
            return Lookup(LookupKind.UNAVAILABLE, detail="ClosedOrders page truncated (count > rows)")
        if self._now() < deadline_ms + ABSENCE_MARGIN_MS:
            return Lookup(LookupKind.UNAVAILABLE, detail="not found yet; absence provable only after deadline + 60 s")
        return Lookup(LookupKind.ABSENT, detail="not in OpenOrders nor ClosedOrders after deadline + 60 s")
