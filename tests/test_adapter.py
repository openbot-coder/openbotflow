"""Tests for protocol adapter: OpenAI ↔ internal ↔ Anthropic conversions."""

from __future__ import annotations

import json

from botflow.protocol_adapter import (
    anthropic_to_internal,
    internal_chunk_to_anthropic_sse,
    internal_chunk_to_openai_sse,
    internal_to_anthropic,
    internal_to_openai,
    models_to_anthropic,
    models_to_openai,
    normalize_messages,
    openai_to_internal,
)


# ---------------------------------------------------------------------------
# OpenAI to internal
# ---------------------------------------------------------------------------

class TestOpenAIToInternal:
    def test_basic_conversion(self):
        body = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
            "temperature": 0.7,
            "max_tokens": 100,
            "stream": False,
        }
        result = openai_to_internal(body)
        assert result["model"] == "gpt-4o"
        assert result["temperature"] == 0.7
        assert result["max_tokens"] == 100
        assert result["stream"] is False
        assert len(result["messages"]) == 1

    def test_extra_fields_preserved(self):
        body = {"model": "gpt-4o", "messages": [], "top_p": 0.9, "seed": 42}
        result = openai_to_internal(body)
        assert result["extra"]["top_p"] == 0.9
        assert result["extra"]["seed"] == 42

    def test_empty_messages(self):
        body = {"model": "gpt-4o", "messages": []}
        result = openai_to_internal(body)
        assert result["messages"] == []


# ---------------------------------------------------------------------------
# Anthropic to internal
# ---------------------------------------------------------------------------

class TestAnthropicToInternal:
    def test_basic_conversion(self):
        body = {
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 4096,
            "stream": False,
        }
        result = anthropic_to_internal(body)
        assert result["model"] == "claude-sonnet-4-20250514"
        assert result["max_tokens"] == 4096
        assert len(result["messages"]) == 1

    def test_system_prompt_extracted(self):
        body = {
            "model": "claude-3-opus",
            "system": "You are helpful",
            "messages": [{"role": "user", "content": "Hi"}],
        }
        result = anthropic_to_internal(body)
        assert result["messages"][0]["role"] == "system"
        assert result["messages"][0]["content"] == "You are helpful"
        assert result["messages"][1]["role"] == "user"


# ---------------------------------------------------------------------------
# Internal to OpenAI response
# ---------------------------------------------------------------------------

class TestInternalToOpenAI:
    def test_basic_conversion(self):
        internal = {
            "id": "chatcmpl-123",
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello!"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30, "cache_tokens": 5},
        }
        result = internal_to_openai(internal)
        assert result["id"] == "chatcmpl-123"
        assert result["object"] == "chat.completion"
        assert result["choices"][0]["message"]["content"] == "Hello!"
        assert result["usage"]["prompt_tokens"] == 10


# ---------------------------------------------------------------------------
# Internal to Anthropic response
# ---------------------------------------------------------------------------

class TestInternalToAnthropic:
    def test_basic_conversion(self):
        internal = {
            "id": "msg_123",
            "model": "claude-sonnet-4-20250514",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello!"},
                    "finish_reason": "end_turn",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30, "cache_tokens": 0},
        }
        result = internal_to_anthropic(internal)
        assert result["type"] == "message"
        assert result["role"] == "assistant"
        assert result["content"][0]["text"] == "Hello!"
        assert result["usage"]["input_tokens"] == 10


# ---------------------------------------------------------------------------
# SSE chunk conversions
# ---------------------------------------------------------------------------

class TestSSEConversion:
    def test_openai_sse_chunk(self):
        chunk = {
            "id": "chunk-1",
            "object": "chat.completion.chunk",
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "Hello"},
                    "finish_reason": None,
                }
            ],
            "usage": None,
        }
        result = internal_chunk_to_openai_sse(chunk)
        assert result["choices"][0]["delta"]["content"] == "Hello"
        assert "usage" not in result

    def test_openai_sse_final_with_usage(self):
        chunk = {
            "id": "chunk-2",
            "model": "gpt-4o",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }
        result = internal_chunk_to_openai_sse(chunk)
        assert result["usage"]["prompt_tokens"] == 10

    def test_anthropic_sse_content_delta(self):
        chunk = {
            "id": "msg_1",
            "model": "claude-sonnet-4-20250514",
            "choices": [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": None}],
        }
        events = internal_chunk_to_anthropic_sse(chunk)
        # No role, so no message_start
        assert len(events) == 1
        assert events[0]["type"] == "content_block_delta"

    def test_anthropic_sse_with_role(self):
        chunk = {
            "id": "msg_1",
            "model": "claude-sonnet-4-20250514",
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hello"}, "finish_reason": None}],
        }
        events = internal_chunk_to_anthropic_sse(chunk)
        assert len(events) == 2  # message_start + content_block_delta
        assert events[0]["type"] == "message_start"
        assert events[1]["type"] == "content_block_delta"

    def test_anthropic_sse_finish(self):
        chunk = {
            "id": "msg_1",
            "model": "claude-sonnet-4-20250514",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }
        events = internal_chunk_to_anthropic_sse(chunk)
        assert len(events) == 1
        assert events[0]["type"] == "message_delta"


# ---------------------------------------------------------------------------
# Models list formats
# ---------------------------------------------------------------------------

class TestModelsList:
    def test_openai_format(self):
        models = [
            {"id": "gpt-4o", "name": "gpt-4o", "provider_type": "openai"},
            {"id": "claude-sonnet-4-20250514", "name": "claude-sonnet-4-20250514", "provider_type": "anthropic"},
        ]
        result = models_to_openai(models)
        assert result["object"] == "list"
        assert len(result["data"]) == 2
        assert result["data"][0]["id"] == "gpt-4o"

    def test_anthropic_format(self):
        models = [{"id": "claude-sonnet-4-20250514", "name": "claude-sonnet-4-20250514", "display_name": "Claude Sonnet"}]
        result = models_to_anthropic(models)
        assert "data" in result
        assert result["data"][0]["type"] == "model"


# ---------------------------------------------------------------------------
# normalize_messages tests
# ---------------------------------------------------------------------------


class TestNormalizeMessages:
    def test_none_content_defaults_to_empty(self):
        msgs = [{"role": "user", "content": None}]
        result = normalize_messages(msgs)
        assert result[0]["content"] == ""

    def test_list_content_with_text_parts(self):
        msgs = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "What is this?"},
                {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}},
            ],
        }]
        result = normalize_messages(msgs)
        assert len(result) == 1
        assert isinstance(result[0]["content"], list)
        assert result[0]["content"][0]["type"] == "text"
        assert result[0]["content"][1]["type"] == "image_url"

    def test_list_content_bare_strings(self):
        msgs = [{"role": "user", "content": ["hello", "world"]}]
        result = normalize_messages(msgs)
        assert result[0]["content"][0] == {"type": "text", "text": "hello"}
        assert result[0]["content"][1] == {"type": "text", "text": "world"}

    def test_list_content_dict_without_type(self):
        msgs = [{"role": "user", "content": [{"text": "hello"}]}]
        result = normalize_messages(msgs)
        assert result[0]["content"][0]["type"] == "text"

    def test_list_content_dict_without_type_with_url(self):
        msgs = [{"role": "user", "content": [{"url": "https://example.com/img.png"}]}]
        result = normalize_messages(msgs)
        assert result[0]["content"][0]["type"] == "image_url"

    def test_duplicate_system_messages_collapsed(self):
        msgs = [
            {"role": "system", "content": "first"},
            {"role": "user", "content": "hi"},
            {"role": "system", "content": "second"},
        ]
        result = normalize_messages(msgs)
        systems = [m for m in result if m["role"] == "system"]
        assert len(systems) == 1
        assert systems[0]["content"] == "first"

    def test_non_dict_messages_skipped(self):
        msgs = ["bad", {"role": "user", "content": "ok"}]
        result = normalize_messages(msgs)
        assert len(result) == 1

    def test_string_content_unchanged(self):
        msgs = [{"role": "user", "content": "hello"}]
        result = normalize_messages(msgs)
        assert result[0]["content"] == "hello"

    def test_empty_messages(self):
        assert normalize_messages([]) == []


# ---------------------------------------------------------------------------
# OpenAI to internal with multimodal content
# ---------------------------------------------------------------------------


class TestOpenAIToInternalMultimodal:
    def test_text_image_parts_preserved(self):
        body = {
            "model": "gpt-4o",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "What do you see?"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}},
                ],
            }],
        }
        result = openai_to_internal(body)
        msg = result["messages"][0]
        assert isinstance(msg["content"], list)
        assert len(msg["content"]) == 2
        assert msg["content"][0]["type"] == "text"
        assert msg["content"][1]["type"] == "image_url"

    def test_base64_image_preserved(self):
        body = {
            "model": "gpt-4o",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this."},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
                ],
            }],
        }
        result = openai_to_internal(body)
        assert "data:image/png;base64" in result["messages"][0]["content"][1]["image_url"]["url"]


# ---------------------------------------------------------------------------
# Anthropic to internal with multimodal content
# ---------------------------------------------------------------------------


class TestAnthropicToInternalMultimodal:
    def test_system_list_content_flattened(self):
        body = {
            "model": "claude-sonnet-4-20250514",
            "system": [{"type": "text", "text": "You are helpful."}],
            "messages": [{"role": "user", "content": "Hi"}],
        }
        result = anthropic_to_internal(body)
        # system should be prepended as a system message with string content
        system_msg = result["messages"][0]
        assert system_msg["role"] == "system"
        assert system_msg["content"] == "You are helpful."

    def test_user_multimodal_content_preserved(self):
        body = {
            "model": "claude-sonnet-4-20250514",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is this?"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "abc"}},
                ],
            }],
        }
        result = anthropic_to_internal(body)
        msg = result["messages"][0]
        assert isinstance(msg["content"], list)
        assert len(msg["content"]) == 2