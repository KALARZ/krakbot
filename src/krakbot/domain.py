"""Domain enums and the trade state machine (plan §3, §4.1). The SQLite schema CHECK constraints are generated
from / tested against these enums (tests/unit/test_schema.py), so the two can never drift apart."""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "ORDER_TRANSITIONS", "TERMINAL_ORDER_STATUSES", "TERMINAL_STATES", "TRANSITIONS", "EventLevel",
    "IllegalTransition", "Liquidity", "Mode", "OrderRole", "OrderStatus", "OrderType", "Side", "SignalAction",
    "TradeState", "assert_order_transition", "assert_transition", "can_transition",
]


class Mode(StrEnum):
    OFF = "off"
    DRY_RUN = "dry_run"
    LIVE = "live"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class SignalAction(StrEnum):
    BUY = "buy"
    EXIT = "exit"
    HOLD = "hold"


class OrderType(StrEnum):  # subset of Kraken `ordertype` actually used by the bot
    LIMIT = "limit"
    MARKET = "market"
    STOP_LOSS = "stop-loss"


class OrderRole(StrEnum):
    ENTRY = "entry"
    STOP = "stop"
    EXIT = "exit"
    TP = "tp"
    FLATTEN = "flatten"
    RECOVER = "recover"      # market exit during boot recovery (plan §4.6, 8a)
    RESTOP = "restop"        # stop re-placed during boot recovery (plan §4.6, 8b)
    DCA = "dca"


class OrderStatus(StrEnum):
    INTENT = "intent"        # written BEFORE sending (write-ahead)
    SENT = "sent"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    EXPIRED = "expired"
    REJECTED = "rejected"
    UNKNOWN = "unknown"      # send outcome unknown (timeout / deadline elapsed): must be resolved, never resent


class Liquidity(StrEnum):
    MAKER = "m"
    TAKER = "t"


class EventLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class TradeState(StrEnum):
    SIGNALLED = "SIGNALLED"
    ENTRY_WORKING = "ENTRY_WORKING"
    ENTRY_CANCELLED = "ENTRY_CANCELLED"
    OPEN_PROTECTED = "OPEN_PROTECTED"
    UNPROTECTED = "UNPROTECTED"
    EXITING = "EXITING"
    EXIT_MARKET = "EXIT_MARKET"
    FLATTENING = "FLATTENING"
    STOPPED = "STOPPED"
    CLOSED = "CLOSED"
    HELD = "HELD"            # DCA tranche filled and held (no stop by design)


TERMINAL_STATES: frozenset[TradeState] = frozenset(
    {TradeState.ENTRY_CANCELLED, TradeState.STOPPED, TradeState.CLOSED, TradeState.HELD}
)

_T = TradeState
TRANSITIONS: dict[TradeState, frozenset[TradeState]] = {
    _T.SIGNALLED: frozenset({_T.ENTRY_WORKING, _T.ENTRY_CANCELLED}),
    _T.ENTRY_WORKING: frozenset({_T.ENTRY_CANCELLED, _T.OPEN_PROTECTED, _T.UNPROTECTED, _T.HELD, _T.FLATTENING}),
    _T.OPEN_PROTECTED: frozenset({_T.EXITING, _T.EXIT_MARKET, _T.STOPPED, _T.UNPROTECTED, _T.FLATTENING}),
    _T.UNPROTECTED: frozenset({_T.OPEN_PROTECTED, _T.EXIT_MARKET, _T.FLATTENING, _T.STOPPED, _T.CLOSED}),
    _T.EXITING: frozenset({_T.CLOSED, _T.EXIT_MARKET, _T.STOPPED, _T.UNPROTECTED, _T.FLATTENING, _T.OPEN_PROTECTED}),
    _T.EXIT_MARKET: frozenset({_T.CLOSED, _T.UNPROTECTED, _T.FLATTENING}),
    _T.FLATTENING: frozenset({_T.CLOSED, _T.UNPROTECTED}),
    _T.ENTRY_CANCELLED: frozenset(),
    _T.STOPPED: frozenset(),
    _T.CLOSED: frozenset(),
    _T.HELD: frozenset(),
}


class IllegalTransition(Exception):
    pass


def can_transition(src: TradeState, dst: TradeState) -> bool:
    return dst in TRANSITIONS[src]


def assert_transition(src: TradeState, dst: TradeState) -> None:
    if not can_transition(src, dst):
        raise IllegalTransition(f"{src} -> {dst} is not allowed")


_O = OrderStatus
TERMINAL_ORDER_STATUSES: frozenset[OrderStatus] = frozenset({_O.FILLED, _O.CANCELED, _O.EXPIRED, _O.REJECTED})
# write-ahead lifecycle: INTENT is persisted before sending; UNKNOWN can only be left by evidence (plan §4.7)
ORDER_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    _O.INTENT: frozenset({_O.SENT, _O.REJECTED, _O.UNKNOWN}),
    _O.SENT: frozenset({_O.OPEN, _O.PARTIALLY_FILLED, _O.FILLED, _O.CANCELED, _O.EXPIRED, _O.REJECTED, _O.UNKNOWN}),
    _O.OPEN: frozenset({_O.PARTIALLY_FILLED, _O.FILLED, _O.CANCELED, _O.EXPIRED}),
    _O.PARTIALLY_FILLED: frozenset({_O.PARTIALLY_FILLED, _O.FILLED, _O.CANCELED, _O.EXPIRED}),
    _O.UNKNOWN: frozenset({_O.OPEN, _O.PARTIALLY_FILLED, _O.FILLED, _O.CANCELED, _O.EXPIRED, _O.REJECTED}),
    _O.FILLED: frozenset(),
    _O.CANCELED: frozenset(),
    _O.EXPIRED: frozenset(),
    _O.REJECTED: frozenset(),
}


def assert_order_transition(src: OrderStatus, dst: OrderStatus) -> None:
    if dst not in ORDER_TRANSITIONS[src]:
        raise IllegalTransition(f"order {src} -> {dst} is not allowed")
