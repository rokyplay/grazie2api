"""Anthropic Messages format: streaming and non-streaming responses."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import TYPE_CHECKING

import httpx
from fastapi.responses import JSONResponse

from src.proxy.converters.common import estimate_tokens, estimate_messages_tokens
from src.proxy.upstream import stream_jb_events, collect_jb_response
from src.config import Settings

if TYPE_CHECKING:
    from src.credentials.entry import CredentialEntry
    from src.stats.recorder import StatsRecorder

log = logging.getLogger("grazie2api.anthropic_fmt")


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


def _calc_anthropic_input_tokens(original_body: dict) -> int:
    """Calculate estimated input tokens for Anthropic format."""
    tokens = estimate_messages_tokens(original_body.get("messages", []))
    sys_text = original_body.get("system", "")
    if isinstance(sys_text, str):
        tokens += estimate_tokens(sys_text)
    elif isinstance(sys_text, list):
        for blk in sys_text:
            if isinstance(blk, dict):
                tokens += estimate_tokens(blk.get("text", ""))
    return tokens


async def anthropic_non_stream(
    jb_body: dict, jb_headers: dict, rid: str, msg_id: str, model: str,
    original_body: dict,
    settings: Settings,
    http_client: httpx.AsyncClient,
    stats: "StatsRecorder | None" = None,
    entry: "CredentialEntry | None" = None,
    started_at: float = 0.0,
) -> JSONResponse:
    prompt_tokens_est = _calc_anthropic_input_tokens(original_body)
    try:
        full_content, finish_reason, function_call, upstream_status, quota_spent_val = await collect_jb_response(
            jb_body, jb_headers, rid, settings, http_client, entry=entry,
        )
    except Exception as e:
        from fastapi import HTTPException
        if isinstance(e, HTTPException):
            _record_stats(stats, entry, model, "messages", e.status_code, started_at,
                          prompt_tokens_est, 0, f"http_{e.status_code}")
            return JSONResponse(status_code=e.status_code, content={"type": "error", "error": {"type": "api_error", "message": str(e.detail)}})
        if isinstance(e, httpx.ReadTimeout):
            _record_stats(stats, entry, model, "messages", 504, started_at, prompt_tokens_est, 0, "timeout")
            return JSONResponse(status_code=504, content={"type": "error", "error": {"type": "api_error", "message": "Upstream timeout"}})
        log.error("[%s] Error: %s", msg_id, e, exc_info=True)
        _record_stats(stats, entry, model, "messages", 500, started_at, prompt_tokens_est, 0, "internal")
        return JSONResponse(status_code=500, content={"type": "error", "error": {"type": "api_error", "message": str(e)}})

    content_blocks: list[dict] = []
    if full_content:
        content_blocks.append({"type": "text", "text": full_content})

    if function_call:
        stop_reason = "tool_use"
        tool_use_id = f"toolu_{uuid.uuid4().hex[:24]}"
        try:
            input_obj = json.loads(function_call["arguments"])
        except (json.JSONDecodeError, TypeError):
            input_obj = function_call["arguments"]
        content_blocks.append({"type": "tool_use", "id": tool_use_id, "name": function_call["name"], "input": input_obj})
    else:
        stop_reason = "end_turn" if finish_reason == "stop" else "max_tokens"

    input_tokens = _calc_anthropic_input_tokens(original_body)
    output_tokens = estimate_tokens(full_content)
    if function_call:
        output_tokens += estimate_tokens(function_call.get("name", "")) + estimate_tokens(function_call.get("arguments", ""))

    log.info("[%s] anthropic non-stream done, in=%d out=%d quota=%.1f",
             msg_id, input_tokens, output_tokens, quota_spent_val or 0)
    _record_stats(stats, entry, model, "messages", upstream_status, started_at,
                  input_tokens, output_tokens, None, quota_spent=quota_spent_val)

    return JSONResponse(content={
        "id": msg_id, "type": "message", "role": "assistant",
        "content": content_blocks, "model": model,
        "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    })


async def anthropic_stream(
    jb_body: dict, jb_headers: dict, rid: str, msg_id: str, model: str,
    original_body: dict,
    settings: Settings,
    http_client: httpx.AsyncClient,
    stats: "StatsRecorder | None" = None,
    entry: "CredentialEntry | None" = None,
    started_at: float = 0.0,
):
    """Yield Anthropic-format SSE events."""
    total_content = ""
    finish_reason = "end_turn"
    block_index = 0
    got_function_call = False
    upstream_status = 200
    error_code: str | None = None
    quota_spent_val: float | None = None

    ant_input_tokens = _calc_anthropic_input_tokens(original_body)

    msg_start = {
        "type": "message_start",
        "message": {
            "id": msg_id, "type": "message", "role": "assistant",
            "content": [], "model": model,
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": ant_input_tokens, "output_tokens": 0},
        },
    }
    yield f"event: message_start\ndata: {json.dumps(msg_start)}\n\n"
    yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': block_index, 'content_block': {'type': 'text', 'text': ''}})}\n\n"

    async for evt_type, data in stream_jb_events(jb_body, jb_headers, rid, settings, http_client, entry=entry):
        if evt_type == "content":
            total_content += data
            yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': block_index, 'delta': {'type': 'text_delta', 'text': data}})}\n\n"

        elif evt_type == "function_call":
            got_function_call = True
            yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': block_index})}\n\n"
            block_index += 1

            tool_use_id = f"toolu_{uuid.uuid4().hex[:24]}"
            fn_name = data["name"]
            fn_args = data["arguments"]

            yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': block_index, 'content_block': {'type': 'tool_use', 'id': tool_use_id, 'name': fn_name, 'input': {}}})}\n\n"
            yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': block_index, 'delta': {'type': 'input_json_delta', 'partial_json': fn_args}})}\n\n"
            yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': block_index})}\n\n"

        elif evt_type == "finish":
            finish_reason = "end_turn" if data == "stop" else "max_tokens"

        elif evt_type == "status":
            upstream_status = int(data)

        elif evt_type == "quota":
            try:
                quota_spent_val = float(data.get("spent", 0))
            except (TypeError, ValueError):
                pass

        elif evt_type == "error":
            error_code = f"upstream_{upstream_status}"
            yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': data}})}\n\n"

    if not got_function_call:
        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"

    if got_function_call:
        finish_reason = "tool_use"

    ant_output_tokens = estimate_tokens(total_content)
    yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': finish_reason, 'stop_sequence': None}, 'usage': {'output_tokens': ant_output_tokens}})}\n\n"
    yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"

    log.info("[%s] anthropic stream done, in=%d out=%d quota=%.1f",
             msg_id, ant_input_tokens, ant_output_tokens, quota_spent_val or 0)
    _record_stats(stats, entry, model, "messages", upstream_status, started_at,
                  ant_input_tokens, ant_output_tokens, error_code, quota_spent=quota_spent_val)
