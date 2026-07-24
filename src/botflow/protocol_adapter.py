"""Front-end protocol adapter.

Converts between external API formats (OpenAI, Anthropic) and the
unified internal format used by the routing engine.
"""

from __future__ import annotations

from typing import Any


def normalize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize message list to a consistent internal format.

    Ensures:
    - All content fields are either str or list-of-parts (no mixed types)
    - Each content part dict has required keys (type, text/url)
    - Duplicate system messages are collapsed
    - Empty/None content defaults to ""
    """
    result: list[dict[str, Any]] = []
    system_seen = False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        content = msg.get("content", "")

        # Default None/missing to empty string
        if content is None:
            content = ""

        # Normalize list content: ensure each part has a "type" key
        if isinstance(content, list):
            normalized_parts: list[dict[str, Any]] = []
            for part in content:
                if isinstance(part, str):
                    # Bare string in a list -> treat as text block
                    normalized_parts.append({"type": "text", "text": part})
                elif isinstance(part, dict):
                    # Ensure type field exists
                    if "type" not in part:
                        if "text" in part:
                            normalized_parts.append({"type": "text", **part})
                        elif "url" in part or "image_url" in part:
                            normalized_parts.append({"type": "image_url", **part})
                        else:
                            normalized_parts.append({"type": "text", "text": str(part)})
                    else:
                        normalized_parts.append(part)
            content = normalized_parts

        # Collapse duplicate system messages
        if role == "system":
            if system_seen:
                continue
            system_seen = True

        result.append({"role": role, "content": content})
    return result


def openai_to_internal(body: dict[str, Any]) -> dict[str, Any]:
    """Convert an OpenAI /v1/chat/completions request to internal parameters.

    Returns:
        Dict with keys: messages, model, temperature, max_tokens, stream, extra
    """
    return {
        "messages": normalize_messages(body.get("messages", [])),
        "model": body.get("model", ""),
        "temperature": body.get("temperature"),
        "max_tokens": body.get("max_tokens"),
        "stream": body.get("stream", False),
        "extra": {k: v for k, v in body.items() if k not in ("messages", "model", "temperature", "max_tokens", "stream")},
    }


def anthropic_to_internal(body: dict[str, Any]) -> dict[str, Any]:
    """Convert an Anthropic /v1/messages request to internal parameters.

    Handles system prompt extraction from top-level field.
    """
    messages = body.get("messages", [])

    # Anthropic has a separate "system" field (string or list of content blocks)
    system = body.get("system", "")
    if isinstance(system, list):
        # Flatten list-type system to string
        text_parts = [b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text"]
        system = "\n".join(text_parts)
    if system:
        messages = [{"role": "system", "content": system}] + messages

    return {
        "messages": normalize_messages(messages),
        "model": body.get("model", ""),
        "temperature": body.get("temperature"),
        "max_tokens": body.get("max_tokens", 4096),
        "stream": body.get("stream", False),
        "extra": {k: v for k, v in body.items() if k not in ("messages", "model", "temperature", "max_tokens", "stream", "system")},
    }


def internal_to_openai(internal: dict[str, Any]) -> dict[str, Any]:
    """Convert a unified internal response to OpenAI format."""
    choices = []
    for c in internal.get("choices", []):
        msg = c.get("message", {})
        message: dict[str, Any] = {
            "role": msg.get("role", "assistant"),
            "content": msg.get("content", ""),
        }
        if msg.get("tool_calls"):
            message["tool_calls"] = msg["tool_calls"]
        if msg.get("function_call"):
            message["function_call"] = msg["function_call"]
        choices.append({
            "index": c.get("index", 0),
            "message": message,
            "finish_reason": c.get("finish_reason", "stop"),
        })

    result: dict[str, Any] = {
        "id": internal.get("id", ""),
        "object": "chat.completion",
        "created": _now_timestamp(),
        "model": internal.get("model", ""),
        "choices": choices,
        "usage": internal.get("usage", {}),
    }
    if internal.get("system_fingerprint"):
        result["system_fingerprint"] = internal["system_fingerprint"]
    return result


def internal_to_anthropic(internal: dict[str, Any]) -> dict[str, Any]:
    """Convert a unified internal response to Anthropic format."""
    raw_choices = internal.get("choices") or []
    choice = raw_choices[0] if raw_choices and raw_choices[0] is not None else {}
    msg = choice.get("message") or {}
    content_text = msg.get("content") or ""
    tool_calls = msg.get("tool_calls") or []

    usage = internal.get("usage", {})

    content: list[dict[str, Any]] = []
    if content_text:
        content.append({"type": "text", "text": content_text})
    for tc in tool_calls:
        tc_func = tc.get("function", {})
        content.append({
            "type": "tool_use",
            "id": tc.get("id", "toolu_" + _now_hex()),
            "name": tc_func.get("name", ""),
            "input": _safe_json_loads(tc_func.get("arguments", "{}")),
        })

    stop_reason = choice.get("finish_reason") or "end_turn"
    if tool_calls and stop_reason == "stop":
        stop_reason = "tool_use"

    return {
        "id": internal.get("id", ""),
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": internal.get("model", ""),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def internal_chunk_to_openai_sse(chunk: dict[str, Any]) -> dict[str, Any]:
    """Convert an internal stream chunk to OpenAI SSE format."""
    choices = chunk.get("choices", [])
    choice = choices[0] if choices else {}
    delta = choice.get("delta", {})

    sse_delta: dict[str, Any] = {
        "role": delta.get("role", ""),
        "content": delta.get("content", ""),
    }

    # Forward tool_calls if present
    if delta.get("tool_calls"):
        sse_delta["tool_calls"] = delta["tool_calls"]

    # Forward reasoning_content (thinking process) if present
    if delta.get("reasoning_content") is not None:
        sse_delta["reasoning_content"] = delta["reasoning_content"]

    sse_chunk = {
        "id": chunk.get("id", ""),
        "object": "chat.completion.chunk",
        "created": _now_timestamp(),
        "model": chunk.get("model", ""),
        "choices": [
            {
                "index": choice.get("index", 0),
                "delta": sse_delta,
                "finish_reason": choice.get("finish_reason"),
            }
        ],
    }

    # Include usage on the final chunk
    usage = chunk.get("usage")
    if usage:
        sse_chunk["usage"] = usage

    return sse_chunk


def internal_chunk_to_anthropic_sse(chunk: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert an internal stream chunk to Anthropic SSE format events.

    Anthropic uses a multi-event SSE format (message_start, content_block_delta, message_delta).
    This produces a list of Anthropic-formatted events from a single internal chunk.

    Returns:
        List of Anthropic SSE event dicts.
    """
    events: list[dict[str, Any]] = []
    choices = chunk.get("choices") or []
    if not choices or choices[0] is None:
        return events
    choice = choices[0]
    if not isinstance(choice, dict):
        return events
    delta = choice.get("delta") or {}
    content = delta.get("content") or ""

    # Message start event — only emit once (first chunk with role, content not None)
    content_is_set = delta.get("content") is not None
    if delta.get("role") == "assistant" and content_is_set:
        events.append({
            "type": "message_start",
            "message": {
                "id": chunk.get("id", "msg_" + _now_hex()),
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": chunk.get("model", ""),
            },
        })

    # Content block delta (text)
    if content:
        events.append({
            "type": "content_block_delta",
            "index": choice.get("index", 0),
            "delta": {
                "type": "text_delta",
                "text": content,
            },
        })

    # Tool use delta
    tool_calls = delta.get("tool_calls")
    if tool_calls:
        for tc in tool_calls:
            tc_index = tc.get("index", 0)
            tc_func = tc.get("function", {})
            # Tool use start
            if tc_func.get("name"):
                events.append({
                    "type": "content_block_start",
                    "index": tc_index,
                    "content_block": {
                        "type": "tool_use",
                        "id": tc.get("id", "toolu_" + _now_hex()),
                        "name": tc_func["name"],
                    },
                })
            # Tool use arguments delta
            if tc_func.get("arguments"):
                events.append({
                    "type": "content_block_delta",
                    "index": tc_index,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": tc_func["arguments"],
                    },
                })

    # Finish event
    finish_reason = choice.get("finish_reason")
    if finish_reason:
        usage = chunk.get("usage", {})
        events.append({
            "type": "message_delta",
            "delta": {
                "stop_reason": _anthropic_stop_reason(finish_reason),
                "stop_sequence": None,
            },
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
        })

    return events


def models_to_openai(models: list[dict], created: int | None = None) -> dict:
    """Convert model list to OpenAI /v1/models response format."""
    return {
        "object": "list",
        "data": [
            {
                "id": m.get("id") or m.get("name", ""),
                "object": "model",
                "created": created or _now_timestamp(),
                "owned_by": m.get("provider_type", "botflow"),
            }
            for m in models
        ],
    }


def models_to_anthropic(models: list[dict]) -> dict:
    """Convert model list to Anthropic /v1/models response format."""
    return {
        "data": [
            {
                "type": "model",
                "id": m.get("id") or m.get("name", ""),
                "display_name": m.get("display_name") or m.get("name", ""),
                "created_at": m.get("created_at", ""),
            }
            for m in models
        ],
    }


def _now_timestamp() -> int:
    import time
    return int(time.time())


def _now_hex() -> str:
    import secrets
    return secrets.token_hex(8)


def _safe_json_loads(s: str) -> Any:
    import json
    try:
        return json.loads(s) if s else {}
    except Exception:
        return {}


def _anthropic_stop_reason(finish_reason: str) -> str:
    mapping = {
        "stop": "end_turn",
        "length": "max_tokens",
        "content_filter": "content_filter",
        "tool_calls": "tool_use",
    }
    return mapping.get(finish_reason, "end_turn")