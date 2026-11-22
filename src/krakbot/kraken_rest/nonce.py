"""Monotonic nonce, persisted BEFORE each private request (plan §2).

Kraken: the nonce is an always-increasing unsigned 64-bit integer per API key and can never be reset to a lower
value. We use milliseconds (the documented convention) and never go below the persisted high-water mark + 1, so a
restart, a clock step backwards or a burst inside one millisecond can never produce a repeated/lower nonce.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from typing import Protocol

__all__ = ["InMemoryNonceStore", "NonceSource", "NonceStore", "SqliteNonceStore"]

_KEY = "nonce_hwm"


class NonceStore(Protocol):
    def load(self) -> int: ...
    def save(self, value: int) -> None: ...


class InMemoryNonceStore:
    def __init__(self, start: int = 0) -> None:
        self.value = start

    def load(self) -> int:
        return self.value

    def save(self, value: int) -> None:
        self.value = value


class SqliteNonceStore:
    """High-water mark in `meta` (synchronous=FULL connection from krakbot.state.connect)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def load(self) -> int:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (_KEY,)).fetchone()
        return int(row[0]) if row else 0

    def save(self, value: int) -> None:
        now = int(time.time() * 1000)
        self._conn.execute(
            "INSERT INTO meta(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at "
            "WHERE CAST(meta.value AS INTEGER) < CAST(excluded.value AS INTEGER)",
            (_KEY, str(value), now),
        )


class NonceSource:
    """Rule (explicit, tested): n_k = max(n_{k-1} + 1, clock_ms()), with n_0 = persisted high-water mark.

    Hence every nonce is at least the previous one + 1 whether the clock is stopped, steps backwards or is behind
    the persisted value; it follows the clock when the clock is ahead. The new value is persisted before it is
    returned (and so before any request uses it); if persisting fails, no nonce is issued and the state is unchanged.
    """

    def __init__(self, store: NonceStore, clock_ms: Callable[[], int] | None = None) -> None:
        self._store = store
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._lock = threading.Lock()
        self._last = store.load()

    def next(self) -> int:
        with self._lock:
            n = max(self._last + 1, self._clock_ms())
            if n >= 2**64:
                raise OverflowError("nonce exceeds uint64")
            self._store.save(n)  # persist first: a crash after sending can never lead to reuse
            self._last = n
            return n
