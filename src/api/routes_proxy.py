"""Proxy routes: /v1/chat/completions, /v1/messages, /v1/responses, /v1/models, /health, /

Per-user JB credential isolation:
  When auth.is_jb_key=True, JWT is resolved from the user's own jb_credentials rows
  in SQLite instead of the global pool.  System keys (is_jb_key=False) still use the
  global pool.
"""

from __future__ import annotations

import base64
import datetime
import json as _json
import logging
import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from src.api.app import state
from src.api.middleware import check_auth, check_body_size, global_pool_limiter  # noqa: F401 – check_auth is now async
from src.auth.authenticator import AuthResult
from src.proxy.models import resolve_model, fetch_profiles, get_cached_profiles
from src.proxy.converters import (
    openai_msgs_to_jb,
    anthropic_msgs_to_jb,
    responses_input_to_jb,
    responses_tools_to_openai,
)
from src.proxy.upstream import prepare_jb_request, build_jb_body_and_headers
from src.proxy.formatters import (
    oai_stream,
    oai_non_stream,
    anthropic_stream,
    anthropic_non_stream,
    responses_stream,
    responses_non_stream,
)
from src.db.jb_credentials import (
    list_user_jb_credentials,
    update_jb_credential_jwt,
)
from src.db.audit import record_usage_and_audit
from src.cron.jwt_refresh import refresh_credential_jwt

log = logging.getLogger("grazie2api.routes_proxy")

router = APIRouter()


@router.get("/health")
async def health():
    import time as _time

    global_count = state.pool.count() if state.pool else 0
    global_available = 0
    global_with_jwt = 0
    global_with_license = 0
    if state.pool:
        for entry in state.pool.all():
            if entry.is_available():
                global_available += 1
            if entry.token_manager.jwt:
                global_with_jwt += 1
            if entry.license_id:
                global_with_license += 1

    private_pools = getattr(state, "private_pools", None)
    private_pool_count = len(private_pools.all_prefixes()) if private_pools else 0
    private_cred_count = private_pools.total_credentials() if private_pools else 0

    return {
        "status": "ok",
        "uptime_info": "running",
        "timestamp": int(_time.time()),
        "strategy": state.strategy,
        "global_pool": {
            "total": global_count,
            "available": global_available,
            "with_jwt": global_with_jwt,
            "with_license": global_with_license,
        },
        "private_pools": {
            "pool_count": private_pool_count,
            "credential_count": private_cred_count,
        },
        "portal_enabled": state.settings.portal.enabled,
    }


@router.get("/")
async def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/dashboard")


@router.get("/info")
async def info():
    return {
        "service": "grazie2api",
        "version": "1.0.0",
        "endpoints": [
            "/v1/chat/completions",
            "/v1/messages",
            "/v1/responses",
            "/v1/models",
            "/api/credentials",
            "/api/stats",
            "/api/stats/requests",
            "/dashboard",
        ],
    }


@router.get("/v1/models")
async def list_models(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
):
    await check_auth(request)
    if state.http_client is None:
        raise HTTPException(status_code=503, detail={"error": {"message": "HTTP client not initialized", "type": "not_ready"}})

    jwt = ""
    try:
        if state.pool is not None and state.pool.count() > 0:
            avail = state.pool.available()
            if not avail:
                avail = [e for e in state.pool.all() if e.license_id]
            if avail:
                jwt = await avail[0].token_manager.ensure_valid_jwt()
        if jwt:
            profiles = await fetch_profiles(state.http_client, jwt, state.settings)
        else:
            profiles = get_cached_profiles(state.settings)
    except Exception as e:
        log.error("Failed to get profiles: %s", e)
        profiles = get_cached_profiles(state.settings)

    now_ts = int(time.time())
    data = []
    for profile in profiles:
        data.append({
            "id": profile,
            "object": "model",
            "created": now_ts,
            "owned_by": "grazie",
            "permission": [],
            "root": profile,
            "parent": None,
        })

    # Also expose alias overrides as model entries pointing to their target
    profile_set = set(profiles)
    for alias, target in state.settings.models.alias_overrides.items():
        if target in profile_set:
            data.append({
                "id": alias,
                "object": "model",
                "created": now_ts,
                "owned_by": "grazie",
                "permission": [],
                "root": target,
                "parent": None,
            })

    return {"object": "list", "data": data}


def _extract_api_key(authorization: str | None, x_api_key: str | None) -> str:
    """Extract API key from headers."""
    if authorization:
        return authorization.removeprefix("Bearer ").strip()
    if x_api_key:
        return x_api_key.strip()
    return ""


def _get_user_pool(authorization: str | None, x_api_key: str | None) -> 'CredentialPool':
    """Get the per-user credential pool based on API key. Raises 401/503 if not found."""
    from src.api.app import get_pool_for_key
    if state.http_client is None:
        raise HTTPException(status_code=503, detail={"error": {"message": "HTTP client not initialized", "type": "not_ready"}})

    api_key = _extract_api_key(authorization, x_api_key)
    if not api_key:
        raise HTTPException(status_code=401, detail={"error": {"message": "API key required", "type": "authentication_error"}})

    # Per-user pool lookup
    pool = get_pool_for_key(api_key)
    if pool is not None:
        return pool

    # Fallback to global pool (admin key or legacy)
    if state.api_key and api_key == state.api_key and state.pool and state.pool.count() > 0:
        return state.pool

    raise HTTPException(status_code=401, detail={"error": {"message": "Invalid API key or no credentials found", "type": "authentication_error"}})


def _ensure_pool_ready():
    if state.http_client is None:
        raise HTTPException(
            status_code=503,
            detail={"error": {"message": "HTTP client not initialized", "type": "not_ready"}},
        )
    if state.pool is None or state.pool.count() == 0:
        raise HTTPException(
            status_code=503,
            detail={"error": {"message": "No credentials in pool", "type": "no_credentials"}},
        )


def _check_global_rate_limit(authorization: str | None, x_api_key: str | None) -> None:
    """Apply per-user rate limit for non-admin callers on the global pool."""
    if not state.api_key:
        return  # Open access mode, no rate limit
    token = None
    if authorization:
        token = authorization.removeprefix("Bearer ").strip()
    elif x_api_key:
        token = x_api_key.strip()
    if token and token == state.api_key:
        return  # Admin key, no rate limit
    # Non-admin user: apply 1 rpm rate limit
    caller_id = token or "anonymous"
    global_pool_limiter.check(caller_id)


# ---------------------------------------------------------------------------
# Per-user JWT selection (1:1 port of Worker app.ts L5819-5908)
# ---------------------------------------------------------------------------

async def _get_jwt_for_user(auth: AuthResult) -> tuple[str, str]:
    """Resolve a valid JWT from the user's own jb_credentials.

    Returns ``(jwt, credential_id)``.
    Raises HTTPException(403) with detailed per-credential error if none found.
    """
    from src.db.database import get_db

    db = get_db()
    creds = await list_user_jb_credentials(db, auth.owner_id)
    available = [c for c in creds if c["status"] == "active" and not c["quota_exhausted"]]

    log.info(
        "[JB-JWT] user=%s total_creds=%d available=%d",
        auth.owner_id, len(creds), len(available),
    )

    for cr in available:
        # Check JWT internal real exp (don't trust jwt_expires_at, may be stale)
        jwt_really_valid = False
        if cr["jwt"]:
            try:
                parts = cr["jwt"].split(".")
                if len(parts) == 3:
                    # Base64-decode the payload, add padding
                    payload_b64 = parts[1] + "=="
                    payload = _json.loads(base64.b64decode(payload_b64))
                    jwt_really_valid = bool(payload.get("exp") and payload["exp"] * 1000 > time.time() * 1000)
            except Exception:
                pass  # decode failed, treat as expired

        if jwt_really_valid:
            log.info("[JB-JWT] found valid jwt from %s", cr["jb_email"])
            return cr["jwt"], cr["id"]

        # JWT expired or empty -> try refresh_token path
        if cr["refresh_token"] and cr["license_id"]:
            log.info("[JB-JWT] %s jwt expired/empty, refreshing...", cr["jb_email"])
            refreshed = await refresh_credential_jwt(
                state.http_client,
                cr["refresh_token"],
                cr["license_id"],
                state.settings,
                email=cr.get("jb_email", ""),
                password=cr.get("jb_password", ""),
            )
            if refreshed and refreshed.get("jwt"):
                await update_jb_credential_jwt(
                    db, cr["id"],
                    jwt=refreshed["jwt"],
                    expires_at=refreshed["expires_at"],
                    refresh_token=refreshed["new_rt"],
                )
                log.info("[JB-JWT] refreshed %s OK", cr["jb_email"])
                return refreshed["jwt"], cr["id"]
            else:
                log.warning("[JB-JWT] refresh %s FAILED", cr["jb_email"])

    # No valid JWT found -> build detailed 403 error
    reasons: list[str] = []
    for cr in creds:
        email = cr["jb_email"]
        if cr["quota_exhausted"]:
            reasons.append(f"{email}: quota exhausted")
            continue
        if not cr["jwt"] and not cr["license_id"]:
            reasons.append(f"{email}: activation failed (need card or region restricted)")
            continue
        if not cr["jwt"] and cr["license_id"]:
            reasons.append(f"{email}: JWT fetch failed, please re-activate")
            continue
        if cr["jwt"] and cr.get("jwt_expires_at", 0) <= int(time.time() * 1000):
            reasons.append(f"{email}: JWT expired, refresh failed")
            continue
        if cr["status"] != "active":
            reasons.append(f"{email}: status={cr['status']}")
            continue
        reasons.append(f"{email}: unknown reason")

    if not creds:
        reasons.append("No JB credentials submitted. Please add credentials in the portal.")

    detail_str = "; ".join(reasons)
    log.warning("[JB-JWT] NO JWT found for user=%s: %s", auth.owner_id, detail_str)
    raise HTTPException(
        status_code=403,
        detail={
            "error": {
                "message": f"No usable credentials: {detail_str}",
                "type": "permission_error",
                "code": "jb_no_credentials",
            }
        },
    )


def _ensure_ready_for_jb():
    """Ensure HTTP client is initialized (for per-user JB path, no pool needed)."""
    if state.http_client is None:
        raise HTTPException(
            status_code=503,
            detail={"error": {"message": "HTTP client not initialized", "type": "not_ready"}},
        )


def _today_date() -> str:
    """Return today's date as YYYY-MM-DD string (UTC)."""
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")


async def _record_audit_bg(
    auth: AuthResult,
    model: str,
    status_code: int,
    latency_ms: int,
    stream: bool,
    error_code: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    credential_id: str = "",
    quota_spent: float = 0,
) -> None:
    """Fire-and-forget audit recording into local SQLite."""
    try:
        from src.db.database import get_db
        db = get_db()
        await record_usage_and_audit(
            db,
            usage_date=_today_date(),
            api_key_id=auth.api_key_id,
            owner_type=auth.owner_type,
            owner_id=auth.owner_id,
            identity=auth.identity,
            tier=auth.tier,
            model=model,
            channel_id="jetbrains",
            status_code=status_code,
            latency_ms=latency_ms,
            stream=stream,
            error_code=error_code,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            credential_id=credential_id,
            quota_spent=quota_spent,
        )
    except Exception as e:
        log.error("Audit recording failed: %s", e)


# ---------------------------------------------------------------------------
# Helper: prepare request for per-user path (JWT from SQLite)
# ---------------------------------------------------------------------------

async def _prepare_per_user_request(
    auth: AuthResult,
    model: str,
    jb_messages: list[dict],
    tools: list[dict] | None = None,
    *,
    temperature: float | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
    stop: list[str] | str | None = None,
) -> tuple[dict, dict, str, str]:
    """Build Grazie request using per-user JWT from SQLite.

    Returns (jb_body, jb_headers, request_id, credential_id).
    """
    jwt, credential_id = await _get_jwt_for_user(auth)
    jb_body, jb_headers, request_id = build_jb_body_and_headers(
        model, jb_messages, jwt, state.settings,
        tools=tools, temperature=temperature, top_p=top_p,
        max_tokens=max_tokens, stop=stop,
    )
    return jb_body, jb_headers, request_id, credential_id


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
):
    auth_result = await check_auth(request)
    _check_global_rate_limit(authorization, x_api_key)
    check_body_size(request)

    body: dict[str, Any] = await request.json()
    model = resolve_model(body.get("model", "anthropic-claude-4-6-sonnet"), state.settings)
    messages = body.get("messages", [])
    stream = body.get("stream", False)
    tools = body.get("tools")
    temperature = body.get("temperature")
    top_p = body.get("top_p")
    max_tokens = body.get("max_completion_tokens") or body.get("max_tokens")
    stop = body.get("stop")

    if not messages:
        raise HTTPException(status_code=400, detail={"error": {"message": "messages is required"}})

    jb_messages = openai_msgs_to_jb(messages)

    # Per-user path: jb- key users get JWT from their own SQLite credentials
    if auth_result.is_jb_key:
        _ensure_ready_for_jb()
        jb_body, jb_headers, rid, cred_id = await _prepare_per_user_request(
            auth_result, model, jb_messages,
            tools=tools, temperature=temperature, top_p=top_p,
            max_tokens=max_tokens, stop=stop,
        )
        request_id = f"chatcmpl-{rid}"
        created = int(time.time())
        started_at = time.time()
        log.info(
            "[%s] per-user model=%s msgs=%d stream=%s cred=%s user=%s",
            request_id, model, len(messages), stream, cred_id, auth_result.owner_id,
        )
        if stream:
            return StreamingResponse(
                oai_stream(jb_body, jb_headers, request_id, model, created, messages,
                           state.settings, state.http_client, state.stats, None, started_at),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
            )
        else:
            return await oai_non_stream(
                jb_body, jb_headers, request_id, model, created, messages,
                state.settings, state.http_client, state.stats, None, started_at,
            )

    # Global pool path: system keys use shared credential pool
    _ensure_pool_ready()
    jb_body, jb_headers, rid, entry = await prepare_jb_request(
        model, jb_messages, state.settings, state.http_client, state.pool, state.stats, state.strategy,
        tools=tools, temperature=temperature, top_p=top_p, max_tokens=max_tokens, stop=stop,
    )
    request_id = f"chatcmpl-{rid}"
    created = int(time.time())
    started_at = time.time()

    log.info(
        "[%s] model=%s msgs=%d stream=%s cred=%s",
        request_id, model, len(messages), stream,
        entry.id if entry else "legacy",
    )

    if stream:
        return StreamingResponse(
            oai_stream(jb_body, jb_headers, request_id, model, created, messages,
                       state.settings, state.http_client, state.stats, entry, started_at),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )
    else:
        return await oai_non_stream(
            jb_body, jb_headers, request_id, model, created, messages,
            state.settings, state.http_client, state.stats, entry, started_at,
        )


@router.post("/v1/messages")
async def anthropic_messages(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
):
    auth_result = await check_auth(request)
    _check_global_rate_limit(authorization, x_api_key)
    check_body_size(request)

    body: dict[str, Any] = await request.json()
    model = resolve_model(body.get("model", "anthropic-claude-4-6-sonnet"), state.settings)
    stream = body.get("stream", False)
    messages = body.get("messages", [])
    temperature = body.get("temperature")
    top_p = body.get("top_p")
    max_tokens = body.get("max_tokens")
    stop = body.get("stop_sequences")

    if not messages:
        raise HTTPException(status_code=400, detail={"type": "error", "error": {"type": "invalid_request_error", "message": "messages is required"}})

    jb_messages, openai_tools = anthropic_msgs_to_jb(body)

    # Per-user path
    if auth_result.is_jb_key:
        _ensure_ready_for_jb()
        jb_body, jb_headers, rid, cred_id = await _prepare_per_user_request(
            auth_result, model, jb_messages,
            tools=openai_tools, temperature=temperature, top_p=top_p,
            max_tokens=max_tokens, stop=stop,
        )
        msg_id = f"msg_{rid}"
        started_at = time.time()
        log.info(
            "[%s] per-user anthropic model=%s msgs=%d stream=%s cred=%s user=%s",
            msg_id, model, len(messages), stream, cred_id, auth_result.owner_id,
        )
        if stream:
            return StreamingResponse(
                anthropic_stream(jb_body, jb_headers, rid, msg_id, model, body,
                                 state.settings, state.http_client, state.stats, None, started_at),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
            )
        else:
            return await anthropic_non_stream(
                jb_body, jb_headers, rid, msg_id, model, body,
                state.settings, state.http_client, state.stats, None, started_at,
            )

    # Global pool path
    _ensure_pool_ready()
    jb_body, jb_headers, rid, entry = await prepare_jb_request(
        model, jb_messages, state.settings, state.http_client, state.pool, state.stats, state.strategy,
        tools=openai_tools, temperature=temperature, top_p=top_p, max_tokens=max_tokens, stop=stop,
    )
    msg_id = f"msg_{rid}"
    started_at = time.time()

    log.info(
        "[%s] anthropic model=%s msgs=%d stream=%s cred=%s",
        msg_id, model, len(messages), stream,
        entry.id if entry else "legacy",
    )

    if stream:
        return StreamingResponse(
            anthropic_stream(jb_body, jb_headers, rid, msg_id, model, body,
                             state.settings, state.http_client, state.stats, entry, started_at),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )
    else:
        return await anthropic_non_stream(
            jb_body, jb_headers, rid, msg_id, model, body,
            state.settings, state.http_client, state.stats, entry, started_at,
        )


@router.post("/v1/responses")
async def openai_responses(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
):
    auth_result = await check_auth(request)
    _check_global_rate_limit(authorization, x_api_key)
    check_body_size(request)

    body: dict[str, Any] = await request.json()
    model = resolve_model(body.get("model", "anthropic-claude-4-6-sonnet"), state.settings)
    input_data = body.get("input", "")
    stream = body.get("stream", False)
    temperature = body.get("temperature")
    top_p = body.get("top_p")
    max_tokens = body.get("max_output_tokens")
    tools = body.get("tools")
    openai_tools = responses_tools_to_openai(tools) if tools else None

    if not input_data:
        raise HTTPException(status_code=400, detail={"error": {"message": "input is required"}})

    jb_messages = responses_input_to_jb(input_data)

    # Per-user path
    if auth_result.is_jb_key:
        _ensure_ready_for_jb()
        jb_body, jb_headers, rid, cred_id = await _prepare_per_user_request(
            auth_result, model, jb_messages,
            tools=openai_tools, temperature=temperature, top_p=top_p,
            max_tokens=max_tokens,
        )
        resp_id = f"resp_{rid}"
        started_at = time.time()
        log.info(
            "[%s] per-user responses model=%s stream=%s cred=%s user=%s",
            resp_id, model, stream, cred_id, auth_result.owner_id,
        )
        if stream:
            return StreamingResponse(
                responses_stream(jb_body, jb_headers, rid, resp_id, model,
                                 state.settings, state.http_client, state.stats, None, started_at),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
            )
        else:
            return await responses_non_stream(
                jb_body, jb_headers, rid, resp_id, model,
                state.settings, state.http_client, state.stats, None, started_at,
            )

    # Global pool path
    _ensure_pool_ready()
    jb_body, jb_headers, rid, entry = await prepare_jb_request(
        model, jb_messages, state.settings, state.http_client, state.pool, state.stats, state.strategy,
        tools=openai_tools, temperature=temperature, top_p=top_p, max_tokens=max_tokens,
    )
    resp_id = f"resp_{rid}"
    started_at = time.time()

    log.info(
        "[%s] responses model=%s stream=%s cred=%s",
        resp_id, model, stream, entry.id if entry else "legacy",
    )

    if stream:
        return StreamingResponse(
            responses_stream(jb_body, jb_headers, rid, resp_id, model,
                             state.settings, state.http_client, state.stats, entry, started_at),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )
    else:
        return await responses_non_stream(
            jb_body, jb_headers, rid, resp_id, model,
            state.settings, state.http_client, state.stats, entry, started_at,
        )
