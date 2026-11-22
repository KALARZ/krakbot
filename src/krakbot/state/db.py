"""SQLite connection (WAL, synchronous=FULL, foreign keys) and forward-only, checksummed migrations."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from importlib import resources
from pathlib import Path

__all__ = ["MigrationError", "connect", "migrate", "transaction"]

_MIGRATION_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


class MigrationError(Exception):
    pass


def _adapt_decimal(d: Decimal) -> str:
    if not d.is_finite():
        raise ValueError("non-finite Decimal cannot be stored")
    return format(abs(d) if d.is_zero() else d, "f")


def _refuse_float(_: float) -> str:
    raise TypeError("floats must not be written to the database: use Decimal (krakbot.numeric.D)")


sqlite3.register_adapter(Decimal, _adapt_decimal)
sqlite3.register_adapter(float, _refuse_float)


def connect(path: Path | str) -> sqlite3.Connection:
    """Open the DB in autocommit mode (explicit transactions via `transaction()`), WAL + synchronous=FULL."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if str(path) != ":memory:" and mode.lower() != "wal":
        raise MigrationError(f"could not enable WAL (got {mode})")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _available() -> list[tuple[int, str, str]]:
    out = []
    pkg = resources.files("krakbot.state") / "migrations"
    for entry in pkg.iterdir():
        m = _MIGRATION_RE.match(entry.name)
        if m:
            out.append((int(m.group(1)), m.group(2), entry.read_text(encoding="utf-8")))
    out.sort()
    versions = [v for v, _, _ in out]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(f"migration versions must be contiguous from 0001: {versions}")
    return out


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply pending migrations in order, each in its own transaction. Returns the versions applied now.

    Refuses to run if an applied migration's file changed (checksum) or the DB is newer than this code."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at INTEGER NOT NULL)"
    )
    avail = _available()
    known = {v: (n, _checksum(sql)) for v, n, sql in avail}
    applied = {r["version"]: r["checksum"] for r in conn.execute("SELECT version, checksum FROM schema_migrations")}
    newer = [v for v in applied if v not in known]
    if newer:
        raise MigrationError(f"database has migrations unknown to this code (newer version?): {sorted(newer)}")
    for v, chk in applied.items():
        if known[v][1] != chk:
            raise MigrationError(f"migration {v:04d} was modified after being applied (checksum mismatch)")
    done = []
    for v, name, sql in avail:
        if v in applied:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in _split_sql(sql):
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_migrations(version, name, checksum, applied_at) VALUES (?,?,?,?)",
                (v, name, _checksum(sql), int(time.time() * 1000)),
            )
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        done.append(v)
    return done


def _split_sql(sql: str) -> list[str]:
    """Split a migration into statements (handles BEGIN...END trigger bodies; strips -- comments)."""
    lines = [ln.split("--", 1)[0] for ln in sql.splitlines()]
    text = "\n".join(lines)
    out: list[str] = []
    buf = ""
    for part in text.split(";"):
        buf += part + ";"
        if sqlite3.complete_statement(buf):
            stmt = buf.strip()
            if stmt != ";":
                out.append(stmt)
            buf = ""
    if buf.strip(" ;\n"):
        raise MigrationError("incomplete SQL statement at end of migration")
    return out
