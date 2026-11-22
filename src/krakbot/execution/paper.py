"""DRY_RUN broker: simulated maker fills from live public trades, zero real orders.

Fill model (conservative, same family as the research simulator): a resting post-only BUY at price P fills only
when a public trade prints STRICTLY BELOW P (the queue at P is assumed ahead of us); fill qty = min(remaining,
trade qty), at P; maker fee charged in quote at the configured rate, rounded UP to the pair's cost decimals.
Post-only: an order at or above the best ask is rejected (it would take liquidity). GTD expiry is enforced on
every refresh. The paper book lives in memory: after a restart, DRY_RUN orders are treated as expired (exactly what
GTD would do on the exchange within `expire_after_s`).
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from krakbot.domain import Liquidity, OrderStatus
from krakbot.execution.types import FillEvent, Lookup, LookupKind, OrderUpdate, PlaceRequest
from krakbot.kraken_rest.errors import KrakenError
from krakbot.kraken_rest.models import Instrument
from krakbot.market_data import MarketData
from krakbot.strategies.dca import fee_for

__all__ = ["PaperBroker"]


@dataclass
class _PaperOrder:
    req: PlaceRequest
    txid: str
    expire_at: int
    status: OrderStatus = OrderStatus.OPEN
    filled: Decimal = Decimal(0)
    fills: list[FillEvent] = field(default_factory=list)
    reason: str | None = None


def _ts(t: Any, default: int) -> int:
    try:
        return int(datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return default


class PaperBroker:
    def __init__(self, md: MarketData, instruments: Mapping[str, Instrument], *, fee_rate: Decimal,
                 now_ms: Callable[[], int]) -> None:
        self._md = md
        self._inst = dict(instruments)
        self._fee = fee_rate
        self._now = now_ms
        self._orders: dict[str, _PaperOrder] = {}
        self._seq = itertools.count(1)
        self.fail_next_place: Exception | None = None   # fault injection (tests / drills)
        self.lookup_unavailable = False
        md.add_trade_listener(self.on_trades)

    def update_instruments(self, instruments: Mapping[str, Instrument]) -> None:
        """Daily AssetPairs refresh (plan §0.1): new tick/lot/minimums apply to the next order."""
        self._inst.update(instruments)

    async def place_post_only_buy(self, req: PlaceRequest) -> OrderUpdate:
        if self.fail_next_place is not None:
            exc, self.fail_next_place = self.fail_next_place, None
            if isinstance(exc, KrakenError) and exc.category == "unknown_outcome":
                # ambiguous: the order MAY have been accepted; keep it so a later lookup can find it
                self._orders[req.cl_ord_id] = _PaperOrder(req, f"SIMTX-{next(self._seq)}",
                                                          self._now() + req.expire_after_s * 1000)
            raise exc
        top = self._md.top(req.pair)
        if top is None:
            return OrderUpdate(OrderStatus.REJECTED, reason="no fresh book")
        if req.price >= top.ask:
            return OrderUpdate(OrderStatus.REJECTED, reason="post-only order would take liquidity")
        o = _PaperOrder(req, f"SIMTX-{next(self._seq)}", self._now() + req.expire_after_s * 1000)
        self._orders[req.cl_ord_id] = o
        return OrderUpdate(OrderStatus.OPEN, txid=o.txid)

    def on_trades(self, pair: str, trades: list[dict[str, Any]]) -> None:
        now = self._now()
        for o in self._orders.values():
            if o.status not in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED) or o.req.pair != pair:
                continue
            if now >= o.expire_at:
                continue
            inst = self._inst[pair]
            for t in trades:
                price = Decimal(str(t["price"]))
                if price >= o.req.price:
                    continue
                q = min(o.req.qty - o.filled, Decimal(str(t["qty"])))
                if q <= 0:
                    break
                fee = fee_for(o.req.price * q, self._fee, inst)
                o.filled += q
                o.fills.append(FillEvent(f"SIM-{o.txid}-{len(o.fills) + 1}", _ts(t.get("timestamp"), now),
                                         o.req.price, q, fee, inst.quote, Liquidity.MAKER))
                o.status = OrderStatus.FILLED if o.filled == o.req.qty else OrderStatus.PARTIALLY_FILLED

    def _update(self, o: _PaperOrder) -> OrderUpdate:
        if o.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED) and self._now() >= o.expire_at:
            o.status = OrderStatus.EXPIRED
        return OrderUpdate(o.status, txid=o.txid, fills=list(o.fills), reason=o.reason)

    async def refresh(self, cl_ord_id: str, txid: str | None) -> OrderUpdate:
        o = self._orders.get(cl_ord_id)
        if o is None:
            return OrderUpdate(OrderStatus.EXPIRED, reason="paper broker restarted (order lost, as GTD would expire)")
        return self._update(o)

    async def cancel(self, cl_ord_id: str, txid: str | None, *, pair: str, order_age_s: float | None) -> None:
        o = self._orders.get(cl_ord_id)
        if o is not None and o.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED):
            o.status, o.reason = OrderStatus.CANCELED, "canceled by bot"

    async def lookup(self, cl_ord_id: str, *, sent_at_ms: int, deadline_ms: int) -> Lookup:
        if self.lookup_unavailable:
            return Lookup(LookupKind.UNAVAILABLE, detail="simulated exchange unreachable")
        o = self._orders.get(cl_ord_id)
        if o is not None:
            return Lookup(LookupKind.FOUND, self._update(o))
        if self._now() < deadline_ms + 60_000:
            return Lookup(LookupKind.UNAVAILABLE, detail="too early to prove absence (deadline + 60 s)")
        return Lookup(LookupKind.ABSENT, detail="not in paper book after deadline + 60 s")
