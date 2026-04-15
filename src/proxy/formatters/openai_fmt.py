"""OpenAI chat completions format: streaming and non-streaming responses."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, TYPE_CHECKING

import httpx
from fastapi.responses import JSONResponse

from src.proxy.converters.common import estimate_tokens, estimate_messages_tokens
from src.proxy.upstream import stream_jb_events, collect_jb_response
from src.config import Settings

if TYPE_CHECKING:
    from src.credentials.entry import CredentialEntry
    from src.stats.recorder import StatsRecorder

log = logging.getLogger("grazie2api.openai_fmt")


def _record_stats(
    stats: "StatsRecorder | None",
    entry: "CredentialEntry | None",
    model: str,
    endpoint: str,
    status_code: int | None,
    started_at: float,
    input_tokens: int | None,
    output_tokens: int | None,
    error_code: str | None,
    quota_spent: float | None = None,
) -> None:
    if stats is None or entry is None:
        return
    latency_ms = int((time.time() - started_at) * 1000) if started_at else 0
    try:
        stats.record(
            credential_id=entry.id,
            model=model,
            endpoint=endpoint,
            status_code=status_code,
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            error_code=error_code,
            quota_spent=quota_spent,
        )
    except Exception as e:
        log.warning("Stats record failed: %s", e)


async def oai_stream(
    jb_body: dict, jb_headers: dict, request_id: str, model: str, created: int,
    original_messages: list[dict],
    settings: Settings,
    http_client: httpx.AsyncClient,
    stats: "StatsRecorder | None" = None,
    entry: "CredentialEntry | None" = None,
    started_at: float = 0.0,
):
    """Yield OpenAI-format SSE chunks."""
    total_content = ""
    finish_reason: str | None = None
    got_function_call = False
    upstream_status = 200
    error_code: str | None = None
    quota_spent_val: float | None = None

    yield f"data: {json.dumps({'id': request_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model, 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': ''}, 'finish_reason': None}]})}\n\n"

    fc_name_acc = ""
    fc_args_acc = ""

    async for evt_type, data in stream_jb_events(jb_body, jb_headers, request_id, settings, http_client, entry=entry):
        if evt_type == "content":
            total_content += data
            chunk = {
                "id": request_id, "object": "chat.completion.chunk", "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"content": data}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk)}\n\n"

        elif evt_type == "function_call":
            got_function_call = True
            fc_name_acc = data["name"]
            fc_args_acc = data["arguments"]
            fc_call_id = f"call_{uuid.uuid4().hex[:24]}"
            fc_chunk = {
                "id": request_id, "object": "chat.completion.chunk", "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": fc_call_id, "type": "function", "function": {"name": fc_name_acc, "arguments": fc_args_acc}}]}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(fc_chunk)}\n\n"

        elif evt_type == "finish":
            finish_reason = data

        elif evt_type == "status":
            upstream_status = int(data)

        elif evt_type == "quota":
            try:
                quota_spent_val = float(data.get("spent", 0))
            except (TypeError, ValueError):
                pass

        elif evt_type == "error":
            error_code = f"upstream_{upstream_status}"
            err_chunk = {
                "id": request_id, "object": "chat.completion.chunk", "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"content": data}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(err_chunk)}\n\n"
            yield "data: [DONE]\n\n"
            _record_stats(stats, entry, model, "chat.completions", upstream_status, started_at,
                          estimate_messages_tokens(original_messages), 0, error_code, quota_spent=quota_spent_val)
            return

    if got_function_call:
        finish_reason = "tool_calls"

    prompt_tokens = estimate_messages_tokens(original_messages)
    completion_tokens = estimate_tokens(total_content)

    final = {
        "id": request_id, "object": "chat.completion.chunk", "created": created, "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason or "stop"}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": prompt_tokens + completion_tokens},
    }
    yield f"data: {json.dumps(final)}\n\n"
    yield "data: [DONE]\n\n"

    log.info("[%s] stream done, in=%d out=%d quota=%.1f",
             request_id, prompt_tokens, completion_tokens, quota_spent_val or 0)
    _record_stats(stats, entry, model, "chat.completions", upstream_status, started_at,
                  prompt_tokens, completion_tokens, None, quota_spent=quota_spent_val)


async def oai_non_stream(
    jb_body: dict, jb_headers: dict, request_id: str, model: str, created: int,
    original_messages: list[dict],
    settings: Settings,
    http_client: httpx.AsyncClient,
    stats: "StatsRecorder | None" = None,
    entry: "CredentialEntry | None" = None,
    started_at: float = 0.0,
) -> JSONResponse:
    """Non-streaming OpenAI response."""
    prompt_tokens_est = estimate_messages_tokens(original_messages)
    try:
        full_content, finish_reason, function_call, upstream_status, quota_spent_val = await collect_jb_response(
            jb_body, jb_headers, request_id, settings, http_client, entry=entry,
        )
    except Exception as e:
        from fastapi import HTTPException
        if isinstance(e, HTTPException):
            _record_stats(stats, entry, model, "chat.completions", e.status_code, started_at,
                          prompt_tokens_est, 0, f"http_{e.status_code}")
            raise
        if isinstance(e, httpx.ReadTimeout):
            _record_stats(stats, entry, model, "chat.completions", 504, started_at,
                          prompt_tokens_est, 0, "timeout")
            return JSONResponse(status_code=504, content={"error": {"message": "Upstream timeout", "type": "timeout_error"}})
        log.error("[%s] Error: %s", request_id, e, exc_info=True)
        _record_stats(stats, entry, model, "chat.completions", 500, started_at,
                      prompt_tokens_est, 0, "internal")
        return JSONResponse(status_code=500, content={"error": {"message": str(e), "type": "internal_error"}})

    message: dict[str, Any] = {"role": "assistant", "content": full_content or None}
    if function_call:
        finish_reason = "tool_calls"
        call_id = f"call_{uuid.uuid4().hex[:24]}"
        message["tool_calls"] = [{"id": call_id, "type": "function", "function": {"name": function_call["name"], "arguments": function_call["arguments"]}}]
        if not full_content:
            message["content"] = None

    prompt_tokens = estimate_messages_tokens(original_messages)
    completion_tokens = estimate_tokens(full_content)
    if function_call:
        completion_tokens += estimate_tokens(function_call.get("name", "")) + estimate_tokens(function_call.get("arguments", ""))

    log.info("[%s] non-stream done, in=%d out=%d quota=%.1f",
             request_id, prompt_tokens, completion_tokens, quota_spent_val or 0)
    _record_stats(stats, entry, model, "chat.completions", upstream_status, started_at,
                  prompt_tokens, completion_tokens, None, quota_spent=quota_spent_val)
    return JSONResponse(content={
        "id": request_id, "object": "chat.completion", "created": created, "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": prompt_tokens + completion_tokens},
    })
