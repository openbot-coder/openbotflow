"""Unit tests for core.py stream serializers (pure functions)."""

from __future__ import annotations

import botflow.core as core


def test_openai_serialize():
    chunk = {
        "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}],
        "usage": {"total_tokens": 3},
    }
    lines, usage = core._openai_serialize(chunk)
    assert lines[0].startswith("data: ")
    assert usage == {"total_tokens": 3}


def test_anthropic_serialize_ok():
    chunk = {"choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}]}
    lines, usage = core._anthropic_serialize(chunk)
    assert len(lines) >= 1
    assert any("data:" in ln for ln in lines)
