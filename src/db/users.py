"""User-related database queries — SQLite via aiosqlite."""

from __future__ import annotations

import time
from typing import Any

import aiosqlite


async def get_user_by_jb_key(db: aiosqlite.Connection, jb_key: str) -> dict[str, Any] | None:
    """Look up a user by their plaintext JB API key.

    Returns a dict with all ``users`` columns, or ``None`` if not found.
    """
    cursor = await db.execute(
        "SELECT * FROM users WHERE jb_api_key = ? LIMIT 1",
        (jb_key,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    columns = [desc[0] for desc in cursor.description]
    return dict(zip(columns, row))


async def get_user_by_discord_id(db: aiosqlite.Connection, discord_id: str) -> dict[str, Any] | None:
    """Look up a user by their Discord user ID.

    Returns a dict with all ``users`` columns, or ``None`` if not found.
    """
    cursor = await db.execute(
        "SELECT * FROM users WHERE discord_user_id = ? LIMIT 1",
        (discord_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    columns = [desc[0] for desc in cursor.description]
    return dict(zip(columns, row))


async def upsert_user(
    db: aiosqlite.Connection,
    discord_id: str,
    username: str,
    global_name: str = "",
    avatar_url: str = "",
    roles_json: str = "[]",
    tier: str = "default",
    status: str = "active",
) -> str:
    """Insert or update a user record. Returns the ``discord_user_id``."""
    now = int(time.time() * 1000)
    await db.execute(
        """INSERT INTO users (
            discord_user_id, username, global_name, avatar_url,
            roles_json, tier, status, created_at, updated_at, last_login_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(discord_user_id) DO UPDATE SET
            username = excluded.username,
            global_name = excluded.global_name,
            avatar_url = excluded.avatar_url,
            roles_json = excluded.roles_json,
            tier = excluded.tier,
            status = excluded.status,
            updated_at = excluded.updated_at,
            last_login_at = excluded.last_login_at""",
        (discord_id, username, global_name, avatar_url,
         roles_json, tier, status, now, now, now),
    )
    await db.commit()
    return discord_id


async def set_jb_api_key(db: aiosqlite.Connection, discord_id: str, jb_api_key: str) -> None:
    """Set the JB API key for a user."""
    now = int(time.time() * 1000)
    await db.execute(
        "UPDATE users SET jb_api_key = ?, updated_at = ? WHERE discord_user_id = ?",
        (jb_api_key, now, discord_id),
    )
    await db.commit()
