"""Kraken dead man's switch (CancelAllOrdersAfter) controller, LIVE only (plan §5).
entry_only: armed while entry orders are working AND no protective stop rests (the DCA has no stops, so: armed
while a DCA order is working); always: armed continuously. Kraken: refresh every 15-30 s with timeout 60 s."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Literal

from krakbot.kraken_rest.client import KrakenRestClient
from krakbot.kraken_rest.errors import KrakenError

__all__ = ["DmsController"]

log = logging.getLogger("krakbot.dms")


class DmsController:
    def __init__(self, rest: KrakenRestClient, *, mode: Literal["entry_only", "always"], timeout_s: int,
                 refresh_s: int, now_ms: Callable[[], int]) -> None:
        self._rest, self._mode, self._timeout, self._refresh, self._now = rest, mode, timeout_s, refresh_s, now_ms
        self.armed = False
        self._last = 0
        self.failures = 0

    async def disarm(self) -> None:
        await self._rest.cancel_all_orders_after(0)
        self.armed = False

    async def maintain(self, *, entries_working: bool, stops_resting: bool) -> None:
        want = True if self._mode == "always" else (entries_working and not stops_resting)
        now = self._now()
        try:
            if want and (not self.armed or now - self._last >= self._refresh * 1000):
                await self._rest.cancel_all_orders_after(self._timeout)
                self.armed, self._last = True, now
            elif not want and self.armed:
                await self._rest.cancel_all_orders_after(0)
                self.armed = False
        except KrakenError as exc:
            self.failures += 1
            log.error("dead man's switch call failed", extra={"error": str(exc)})
