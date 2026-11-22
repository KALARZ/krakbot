import random

import pytest

from krakbot.kraken_rest import InMemoryNonceStore, NonceSource, SqliteNonceStore
from krakbot.state import connect, migrate


def _seq(src, k):
    return [src.next() for _ in range(k)]


def test_stopped_clock_advances_by_exactly_one():
    src = NonceSource(InMemoryNonceStore(), clock_ms=lambda: 5_000)
    assert _seq(src, 5) == [5_000, 5_001, 5_002, 5_003, 5_004]


def test_clock_behind_persisted_high_water_mark():
    src = NonceSource(InMemoryNonceStore(start=10_000), clock_ms=lambda: 5)
    assert _seq(src, 3) == [10_001, 10_002, 10_003]


def test_clock_stepping_backwards_never_lowers_the_nonce():
    t = [9_000]
    src = NonceSource(InMemoryNonceStore(), clock_ms=lambda: t[0])
    a = src.next()
    t[0] = 1_000  # NTP step back by 8 s
    b, c = src.next(), src.next()
    assert (a, b, c) == (9_000, 9_001, 9_002)
    t[0] = 20_000  # clock ahead again: follow it
    assert src.next() == 20_000


def test_property_random_clock_strictly_plus_one_or_more():
    rng = random.Random(1234)
    t = [1_700_000_000_000]

    def clock():
        t[0] += rng.choice([-5_000, -1, 0, 0, 0, 1, 3, 1_000])  # backwards, stopped and forward moves
        return t[0]

    src = NonceSource(InMemoryNonceStore(), clock_ms=clock)
    prev = src.next()
    for _ in range(5_000):
        n = src.next()
        assert n >= prev + 1
        prev = n


def test_restart_with_stopped_clock_continues_from_persisted_value(tmp_path):
    db = tmp_path / "n.db"
    conn = connect(db)
    migrate(conn)
    first = _seq(NonceSource(SqliteNonceStore(conn), clock_ms=lambda: 42), 3)
    conn.close()
    conn = connect(db)  # process restart: new connection, new NonceSource, same (stopped) clock
    try:
        again = _seq(NonceSource(SqliteNonceStore(conn), clock_ms=lambda: 42), 2)
    finally:
        conn.close()
    assert first == [42, 43, 44] and again == [45, 46]


class _FailingStore(InMemoryNonceStore):
    fail = True

    def save(self, value: int) -> None:
        if self.fail:
            raise OSError("disk full")
        super().save(value)


def test_failed_persist_issues_no_nonce_and_keeps_state():
    store = _FailingStore(start=100)
    src = NonceSource(store, clock_ms=lambda: 0)
    with pytest.raises(OSError):
        src.next()
    assert store.value == 100
    store.fail = False
    assert src.next() == 101  # nothing was sent with the failed value, so it is safe to issue it now
