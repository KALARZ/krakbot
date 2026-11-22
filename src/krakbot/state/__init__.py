"""Persistence: SQLite (WAL) connection, migrations, gates. Repositories are added in later blocks."""

from krakbot.state.db import MigrationError, connect, migrate, transaction

__all__ = ["MigrationError", "connect", "migrate", "transaction"]
