"""OpenAI chat messages -> Grazie format conversion."""

from __future__ import annotations

import json
from typing import Any

from src.proxy.converters.common import (
    ROLE_TO_TYPE,
    extract_text_content,
    build_tool_call_id_map,
    sanitize_jb_messages,
)


def openai_msgs_to_jb(messages: list[dict]) -> list[dict]:
    """Convert OpenAI chat messages to Grazie format."""
    tc_id_map = build_tool_call_id_map(messages)
    out: list[dict] = []
    for m in messages:
        role = m.get("role", "user")
        content = extract_text_content(m.get("content"))

        # Assistant with tool_calls (emit one JB message per tool_call)
        if role == "assistant" and m.get("tool_calls"):
            for i, tc in enumerate(m["tool_calls"]):
                jb_msg: dict[str, Any] = {
                    "type": "assistant_message",
                    "content": content if i == 0 else "",
                    "functionCall": {
                        "functionName": tc["function"]["name"],
                        "content": tc["function"]["arguments"],
                    },
                }
                out.append(jb_msg)
            continue

        # Tool result (role: "tool" or legacy role: "function")
        if role in ("tool", "function"):
            tool_call_id = m.get("tool_call_id", "")
            fn_name = tc_id_map.get(tool_call_id, m.get("name", "unknown"))
            out.append({
                "type": "function_message",
                "content": content,
                "functionName": fn_name,
            })
            continue

        msg_type = ROLE_TO_TYPE.get(role, "user_message")
        out.append({"type": msg_type, "content": content})

    return sanitize_jb_messages(out)
