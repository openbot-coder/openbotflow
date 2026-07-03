"""Front-end protocol adapter.

Converts between external API formats (OpenAI, Anthropic) and the
unified internal format used by the routing engine.
"""

from __future__ import annotations

from typing import Any


def openai_to_internal(body: dict[str, Any]) -> dict[str, Any]:
    """Convert an OpenAI /v1/chat/completions request to internal parameters.

    Returns:
        Dict with keys: messages, model, temperature, max_tokens, stream, extra
    """
    return {
        "messages": body.get("messages", []),
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

    # Anthropic has a separate "system" field
    system = body.get("system", "")
    if system:
        messages = [{"role": "system", "content": system}] + messages

    return {
        "messages": messages,
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
        choices.append({
            "index": c.get("index", 0),
            "message": {
                "role": msg.get("role", "assistant"),
                "content": msg.get("content", ""),
            },
            "finish_reason": c.get("finish_reason", "stop"),
        })

    return {
        "id": internal.get("id", ""),
        "object": "chat.completion",
        "created": _now_timestamp(),
        "model": internal.get("model", ""),
        "choices": choices,
        "usage": internal.get("usage", {}),
    }


def internal_to_anthropic(internal: dict[str, Any]) -> dict[str, Any]:
    """Convert a unified internal response to Anthropic format."""
    raw_choices = internal.get("choices") or []
    choice = raw_choices[0] if raw_choices and raw_choices[0] is not None else {}
    msg = choice.get("message") or {}
    content_text = msg.get("content") or ""

    usage = internal.get("usage", {})

    return {
        "id": internal.get("id", ""),
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": content_text}],
        "model": internal.get("model", ""),
        "stop_reason": choice.get("finish_reason") or "end_turn",
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

    sse_chunk = {
        "id": chunk.get("id", ""),
        "object": "chat.completion.chunk",
        "created": _now_timestamp(),
        "model": chunk.get("model", ""),
        "choices": [
            {
                "index": choice.get("index", 0),
                "delta": {
                    "role": delta.get("role", ""),
                    "content": delta.get("content", ""),
                },
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
    choice = choices[0] if choices and choices[0] is not None else {}
    delta = choice.get("delta") or {}
    content = delta.get("content") or ""

    # Message start event
    if delta.get("role") == "assistant":
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

    # Content block delta
    if content:
        events.append({
            "type": "content_block_delta",
            "index": choice.get("index", 0),
            "delta": {
                "type": "text_delta",
                "text": content,
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


def _anthropic_stop_reason(finish_reason: str) -> str:
    mapping = {
        "stop": "end_turn",
        "length": "max_tokens",
        "content_filter": "content_filter",
    }
    return mapping.get(finish_reason, "end_turn")
