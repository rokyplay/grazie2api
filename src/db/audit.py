"""Audit and usage recording — SQLite via aiosqlite.

1:1 port of codex2api-workers recordUsageAndAudit (db.ts).
Writes to both daily_usage (aggregated) and request_audit (per-request).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

import aiosqlite

log = logging.getLogger("grazie2api.db.audit")


async def record_usage_and_audit(
    db: aiosqlite.Connection,
    *,
    usage_date: str,
    api_key_id: str,
    owner_type: str,
    owner_id: str,
    identity: str,
    tier: str,
    model: str,
    channel_id: str,
    status_code: int,
    latency_ms: int,
    stream: bool,
    error_code: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_tokens: int = 0,
    reasoning_tokens: int = 0,
    credential_id: str = "",
    quota_spent: float = 0,
    audit_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record a request in daily_usage (upsert) and request_audit (insert).

    Mirrors the Worker's recordUsageAndAudit batch insert.
    """
    now_ms = int(time.time() * 1000)
    audit_id = audit_id or str(uuid.uuid4())

    # Build metadata_json with token counts + any extra data
    meta = {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "cachedTokens": cached_tokens,
        "reasoningTokens": reasoning_tokens,
        "totalTokens": input_tokens + output_tokens + cached_tokens + reasoning_tokens,
    }
    if credential_id:
        meta["credentialId"] = credential_id
    if quota_spent:
        meta["quotaSpent"] = quota_spent
    if metadata:
        meta.update(metadata)
    metadata_json = json.dumps(meta)

    try:
        # 1. daily_usage upsert
        await db.execute(
            """INSERT INTO daily_usage (
                usage_date, api_key_id, owner_type, owner_id, identity, tier,
                requests, input_tokens, output_tokens, cached_tokens, reasoning_tokens,
                last_model, last_channel_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(usage_date, api_key_id) DO UPDATE SET
                requests = daily_usage.requests + 1,
                input_tokens = daily_usage.input_tokens + excluded.input_tokens,
                output_tokens = daily_usage.output_tokens + excluded.output_tokens,
                cached_tokens = daily_usage.cached_tokens + excluded.cached_tokens,
                reasoning_tokens = daily_usage.reasoning_tokens + excluded.reasoning_tokens,
                last_model = excluded.last_model,
                last_channel_id = excluded.last_channel_id,
                updated_at = excluded.updated_at""",
            (
                usage_date, api_key_id, owner_type, owner_id, identity, tier,
                input_tokens, output_tokens, cached_tokens, reasoning_tokens,
                model, channel_id, now_ms,
            ),
        )

        # 2. request_audit insert
        await db.execute(
            """INSERT INTO request_audit (
                id, created_at, api_key_id, owner_type, owner_id, identity,
                model, channel_id, status_code, latency_ms, request_kind,
                stream, error_code, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'chat.completions', ?, ?, ?)""",
            (
                audit_id, now_ms, api_key_id, owner_type, owner_id, identity,
                model, channel_id, status_code, latency_ms,
                1 if stream else 0, error_code, metadata_json,
            ),
        )

        await db.commit()
    except Exception as e:
        log.error("Failed to record audit: %s", e, exc_info=True)


async def get_daily_request_count(
    db: aiosqlite.Connection,
    usage_date: str,
    api_key_id: str,
) -> int:
    """Get the total request count for a given date and API key."""
    cursor = await db.execute(
        "SELECT COALESCE(SUM(requests), 0) FROM daily_usage WHERE usage_date = ? AND api_key_id = ?",
        (usage_date, api_key_id),
    )
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def get_user_daily_usage(
    db: aiosqlite.Connection,
    owner_id: str,
    usage_date: str,
) -> dict[str, Any]:
    """Get aggregated daily usage for a user across all their API keys."""
    cursor = await db.execute(
        "SELECT COALESCE(SUM(requests), 0), COALESCE(SUM(input_tokens), 0), "
        "COALESCE(SUM(output_tokens), 0) FROM daily_usage "
        "WHERE owner_id = ? AND usage_date = ?",
        (owner_id, usage_date),
    )
    row = await cursor.fetchone()
    if row is None:
        return {"requests": 0, "input_tokens": 0, "output_tokens": 0}
    return {
        "requests": int(row[0]),
        "input_tokens": int(row[1]),
        "output_tokens": int(row[2]),
    }
