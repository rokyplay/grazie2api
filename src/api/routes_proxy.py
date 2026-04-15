"""Proxy routes: /v1/chat/completions, /v1/messages, /v1/responses, /v1/models, /health, /"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from src.api.app import state
from src.api.middleware import check_auth, check_body_size, global_pool_limiter
from src.proxy.models import resolve_model, fetch_profiles, get_cached_profiles
from src.proxy.converters import (
    openai_msgs_to_jb,
    anthropic_msgs_to_jb,
    responses_input_to_jb,
    responses_tools_to_openai,
)
from src.proxy.upstream import prepare_jb_request
from src.proxy.formatters import (
    oai_stream,
    oai_non_stream,
    anthropic_stream,
    anthropic_non_stream,
    responses_stream,
    responses_non_stream,
)

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
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
):
    check_auth(authorization, x_api_key)
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


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
):
    check_auth(authorization, x_api_key)
    _check_global_rate_limit(authorization, x_api_key)
    check_body_size(request)
    _ensure_pool_ready()

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
    check_auth(authorization, x_api_key)
    _check_global_rate_limit(authorization, x_api_key)
    check_body_size(request)
    _ensure_pool_ready()

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
    check_auth(authorization, x_api_key)
    _check_global_rate_limit(authorization, x_api_key)
    check_body_size(request)
    _ensure_pool_ready()

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
