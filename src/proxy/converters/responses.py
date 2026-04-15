"""OpenAI Responses API -> Grazie format conversion."""

from __future__ import annotations

import json

from src.proxy.converters.common import (
    ROLE_TO_TYPE,
    extract_text_content,
    sanitize_jb_messages,
)


def responses_tools_to_openai(resp_tools: list[dict]) -> list[dict] | None:
    """Convert Responses API tool definitions to OpenAI tools format."""
    openai_tools: list[dict] = []
    for t in resp_tools:
        if t.get("type") == "function":
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", {}),
                },
            })
    return openai_tools if openai_tools else None


def responses_input_to_jb(input_data) -> list[dict]:
    """Convert OpenAI Responses API input to JB messages."""
    if isinstance(input_data, str):
        return [{"type": "user_message", "content": input_data}]

    if isinstance(input_data, list):
        out: list[dict] = []
        fc_id_map: dict[str, str] = {}
        for item in input_data:
            if isinstance(item, dict) and item.get("type") == "function_call":
                fc_id_map[item.get("call_id", item.get("id", ""))] = item.get("name", "")

        for item in input_data:
            if isinstance(item, str):
                out.append({"type": "user_message", "content": item})
            elif isinstance(item, dict):
                item_type = item.get("type", "")

                if item_type == "message" or (not item_type and "role" in item):
                    role = item.get("role", "user")
                    content = item.get("content", "")
                    if isinstance(content, list):
                        text_parts = []
                        for part in content:
                            if isinstance(part, dict):
                                ptype = part.get("type", "")
                                if ptype in ("input_text", "text", "output_text"):
                                    text_parts.append(part.get("text", ""))
                            elif isinstance(part, str):
                                text_parts.append(part)
                        content = "\n".join(text_parts)
                    msg_type = ROLE_TO_TYPE.get(role, "user_message")
                    out.append({"type": msg_type, "content": content})

                elif item_type == "function_call":
                    arguments = item.get("arguments", "")
                    if isinstance(arguments, dict):
                        arguments = json.dumps(arguments)
                    out.append({
                        "type": "assistant_message",
                        "content": "",
                        "functionCall": {
                            "functionName": item.get("name", ""),
                            "content": arguments,
                        },
                    })

                elif item_type == "function_call_output":
                    call_id = item.get("call_id", "")
                    fn_name = fc_id_map.get(call_id, "unknown")
                    output = item.get("output", "")
                    if not isinstance(output, str):
                        output = json.dumps(output)
                    out.append({
                        "type": "function_message",
                        "content": output,
                        "functionName": fn_name,
                    })

                else:
                    role = item.get("role", "user")
                    content = extract_text_content(item.get("content", item.get("text", "")))
                    msg_type = ROLE_TO_TYPE.get(role, "user_message")
                    out.append({"type": msg_type, "content": content})
        return sanitize_jb_messages(out)

    return [{"type": "user_message", "content": str(input_data)}]
