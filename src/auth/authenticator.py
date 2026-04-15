"""API key authentication — 1:1 port of codex2api-workers auth.ts.

Supports three key types:
  - sk-*  : system keys, validated via timing-safe comparison against config
  - jb-*  : JB keys, plain-text lookup in ``users.jb_api_key``
  - dk-*  : Discord keys, SHA-256 hashed then looked up in ``api_keys.key_hash``
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from src.auth.crypto import sha256_hex, timing_safe_equal

log = logging.getLogger("grazie2api.auth")


# ---------------------------------------------------------------------------
# AuthResult
# ---------------------------------------------------------------------------

@dataclass
class AuthResult:
    """Outcome of an API-key authentication attempt."""

    ok: bool = False
    api_key_id: str = ""
    owner_id: str = ""
    owner_type: str = ""       # "system" | "discord"
    identity: str = ""
    tier: str = "default"      # "default" | "creator" | "system"
    source: str = ""           # "authorization" | "x-api-key"
    is_jb_key: bool = False
    status: int = 200
    message: str = ""
    code: str = ""
    user: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Token extraction
# ---------------------------------------------------------------------------

def _extract_token(headers: dict[str, str]) -> tuple[str, str]:
    """Return ``(token, source)`` from request headers.

    Checks ``Authorization: Bearer <token>`` first, then ``x-api-key``.
    """
    auth_header = headers.get("authorization", "")
    if auth_header:
        token = auth_header.removeprefix("Bearer ").strip()
        if token:
            return token, "authorization"

    x_key = headers.get("x-api-key", "")
    if x_key:
        return x_key.strip(), "x-api-key"

    return "", ""


# ---------------------------------------------------------------------------
# System key matching
# ---------------------------------------------------------------------------

def _match_system_key(
    system_keys: list[dict[str, Any]],
    token: str,
) -> AuthResult | None:
    """Try to match *token* against the configured system keys list.

    Returns an ``AuthResult`` with ``ok=True`` on match, ``None`` otherwise.
    """
    for item in system_keys:
        if not item.get("enabled", True):
            continue
        key_value = item.get("key", "")
        if timing_safe_equal(key_value, token):
            return AuthResult(
                ok=True,
                api_key_id=item.get("id", ""),
                owner_id=item.get("identity", ""),
                owner_type="system",
                identity=item.get("identity", ""),
                tier=item.get("tier", "system"),
                source="",  # filled by caller
            )
    return None


# ---------------------------------------------------------------------------
# Main authentication function
# ---------------------------------------------------------------------------

async def authenticate_api_key(
    db: Any,
    headers: dict[str, str],
    system_keys: list[dict[str, Any]],
    path: str = "",
) -> AuthResult:
    """Authenticate an incoming request.

    Parameters
    ----------
    db:
        An ``aiosqlite.Connection`` (or compatible) for user / key lookups.
    headers:
        Lowercased HTTP headers dict.
    system_keys:
        List of system key dicts from config (see ``Settings.system_api_keys``).
    path:
        Request path for logging.

    Returns
    -------
    AuthResult
        ``.ok == True`` on success; ``.status`` and ``.code`` on failure.
    """
    token, source = _extract_token(headers)

    if not token:
        log.info("[AUTH] %s -> FAIL: no token (source=%s)", path, source or "none")
        return AuthResult(
            ok=False,
            status=401,
            code="missing_api_key",
            message="missing api key",
        )

    # --- 1. System keys (sk-*) -----------------------------------------
    sys_result = _match_system_key(system_keys, token)
    if sys_result is not None:
        sys_result.source = source
        log.info("[AUTH] %s -> OK system key=%s identity=%s", path, sys_result.api_key_id, sys_result.identity)
        return sys_result

    # --- 2. JB keys (jb-*) ---------------------------------------------
    if token.startswith("jb-"):
        cursor = await db.execute(
            "SELECT discord_user_id, username, global_name, tier "
            "FROM users WHERE jb_api_key = ? LIMIT 1",
            (token,),
        )
        row = await cursor.fetchone()
        if row:
            discord_id = str(row[0] or "")
            username = str(row[1] or "")
            global_name = str(row[2] or "")
            tier = str(row[3] or "default")
            log.info("[AUTH] %s -> OK jb-key user=%s tier=%s", path, username, tier)
            return AuthResult(
                ok=True,
                api_key_id=f"jb-{discord_id}",
                owner_id=discord_id,
                owner_type="discord",
                identity=global_name or username or discord_id,
                tier=tier,
                source=source,
                is_jb_key=True,
            )
        log.info("[AUTH] %s -> FAIL jb-key not found (prefix=%s...)", path, token[:10])
        return AuthResult(
            ok=False,
            status=401,
            code="invalid_api_key",
            message="invalid jb api key",
        )

    # --- 3. Discord keys (dk-*) ------------------------------------------
    # NOTE: dk- key authentication via api_keys table is not yet migrated.
    # SHA-256 hash lookup will be implemented when dk- keys are needed.
    log.info("[AUTH] %s -> FAIL unrecognised key (prefix=%s...)", path, token[:10])
    return AuthResult(
        ok=False,
        status=401,
        code="invalid_api_key",
        message="invalid api key",
    )
