"""Heartbeats: DB minute marks (live gate coverage), systemd watchdog (sd_notify) and an optional external ping.
A dead process cannot alert: only the external monitor (HEALTHCHECK_URL) can tell you the bot stopped."""

from __future__ import annotations

import logging
import os
import socket

import httpx
from pydantic import SecretStr

__all__ = ["ExternalPing", "sd_notify"]

log = logging.getLogger("krakbot.heartbeat")


def sd_notify(state: str) -> bool:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(state.encode())
        return True
    except OSError:
        return False


class ExternalPing:
    def __init__(self, http: httpx.AsyncClient, url: SecretStr | None) -> None:
        self._http, self._url = http, url

    async def ping(self) -> bool:
        if self._url is None:
            return False
        try:
            r = await self._http.get(self._url.get_secret_value(), timeout=10)
            return r.status_code < 300
        except httpx.HTTPError as exc:
            log.warning("external heartbeat ping failed", extra={"error": type(exc).__name__})
            return False
