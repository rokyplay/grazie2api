"""Shared conversion utilities for Grazie API message format."""

from __future__ import annotations

import logging

log = logging.getLogger("grazie2api.converters")

ROLE_TO_TYPE = {
    "system": "system_message",
    "user": "user_message",
    "assistant": "assistant_message",
}


def map_finish_reason(reason: str) -> str:
    """Map Grazie FinishMetadata reason to OpenAI finish_reason."""
    if reason == "stop":
        return "stop"
    if reason == "function_call":
        return "tool_calls"
    if reason in ("length", "max_tokens"):
        return "length"
    if reason in ("content_filter", "refusal"):
        return "content_filter"
    return "stop"


def extract_text_content(content) -> str:
    """Extract text from content that may be a string, list of parts, or None.

    Non-text parts (image_url, input_image, input_file, etc.) are silently dropped
    with a warning log since Grazie does not support multimodal input.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text_parts.append(part["text"])
            elif isinstance(part, str):
                text_parts.append(part)
            elif isinstance(part, dict):
                ptype = part.get("type", "unknown")
                log.warning("Dropping non-text content part type=%s (Grazie does not support multimodal)", ptype)
        return "\n".join(text_parts)
    return str(content)


def _get_encoding():
    """Lazy-load tiktoken encoding (cached after first call)."""
    global _encoding
    if _encoding is None:
        import tiktoken
        _encoding = tiktoken.get_encoding("cl100k_base")
    return _encoding

_encoding = None


def count_tokens(text: str) -> int:
    """Count tokens using tiktoken cl100k_base encoding."""
    if not text:
        return 0
    try:
        return len(_get_encoding().encode(text))
    except Exception:
        # Fallback to chars/4 if tiktoken fails
        return max(1, len(text) // 4)


def estimate_tokens(text: str) -> int:
    """Count tokens accurately using tiktoken."""
    return count_tokens(text)


def estimate_messages_tokens(messages: list[dict]) -> int:
    """Estimate prompt token count from original messages."""
    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total += estimate_tokens(part.get("text", ""))
                elif isinstance(part, str):
                    total += estimate_tokens(part)
        total += 4  # overhead per message
    return total


def build_tool_call_id_map(messages: list[dict]) -> dict[str, str]:
    """Scan messages for assistant tool_calls and build tool_call_id -> function_name map."""
    id_map: dict[str, str] = {}
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                tc_id = tc.get("id", "")
                fn_name = tc.get("function", {}).get("name", "")
                if tc_id and fn_name:
                    id_map[tc_id] = fn_name
    return id_map


def strip_trailing_assistant(messages: list[dict]) -> list[dict]:
    """Fix trailing non-user messages for Grazie compatibility.

    SillyTavern sends trailing assistant messages as prefill, or messages
    with role="content" instead of "user". Grazie requires messages to end
    with a user_message.

    Strategy (matches Replit deploy-prompt-v2 approach):
    1. If last message role is "content" -> change to user_message
    2. If last message is assistant with empty content -> remove it
    3. If last message is assistant with non-empty content -> convert to
       user_message with "[Continue from here:] {content}" wrapper so the
       intent is preserved and Grazie accepts it
    """
    if not messages:
        return messages

    last = messages[-1]
    msg_type = last.get("type", "")

    # SillyTavern sometimes sends role="content" instead of "user"
    if msg_type == "content" or last.get("role") == "content":
        last["type"] = "user_message"
        last.pop("role", None)
        return messages

    if msg_type == "assistant_message":
        content = last.get("content", "")
        if not content or not content.strip():
            messages.pop()
        else:
            # Convert prefill assistant to user message so Grazie accepts it
            last["type"] = "user_message"
            last["content"] = f"[Continue from here:] {content.strip()}"

    # function_message at the end: only convert if it's orphaned (no preceding functionCall)
    # If it's a valid tool result following a functionCall, keep it — Grazie needs it
    if messages and messages[-1].get("type") == "function_message":
        has_call = False
        for j in range(len(messages) - 2, -1, -1):
            if messages[j].get("type") == "assistant_message" and messages[j].get("functionCall"):
                has_call = True
                break
            if messages[j].get("type") != "function_message":
                break
        if not has_call:
            fn_last = messages[-1]
            fn_last["type"] = "user_message"
            fn_content = fn_last.get("content", "")
            fn_last["content"] = fn_content or "[Continue]"
            fn_last.pop("functionName", None)

    return messages


def sanitize_jb_messages(raw: list[dict]) -> list[dict]:
    """Clean JB message array: filter empties + fix orphaned function_messages + fix trailing.

    All conversion paths (OpenAI / Anthropic / Responses) MUST call this.
    Backported from Worker grazie.ts sanitizeJbMessages().
    """
    # Filter empty system/user messages
    filtered = [
        m for m in raw
        if (m.get("type") not in ("system_message", "user_message"))
        or (m.get("content", "").strip())
    ]

    # Pass 1: orphaned function_message -> user_message
    pass1: list[dict] = []
    for i, msg in enumerate(filtered):
        if msg.get("type") == "function_message":
            has_preceding_call = False
            for j in range(len(pass1) - 1, -1, -1):
                if pass1[j].get("type") == "assistant_message" and pass1[j].get("functionCall"):
                    has_preceding_call = True
                    break
                if pass1[j].get("type") != "function_message":
                    break
            if not has_preceding_call:
                fn_name = msg.get("functionName", "unknown")
                pass1.append({
                    "type": "user_message",
                    "content": f"[Tool result: {fn_name}] {msg.get('content', '')}",
                })
                continue
        pass1.append(msg)

    # Pass 2: Grazie requires strict alternation: assistant(fc) → fn → assistant(fc) → fn
    # OpenAI format: assistant(tc1,tc2) → tool1 → tool2
    # After openai_msgs_to_jb: assistant(fc1) → assistant(fc2) → fn1 → fn2
    # Must interleave to: assistant(fc1) → fn1 → assistant(fc2) → fn2
    out: list[dict] = []
    i = 0
    while i < len(pass1):
        msg = pass1[i]
        if msg.get("type") == "assistant_message" and msg.get("functionCall"):
            calls: list[dict] = []
            j = i
            while j < len(pass1) and pass1[j].get("type") == "assistant_message" and pass1[j].get("functionCall"):
                calls.append(pass1[j])
                j += 1
            fns: list[dict] = []
            k = j
            while k < len(pass1) and pass1[k].get("type") == "function_message":
                fns.append(pass1[k])
                k += 1
            if len(fns) >= len(calls):
                for idx in range(len(calls)):
                    out.append(calls[idx])
                    out.append(fns[idx])
                for idx in range(len(calls), len(fns)):
                    out.append(fns[idx])
            else:
                for c in calls:
                    out.append({"type": "assistant_message", "content": c.get("content") or "[tool call removed]"})
                for fn in fns:
                    out.append(fn)
            i = k
        else:
            out.append(msg)
            i += 1

    strip_trailing_assistant(out)
    return out
