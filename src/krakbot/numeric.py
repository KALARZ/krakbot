"""Decimal-only numeric helpers. Prices and quantities are never floats (plan §2, user constraint).

All helpers reject float input on purpose: a float that reaches an order would carry binary rounding error
into price/volume strings sent to Kraken.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Context, Decimal, InvalidOperation
from typing import Literal

__all__ = ["D", "ceil_to_step", "floor_to_step", "from_db", "round_price_passive", "step_from_decimals", "to_db"]

_CTX = Context(prec=34)


def D(value: str | int | Decimal) -> Decimal:
    """Build a finite Decimal from str/int/Decimal. Floats and bools are rejected."""
    if isinstance(value, (bool, float)):
        raise TypeError(f"float/bool not allowed for monetary values: {value!r}")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, int):
        d = Decimal(value)
    elif isinstance(value, str):
        try:
            d = Decimal(value.strip())
        except InvalidOperation as exc:
            raise ValueError(f"not a decimal number: {value!r}") from exc
    else:
        raise TypeError(f"unsupported type for Decimal: {type(value).__name__}")
    if not d.is_finite():
        raise ValueError(f"non-finite decimal: {value!r}")
    return d


def to_db(value: Decimal | None) -> str | None:
    """Canonical TEXT representation for SQLite: plain notation, never exponent ('1E-8' -> '0.00000001')."""
    if value is None:
        return None
    if not isinstance(value, Decimal):
        raise TypeError(f"expected Decimal, got {type(value).__name__}")
    if not value.is_finite():
        raise ValueError("non-finite decimal")
    if value.is_zero():
        value = abs(value)  # no "-0" in the database
    return format(value, "f")


def from_db(value: str | None) -> Decimal | None:
    return None if value is None else D(value)


def step_from_decimals(decimals: int) -> Decimal:
    """Kraken `lot_decimals`/`pair_decimals` -> step (8 -> 0.00000001)."""
    if decimals < 0 or decimals > 18:
        raise ValueError(f"decimals out of range: {decimals}")
    return Decimal(1).scaleb(-decimals)


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Largest multiple of `step` <= value (volumes are always rounded down: never exceed balance/size)."""
    if step <= 0:
        raise ValueError("step must be > 0")
    n = (value / step).to_integral_value(rounding=ROUND_FLOOR, context=_CTX)
    return (n * step).quantize(step)


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step must be > 0")
    n = (value / step).to_integral_value(rounding=ROUND_CEILING, context=_CTX)
    return (n * step).quantize(step)


def round_price_passive(price: Decimal, tick: Decimal, side: Literal["buy", "sell"]) -> Decimal:
    """Round to the tick on the passive side: buys down, sells up (keeps post-only orders off the touch)."""
    return floor_to_step(price, tick) if side == "buy" else ceil_to_step(price, tick)
