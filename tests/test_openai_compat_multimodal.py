"""Tests for multimodal content extraction in openai_compat provider.

Covers:
- _extract_text_from_content: str → str passthrough
- _extract_text_from_content: list-of-dicts → concatenated text
- _extract_text_from_content: None / empty → ""
- _to_unified: list content blocks → plain text in output
- _chunk_to_unified: list content delta → plain text in output
- DeepSeek reasoning_content preservation when content is empty/list
"""

from __future__ import annotations

import pytest

from botflow.providers.openai_compat import OpenAICompatProvider, _extract_text_from_content


# ---------------------------------------------------------------------------
# _extract_text_from_content unit tests
# ---------------------------------------------------------------------------


class TestExtractTextFromContent:
    def test_str_passthrough(self):
        assert _extract_text_from_content("hello") == "hello"

    def test_empty_string(self):
        assert _extract_text_from_content("") == ""

    def test_none(self):
        assert _extract_text_from_content(None) == ""

    def test_text_blocks_only(self):
        blocks = [
            {"type": "text", "text": "Hello "},
            {"type": "text", "text": "world"},
        ]
        assert _extract_text_from_content(blocks) == "Hello  world"

    def test_mixed_blocks_extracts_text(self):
        blocks = [
            {"type": "text", "text": "Look at this:"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            {"type": "text", "text": " pretty, right?"},
        ]
        assert _extract_text_from_content(blocks) == "Look at this:  pretty, right?"

    def test_no_text_blocks(self):
        blocks = [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
        ]
        assert _extract_text_from_content(blocks) == ""

    def test_single_text_block(self):
        assert _extract_text_from_content([{"type": "text", "text": "only"}]) == "only"

    def test_non_dict_items_ignored(self):
        blocks = ["not a dict", 42, {"type": "text", "text": "ok"}]
        assert _extract_text_from_content(blocks) == "ok"

    def test_fallback_to_str(self):
        assert _extract_text_from_content(42) == "42"


# ---------------------------------------------------------------------------
# _to_unified integration tests
# ---------------------------------------------------------------------------


def _make_provider():
    return OpenAICompatProvider(api_key="test-key", base_url="https://test.example.com/v1")


class TestToUnifiedMultimodal:
    def test_list_content_extracted(self):
        """When OpenAI returns list content, output is plain string."""
        provider = _make_provider()
        data = {
            "id": "chatcmpl-test",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "The sky is "},
                            {"type": "text", "text": "blue."},
                        ],
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        result = provider._to_unified(data, "gpt-4")
        assert isinstance(result["choices"][0]["message"]["content"], str)
        assert result["choices"][0]["message"]["content"] == "The sky is  blue."

    def test_str_content_unchanged(self):
        """String content passes through unchanged."""
        provider = _make_provider()
        data = {
            "id": "chatcmpl-test",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "plain text"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        result = provider._to_unified(data, "gpt-4")
        assert result["choices"][0]["message"]["content"] == "plain text"

    def test_reasoning_content_preserved_when_content_empty(self):
        """When content is empty and reasoning_content exists, both fields are preserved.

        The old 'fallback' merged reasoning_content into content, which broke
        round-trip: upstream APIs like mimo require reasoning_content to be
        passed back verbatim in subsequent requests.  Keeping them separate
        ensures the client can echo reasoning_content back.
        """
        provider = _make_provider()
        data = {
            "id": "chatcmpl-test",
            "model": "deepseek-reasoner",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": [],
                        "reasoning_content": "thinking step by step...",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        result = provider._to_unified(data, "deepseek-reasoner")
        msg = result["choices"][0]["message"]
        # Empty list extracts to "" — reasoning_content stays separate
        assert msg["content"] == ""
        assert msg["reasoning_content"] == "thinking step by step..."


# ---------------------------------------------------------------------------
# _chunk_to_unified integration tests
# ---------------------------------------------------------------------------


class TestChunkToUnifiedMultimodal:
    def test_list_delta_extracted(self):
        """When stream delta has list content, output is plain string."""
        provider = _make_provider()
        data = {
            "id": "chatcmpl-test",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "chunk1"}],
                    },
                    "finish_reason": None,
                }
            ],
        }
        result = provider._chunk_to_unified(data, "gpt-4")
        delta_content = result["choices"][0]["delta"]["content"]
        assert isinstance(delta_content, str)
        assert delta_content == "chunk1"

    def test_str_delta_unchanged(self):
        provider = _make_provider()
        data = {
            "id": "chatcmpl-test",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "plain"},
                    "finish_reason": None,
                }
            ],
        }
        result = provider._chunk_to_unified(data, "gpt-4")
        assert result["choices"][0]["delta"]["content"] == "plain"

    def test_none_delta_content(self):
        provider = _make_provider()
        data = {
            "id": "chatcmpl-test",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant"},
                    "finish_reason": "stop",
                }
            ],
        }
        result = provider._chunk_to_unified(data, "gpt-4")
        assert result["choices"][0]["delta"]["content"] == ""
