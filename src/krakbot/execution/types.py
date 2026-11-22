"""Broker-neutral types shared by the paper (DRY_RUN) and Kraken (LIVE) brokers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from krakbot.domain import Liquidity, OrderStatus
from krakbot.kraken_rest.models import Instrument

__all__ = ["Broker", "FillEvent", "Lookup", "LookupKind", "OrderUpdate", "PlaceRequest"]


@dataclass(frozen=True)
class PlaceRequest:
    cl_ord_id: str
    pair: str              # wsname (bot id), e.g. 'XBT/EUR'
    price: Decimal
    qty: Decimal
    expire_after_s: int    # GTD lifetime
    deadline_s: int


@dataclass(frozen=True)
class FillEvent:
    exec_id: str
    ts_ms: int
    price: Decimal
    qty: Decimal
    fee: Decimal
    fee_ccy: str
    liquidity: Liquidity


@dataclass(frozen=True)
class OrderUpdate:
    status: OrderStatus
    txid: str | None = None
    fills: list[FillEvent] = field(default_factory=list)
    reason: str | None = None


class LookupKind(StrEnum):
    FOUND = "found"
    ABSENT = "absent"            # proven: successful queries after deadline + margin, order not present
    UNAVAILABLE = "unavailable"  # exchange unreachable / partial answers: NEVER evidence of absence


@dataclass(frozen=True)
class Lookup:
    kind: LookupKind
    update: OrderUpdate | None = None
    detail: str = ""


class Broker(Protocol):
    async def place_post_only_buy(self, req: PlaceRequest) -> OrderUpdate:
        """Returns OPEN (with txid) or REJECTED. Raises kraken_rest.OutcomeUnknownError when ambiguous."""
        ...

    async def refresh(self, cl_ord_id: str, txid: str | None) -> OrderUpdate: ...

    async def lookup(self, cl_ord_id: str, *, sent_at_ms: int, deadline_ms: int) -> Lookup: ...

    async def cancel(self, cl_ord_id: str, txid: str | None, *, pair: str, order_age_s: float | None) -> None: ...

    def update_instruments(self, instruments: Mapping[str, Instrument]) -> None: ...
