"""Tests for context window management (100% coverage)."""

from __future__ import annotations

from botflow.common.context import (
    _extract_text,
    estimate_tokens,
    truncate_to_context_window,
    _cjk_ratio,
)


def test_extract_text_str():
    assert _extract_text("hello") == "hello"


def test_extract_text_list():
    content = [
        {"type": "text", "text": "foo"},
        {"type": "image_url", "image_url": {"url": "x"}},
        {"type": "text", "text": "bar"},
    ]
    assert _extract_text(content) == "foo bar"


def test_extract_text_other():
    assert _extract_text(None) == ""
    assert _extract_text(123) == "123"


def test_estimate_tokens_empty():
    assert estimate_tokens([]) == 0


def test_estimate_tokens_english():
    msgs = [{"role": "user", "content": "hello world this is a test"}]
    n = estimate_tokens(msgs)
    assert n > 0


def test_estimate_tokens_cjk():
    msgs = [{"role": "user", "content": "你好世界这是一段中文测试"}]
    n = estimate_tokens(msgs)
    assert n > 0


def test_estimate_tokens_list_content():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi there"}]}]
    assert estimate_tokens(msgs) > 0


def test_cjk_ratio_empty():
    assert _cjk_ratio("") == 0.0


def test_cjk_ratio_mixed():
    r = _cjk_ratio("ab你好cd")
    assert 0.0 < r < 1.0


def test_cjk_ratio_full():
    assert _cjk_ratio("你好") == 1.0


def test_truncate_zero_window():
    msgs = [{"role": "user", "content": "x"}]
    assert truncate_to_context_window(msgs, 0) == msgs
    assert truncate_to_context_window(msgs, -5) == msgs


def test_truncate_fits():
    msgs = [{"role": "user", "content": "short"}]
    assert truncate_to_context_window(msgs, 100000) == msgs


def test_truncate_with_system_kept():
    msgs = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "a" * 5000},
        {"role": "user", "content": "b" * 5000},
        {"role": "user", "content": "c" * 5000},
    ]
    out = truncate_to_context_window(msgs, context_window=50, max_tokens=0)
    roles = [m["role"] for m in out]
    assert roles[0] == "system"
    assert len(out) < len(msgs)


def test_truncate_only_system_fits():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "y" * 5000},
    ]
    out = truncate_to_context_window(msgs, context_window=10, max_tokens=0)
    assert out == [{"role": "system", "content": "sys"}]


def test_truncate_no_system_keeps_last():
    msgs = [
        {"role": "user", "content": "a" * 5000},
        {"role": "user", "content": "b" * 5000},
        {"role": "user", "content": "c" * 5000},
    ]
    out = truncate_to_context_window(msgs, context_window=10, max_tokens=0)
    assert out == [msgs[-1]]


def test_truncate_empty():
    assert truncate_to_context_window([], 1000) == []
