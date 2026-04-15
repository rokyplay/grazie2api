"""Background quota refresher for all credentials."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

import httpx

from src.config import Settings
from src.proxy.upstream import redact_log

if TYPE_CHECKING:
    from src.credentials.entry import CredentialEntry
    from src.credentials.pool import CredentialPool

log = logging.getLogger("grazie2api.quota")


async def fetch_quota_for(entry: "CredentialEntry", client: httpx.AsyncClient, settings: Settings) -> dict | None:
    """Fetch quota for one credential, updating the entry in-place."""
    try:
        jwt = await entry.token_manager.ensure_valid_jwt()
    except Exception as e:
        entry.last_error = f"jwt refresh failed: {e}"
        log.warning("[cred %s] quota: jwt refresh failed: %s", entry.id, e)
        return None

    try:
        resp = await client.post(
            settings.urls.quota_url,
            headers={
                "grazie-authenticate-jwt": jwt,
                "grazie-agent": settings.grazie.agent_json,
                "User-Agent": "ktor-client",
                "Content-Type": "application/json",
            },
            json={},
        )
        if resp.status_code == 401:
            # JWT rejected by server — force refresh and retry once
            log.warning("[cred %s] quota 401, forcing JWT refresh", entry.id)
            entry.token_manager.jwt = ""
            entry.token_manager.jwt_expires = 0
            try:
                jwt = await entry.token_manager.ensure_valid_jwt()
                resp = await client.post(
                    settings.urls.quota_url,
                    headers={
                        "grazie-authenticate-jwt": jwt,
                        "grazie-agent": settings.grazie.agent_json,
                        "User-Agent": "ktor-client",
                        "Content-Type": "application/json",
                    },
                    json={},
                )
            except Exception as e2:
                entry.last_error = f"quota 401 retry failed: {e2}"
                log.warning("[cred %s] quota 401 retry failed: %s", entry.id, e2)
                return None
        if resp.status_code != 200:
            entry.last_error = f"quota http {resp.status_code}"
            log.warning(
                "[cred %s] quota http %s: %s",
                entry.id, resp.status_code, redact_log(resp.text[:200]),
            )
            return None
        body = resp.json()
    except Exception as e:
        entry.last_error = f"quota request failed: {e}"
        log.warning("[cred %s] quota request failed: %s", entry.id, e)
        return None

    tariff = body.get("tariffQuota") or body.get("current") or {}
    current = None
    maximum = None
    available = None
    until = None
    if isinstance(tariff, dict):
        cur = tariff.get("current") or {}
        mx = tariff.get("maximum") or {}
        avail = tariff.get("available") or {}
        if isinstance(cur, dict):
            current = cur.get("amount")
        if isinstance(mx, dict):
            maximum = mx.get("amount")
        if isinstance(avail, dict):
            available = avail.get("amount")
        until = tariff.get("until") or tariff.get("resetAt")
        # If available not found at top level, check nested tariffQuota
        if available is None:
            nested_tq = tariff.get("tariffQuota") or {}
            if isinstance(nested_tq, dict):
                nested_avail = nested_tq.get("available") or {}
                if isinstance(nested_avail, dict):
                    available = nested_avail.get("amount")

    if current is None:
        daily = body.get("license", {}).get("daily") if isinstance(body.get("license"), dict) else None
        if isinstance(daily, dict):
            current = daily.get("current", {}).get("amount") if isinstance(daily.get("current"), dict) else None
            maximum = daily.get("maximum", {}).get("amount") if isinstance(daily.get("maximum"), dict) else None
            avail_d = daily.get("available") or {}
            if isinstance(avail_d, dict):
                available = avail_d.get("amount")
            until = daily.get("until")

    snapshot = {
        "current": current,
        "maximum": maximum,
        "available": available,
        "until": until,
        "raw": body,
    }
    entry.quota = snapshot
    entry.quota_fetched_at = time.time()
    # Use available to determine if quota is remaining; fallback to current
    if available is not None:
        try:
            if float(available) > 0:
                entry.clear_cooldown()
        except (TypeError, ValueError):
            pass
    elif current is not None:
        try:
            if float(current) > 0:
                entry.clear_cooldown()
        except (TypeError, ValueError):
            pass
    return snapshot


def save_quota_cache(pool: "CredentialPool", settings: Settings) -> None:
    cache = {
        e.id: {
            "label": e.label,
            "license_id": e.license_id,
            "quota": {
                "current": (e.quota or {}).get("current"),
                "maximum": (e.quota or {}).get("maximum"),
                "available": (e.quota or {}).get("available"),
                "until": (e.quota or {}).get("until"),
            },
            "fetched_at": e.quota_fetched_at,
        }
        for e in pool.all()
    }
    try:
        settings.quota_cache_file.write_text(
            json.dumps(cache, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        log.warning("Failed to write quota cache: %s", e)


def load_quota_cache(pool: "CredentialPool", settings: Settings) -> None:
    path = settings.quota_cache_file
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        for entry in pool.all():
            cached = data.get(entry.id)
            if isinstance(cached, dict):
                entry.quota = cached.get("quota") or {}
                entry.quota_fetched_at = cached.get("fetched_at") or 0
    except Exception as e:
        log.warning("Failed to load quota cache: %s", e)


async def fetch_quota_delta(entry: "CredentialEntry", client: httpx.AsyncClient, settings: Settings) -> int | None:
    """Fetch current quota and return the delta (tokens consumed) since last known value.

    Returns the delta as a positive integer, or None if unable to calculate.
    Updates entry.quota['current'] with the new value for future delta calculations.
    """
    old_current = None
    if entry.quota and entry.quota.get("current") is not None:
        try:
            old_current = float(entry.quota["current"])
        except (TypeError, ValueError):
            pass

    snapshot = await fetch_quota_for(entry, client, settings)
    if snapshot is None:
        return None

    new_current = snapshot.get("current")
    if new_current is None or old_current is None:
        return None

    try:
        delta = float(new_current) - float(old_current)
        # current goes UP as tokens are consumed (used amount)
        if delta > 0:
            return int(delta)
        elif delta == 0:
            return 0
        else:
            # negative delta means quota was reset, can't determine usage
            log.info("[cred %s] quota delta negative (%.0f -> %.0f), likely reset", entry.id, old_current, float(new_current))
            return None
    except (TypeError, ValueError):
        return None


async def quota_refresher_loop(pool: "CredentialPool", client: httpx.AsyncClient, settings: Settings) -> None:
    """Background asyncio task: refresh all credentials' quotas periodically."""
    interval = settings.quota.refresh_interval_seconds
    log.info("Quota refresher started (interval=%ds)", interval)
    while True:
        try:
            for entry in pool.all():
                try:
                    await fetch_quota_for(entry, client, settings)
                except Exception as e:
                    log.error("[cred %s] quota refresh error: %s", entry.id, e)
            save_quota_cache(pool, settings)
        except asyncio.CancelledError:
            log.info("Quota refresher cancelled")
            raise
        except Exception as e:
            log.error("Quota refresher loop error: %s", e)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
