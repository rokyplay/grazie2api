"""SQLite database module using aiosqlite.

Provides:
- init_db(settings): create tables from schema.sql
- get_db(): get the singleton aiosqlite connection
- close_db(): close the connection
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

if TYPE_CHECKING:
    from src.config import Settings

log = logging.getLogger("grazie2api.db")

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Singleton connection
_db: aiosqlite.Connection | None = None


async def init_db(settings: "Settings") -> aiosqlite.Connection:
    """Initialize the SQLite database.

    - Ensures config_home exists
    - Opens (or creates) the main.db file
    - Executes schema.sql to create all tables (IF NOT EXISTS = idempotent)
    - Enables WAL mode for concurrent reads
    - Returns the connection
    """
    global _db

    if _db is not None:
        return _db

    settings.ensure_config_home()
    db_path = settings.main_db_file

    log.info("Opening database at %s", db_path)
    conn = await aiosqlite.connect(str(db_path))

    # Enable WAL mode for better concurrent read performance
    await conn.execute("PRAGMA journal_mode=WAL")
    # Enable foreign keys
    await conn.execute("PRAGMA foreign_keys=ON")

    # Execute full schema (all CREATE IF NOT EXISTS, idempotent)
    schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    await conn.executescript(schema_sql)
    await conn.commit()

    # Verify table count
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    row = await cursor.fetchone()
    table_count = row[0] if row else 0
    log.info("Database initialized: %d tables", table_count)

    _db = conn
    return conn


def get_db() -> aiosqlite.Connection:
    """Get the singleton database connection.

    Raises RuntimeError if init_db() has not been called.
    """
    if _db is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _db


async def close_db() -> None:
    """Close the database connection."""
    global _db
    if _db is not None:
        await _db.close()
        _db = None
        log.info("Database connection closed")
