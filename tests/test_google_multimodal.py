"""Tests for Google provider multimodal content handling.

Tests the _convert_content_parts and _convert_messages methods
that convert OpenAI-format multimodal content to Gemini Part objects.
"""

from __future__ import annotations

import base64

import pytest

from botflow.providers.google_provider import GoogleProvider, _extract_system_text, _parse_data_url


# A 1x1 red PNG for testing (minimal valid PNG)
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
    "nGP4z8BQDwAEgAF/pooBPQAAAABJRU5ErkJggg=="
)
_TINY_PNG_DATA_URI = f"data:image/png;base64,{_TINY_PNG_B64}"


# ---------------------------------------------------------------------------
# _parse_data_url
# ---------------------------------------------------------------------------


class TestParseDataUrl:
    def test_valid_png(self):
        mime, data = _parse_data_url(_TINY_PNG_DATA_URI)
        assert mime == "image/png"
        assert data == _TINY_PNG_B64

    def test_valid_jpeg(self):
        url = "data:image/jpeg;base64,/9j/4AAQ"
        mime, data = _parse_data_url(url)
        assert mime == "image/jpeg"
        assert data == "/9j/4AAQ"

    def test_not_data_uri(self):
        assert _parse_data_url("https://example.com/img.png") is None

    def test_empty_string(self):
        assert _parse_data_url("") is None


# ---------------------------------------------------------------------------
# _convert_content_parts
# ---------------------------------------------------------------------------


class TestConvertContentParts:
    def test_text_only(self):
        blocks = [{"type": "text", "text": "Hello Gemini"}]
        parts = GoogleProvider._convert_content_parts(blocks)
        assert len(parts) == 1
        assert parts[0].text == "Hello Gemini"

    def test_image_data_uri(self):
        blocks = [
            {"type": "text", "text": "Describe:"},
            {"type": "image_url", "image_url": {"url": _TINY_PNG_DATA_URI}},
        ]
        parts = GoogleProvider._convert_content_parts(blocks)
        assert len(parts) == 2
        assert parts[0].text == "Describe:"
        assert parts[1].inline_data is not None
        assert parts[1].inline_data.mime_type == "image/png"
        # Blob decodes base64 to bytes
        assert isinstance(parts[1].inline_data.data, bytes)
        assert len(parts[1].inline_data.data) > 0

    def test_image_http_url(self):
        blocks = [
            {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}},
        ]
        parts = GoogleProvider._convert_content_parts(blocks)
        assert len(parts) == 1
        assert parts[0].file_data is not None
        assert parts[0].file_data.file_uri == "https://example.com/img.png"

    def test_empty_list(self):
        parts = GoogleProvider._convert_content_parts([])
        assert parts == []

    def test_unknown_type_ignored(self):
        blocks = [
            {"type": "text", "text": "Hello"},
            {"type": "custom_thing", "data": "ignored"},
        ]
        parts = GoogleProvider._convert_content_parts(blocks)
        assert len(parts) == 1


# ---------------------------------------------------------------------------
# _convert_messages
# ---------------------------------------------------------------------------


class TestConvertMessages:
    def test_string_content(self):
        messages = [{"role": "user", "content": "Hello"}]
        contents, system = GoogleProvider._convert_messages(messages)
        assert system == ""
        assert len(contents) == 1
        assert contents[0].role == "user"
        assert contents[0].parts[0].text == "Hello"

    def test_system_message(self):
        messages = [
            {"role": "system", "content": "Be helpful"},
            {"role": "user", "content": "Hi"},
        ]
        contents, system = GoogleProvider._convert_messages(messages)
        assert system == "Be helpful"
        assert len(contents) == 1
        assert contents[0].role == "user"

    def test_assistant_role(self):
        messages = [{"role": "assistant", "content": "I am Claude"}]
        contents, system = GoogleProvider._convert_messages(messages)
        assert contents[0].role == "model"

    def test_multimodal_list_content(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this:"},
                    {"type": "image_url", "image_url": {"url": _TINY_PNG_DATA_URI}},
                ],
            }
        ]
        contents, system = GoogleProvider._convert_messages(messages)
        assert len(contents) == 1
        assert len(contents[0].parts) == 2
        assert contents[0].parts[0].text == "Describe this:"
        assert contents[0].parts[1].inline_data is not None
        assert contents[0].parts[1].inline_data.mime_type == "image/png"

    def test_empty_messages(self):
        contents, system = GoogleProvider._convert_messages([])
        assert system == ""
        assert len(contents) == 1  # Default empty user message
        assert contents[0].parts[0].text == ""

    def test_non_string_non_list_content(self):
        """Non-string, non-list content should be stringified."""
        messages = [{"role": "user", "content": 42}]
        contents, system = GoogleProvider._convert_messages(messages)
        assert contents[0].parts[0].text == "42"


# ---------------------------------------------------------------------------
# _extract_system_text
# ---------------------------------------------------------------------------


class TestExtractSystemText:
    def test_string_passthrough(self):
        assert _extract_system_text("Be helpful") == "Be helpful"

    def test_list_single_text(self):
        blocks = [{"type": "text", "text": "Rule 1"}]
        assert _extract_system_text(blocks) == "Rule 1"

    def test_list_multiple_text(self):
        blocks = [
            {"type": "text", "text": "Rule 1"},
            {"type": "text", "text": "Rule 2"},
        ]
        assert _extract_system_text(blocks) == "Rule 1\nRule 2"

    def test_empty_list(self):
        assert _extract_system_text([]) == ""

    def test_none_returns_empty(self):
        assert _extract_system_text(None) == ""

    def test_non_string_non_list(self):
        assert _extract_system_text(42) == "42"


class TestConvertMessagesSystem:
    def test_system_with_list_content(self):
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
        contents, system = GoogleProvider._convert_messages(messages)
        assert system == "Rule 1\nRule 2"
        assert len(contents) == 1

    def test_non_string_non_list_system(self):
        messages = [{"role": "system", "content": 42}]
        contents, system = GoogleProvider._convert_messages(messages)
        assert system == "42"
