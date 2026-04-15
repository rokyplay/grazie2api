"""JB credential database queries — SQLite via aiosqlite.

Per-user credential isolation: each user's JB credentials are stored
in the jb_credentials table keyed by user_id (discord_user_id).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import aiosqlite

log = logging.getLogger("grazie2api.db.jb_credentials")


async def list_user_jb_credentials(
    db: aiosqlite.Connection, user_id: str
) -> list[dict[str, Any]]:
    """List all JB credentials for a user, ordered by creation time DESC.

    Returns all credentials regardless of status — caller filters as needed.
    """
    cursor = await db.execute(
        "SELECT * FROM jb_credentials WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    )
    rows = await cursor.fetchall()
    if not rows:
        return []
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in rows]


async def get_jb_credential_by_id(
    db: aiosqlite.Connection, cred_id: str
) -> dict[str, Any] | None:
    """Get a single JB credential by its ID."""
    cursor = await db.execute(
        "SELECT * FROM jb_credentials WHERE id = ? LIMIT 1",
        (cred_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    columns = [desc[0] for desc in cursor.description]
    return dict(zip(columns, row))


async def update_jb_credential_jwt(
    db: aiosqlite.Connection,
    cred_id: str,
    jwt: str,
    expires_at: int,
    refresh_token: str,
) -> None:
    """Update JWT, expiry, and refresh_token for a credential after a successful refresh."""
    now = int(time.time() * 1000)
    await db.execute(
        "UPDATE jb_credentials SET jwt = ?, jwt_expires_at = ?, refresh_token = ?, updated_at = ? WHERE id = ?",
        (jwt, expires_at, refresh_token, now, cred_id),
    )
    await db.commit()


async def mark_credential_exhausted(
    db: aiosqlite.Connection, cred_id: str
) -> None:
    """Mark a credential as quota-exhausted."""
    now = int(time.time() * 1000)
    await db.execute(
        "UPDATE jb_credentials SET quota_exhausted = 1, updated_at = ? WHERE id = ?",
        (now, cred_id),
    )
    await db.commit()


async def update_credential_quota(
    db: aiosqlite.Connection,
    cred_id: str,
    available: int,
    maximum: int,
) -> None:
    """Update quota fields for a credential. Auto-sets quota_exhausted if available <= 0."""
    now = int(time.time() * 1000)
    exhausted = 1 if available <= 0 else 0
    await db.execute(
        "UPDATE jb_credentials SET quota_available = ?, quota_maximum = ?, "
        "quota_exhausted = ?, updated_at = ? WHERE id = ?",
        (available, maximum, exhausted, now, cred_id),
    )
    await db.commit()


async def soft_delete_credential(
    db: aiosqlite.Connection, cred_id: str, user_id: str
) -> bool:
    """Soft-delete a credential (set status='deleted'). Returns True if a row was affected."""
    now = int(time.time() * 1000)
    cursor = await db.execute(
        "UPDATE jb_credentials SET status = 'deleted', updated_at = ? WHERE id = ? AND user_id = ?",
        (now, cred_id, user_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def list_credentials_needing_refresh(
    db: aiosqlite.Connection,
    margin_ms: int = 600_000,
) -> list[dict[str, Any]]:
    """List active credentials whose JWT expires within margin_ms, or has no JWT.

    Used by the background JWT refresh cron.
    """
    now_ms = int(time.time() * 1000)
    threshold = now_ms + margin_ms
    cursor = await db.execute(
        "SELECT * FROM jb_credentials "
        "WHERE status = 'active' AND quota_exhausted = 0 "
        "AND (jwt = '' OR jwt_expires_at < ?) "
        "ORDER BY jwt_expires_at ASC",
        (threshold,),
    )
    rows = await cursor.fetchall()
    if not rows:
        return []
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in rows]
