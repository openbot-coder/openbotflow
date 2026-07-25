"""Tests for Anthropic provider multimodal content handling.

Tests _extract_system, _to_unified, and inbound content conversion
for OpenAI→Anthropic format mapping.
"""

from __future__ import annotations

import pytest

from botflow.providers.anthropic_provider import (
    AnthropicProvider,
    _extract_response_content,
    _extract_text_content,
)


# ---------------------------------------------------------------------------
# _extract_text_content
# ---------------------------------------------------------------------------


class TestExtractTextContent:
    def test_string_passthrough(self):
        assert _extract_text_content("hello") == "hello"

    def test_list_single_text(self):
        blocks = [{"type": "text", "text": "hello"}]
        assert _extract_text_content(blocks) == "hello"

    def test_list_multiple_text(self):
        blocks = [
            {"type": "text", "text": "part1"},
            {"type": "text", "text": "part2"},
        ]
        assert _extract_text_content(blocks) == "part1\npart2"

    def test_list_with_image_ignored(self):
        blocks = [
            {"type": "text", "text": "Describe:"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "abc"}},
        ]
        assert _extract_text_content(blocks) == "Describe:"

    def test_empty_list(self):
        assert _extract_text_content([]) == ""

    def test_none_returns_empty(self):
        assert _extract_text_content(None) == ""

    def test_non_string_non_list(self):
        assert _extract_text_content(42) == "42"


# ---------------------------------------------------------------------------
# _extract_response_content
# ---------------------------------------------------------------------------


class TestExtractResponseContent:
    def test_single_text_block(self):
        blocks = [{"type": "text", "text": "Hello"}]
        content, reasoning = _extract_response_content(blocks)
        assert content == "Hello"
        assert reasoning == ""

    def test_thinking_plus_text(self):
        blocks = [
            {"type": "thinking", "thinking": "Let me think..."},
            {"type": "text", "text": "The answer is 42."},
        ]
        content, reasoning = _extract_response_content(blocks)
        assert content == "The answer is 42."
        assert reasoning == "Let me think..."

    def test_multiple_thinking_blocks(self):
        blocks = [
            {"type": "thinking", "thinking": "step 1"},
            {"type": "thinking", "thinking": "step 2"},
            {"type": "text", "text": "Result"},
        ]
        content, reasoning = _extract_response_content(blocks)
        assert content == "Result"
        assert reasoning == "step 1\nstep 2"

    def test_multiple_text_blocks(self):
        blocks = [
            {"type": "text", "text": "part1"},
            {"type": "text", "text": "part2"},
        ]
        content, reasoning = _extract_response_content(blocks)
        assert content == "part1\npart2"
        assert reasoning == ""

    def test_empty_blocks(self):
        content, reasoning = _extract_response_content([])
        assert content == ""
        assert reasoning == ""


# ---------------------------------------------------------------------------
# _extract_system
# ---------------------------------------------------------------------------


class TestExtractSystem:
    def setup_method(self):
        self.provider = AnthropicProvider(api_key="test-key")

    def test_string_system(self):
        messages = [
            {"role": "system", "content": "Be helpful"},
            {"role": "user", "content": "Hi"},
        ]
        system, filtered = self.provider._extract_system(messages)
        assert system == "Be helpful"
        assert len(filtered) == 1
        assert filtered[0]["role"] == "user"

    def test_list_system_extracts_text(self):
        messages = [
            {
                "role": "system",
                "content": [
                    {"type": "text", "text": "Rule 1"},
                    {"type": "text", "text": "Rule 2"},
                ],
            },
            {"role": "user", "content": "Hi"},
        ]
        system, filtered = self.provider._extract_system(messages)
        assert system == "Rule 1\nRule 2"
        assert len(filtered) == 1

    def test_no_system(self):
        messages = [{"role": "user", "content": "Hi"}]
        system, filtered = self.provider._extract_system(messages)
        assert system == ""
        assert len(filtered) == 1

    def test_inbound_image_url_converted(self):
        """OpenAI image_url format is converted to Anthropic image format."""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe:"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,abc123"},
                    },
                ],
            }
        ]
        _, filtered = self.provider._extract_system(messages)
        assert len(filtered) == 1
        user_content = filtered[0]["content"]
        # Should be converted to Anthropic format
        assert isinstance(user_content, list)
        assert user_content[0] == {"type": "text", "text": "Describe:"}
        assert user_content[1]["type"] == "image"
        assert user_content[1]["source"]["type"] == "base64"
        assert user_content[1]["source"]["media_type"] == "image/png"

    def test_string_content_passthrough(self):
        messages = [{"role": "user", "content": "Hello"}]
        _, filtered = self.provider._extract_system(messages)
        assert filtered[0]["content"] == "Hello"


# ---------------------------------------------------------------------------
# _to_unified
# ---------------------------------------------------------------------------


class TestToUnified:
    def setup_method(self):
        self.provider = AnthropicProvider(api_key="test-key")

    def test_basic_response(self):
        data = {
            "id": "msg-123",
            "content": [{"type": "text", "text": "Hello"}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_read_input_tokens": 5,
            },
        }
        result = self.provider._to_unified(data, "claude-sonnet-4-20250514")
        assert result["id"] == "msg-123"
        assert result["choices"][0]["message"]["content"] == "Hello"
        assert result["choices"][0]["message"]["role"] == "assistant"
        assert "reasoning_content" not in result["choices"][0]["message"]
        assert result["usage"]["prompt_tokens"] == 10
        assert result["usage"]["completion_tokens"] == 20
        assert result["usage"]["cache_tokens"] == 5

    def test_thinking_blocks_extracted(self):
        data = {
            "id": "msg-456",
            "content": [
                {"type": "thinking", "thinking": "Let me reason..."},
                {"type": "text", "text": "The answer is 42."},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 10},
        }
        result = self.provider._to_unified(data, "claude-sonnet-4-20250514")
        msg = result["choices"][0]["message"]
        assert msg["content"] == "The answer is 42."
        assert msg["reasoning_content"] == "Let me reason..."

    def test_empty_content(self):
        data = {"id": "msg-789", "content": [], "stop_reason": "end_turn", "usage": {}}
        result = self.provider._to_unified(data, "model")
        assert result["choices"][0]["message"]["content"] == ""
