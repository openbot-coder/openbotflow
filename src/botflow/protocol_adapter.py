"""Front-end protocol adapter.

Converts between external API formats (OpenAI, Anthropic) and the
unified internal format used by the routing engine.
"""

from __future__ import annotations

from typing import Any

from botflow.common.content_converters import anthropic_to_openai_content


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
    Converts Anthropic content blocks to OpenAI format for provider compatibility.
    """
    messages = body.get("messages", [])

    # Anthropic has a separate "system" field
    system = body.get("system", "")
    if system:
        messages = [{"role": "system", "content": system}] + messages

    # Convert Anthropic content blocks to OpenAI format
    converted_messages = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            content = anthropic_to_openai_content(content)
        converted_messages.append({**msg, "content": content})

    return {
        "messages": converted_messages,
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
        if msg.get("reasoning_content") is not None:
            message["reasoning_content"] = msg["reasoning_content"]
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
    reasoning = msg.get("reasoning_content")

    usage = internal.get("usage", {})

    content: list[dict[str, Any]] = []
    if reasoning:
        content.append({"type": "thinking", "thinking": reasoning})
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
    reasoning = delta.get("reasoning_content")
    if reasoning:
        events.append({
            "type": "content_block_delta",
            "index": choice.get("index", 0),
            "delta": {
                "type": "thinking_delta",
                "thinking": reasoning,
            },
        })
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


# ---------------------------------------------------------------------------
# OpenAI Responses API  (POST /v1/responses)
# ---------------------------------------------------------------------------

def responses_to_internal(body: dict[str, Any]) -> dict[str, Any]:
    """Convert an OpenAI Responses API request to internal parameters.

    ``input`` is either a plain string or a list of content items.
    ``instructions`` becomes the system prompt.
    """
    # --- build messages from input + instructions ---
    instructions = body.get("instructions", "")
    raw_input = body.get("input", "")

    if isinstance(raw_input, str):
        # Simple string → single user message
        messages: list[dict[str, Any]] = []
        if instructions:
            messages.append({"role": "system", "content": instructions})
        if raw_input:
            messages.append({"role": "user", "content": raw_input})
    elif isinstance(raw_input, list):
        # Array of content items – convert to messages
        messages = _responses_input_to_messages(raw_input, instructions)
    else:
        messages = []

    return {
        "messages": messages,
        "model": body.get("model", ""),
        "temperature": body.get("temperature"),
        "max_tokens": body.get("max_output_tokens") or body.get("max_tokens"),
        "stream": body.get("stream", False),
        "extra": {
            k: v for k, v in body.items()
            if k not in (
                "input", "instructions", "model", "temperature",
                "max_output_tokens", "max_tokens", "stream",
            )
        },
    }


def _responses_input_to_messages(
    items: list[dict[str, Any]], instructions: str,
) -> list[dict[str, Any]]:
    """Convert Responses API ``input`` array to OpenAI-style messages."""
    messages: list[dict[str, Any]] = []
    if instructions:
        messages.append({"role": "system", "content": instructions})

    for item in items:
        role = item.get("role", "user")
        content = item.get("content", "")

        if isinstance(content, str):
            messages.append({"role": role, "content": content})
        elif isinstance(content, list):
            # Flatten content parts to a single string (image_url parts ignored
            # for now as they need special handling at provider level).
            parts = []
            for part in content:
                if part.get("type") == "input_text":
                    parts.append(part.get("text", ""))
                elif part.get("type") == "text":
                    parts.append(part.get("text", ""))
                elif part.get("type") == "input_image":
                    pass  # TODO: image support
            messages.append({"role": role, "content": " ".join(parts) if parts else ""})
        else:
            messages.append({"role": role, "content": str(content)})

    return messages


def internal_to_responses(internal: dict[str, Any]) -> dict[str, Any]:
    """Convert a unified internal response to OpenAI Responses API format."""
    choices = internal.get("choices") or []
    choice = choices[0] if choices and choices[0] is not None else {}
    msg = choice.get("message") or {}
    content_text = msg.get("content") or ""
    tool_calls = msg.get("tool_calls") or []
    reasoning = msg.get("reasoning_content")

    usage = internal.get("usage", {})
    finish_reason = choice.get("finish_reason") or "stop"

    # --- build output array ---
    output: list[dict[str, Any]] = []
    output_text = ""

    if reasoning:
        output.append({
            "id": "out_" + _now_hex(),
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": reasoning}],
            "status": "completed",
        })

    if content_text:
        text_id = "out_" + _now_hex()
        output.append({
            "id": text_id,
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{
                "type": "output_text",
                "text": content_text,
                "annotations": [],
            }],
        })
        output_text = content_text

    for tc in tool_calls:
        tc_id = "out_" + _now_hex()
        tc_func = tc.get("function", {})
        output.append({
            "id": tc_id,
            "type": "function_call",
            "call_id": tc.get("id", "call_" + _now_hex()),
            "name": tc_func.get("name", ""),
            "arguments": tc_func.get("arguments", "{}"),
            "status": "completed",
        })

    # --- status mapping ---
    status = "completed"
    if finish_reason == "length":
        status = "incomplete"

    return {
        "id": internal.get("id", "resp_" + _now_hex()),
        "object": "response",
        "created_at": _now_timestamp(),
        "status": status,
        "error": None,
        "incomplete_details": None if status == "completed" else {
            "reason": "max_output_tokens",
        },
        "model": internal.get("model", ""),
        "output": output,
        "output_text": output_text,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
        "temperature": None,
        "tools": [],
    }


def internal_chunk_to_responses_sse(
    chunk: dict[str, Any],
    *,
    is_first: bool = False,
    is_last: bool = False,
    response_id: str = "",
    created_at: int = 0,
) -> list[dict[str, Any]]:
    """Convert an internal stream chunk to Responses API SSE events.

    Returns a list of SSE event dicts for the Responses streaming protocol.
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
    finish_reason = choice.get("finish_reason")
    model = chunk.get("model", "")

    # --- response.created (first chunk only) ---
    if is_first:
        events.append({
            "type": "response.created",
            "response": {
                "id": response_id,
                "object": "response",
                "created_at": created_at,
                "status": "in_progress",
                "model": model,
                "output": [],
                "usage": None,
            },
        })
        events.append({
            "type": "response.in_progress",
            "response": {
                "id": response_id,
                "object": "response",
                "status": "in_progress",
                "output": [],
            },
        })

    # --- output_text.delta ---
    if content:
        events.append({
            "type": "response.output_text.delta",
            "item_id": delta.get("_item_id", "out_" + _now_hex()),
            "output_index": 0,
            "content_index": 0,
            "delta": content,
        })

    # --- reasoning_content delta ---
    reasoning = delta.get("reasoning_content")
    if reasoning:
        events.append({
            "type": "response.reasoning_summary_text.delta",
            "item_id": delta.get("_reasoning_item_id", "out_" + _now_hex()),
            "output_index": 0,
            "content_index": 0,
            "delta": reasoning,
        })

    # --- tool_calls ---
    tool_calls = delta.get("tool_calls")
    if tool_calls:
        for tc in tool_calls:
            tc_func = tc.get("function", {})
            if tc_func.get("name"):
                events.append({
                    "type": "response.function_call_arguments.start",
                    "item_id": "out_" + _now_hex(),
                    "output_index": 0,
                    "name": tc_func["name"],
                })
            if tc_func.get("arguments"):
                events.append({
                    "type": "response.function_call_arguments.delta",
                    "output_index": 0,
                    "arguments_delta": tc_func["arguments"],
                })

    # --- finish ---
    if is_last or finish_reason:
        reason = "stop"
        if finish_reason == "length":
            reason = "max_output_tokens"
        usage = chunk.get("usage", {})
        events.append({
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "id": "out_" + _now_hex(),
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{
                    "type": "output_text",
                    "text": "",
                    "annotations": [],
                }],
            },
        })
        events.append({
            "type": "response.completed",
            "response": {
                "id": response_id,
                "object": "response",
                "status": "completed",
                "output": [],
                "usage": {
                    "input_tokens": usage.get("prompt_tokens", 0),
                    "output_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                },
            },
        })

    return events
