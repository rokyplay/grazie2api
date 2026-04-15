"""API-key database queries — SQLite via aiosqlite."""

from __future__ import annotations

import time
import uuid
from typing import Any

import aiosqlite


async def find_api_key_by_hash(db: aiosqlite.Connection, key_hash: str) -> dict[str, Any] | None:
    """Find an active (enabled + not revoked) API key by its SHA-256 hash.

    Returns a dict with all ``api_keys`` columns, or ``None``.
    """
    cursor = await db.execute(
        "SELECT * FROM api_keys "
        "WHERE key_hash = ? AND revoked_at = 0 AND enabled = 1 "
        "LIMIT 1",
        (key_hash,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    columns = [desc[0] for desc in cursor.description]
    return dict(zip(columns, row))


async def create_api_key_record(
    db: aiosqlite.Connection,
    key_hash: str,
    owner_id: str,
    tier: str = "default",
    owner_type: str = "discord",
    label: str = "",
    key_prefix: str = "",
    key_last4: str = "",
) -> str:
    """Insert a new API key record. Returns the generated key ``id``."""
    key_id = str(uuid.uuid4())
    now = int(time.time() * 1000)
    await db.execute(
        """INSERT INTO api_keys (
            id, owner_type, owner_id, label, key_hash, key_prefix, key_last4,
            tier, enabled, created_at, updated_at, last_used_at, revoked_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 0, 0)""",
        (key_id, owner_type, owner_id, label, key_hash, key_prefix, key_last4,
         tier, now, now),
    )
    await db.commit()
    return key_id


async def revoke_api_key(db: aiosqlite.Connection, key_id: str) -> None:
    """Mark an API key as revoked."""
    now = int(time.time() * 1000)
    await db.execute(
        "UPDATE api_keys SET revoked_at = ?, updated_at = ? WHERE id = ?",
        (now, now, key_id),
    )
    await db.commit()


async def get_active_api_key_for_user(
    db: aiosqlite.Connection,
    owner_id: str,
    owner_type: str = "discord",
) -> dict[str, Any] | None:
    """Get the most recent active API key for a user."""
    cursor = await db.execute(
        "SELECT * FROM api_keys "
        "WHERE owner_type = ? AND owner_id = ? AND enabled = 1 AND revoked_at = 0 "
        "ORDER BY created_at DESC LIMIT 1",
        (owner_type, owner_id),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    columns = [desc[0] for desc in cursor.description]
    return dict(zip(columns, row))


async def touch_api_key(db: aiosqlite.Connection, key_id: str) -> None:
    """Update ``last_used_at`` for an API key."""
    now = int(time.time() * 1000)
    await db.execute(
        "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
        (now, key_id),
    )
    await db.commit()
