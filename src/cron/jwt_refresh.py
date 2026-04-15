"""Background JWT refresh cron — replaces Worker scheduled triggers.

Periodically scans jb_credentials for tokens that are expired or about to expire,
then refreshes them via the RT -> id_token -> JWT pipeline.

Also supports password-based re-login when the refresh token is dead.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from typing import Any, TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from src.config import Settings

from src.db.database import get_db
from src.db.jb_credentials import (
    list_credentials_needing_refresh,
    update_jb_credential_jwt,
)

log = logging.getLogger("grazie2api.cron.jwt_refresh")


async def refresh_credential_jwt(
    http_client: httpx.AsyncClient,
    refresh_token: str,
    license_id: str,
    settings: "Settings",
    email: str = "",
    password: str = "",
) -> dict[str, Any] | None:
    """Refresh a single credential's JWT using refresh_token -> id_token -> JWT.

    1:1 port of Worker's refreshPoolCredentialJwt.
    Falls back to password re-login if RT is dead and email+password are available.

    Returns {"jwt": str, "id_token": str, "new_rt": str, "expires_at": int} or None.
    """
    try:
        # Step 1: RT -> id_token
        token_resp = await http_client.post(
            settings.urls.hub_token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": settings.auth.client_id,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        if token_resp.status_code != 200:
            log.warning(
                "[jwt_refresh] RT refresh failed for %s (status=%d), trying password fallback",
                email or "unknown", token_resp.status_code,
            )
            # RT expired/invalid -- fallback to password re-login
            if email and password:
                return await _relogin_with_password(http_client, email, password, settings)
            return None

        token_data = token_resp.json()
        id_token = str(token_data.get("id_token") or token_data.get("access_token", ""))
        new_rt = str(token_data.get("refresh_token") or refresh_token)
        if not id_token:
            log.warning("[jwt_refresh] No id_token in token response for %s", email or "unknown")
            return None

        # Step 2: id_token -> JWT via provide-access
        jwt_resp = await http_client.post(
            settings.urls.jwt_url,
            json={"licenseId": license_id},
            headers={
                "Authorization": f"Bearer {id_token}",
                "Content-Type": "application/json",
                "User-Agent": "ktor-client",
            },
        )
        if jwt_resp.status_code != 200:
            log.warning(
                "[jwt_refresh] provide-access failed for %s (status=%d)",
                email or "unknown", jwt_resp.status_code,
            )
            return None

        jwt_data = jwt_resp.json()
        jwt = str(jwt_data.get("token", ""))
        if not jwt:
            log.warning("[jwt_refresh] No token in provide-access response for %s", email or "unknown")
            return None

        expires_at = int(time.time() * 1000) + 23 * 60 * 60 * 1000  # 23 hours
        return {
            "jwt": jwt,
            "id_token": id_token,
            "new_rt": new_rt,
            "expires_at": expires_at,
        }
    except Exception as e:
        log.error("[jwt_refresh] Exception refreshing %s: %s", email or "unknown", e)
        return None


async def _relogin_with_password(
    http_client: httpx.AsyncClient,
    email: str,
    raw_password: str,
    settings: "Settings",
) -> dict[str, Any] | None:
    """Re-login with email+password when RT is dead.

    Password may be base64-encoded (from user_contributions) or plaintext.
    Uses the same jbApiLogin flow as the Worker.
    """
    # Decode base64 password if applicable
    password = raw_password
    try:
        decoded = base64.b64decode(raw_password).decode("utf-8")
        if decoded and all(0x20 <= ord(c) <= 0x7e for c in decoded):
            password = decoded
    except Exception:
        pass  # not base64, use as-is

    # Import the OAuth login flow (same as used for initial credential setup)
    try:
        from src.auth.oauth import oauth_login, discover_license_id
        from src.auth.pkce import generate_pkce

        # Step 1: OAuth login
        login_result = await oauth_login(http_client, email, password, settings)
        if not login_result or not login_result.get("refresh_token"):
            log.warning("[jwt_refresh] Password re-login failed for %s", email)
            return None

        id_token = login_result.get("id_token", "")
        new_rt = login_result.get("refresh_token", "")

        # Step 2: Discover license ID
        license_id = await discover_license_id(http_client, id_token, settings)
        if not license_id:
            log.warning("[jwt_refresh] No license found after re-login for %s", email)
            return None

        # Step 3: Get JWT
        jwt_resp = await http_client.post(
            settings.urls.jwt_url,
            json={"licenseId": license_id},
            headers={
                "Authorization": f"Bearer {id_token}",
                "Content-Type": "application/json",
                "User-Agent": "ktor-client",
            },
        )
        if jwt_resp.status_code != 200:
            return None

        jwt_data = jwt_resp.json()
        jwt = str(jwt_data.get("token", ""))
        if not jwt:
            return None

        expires_at = int(time.time() * 1000) + 23 * 60 * 60 * 1000
        return {
            "jwt": jwt,
            "id_token": id_token,
            "new_rt": new_rt,
            "expires_at": expires_at,
        }
    except Exception as e:
        log.error("[jwt_refresh] Password re-login exception for %s: %s", email, e)
        return None


async def jwt_refresh_loop(
    settings: "Settings",
    http_client: httpx.AsyncClient,
    interval: int = 600,
) -> None:
    """Background loop that refreshes expiring JWTs every `interval` seconds.

    Scans jb_credentials for tokens expiring within 10 minutes,
    refreshes them, and updates the DB.
    """
    log.info("[jwt_cron] Starting JWT refresh loop (interval=%ds)", interval)

    while True:
        try:
            await asyncio.sleep(interval)
            db = get_db()

            # Find credentials needing refresh (JWT expires within 10 min margin)
            margin_ms = interval * 1000  # match the interval as margin
            creds = await list_credentials_needing_refresh(db, margin_ms=margin_ms)

            if not creds:
                log.debug("[jwt_cron] No credentials need refresh")
                continue

            log.info("[jwt_cron] Refreshing %d credentials", len(creds))

            refreshed_count = 0
            failed_count = 0

            for cred in creds:
                cred_id = cred["id"]
                email = cred.get("jb_email", "")
                rt = cred.get("refresh_token", "")
                lid = cred.get("license_id", "")
                pwd = cred.get("jb_password", "")

                if not rt and not (email and pwd):
                    log.warning("[jwt_cron] Credential %s (%s) has no RT and no password, skipping", cred_id, email)
                    failed_count += 1
                    continue

                if not lid:
                    log.warning("[jwt_cron] Credential %s (%s) has no license_id, skipping", cred_id, email)
                    failed_count += 1
                    continue

                result = await refresh_credential_jwt(
                    http_client, rt, lid, settings,
                    email=email, password=pwd,
                )

                if result and result.get("jwt"):
                    await update_jb_credential_jwt(
                        db, cred_id,
                        jwt=result["jwt"],
                        expires_at=result["expires_at"],
                        refresh_token=result["new_rt"],
                    )
                    refreshed_count += 1
                    log.info("[jwt_cron] Refreshed %s (%s) OK", cred_id, email)
                else:
                    failed_count += 1
                    log.warning("[jwt_cron] Failed to refresh %s (%s)", cred_id, email)

                # Small delay between refreshes to avoid hammering JB servers
                await asyncio.sleep(2)

            log.info("[jwt_cron] Batch done: %d refreshed, %d failed", refreshed_count, failed_count)

        except asyncio.CancelledError:
            log.info("[jwt_cron] JWT refresh loop cancelled")
            raise
        except Exception as e:
            log.error("[jwt_cron] Unexpected error in refresh loop: %s", e, exc_info=True)
            # Continue the loop; don't crash on transient errors
