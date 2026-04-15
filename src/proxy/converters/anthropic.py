"""Anthropic Messages -> Grazie format conversion."""

from __future__ import annotations

import json

from src.proxy.converters.common import (
    ROLE_TO_TYPE,
    extract_text_content,
    sanitize_jb_messages,
)


def anthropic_tools_to_openai(anthropic_tools: list[dict]) -> list[dict]:
    """Convert Anthropic tool definitions to OpenAI tools format."""
    openai_tools: list[dict] = []
    for t in anthropic_tools:
        openai_tools.append({
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {}),
            },
        })
    return openai_tools


def anthropic_msgs_to_jb(body: dict) -> tuple[list[dict], list[dict] | None]:
    """Convert Anthropic Messages format to JB Grazie messages.

    Returns (jb_messages, openai_tools_or_None).
    """
    out: list[dict] = []

    # System prompt (top-level)
    system = body.get("system")
    if system:
        if isinstance(system, str):
            out.append({"type": "system_message", "content": system})
        elif isinstance(system, list):
            text_parts = []
            for block in system:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block["text"])
                elif isinstance(block, str):
                    text_parts.append(block)
            out.append({"type": "system_message", "content": "\n".join(text_parts)})

    # Build tool_use id -> name map
    tool_use_id_map: dict[str, str] = {}
    for m in body.get("messages", []):
        content = m.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_use_id_map[block.get("id", "")] = block.get("name", "")

    for m in body.get("messages", []):
        role = m.get("role", "user")
        content = m.get("content", "")

        if isinstance(content, str):
            msg_type = ROLE_TO_TYPE.get(role, "user_message")
            out.append({"type": msg_type, "content": content})
            continue

        if isinstance(content, list):
            text_parts: list[str] = []
            tool_use_blocks: list[dict] = []
            tool_result_blocks: list[dict] = []

            for block in content:
                if isinstance(block, str):
                    text_parts.append(block)
                elif isinstance(block, dict):
                    btype = block.get("type", "")
                    if btype == "text":
                        text_parts.append(block.get("text", ""))
                    elif btype == "tool_use":
                        tool_use_blocks.append(block)
                    elif btype == "tool_result":
                        tool_result_blocks.append(block)

            if role == "assistant" and tool_use_blocks:
                text = "\n".join(text_parts) if text_parts else ""
                for i, tub in enumerate(tool_use_blocks):
                    arguments = tub.get("input", {})
                    if isinstance(arguments, dict):
                        arguments = json.dumps(arguments)
                    out.append({
                        "type": "assistant_message",
                        "content": text if i == 0 else "",
                        "functionCall": {
                            "functionName": tub.get("name", ""),
                            "content": arguments,
                        },
                    })
                continue

            if role == "user" and tool_result_blocks:
                if text_parts:
                    out.append({"type": "user_message", "content": "\n".join(text_parts)})
                for tr in tool_result_blocks:
                    tr_content = tr.get("content", "")
                    if isinstance(tr_content, list):
                        tr_content = "\n".join(
                            b.get("text", "") for b in tr_content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    fn_name = tool_use_id_map.get(tr.get("tool_use_id", ""), "unknown")
                    out.append({
                        "type": "function_message",
                        "content": tr_content if isinstance(tr_content, str) else str(tr_content),
                        "functionName": fn_name,
                    })
                continue

            text = "\n".join(text_parts) if text_parts else ""
            msg_type = ROLE_TO_TYPE.get(role, "user_message")
            out.append({"type": msg_type, "content": text})
            continue

        msg_type = ROLE_TO_TYPE.get(role, "user_message")
        out.append({"type": msg_type, "content": extract_text_content(content)})

    anthropic_tools = body.get("tools")
    openai_tools: list[dict] | None = None
    if anthropic_tools:
        openai_tools = anthropic_tools_to_openai(anthropic_tools)

    return sanitize_jb_messages(out), openai_tools
