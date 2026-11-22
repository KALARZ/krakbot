"""Injectable clock so rate limiters and retries are deterministic in tests."""

from __future__ import annotations

import asyncio
import time
from typing import Protocol

__all__ = ["Clock", "ManualClock", "SystemClock"]


class Clock(Protocol):
    def monotonic(self) -> float: ...
    def time(self) -> float: ...
    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def time(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


class ManualClock:
    """Test clock: sleeping advances time instantly and is recorded."""

    def __init__(self, start_wall: float = 1_795_000_000.0) -> None:
        self._mono = 1000.0
        self._wall = start_wall
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self._mono

    def time(self) -> float:
        return self._wall

    def advance(self, seconds: float) -> None:
        self._mono += seconds
        self._wall += seconds

    async def sleep(self, seconds: float) -> None:
        s = max(0.0, seconds)
        self.sleeps.append(s)
        self.advance(s)
        await asyncio.sleep(0)
