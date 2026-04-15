"""Grazie SSE upstream call: builds request, streams events."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any, TYPE_CHECKING

import httpx
from fastapi import HTTPException

from src.proxy.converters.common import map_finish_reason
from src.config import Settings

if TYPE_CHECKING:
    from src.credentials.entry import CredentialEntry
    from src.credentials.pool import CredentialPool
    from src.stats.recorder import StatsRecorder

log = logging.getLogger("grazie2api.upstream")


def redact_log(text: str) -> str:
    """Redact token-like patterns from log text to prevent credential leakage."""
    return re.sub(r'(Bearer\s+)[A-Za-z0-9\-_\.]{20,}', r'\1<REDACTED>', text)


def map_jb_error(status: int, body: bytes, settings: Settings, entry: "CredentialEntry | None" = None) -> str:
    """Map upstream error to a human-readable message.

    Also updates credential state when needed (cooldown on 477, clear JWT on 401).
    """
    text = redact_log(body.decode("utf-8", errors="replace")[:500])
    if status == 477:
        if entry is not None:
            entry.mark_cooldown(settings.credentials.cooldown_seconds, "quota exhausted (477)")
        return "Grazie AI quota exhausted"
    if status == 401:
        if entry is not None:
            entry.token_manager.jwt = ""
            entry.token_manager.jwt_expires = 0
        return "Grazie AI auth expired, will retry on next request"
    if status == 403:
        if entry is not None:
            entry.mark_cooldown(300, "access forbidden (403)")
        return "Grazie AI access forbidden"
    if status == 429:
        if entry is not None:
            entry.mark_cooldown(120, "rate limited (429)")
        return "Grazie AI rate limited"
    return f"Grazie AI error {status}: {text}"


def build_jb_body_and_headers(
    model: str,
    jb_messages: list[dict],
    jwt: str,
    settings: Settings,
    tools: list[dict] | None = None,
    *,
    temperature: float | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
    stop: list[str] | str | None = None,
    reasoning_effort: str | None = None,
) -> tuple[dict, dict, str]:
    """Build Grazie request body + headers from a pre-obtained JWT.

    Returns (jb_body, jb_headers, request_id).
    This is the pure body-builder extracted from prepare_jb_request
    so it can be reused by both global-pool and per-user paths.
    """
    jb_body: dict[str, Any] = {
        "prompt": settings.grazie.chat_prompt,
        "profile": model,
        "chat": {"messages": jb_messages},
    }

    param_data: list[dict] = []

    # Tools injection (paired format: fqdn entry + value entry)
    if tools:
        tool_defs = []
        for t in tools:
            if "function" not in t:
                continue
            fn = dict(t["function"])
            if fn.get("parameters") is None:
                fn["parameters"] = {"type": "object", "properties": {}}
            tool_defs.append(fn)
        if tool_defs:
            param_data.append({"type": "json", "fqdn": "llm.parameters.functions"})
            param_data.append({"type": "json", "value": json.dumps(tool_defs)})
            log.info("Injecting %d tool definitions", len(tool_defs))

    # Sampling parameters: temperature and top_p are mutually exclusive
    # OpenAI provider models reject temperature/top_p (400 "not supported for chat OpenAI provider")
    is_openai_provider = model.lower().startswith("openai-")
    if not is_openai_provider:
        has_temp = temperature is not None
        has_top_p = top_p is not None
        if has_temp:
            param_data.append({"type": "double", "fqdn": "llm.parameters.temperature"})
            param_data.append({"type": "double", "value": str(temperature)})
        elif has_top_p:
            param_data.append({"type": "double", "fqdn": "llm.parameters.top-p"})
            param_data.append({"type": "double", "value": str(top_p)})

    # reasoning-effort only for thinking models (o3/o4 etc), others will 400
    if reasoning_effort and re.search(r'o[34]|thinking', model, re.IGNORECASE):
        param_data.append({"type": "text", "fqdn": "llm.parameters.reasoning-effort"})
        param_data.append({"type": "text", "value": str(reasoning_effort)})

    # NOTE: top_k, seed, response_format are NOT forwarded — they cause 400 "Failed to convert"
    # NOTE: max_tokens and stop are NOT supported by Grazie parameters.data

    if param_data:
        jb_body["parameters"] = {"data": param_data}

    jb_headers = {
        "grazie-authenticate-jwt": jwt,
        "grazie-agent": settings.grazie.agent_json,
        "User-Agent": "ktor-client",
        "Content-Type": "application/json",
    }
    request_id = uuid.uuid4().hex[:24]
    return jb_body, jb_headers, request_id


async def prepare_jb_request(
    model: str,
    jb_messages: list[dict],
    settings: Settings,
    http_client: httpx.AsyncClient,
    pool: "CredentialPool",
    stats: "StatsRecorder | None",
    strategy: str,
    tools: list[dict] | None = None,
    *,
    temperature: float | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
    stop: list[str] | str | None = None,
    reasoning_effort: str | None = None,
) -> tuple[dict, dict, str, "CredentialEntry"]:
    """Build request body + headers via global pool, get JWT.

    Returns (jb_body, jb_headers, request_id, credential_entry).
    """
    entry = await pool.pick(strategy, stats)
    try:
        jwt = await entry.token_manager.ensure_valid_jwt()
    except httpx.HTTPStatusError as e:
        log.error(
            "[cred %s] Token refresh failed: %s %s",
            entry.id, e.response.status_code, redact_log(e.response.text[:500]),
        )
        entry.mark_cooldown(60, f"token refresh http {e.response.status_code}")
        raise HTTPException(
            status_code=502,
            detail={"error": {"message": "Token refresh failed", "type": "upstream_error"}},
        )

    jb_body, jb_headers, request_id = build_jb_body_and_headers(
        model, jb_messages, jwt, settings,
        tools=tools, temperature=temperature, top_p=top_p,
        max_tokens=max_tokens, stop=stop, reasoning_effort=reasoning_effort,
    )
    return jb_body, jb_headers, request_id, entry


async def stream_jb_events(
    jb_body: dict,
    jb_headers: dict,
    request_id: str,
    settings: Settings,
    http_client: httpx.AsyncClient,
    entry: "CredentialEntry | None" = None,
):
    """Yield (event_type, data) tuples from JB upstream SSE.

    event_type: 'content' | 'function_call' | 'finish' | 'quota' | 'error' | 'status'
    """
    try:
        async with http_client.stream("POST", settings.urls.chat_url, json=jb_body, headers=jb_headers) as resp:
            if resp.status_code != 200:
                error_body = await resp.aread()
                error_msg = map_jb_error(resp.status_code, error_body, settings, entry=entry)
                yield ("status", resp.status_code)
                yield ("error", f"[Error {resp.status_code}] {error_msg}")
                return

            pending_fc: dict | None = None
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:].strip()
                if payload == "end":
                    break
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                evt_type = event.get("type", "")
                if evt_type == "Content":
                    yield ("content", event.get("content", ""))
                elif evt_type == "FunctionCall":
                    fc_name = event.get("functionName") or event.get("name")
                    fc_content = event.get("content", "")
                    if pending_fc is None:
                        pending_fc = {"name": fc_name or "", "arguments": fc_content}
                    else:
                        if fc_name and not pending_fc["name"]:
                            pending_fc["name"] = fc_name
                        pending_fc["arguments"] += fc_content
                elif evt_type == "FinishMetadata":
                    if pending_fc is not None:
                        yield ("function_call", pending_fc)
                        pending_fc = None
                    reason = event.get("reason", "stop")
                    yield ("finish", map_finish_reason(reason))
                elif evt_type == "QuotaMetadata":
                    spent = event.get("spent", {})
                    updated = event.get("updated", {})
                    spent_amount = spent.get("amount", "0")
                    log.info("[%s] quota spent: %s", request_id, spent_amount)
                    # Update entry quota from real-time data
                    if entry is not None and updated:
                        tq = updated.get("tariffQuota", {})
                        entry.quota = {
                            "current": (updated.get("current") or {}).get("amount"),
                            "maximum": (updated.get("maximum") or {}).get("amount"),
                            "available": (tq.get("available") or {}).get("amount"),
                            "until": updated.get("until"),
                        }
                        import time as _time
                        entry.quota_fetched_at = _time.time()
                    yield ("quota", {"spent": spent_amount})

            if pending_fc is not None:
                yield ("function_call", pending_fc)

    except httpx.ReadTimeout:
        log.error("[%s] Read timeout from JB upstream", request_id)
        yield ("error", "[Error] Upstream read timeout")
    except Exception as e:
        log.error("[%s] Stream error: %s", request_id, e, exc_info=True)
        yield ("error", f"[Error] {e}")


async def collect_jb_response(
    jb_body: dict,
    jb_headers: dict,
    request_id: str,
    settings: Settings,
    http_client: httpx.AsyncClient,
    entry: "CredentialEntry | None" = None,
) -> tuple[str, str, dict | None, int, float | None]:
    """Collect full response (non-streaming).

    Returns (text, finish_reason, function_call, upstream_status, quota_spent).
    """
    full_content = ""
    finish_reason = "stop"
    function_call: dict | None = None
    upstream_status = 200
    quota_spent: float | None = None

    async for evt_type, data in stream_jb_events(jb_body, jb_headers, request_id, settings, http_client, entry=entry):
        if evt_type == "content":
            full_content += data
        elif evt_type == "function_call":
            function_call = data
        elif evt_type == "finish":
            finish_reason = data
        elif evt_type == "status":
            upstream_status = int(data)
        elif evt_type == "quota":
            try:
                quota_spent = float(data.get("spent", 0))
            except (TypeError, ValueError):
                pass
        elif evt_type == "error":
            raise HTTPException(status_code=502, detail={"error": {"message": data, "type": "upstream_error"}})

    if function_call:
        finish_reason = "tool_calls"
    return full_content, finish_reason, function_call, upstream_status, quota_spent
