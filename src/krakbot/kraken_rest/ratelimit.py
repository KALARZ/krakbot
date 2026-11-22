"""Client-side rate limiting mirroring Kraken's counters (docs: guides/rest/ratelimits, guides/general/ratelimits).

* RestCallCounter: per-API-key REST counter (max/decay by verification tier); TradesHistory/Ledgers cost 2,
  AddOrder/CancelOrder are on the engine limiter instead (cost 0 here).
* EngineCounter: per-pair matching-engine counter shared by REST/WS/FIX; amend/cancel penalties depend on order age.
* PublicLimiter: public endpoints are IP-limited; keep >= 1 s between calls (Kraken's recommended safe delay).
We wait proactively at a safety fraction of each threshold instead of discovering the limit through rejections.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from krakbot.kraken_rest.clock import Clock

__all__ = [
    "ENGINE_TIERS", "REST_TIERS", "EngineCounter", "PublicLimiter", "RestCallCounter", "Tier", "engine_cost",
    "rest_cost",
]

Tier = Literal["starter", "intermediate", "pro"]


@dataclass(frozen=True)
class _Limits:
    maximum: float
    decay_per_s: float


REST_TIERS: dict[str, _Limits] = {
    "starter": _Limits(15, 0.33), "intermediate": _Limits(20, 0.5), "pro": _Limits(20, 1.0),
}
ENGINE_TIERS: dict[str, _Limits] = {
    "starter": _Limits(60, 1.0), "intermediate": _Limits(125, 2.34), "pro": _Limits(180, 3.75),
}

_REST_COST = {"Ledgers": 2, "QueryLedgers": 2, "TradesHistory": 2, "QueryTrades": 2, "AddOrder": 0, "CancelOrder": 0}


def rest_cost(method: str) -> int:
    return _REST_COST.get(method, 1)


def _by_age(age_s: float | None, table: Sequence[tuple[float, int]]) -> int:
    if age_s is None:
        return table[0][1]  # unknown age -> assume the youngest (worst) bucket
    for limit, cost in table:
        if age_s < limit:
            return cost
    return 0


_AMEND = [(5, 3), (10, 2), (15, 1)]
_EDIT = [(5, 6), (10, 5), (15, 4), (45, 2), (90, 1)]
_CANCEL = [(5, 8), (10, 6), (15, 5), (45, 4), (90, 2), (300, 1)]


def engine_cost(action: Literal["add", "amend", "edit", "cancel"], order_age_s: float | None = None) -> int:
    if action == "add":
        return 1
    if action == "amend":
        return 1 + _by_age(order_age_s, _AMEND)
    if action == "edit":
        return 1 + _by_age(order_age_s, _EDIT)
    return _by_age(order_age_s, _CANCEL)


class _DecayingCounter:
    def __init__(self, limits: _Limits, clock: Clock, safety: float) -> None:
        self._max = limits.maximum * safety
        self._decay = limits.decay_per_s
        self._clock = clock
        self._value = 0.0
        self._t = clock.monotonic()

    def _refresh(self) -> None:
        now = self._clock.monotonic()
        self._value = max(0.0, self._value - (now - self._t) * self._decay)
        self._t = now

    @property
    def value(self) -> float:
        self._refresh()
        return self._value

    def wait_needed(self, cost: float) -> float:
        self._refresh()
        if cost > self._max:
            raise ValueError(f"single request cost {cost} exceeds the counter budget {self._max}")
        over = self._value + cost - self._max
        # Tolerance + small overshoot: without them a residual like 1e-17 s can never be slept away on a clock whose
        # value is ~1e3-1e6 s (float spacing), and the acquire loop would spin forever.
        return 0.0 if over <= 1e-9 else over / self._decay + 1e-6

    def add(self, cost: float) -> None:
        self._refresh()
        self._value += cost


class RestCallCounter:
    def __init__(self, tier: Tier, clock: Clock, safety: float = 0.8) -> None:
        self._c = _DecayingCounter(REST_TIERS[tier], clock, safety)
        self._clock = clock
        self._lock = asyncio.Lock()

    @property
    def value(self) -> float:
        return self._c.value

    async def acquire(self, method: str) -> None:
        cost = rest_cost(method)
        if cost == 0:
            return
        async with self._lock:
            while (w := self._c.wait_needed(cost)) > 0:
                await self._clock.sleep(w)
            self._c.add(cost)


class EngineCounter:
    def __init__(self, tier: Tier, clock: Clock, safety: float = 0.8) -> None:
        self._tier = tier
        self._clock = clock
        self._safety = safety
        self._pairs: dict[str, _DecayingCounter] = {}
        self._lock = asyncio.Lock()

    def value(self, pair: str) -> float:
        c = self._pairs.get(pair)
        return 0.0 if c is None else c.value

    async def acquire(self, pair: str, cost: int) -> None:
        async with self._lock:
            c = self._pairs.setdefault(pair, _DecayingCounter(ENGINE_TIERS[self._tier], self._clock, self._safety))
            while (w := c.wait_needed(cost)) > 0:
                await self._clock.sleep(w)
            c.add(cost)


class PublicLimiter:
    def __init__(self, clock: Clock, min_interval_s: float = 1.0) -> None:
        self._clock = clock
        self._min = min_interval_s
        self._last: float | None = None
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            if self._last is not None:
                wait = self._last + self._min - self._clock.monotonic()
                if wait > 0:
                    await self._clock.sleep(wait)
            self._last = self._clock.monotonic()
