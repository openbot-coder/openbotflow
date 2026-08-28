"""Additional coverage for protocol_adapter (internal <-> openai branches)."""

from __future__ import annotations

from botflow.common.content_converters import (
    anthropic_to_openai_content,
    openai_to_anthropic_content,
    openai_to_google_parts,
)
from botflow.protocol_adapter import internal_to_openai, openai_to_internal


def test_internal_to_openai_tool_calls():
    data = {
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "f", "arguments": "{}"},
                }],
            },
            "finish_reason": "tool_calls",
        }],
    }
    out = internal_to_openai(data)
    msg = out["choices"][0]["message"]
    assert msg["tool_calls"][0]["id"] == "call_1"
    assert out["choices"][0]["finish_reason"] == "tool_calls"


def test_internal_to_openai_function_call():
    data = {
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "hi",
                "function_call": {"name": "g", "arguments": "{}"},
            },
            "finish_reason": "stop",
        }],
    }
    out = internal_to_openai(data)
    assert out["choices"][0]["message"]["function_call"]["name"] == "g"


def test_internal_to_openai_system_fingerprint_and_usage():
    data = {
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "x"}, "finish_reason": "stop"}],
        "system_fingerprint": "fp_1",
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        "id": "chatcmpl-1",
        "model": "m",
        "created": 123,
    }
    out = internal_to_openai(data)
    assert out["system_fingerprint"] == "fp_1"
    assert out["usage"]["total_tokens"] == 3
    assert out["id"] == "chatcmpl-1"


def test_internal_to_openai_minimal_no_usage():
    data = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "x"}, "finish_reason": "stop"}]}
    out = internal_to_openai(data)
    assert out["usage"] == {}


def test_internal_to_openai_roles_passthrough():
    data = {
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "x"},
            "finish_reason": "stop",
        }],
    }
    out = internal_to_openai(data)
    assert out["choices"][0]["message"]["role"] == "assistant"


def test_internal_to_openai_meta():
    data = {
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "x"}, "finish_reason": "stop"}],
        "error": None,
    }
    out = internal_to_openai(data)
    assert "meta" not in out


def test_openai_to_internal_full():
    data = {
        "messages": [{"role": "user", "content": "y"}],
        "model": "m",
        "temperature": 0.5,
        "max_tokens": 10,
        "stream": True,
    }
    out = openai_to_internal(data)
    assert out["messages"][0]["content"] == "y"
    assert out["model"] == "m"
    assert out["temperature"] == 0.5
    assert out["max_tokens"] == 10
    assert out["stream"] is True


# ---- content converter branches still worth pinning ----


def test_anthropic_content_with_tool_use():
    blocks = [
        {"type": "text", "text": "thinking"},
        {"type": "tool_use", "id": "t1", "name": "f", "input": {"a": 1}},
    ]
    out = anthropic_to_openai_content(blocks)
    assert out[0]["type"] == "text"
    assert out[1]["type"] == "tool_use"
    assert out[1]["id"] == "t1"


def test_anthropic_content_empty():
    out = anthropic_to_openai_content([])
    assert out == ""


def test_google_parts_image_with_mime():
    blocks = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
    out = openai_to_google_parts(blocks)
    assert out[0]["inline_data"]["mime_type"] == "image/png"


def test_google_parts_empty():
    out = openai_to_google_parts([])
    assert out == [{"text": ""}]


def test_openai_to_anthropic_image_data_uri():
    blocks = [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,XYZ"}}]
    out = openai_to_anthropic_content(blocks)
    assert out[0]["source"]["type"] == "base64"
    assert out[0]["source"]["media_type"] == "image/jpeg"
