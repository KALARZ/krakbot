"""Typed request/response models. Every price/quantity is Decimal; requests are validated locally against the
instrument (tick, lot, ordermin, costmin) so malformed orders never reach Kraken."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from krakbot.domain import OrderType, Side
from krakbot.numeric import D, step_from_decimals, to_db

__all__ = [
    "AddOrderRequest", "BookTop", "Instrument", "LocalValidationError", "instrument_from_ws_v2",
    "merge_display_symbols", "parse_asset_pairs",
    "parse_ticker", "valid_cl_ord_id",
]

_UUID_LONG = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_UUID_SHORT = re.compile(r"^[0-9a-f]{32}$")
_FREE = re.compile(r"^[\x21-\x7e]{1,18}$")


def valid_cl_ord_id(value: str) -> bool:
    """Kraken cl_ord_id formats: long UUID, short UUID (32 hex) or free ASCII text up to 18 chars."""
    return bool(_UUID_LONG.match(value) or _UUID_SHORT.match(value) or _FREE.match(value))


class LocalValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Instrument:
    rest_key: str      # internal REST key, e.g. 'XXBTZEUR' (TradeVolume/fees are keyed by it)
    altname: str       # REST pair parameter, e.g. 'XBTEUR'
    wsname: str        # WS v1 name, e.g. 'XBT/EUR' (also the bot's internal pair id)
    base: str
    quote: str
    pair_decimals: int
    lot_decimals: int
    cost_decimals: int
    tick_size: Decimal
    ordermin: Decimal
    costmin: Decimal
    status: str
    symbol: str = ""   # WS v2 / display symbol, e.g. 'BTC/EUR' (from AssetPairs?assetVersion=1)

    @property
    def ws_symbol(self) -> str:
        if not self.symbol:
            raise ValueError(f"{self.wsname}: WS v2 symbol unknown (AssetPairs assetVersion=1 not merged)")
        return self.symbol

    @property
    def lot_step(self) -> Decimal:
        return step_from_decimals(self.lot_decimals)

    def min_cost(self, price: Decimal) -> Decimal:
        """Smallest order notional Kraken accepts at `price`: max(ordermin * price, costmin)."""
        return max(self.ordermin * price, self.costmin)


def merge_display_symbols(by_wsname: dict[str, Instrument], display_result: dict[str, Any]) -> dict[str, Instrument]:
    """Attach the WS v2 symbol from an AssetPairs?assetVersion=1 result (keys are display symbols) by altname."""
    sym_by_alt = {str(v["altname"]): k for k, v in display_result.items()}
    out = {}
    for ws, inst in by_wsname.items():
        sym = sym_by_alt.get(inst.altname, "")
        out[ws] = Instrument(**{**inst.__dict__, "symbol": sym})
    return out


def instrument_from_ws_v2(pair: dict[str, Any], wsname: str) -> Instrument:
    """Instrument from a WS v2 `instrument` channel pair (REST-free consumers, e.g. the shadow run). The REST keys
    are unknown on this path and left empty: such an Instrument is only for the paper broker."""
    def dec(k: str) -> Decimal:
        v = pair[k]
        return v if isinstance(v, Decimal) else D(str(v))
    inst = Instrument(rest_key="", altname="", wsname=wsname, base=str(pair["base"]), quote=str(pair["quote"]),
                      pair_decimals=int(pair["price_precision"]), lot_decimals=int(pair["qty_precision"]),
                      cost_decimals=int(pair["cost_precision"]), tick_size=dec("price_increment"),
                      ordermin=dec("qty_min"), costmin=dec("cost_min"), status=str(pair.get("status", "unknown")),
                      symbol=str(pair["symbol"]))
    if inst.tick_size <= 0 or inst.ordermin <= 0 or inst.costmin < 0:
        raise ValueError(f"instrument {inst.symbol}: non-positive increment/qty_min or negative cost_min")
    return inst


def parse_asset_pairs(result: dict[str, Any]) -> dict[str, Instrument]:
    """Parse /0/public/AssetPairs `result`; keyed by wsname (e.g. 'XBT/EUR'). Malformed entries raise."""
    out: dict[str, Instrument] = {}
    for key, v in result.items():
        if "wsname" not in v:
            continue  # some legacy/dark pairs have no websocket name; the bot never trades them
        inst = Instrument(
            rest_key=key, altname=str(v["altname"]), wsname=str(v["wsname"]), base=str(v["base"]),
            quote=str(v["quote"]),
            pair_decimals=int(v["pair_decimals"]), lot_decimals=int(v["lot_decimals"]),
            cost_decimals=int(v["cost_decimals"]), tick_size=D(str(v["tick_size"])), ordermin=D(str(v["ordermin"])),
            costmin=D(str(v["costmin"])), status=str(v.get("status", "unknown")),
        )
        if inst.tick_size <= 0 or inst.ordermin <= 0 or inst.costmin < 0:
            raise ValueError(f"AssetPairs {key}: non-positive tick/ordermin or negative costmin")
        out[inst.wsname] = inst
    return out


@dataclass(frozen=True)
class BookTop:
    bid: Decimal
    ask: Decimal
    last: Decimal

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2


def parse_ticker(result: dict[str, Any]) -> dict[str, BookTop]:
    return {k: BookTop(bid=D(v["b"][0]), ask=D(v["a"][0]), last=D(v["c"][0])) for k, v in result.items()}


@dataclass(frozen=True)
class AddOrderRequest:
    pair: str                     # altname, e.g. "XBTEUR"
    side: Side
    ordertype: OrderType
    volume: Decimal
    cl_ord_id: str
    price: Decimal | None = None  # limit price, or trigger price for stop-loss
    trigger: Literal["last", "index"] | None = None
    post_only: bool = False
    fee_in_quote: bool = False    # oflags fciq: fee charged in the quote currency (EUR)
    timeinforce: Literal["GTC", "GTD", "IOC"] = "GTC"
    expire_after_s: int | None = None
    deadline_s: int | None = None
    close_ordertype: OrderType | None = None
    close_price: Decimal | None = None
    validate_only: bool = False

    def problems(self, inst: Instrument | None = None) -> list[str]:
        p: list[str] = []
        for name in ("volume", "price", "close_price"):
            v = getattr(self, name)
            if v is not None and (not isinstance(v, Decimal) or not v.is_finite()):
                p.append(f"{name} must be a finite Decimal")
        if p:
            return p
        if self.volume <= 0:
            p.append("volume must be > 0")
        if not valid_cl_ord_id(self.cl_ord_id):
            p.append("invalid cl_ord_id (UUID, 32-hex, or <=18 ASCII chars)")
        if self.ordertype in (OrderType.LIMIT, OrderType.STOP_LOSS) and (self.price is None or self.price <= 0):
            p.append(f"{self.ordertype} requires a positive price")
        if self.ordertype == OrderType.MARKET and self.price is not None:
            p.append("market orders must not carry a price")
        if self.post_only and self.ordertype != OrderType.LIMIT:
            p.append("post_only is only valid for limit orders")
        if self.trigger is not None and self.ordertype != OrderType.STOP_LOSS:
            p.append("trigger only applies to stop-loss orders")
        if self.timeinforce == "GTD" and (self.expire_after_s is None or self.expire_after_s < 5):
            p.append("GTD requires expire_after_s >= 5 (Kraken minimum)")
        if self.timeinforce != "GTD" and self.expire_after_s is not None:
            p.append("expire_after_s requires timeinforce=GTD")
        if self.deadline_s is not None and not 2 <= self.deadline_s <= 60:
            p.append("deadline_s must be within [2, 60] seconds (Kraken)")
        if (self.close_ordertype is None) != (self.close_price is None):
            p.append("close_ordertype and close_price must be given together")
        if self.close_ordertype is not None and self.close_ordertype != OrderType.STOP_LOSS:
            p.append("only a stop-loss conditional close is used by the bot")
        if (self.close_price is not None and self.price is not None and self.side == Side.BUY
                and self.close_price >= self.price):
            p.append("conditional stop-loss for a buy must be below the entry price")
        if inst is not None:
            if self.pair not in (inst.altname, inst.wsname):
                p.append(f"pair {self.pair} does not match instrument {inst.wsname}")
            if self.volume % inst.lot_step != 0:
                p.append(f"volume {self.volume} is not a multiple of lot step {inst.lot_step}")
            if self.volume < inst.ordermin:
                p.append(f"volume {self.volume} < ordermin {inst.ordermin}")
            for name in ("price", "close_price"):
                v = getattr(self, name)
                if v is not None and v % inst.tick_size != 0:
                    p.append(f"{name} {v} is not a multiple of tick_size {inst.tick_size}")
            if self.price is not None and self.volume * self.price < inst.costmin:
                p.append(f"cost {self.volume * self.price} < costmin {inst.costmin}")
        return p

    def to_params(self, now: datetime) -> list[tuple[str, str]]:
        params: list[tuple[str, str]] = [
            ("ordertype", str(self.ordertype)), ("type", str(self.side)), ("volume", _s(self.volume)),
            ("pair", self.pair),
            ("cl_ord_id", self.cl_ord_id),
        ]
        if self.price is not None:
            params.append(("price", _s(self.price)))
        if self.trigger is not None:
            params.append(("trigger", self.trigger))
        flags = [f for f, on in (("post", self.post_only), ("fciq", self.fee_in_quote)) if on]
        if flags:
            params.append(("oflags", ",".join(flags)))
        if self.timeinforce != "GTC":
            params.append(("timeinforce", self.timeinforce))
        if self.expire_after_s is not None:
            params.append(("expiretm", f"+{self.expire_after_s}"))
        if self.deadline_s is not None:
            dl = now.astimezone(UTC) + timedelta(seconds=self.deadline_s)
            stamp = dl.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dl.microsecond // 1000:03d}Z"
            params.append(("deadline", stamp))
        if self.close_ordertype is not None and self.close_price is not None:
            params += [("close[ordertype]", str(self.close_ordertype)), ("close[price]", _s(self.close_price))]
        if self.validate_only:
            params.append(("validate", "true"))
        return params


def _s(d: Decimal) -> str:
    s = to_db(d)
    assert s is not None
    return s
