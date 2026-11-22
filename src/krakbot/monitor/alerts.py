"""Alert sinks. Business code depends only on the `Alerts` protocol; failures to deliver never raise.

TelegramAlerts: Bot API sendMessage, min 1 s between messages, de-duplication per key (repeat interval per key),
the bot token never appears in logs or exceptions."""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import StrEnum
from typing import Protocol

import httpx
from pydantic import SecretStr

__all__ = ["AlertLevel", "Alerts", "MemoryAlerts", "NullAlerts", "TelegramAlerts"]

log = logging.getLogger("krakbot.alerts")


class AlertLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Alerts(Protocol):
    async def send(self, text: str, *, level: AlertLevel = AlertLevel.INFO, key: str | None = None,
                   repeat_after_s: int = 0) -> bool: ...


class NullAlerts:
    async def send(self, text: str, *, level: AlertLevel = AlertLevel.INFO, key: str | None = None,
                   repeat_after_s: int = 0) -> bool:
        log.info("alert", extra={"alert_level": str(level), "text": text, "key": key})
        return True


class _Dedup:
    def __init__(self, now_ms: Callable[[], int]) -> None:
        self._now = now_ms
        self._last: dict[str, int] = {}

    def allow(self, key: str | None, repeat_after_s: int) -> bool:
        if key is None:
            return True
        now = self._now()
        last = self._last.get(key)
        if last is not None and (repeat_after_s <= 0 or now - last < repeat_after_s * 1000):
            return False
        self._last[key] = now
        return True


class MemoryAlerts:
    """Test/dry-run sink that records what would have been sent (same de-dup semantics as Telegram)."""

    def __init__(self, now_ms: Callable[[], int]) -> None:
        self.sent: list[tuple[AlertLevel, str, str | None]] = []
        self._dedup = _Dedup(now_ms)

    async def send(self, text: str, *, level: AlertLevel = AlertLevel.INFO, key: str | None = None,
                   repeat_after_s: int = 0) -> bool:
        if not self._dedup.allow(key, repeat_after_s):
            return False
        self.sent.append((level, text, key))
        return True


class TelegramAlerts:
    def __init__(self, http: httpx.AsyncClient, token: SecretStr, chat_id: str, *, now_ms: Callable[[], int],
                 prefix: str = "krakbot") -> None:
        self._http, self._token, self._chat, self._prefix = http, token, chat_id, prefix
        self._dedup = _Dedup(now_ms)
        self._now = now_ms
        self._last_send = 0

    async def send(self, text: str, *, level: AlertLevel = AlertLevel.INFO, key: str | None = None,
                   repeat_after_s: int = 0) -> bool:
        if not self._dedup.allow(key, repeat_after_s):
            return False
        tag = {"info": "INFO", "warning": "WARN", "critical": "CRITICAL"}[str(level)]
        body = {"chat_id": self._chat, "text": f"[{tag}] [{self._prefix}] {text}"[:4000],
                "disable_web_page_preview": True}
        url = f"https://api.telegram.org/bot{self._token.get_secret_value()}/sendMessage"
        try:
            r = await self._http.post(url, json=body, timeout=10)
            ok = r.status_code == 200
            if not ok:
                log.error("telegram alert failed", extra={"status": r.status_code})
            return ok
        except httpx.HTTPError as exc:
            log.error("telegram alert failed", extra={"error": type(exc).__name__})  # never the URL: it has the token
            return False
