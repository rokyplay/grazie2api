"""OpenAI Responses API format: streaming and non-streaming responses."""

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

log = logging.getLogger("grazie2api.responses_fmt")


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


async def responses_non_stream(
    jb_body: dict, jb_headers: dict, rid: str, resp_id: str, model: str,
    settings: Settings,
    http_client: httpx.AsyncClient,
    stats: "StatsRecorder | None" = None,
    entry: "CredentialEntry | None" = None,
    started_at: float = 0.0,
) -> JSONResponse:
    prompt_tokens_est = estimate_messages_tokens(jb_body.get("chat", {}).get("messages", []))
    try:
        full_content, finish_reason, function_call, upstream_status, quota_spent_val = await collect_jb_response(
            jb_body, jb_headers, rid, settings, http_client, entry=entry,
        )
    except Exception as e:
        from fastapi import HTTPException
        if isinstance(e, HTTPException):
            _record_stats(stats, entry, model, "responses", e.status_code, started_at,
                          prompt_tokens_est, 0, f"http_{e.status_code}")
            return JSONResponse(status_code=e.status_code, content={"error": {"message": str(e.detail), "type": "server_error"}})
        if isinstance(e, httpx.ReadTimeout):
            _record_stats(stats, entry, model, "responses", 504, started_at, prompt_tokens_est, 0, "timeout")
            return JSONResponse(status_code=504, content={"error": {"message": "Upstream timeout", "type": "server_error"}})
        log.error("[%s] Error: %s", resp_id, e, exc_info=True)
        _record_stats(stats, entry, model, "responses", 500, started_at, prompt_tokens_est, 0, "internal")
        return JSONResponse(status_code=500, content={"error": {"message": str(e), "type": "server_error"}})

    output_items: list[dict] = []
    if full_content:
        output_items.append({
            "type": "message", "id": f"msg_{rid}", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": full_content, "annotations": []}],
        })
    if function_call:
        call_id = f"call_{uuid.uuid4().hex[:24]}"
        output_items.append({
            "type": "function_call", "id": f"fc_{rid}", "call_id": call_id,
            "name": function_call["name"], "arguments": function_call["arguments"], "status": "completed",
        })
        if not full_content:
            output_items.insert(0, {"type": "message", "id": f"msg_{rid}", "role": "assistant", "status": "completed", "content": []})
    if not output_items:
        output_items.append({"type": "message", "id": f"msg_{rid}", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "", "annotations": []}]})

    status = "completed" if finish_reason in ("stop", "tool_calls") else "incomplete"
    input_tokens = estimate_messages_tokens(jb_body.get("chat", {}).get("messages", []))
    output_tokens = estimate_tokens(full_content)
    if function_call:
        output_tokens += estimate_tokens(function_call.get("name", "")) + estimate_tokens(function_call.get("arguments", ""))

    log.info("[%s] responses non-stream done, in=%d out=%d quota=%.1f",
             resp_id, input_tokens, output_tokens, quota_spent_val or 0)
    _record_stats(stats, entry, model, "responses", upstream_status, started_at,
                  input_tokens, output_tokens, None, quota_spent=quota_spent_val)

    return JSONResponse(content={
        "id": resp_id, "object": "response", "created_at": int(time.time()),
        "model": model, "status": status, "output": output_items,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens},
    })


async def responses_stream(
    jb_body: dict, jb_headers: dict, rid: str, resp_id: str, model: str,
    settings: Settings,
    http_client: httpx.AsyncClient,
    stats: "StatsRecorder | None" = None,
    entry: "CredentialEntry | None" = None,
    started_at: float = 0.0,
):
    """Yield OpenAI Responses-format SSE events."""
    total_content = ""
    output_index = 0
    content_index = 0
    finish_status = "completed"
    got_function_call = False
    output_items: list[dict] = []
    upstream_status = 200
    error_code: str | None = None
    quota_spent_val: float | None = None

    yield f"data: {json.dumps({'type': 'response.created', 'response': {'id': resp_id, 'object': 'response', 'created_at': int(time.time()), 'model': model, 'status': 'in_progress', 'output': []}})}\n\n"
    yield f"data: {json.dumps({'type': 'response.in_progress', 'response': {'id': resp_id, 'status': 'in_progress'}})}\n\n"

    msg_item = {"type": "message", "id": f"msg_{rid}", "role": "assistant", "status": "in_progress", "content": []}
    yield f"data: {json.dumps({'type': 'response.output_item.added', 'output_index': output_index, 'item': msg_item})}\n\n"
    yield f"data: {json.dumps({'type': 'response.content_part.added', 'output_index': output_index, 'content_index': content_index, 'part': {'type': 'output_text', 'text': '', 'annotations': []}})}\n\n"

    async for evt_type, data in stream_jb_events(jb_body, jb_headers, rid, settings, http_client, entry=entry):
        if evt_type == "content":
            total_content += data
            yield f"data: {json.dumps({'type': 'response.output_text.delta', 'output_index': output_index, 'content_index': content_index, 'delta': data})}\n\n"

        elif evt_type == "function_call":
            got_function_call = True
            yield f"data: {json.dumps({'type': 'response.output_text.done', 'output_index': output_index, 'content_index': content_index, 'text': total_content})}\n\n"
            yield f"data: {json.dumps({'type': 'response.content_part.done', 'output_index': output_index, 'content_index': content_index, 'part': {'type': 'output_text', 'text': total_content, 'annotations': []}})}\n\n"

            msg_done_item = {"type": "message", "id": f"msg_{rid}", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": total_content, "annotations": []}] if total_content else []}
            yield f"data: {json.dumps({'type': 'response.output_item.done', 'output_index': output_index, 'item': msg_done_item})}\n\n"
            output_items.append(msg_done_item)

            output_index += 1
            call_id = f"call_{uuid.uuid4().hex[:24]}"
            fc_item = {"type": "function_call", "id": f"fc_{rid}", "call_id": call_id, "name": data["name"], "arguments": data["arguments"], "status": "completed"}
            yield f"data: {json.dumps({'type': 'response.output_item.added', 'output_index': output_index, 'item': fc_item})}\n\n"
            yield f"data: {json.dumps({'type': 'response.output_item.done', 'output_index': output_index, 'item': fc_item})}\n\n"
            output_items.append(fc_item)

        elif evt_type == "finish":
            finish_status = "completed" if data == "stop" else "incomplete"

        elif evt_type == "status":
            upstream_status = int(data)

        elif evt_type == "quota":
            try:
                quota_spent_val = float(data.get("spent", 0))
            except (TypeError, ValueError):
                pass

        elif evt_type == "error":
            error_code = f"upstream_{upstream_status}"
            yield f"data: {json.dumps({'type': 'response.output_text.delta', 'output_index': output_index, 'content_index': content_index, 'delta': data})}\n\n"

    if not got_function_call:
        yield f"data: {json.dumps({'type': 'response.output_text.done', 'output_index': output_index, 'content_index': content_index, 'text': total_content})}\n\n"
        yield f"data: {json.dumps({'type': 'response.content_part.done', 'output_index': output_index, 'content_index': content_index, 'part': {'type': 'output_text', 'text': total_content, 'annotations': []}})}\n\n"
        msg_done_item = {"type": "message", "id": f"msg_{rid}", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": total_content, "annotations": []}]}
        yield f"data: {json.dumps({'type': 'response.output_item.done', 'output_index': output_index, 'item': msg_done_item})}\n\n"
        output_items.append(msg_done_item)

    if got_function_call:
        finish_status = "completed"

    input_tokens = estimate_messages_tokens(jb_body.get("chat", {}).get("messages", []))
    output_tokens = estimate_tokens(total_content)

    yield f"data: {json.dumps({'type': 'response.completed', 'response': {'id': resp_id, 'object': 'response', 'model': model, 'status': finish_status, 'output': output_items, 'usage': {'input_tokens': input_tokens, 'output_tokens': output_tokens, 'total_tokens': input_tokens + output_tokens}}})}\n\n"

    log.info("[%s] responses stream done, in=%d out=%d quota=%.1f",
             resp_id, input_tokens, output_tokens, quota_spent_val or 0)
    _record_stats(stats, entry, model, "responses", upstream_status, started_at,
                  input_tokens, output_tokens, error_code, quota_spent=quota_spent_val)
