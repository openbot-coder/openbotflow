"""Tests for cross-provider content format converters.

Covers:
- openai_to_anthropic_content: OpenAI content blocks → Anthropic content blocks
- anthropic_to_openai_content: Anthropic content blocks → OpenAI content blocks
- openai_to_google_parts: OpenAI content blocks → Google Gemini Part dicts
- Protocol adapter integration: Anthropic multimodal → internal format
"""

from __future__ import annotations

import base64
import json

import pytest

from botflow.common.content_converters import (
    anthropic_to_openai_content,
    openai_to_anthropic_content,
    openai_to_google_parts,
)


# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

# A 1x1 red PNG for testing (minimal valid PNG)
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
    "nGP4z8BQDwAEgAF/pooBPQAAAABJRU5ErkJggg=="
)
_TINY_PNG_DATA_URI = f"data:image/png;base64,{_TINY_PNG_B64}"
_TINY_JPG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVQI12NgAAIABQABNjN9GQAAAABJRU5ErkJggg=="


# ---------------------------------------------------------------------------
# openai_to_anthropic_content
# ---------------------------------------------------------------------------


class TestOpenAIToAnthropicContent:
    def test_string_passthrough(self):
        """String content should become a single text block."""
        result = openai_to_anthropic_content("Hello world")
        assert result == [{"type": "text", "text": "Hello world"}]

    def test_text_blocks(self):
        """Text blocks should be preserved."""
        blocks = [{"type": "text", "text": "What is this?"}]
        result = openai_to_anthropic_content(blocks)
        assert len(result) == 1
        assert result[0] == {"type": "text", "text": "What is this?"}

    def test_image_url_data_uri(self):
        """Base64 data URI image should become Anthropic image source."""
        blocks = [
            {"type": "text", "text": "Describe this image:"},
            {"type": "image_url", "image_url": {"url": _TINY_PNG_DATA_URI}},
        ]
        result = openai_to_anthropic_content(blocks)
        assert len(result) == 2
        assert result[0]["type"] == "text"
        assert result[1]["type"] == "image"
        assert result[1]["source"]["type"] == "base64"
        assert result[1]["source"]["media_type"] == "image/png"
        assert result[1]["source"]["data"] == _TINY_PNG_B64

    def test_image_url_http_ignored(self):
        """Non-data-URI image URLs should be ignored (no download)."""
        blocks = [
            {"type": "text", "text": "Look:"},
            {"type": "image_url", "image_url": {"url": "https://example.com/photo.jpg"}},
        ]
        result = openai_to_anthropic_content(blocks)
        assert len(result) == 1
        assert result[0]["type"] == "text"

    def test_image_url_non_image_mime_ignored(self):
        """Non-image MIME types in data URIs should be ignored."""
        blocks = [
            {"type": "image_url", "image_url": {"url": "data:application/pdf;base64,dGVzdA=="}},
        ]
        result = openai_to_anthropic_content(blocks)
        assert len(result) == 0

    def test_mixed_content(self):
        """Multiple content types in one message."""
        blocks = [
            {"type": "text", "text": "Here are two images:"},
            {"type": "image_url", "image_url": {"url": _TINY_PNG_DATA_URI}},
            {"type": "image_url", "image_url": {"url": _TINY_PNG_DATA_URI}},
            {"type": "text", "text": "Compare them."},
        ]
        result = openai_to_anthropic_content(blocks)
        assert len(result) == 4
        assert result[0]["type"] == "text"
        assert result[1]["type"] == "image"
        assert result[2]["type"] == "image"
        assert result[3]["type"] == "text"

    def test_anthropic_passthrough(self):
        """Already Anthropic-format blocks should pass through."""
        blocks = [
            {"type": "text", "text": "Hello"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "abc"}},
        ]
        result = openai_to_anthropic_content(blocks)
        assert len(result) == 2
        assert result[0]["type"] == "text"
        assert result[1]["type"] == "image"

    def test_empty_list(self):
        """Empty content list returns empty result."""
        assert openai_to_anthropic_content([]) == []

    def test_tool_use_passthrough(self):
        """Tool use blocks should be preserved."""
        blocks = [
            {"type": "tool_use", "id": "call_123", "name": "search", "input": {"q": "hello"}},
        ]
        result = openai_to_anthropic_content(blocks)
        assert result[0]["type"] == "tool_use"
        assert result[0]["name"] == "search"

    def test_tool_result_passthrough(self):
        """Tool result blocks should be preserved."""
        blocks = [
            {"type": "tool_result", "tool_use_id": "call_123", "content": "result here"},
        ]
        result = openai_to_anthropic_content(blocks)
        assert result[0]["type"] == "tool_result"
        assert result[0]["tool_use_id"] == "call_123"

    def test_unknown_type_skipped(self):
        """Unknown block types should be skipped silently."""
        blocks = [
            {"type": "text", "text": "Hello"},
            {"type": "custom_thing", "data": "ignored"},
        ]
        result = openai_to_anthropic_content(blocks)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# anthropic_to_openai_content
# ---------------------------------------------------------------------------


class TestAnthropicToOpenAIContent:
    def test_string_passthrough(self):
        """String content returned as-is."""
        assert anthropic_to_openai_content("Hello") == "Hello"

    def test_text_only_returns_string(self):
        """Single text block should return a plain string for efficiency."""
        blocks = [{"type": "text", "text": "Hello"}]
        result = anthropic_to_openai_content(blocks)
        assert result == "Hello"

    def test_multiple_text_returns_string(self):
        """Multiple text blocks joined with newlines."""
        blocks = [
            {"type": "text", "text": "Hello"},
            {"type": "text", "text": "World"},
        ]
        result = anthropic_to_openai_content(blocks)
        assert result == "Hello\nWorld"

    def test_image_to_data_uri(self):
        """Anthropic image source should convert to OpenAI data URI."""
        blocks = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": _TINY_PNG_B64,
                },
            }
        ]
        result = anthropic_to_openai_content(blocks)
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["type"] == "image_url"
        assert result[0]["image_url"]["url"] == _TINY_PNG_DATA_URI

    def test_mixed_text_and_image(self):
        """Mixed content returns list format (not plain string)."""
        blocks = [
            {"type": "text", "text": "Describe:"},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": _TINY_PNG_B64},
            },
        ]
        result = anthropic_to_openai_content(blocks)
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["type"] == "text"
        assert result[1]["type"] == "image_url"

    def test_tool_use_preserved(self):
        """Tool use blocks should be preserved."""
        blocks = [
            {"type": "tool_use", "id": "tu_1", "name": "calc", "input": {"x": 1}},
        ]
        result = anthropic_to_openai_content(blocks)
        assert isinstance(result, list)
        assert result[0]["type"] == "tool_use"
        assert result[0]["name"] == "calc"

    def test_tool_result_preserved(self):
        """Tool result blocks should be preserved."""
        blocks = [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "42"},
        ]
        result = anthropic_to_openai_content(blocks)
        assert isinstance(result, list)
        assert result[0]["type"] == "tool_result"

    def test_empty_list(self):
        """Empty list returns empty string."""
        assert anthropic_to_openai_content([]) == ""

    def test_non_base64_source_ignored(self):
        """Non-base64 source types should be skipped."""
        blocks = [
            {"type": "image", "source": {"type": "url", "url": "https://example.com/img.png"}},
        ]
        result = anthropic_to_openai_content(blocks)
        assert result == ""

    def test_empty_data_ignored(self):
        """Empty data in base64 source should be skipped."""
        blocks = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": ""}},
        ]
        result = anthropic_to_openai_content(blocks)
        assert result == ""


# ---------------------------------------------------------------------------
# openai_to_google_parts
# ---------------------------------------------------------------------------


class TestOpenAIToGoogleParts:
    def test_string_content(self):
        """String content becomes text part."""
        result = openai_to_google_parts("Hello Gemini")
        assert result == [{"text": "Hello Gemini"}]

    def test_text_block(self):
        """Text block becomes text part."""
        blocks = [{"type": "text", "text": "Hello"}]
        result = openai_to_google_parts(blocks)
        assert result == [{"text": "Hello"}]

    def test_image_data_uri(self):
        """Base64 data URI image becomes inline_data part."""
        blocks = [
            {"type": "text", "text": "Describe:"},
            {"type": "image_url", "image_url": {"url": _TINY_PNG_DATA_URI}},
        ]
        result = openai_to_google_parts(blocks)
        assert len(result) == 2
        assert result[0] == {"text": "Describe:"}
        assert "inline_data" in result[1]
        assert result[1]["inline_data"]["mime_type"] == "image/png"
        assert result[1]["inline_data"]["data"] == _TINY_PNG_B64

    def test_image_http_url(self):
        """HTTP URL image becomes file_data part."""
        blocks = [
            {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}},
        ]
        result = openai_to_google_parts(blocks)
        assert len(result) == 1
        assert "file_data" in result[0]
        assert result[0]["file_data"]["file_uri"] == "https://example.com/img.png"

    def test_empty_content(self):
        """Empty list returns default empty text part."""
        result = openai_to_google_parts([])
        assert result == [{"text": ""}]


# ---------------------------------------------------------------------------
# Round-trip tests
# ---------------------------------------------------------------------------


class TestRoundTrips:
    def test_openai_to_anthropic_and_back_text(self):
        """Text-only round trip: OpenAI → Anthropic → OpenAI."""
        original = [{"type": "text", "text": "Hello"}]
        anthropic = openai_to_anthropic_content(original)
        back = anthropic_to_openai_content(anthropic)
        assert back == "Hello"

    def test_openai_to_anthropic_and_back_image(self):
        """Image round trip: OpenAI → Anthropic → OpenAI."""
        original = [
            {"type": "text", "text": "Look:"},
            {"type": "image_url", "image_url": {"url": _TINY_PNG_DATA_URI}},
        ]
        anthropic = openai_to_anthropic_content(original)
        assert anthropic[1]["type"] == "image"

        back = anthropic_to_openai_content(anthropic)
        assert isinstance(back, list)
        assert back[0]["type"] == "text"
        assert back[1]["type"] == "image_url"
        assert back[1]["image_url"]["url"] == _TINY_PNG_DATA_URI

    def test_anthropic_to_openai_and_back_image(self):
        """Image round trip: Anthropic → OpenAI → Anthropic."""
        original = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": _TINY_PNG_B64},
            }
        ]
        openai = anthropic_to_openai_content(original)
        assert openai[0]["type"] == "image_url"

        back = openai_to_anthropic_content(openai)
        assert back[0]["type"] == "image"
        assert back[0]["source"]["data"] == _TINY_PNG_B64
        assert back[0]["source"]["media_type"] == "image/png"


# ---------------------------------------------------------------------------
# Protocol adapter integration
# ---------------------------------------------------------------------------


class TestProtocolAdapterMultimodal:
    """Test that anthropic_to_internal converts Anthropic multimodal content."""

    def test_anthropic_image_converted(self):
        """Anthropic image blocks should be converted to OpenAI format in internal."""
        from botflow.protocol_adapter import anthropic_to_internal

        body = {
            "model": "claude-sonnet-4-20250514",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this image:"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": _TINY_PNG_B64,
                            },
                        },
                    ],
                }
            ],
        }
        result = anthropic_to_internal(body)
        msg_content = result["messages"][0]["content"]
        # Should be converted to OpenAI format
        assert isinstance(msg_content, list)
        assert msg_content[0]["type"] == "text"
        assert msg_content[1]["type"] == "image_url"
        assert "data:image/png;base64," in msg_content[1]["image_url"]["url"]

    def test_anthropic_text_only_preserved(self):
        """Text-only Anthropic content should work normally."""
        from botflow.protocol_adapter import anthropic_to_internal

        body = {
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "Hello"}],
        }
        result = anthropic_to_internal(body)
        assert result["messages"][0]["content"] == "Hello"

    def test_anthropic_tool_use_converted(self):
        """Anthropic tool_use blocks should be preserved in internal format."""
        from botflow.protocol_adapter import anthropic_to_internal

        body = {
            "model": "claude-sonnet-4-20250514",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call_123",
                            "name": "search",
                            "input": {"q": "hello"},
                        }
                    ],
                }
            ],
        }
        result = anthropic_to_internal(body)
        content = result["messages"][0]["content"]
        assert isinstance(content, list)
        assert content[0]["type"] == "tool_use"
        assert content[0]["name"] == "search"
