"""Tests for context window management (100% coverage)."""

from __future__ import annotations

import pathlib

import botflow.common.context as context
from botflow.common.context import (
    _encoding,
    _extract_text,
    _MESSAGE_OVERHEAD,
    _token_upper_bound,
    estimate_tokens,
    truncate_to_context_window,
)


# ---------------------------------------------------------------------------
# Kept legacy tests (coverage of _extract_text branches & truncate edge paths)
# ---------------------------------------------------------------------------


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


def test_estimate_tokens_cjk():
    msgs = [{"role": "user", "content": "你好世界这是一段中文测试"}]
    n = estimate_tokens(msgs)
    assert n > 0


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


# ---------------------------------------------------------------------------
# F1: estimate_tokens uses tiktoken (正例 / 反例 / 边界)
# ---------------------------------------------------------------------------


def test_estimate_tokens_chinese():
    msgs = [{"role": "user", "content": "你好世界"}]
    enc = _encoding()
    expected = (
        len(enc.encode_ordinary("user"))
        + len(enc.encode_ordinary("你好世界"))
        + _MESSAGE_OVERHEAD
    )
    assert estimate_tokens(msgs) == expected
    assert estimate_tokens(msgs) > 0


def test_estimate_tokens_english():
    msgs = [{"role": "user", "content": "hello world this is a test"}]
    enc = _encoding()
    expected = (
        len(enc.encode_ordinary("user"))
        + len(enc.encode_ordinary("hello world this is a test"))
        + _MESSAGE_OVERHEAD
    )
    assert estimate_tokens(msgs) == expected


def test_estimate_tokens_empty_list():
    assert estimate_tokens([]) == 0


def test_estimate_tokens_list_content():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi there"}]}]
    assert estimate_tokens(msgs) > 0


def test_estimate_tokens_special_token_literal():
    # 反例（关键坑）：正文含 <|endoftext|> 字面量不得抛 ValueError
    msgs = [{"role": "user", "content": "hello <|endoftext|> world"}]
    n = estimate_tokens(msgs)  # must not raise
    assert n > 0


def test_estimate_tokens_long_special_literal():
    lit = "<|endoftext|>" * 50
    msgs = [{"role": "user", "content": "a" * 1000 + lit + "b" * 1000}]
    n = estimate_tokens(msgs)  # must not raise
    assert n > 0


def test_estimate_tokens_single_char():
    msgs = [{"role": "u", "content": "a"}]
    # 即使正文只有 1 字符，每条仍有固定开销
    assert estimate_tokens(msgs) >= _MESSAGE_OVERHEAD


# ---------------------------------------------------------------------------
# F2: _cjk_ratio removed (反例 / 正例 / 边界)
# ---------------------------------------------------------------------------


def test_cjk_ratio_removed_from_module():
    assert not hasattr(context, "_cjk_ratio")


def test_estimate_tokens_no_heuristic_path():
    # 间接证明不再走 _cjk_ratio 的 4 - cjk_ratio*2 公式（旧公式对中文显著低估）
    msgs = [{"role": "user", "content": "你好 world 混合测试"}]
    enc = _encoding()
    expected = (
        len(enc.encode_ordinary("user"))
        + len(enc.encode_ordinary("你好 world 混合测试"))
        + _MESSAGE_OVERHEAD
    )
    assert estimate_tokens(msgs) == expected


def test_no_cjk_ratio_refs_in_src():
    # 只扫 .py 源文件（.cover 等注释产物已清理，不计入源码引用）
    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "botflow"
    matches = []
    for p in root.rglob("*.py"):
        if "_cjk_ratio" in p.read_text(encoding="utf-8"):
            matches.append(str(p))
    assert matches == [], f"_cjk_ratio found in: {matches}"


# ---------------------------------------------------------------------------
# F3: encoder singleton via lru_cache (正例 / 正例 / 边界)
# ---------------------------------------------------------------------------


def test_encoding_singleton_returns_same_object():
    a = _encoding()
    b = _encoding()
    assert a is b


def test_encoding_loads_on_first_call():
    enc = _encoding()
    assert len(enc.encode_ordinary("x")) > 0


def test_encoding_lru_cache_maxsize_one():
    results = [_encoding() for _ in range(5)]
    assert all(r is results[0] for r in results)


# ---------------------------------------------------------------------------
# F4: byte upper-bound short-circuit (正例 / 正例 / 边界 / 边界 / 反例 / 正例 / 反例)
# ---------------------------------------------------------------------------


def test_truncate_zero_window_returns_as_is():
    msgs = [{"role": "user", "content": "x"}]
    assert truncate_to_context_window(msgs, 0) == msgs
    assert truncate_to_context_window(msgs, -5) == msgs


def test_truncate_upper_bound_fits():
    msgs = [{"role": "user", "content": "short"}]
    assert truncate_to_context_window(msgs, 100000) == msgs


def test_truncate_upper_bound_equal(monkeypatch):
    # 构造 _token_upper_bound == limit → 走短路、不进编码
    msgs = [{"role": "user", "content": "x" * 100}]
    ub = _token_upper_bound(msgs)
    calls = {"n": 0}
    orig = context.estimate_tokens

    def spy(messages):
        calls["n"] += 1
        return orig(messages)

    monkeypatch.setattr(context, "estimate_tokens", spy)
    out = truncate_to_context_window(msgs, context_window=ub + 1024, max_tokens=1024)
    assert out == msgs
    assert calls["n"] == 0  # 短路，未调用 estimate_tokens（未进编码）


def test_truncate_upper_bound_plus_one(monkeypatch):
    # 构造 _token_upper_bound == limit + 1 → 不短路、进编码/二分路径
    msgs = [{"role": "user", "content": "x" * 100}]
    ub = _token_upper_bound(msgs)
    calls = {"n": 0}
    orig = context.estimate_tokens

    def spy(messages):
        calls["n"] += 1
        return orig(messages)

    monkeypatch.setattr(context, "estimate_tokens", spy)
    truncate_to_context_window(msgs, context_window=(ub - 1) + 1024, max_tokens=1024)
    assert calls["n"] >= 1  # 进了编码路径


def test_truncate_oversize_gets_truncated():
    msgs = [{"role": "user", "content": "x" * 5000} for _ in range(5)]
    out = truncate_to_context_window(msgs, context_window=100, max_tokens=0)
    assert len(out) < len(msgs)


def test_truncate_keeps_system():
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


def test_upper_bound_covers_empty_messages(monkeypatch):
    # 集成期新增回归守卫：每条空 role/空 content 消息字节数为 0，
    # 若 _token_upper_bound 漏算 _MESSAGE_OVERHEAD，上界会误为 0 而短路放行。
    N = 1000
    msgs = [{"role": "", "content": ""} for _ in range(N)]
    assert estimate_tokens(msgs) == N * _MESSAGE_OVERHEAD
    assert _token_upper_bound(msgs) >= estimate_tokens(msgs)

    calls = {"n": 0}
    orig = context.estimate_tokens

    def spy(messages):
        calls["n"] += 1
        return orig(messages)

    monkeypatch.setattr(context, "estimate_tokens", spy)
    # context_window=1000, max_tokens=None → reserve=1024 → limit=max(1000-1024,1)=1
    # 上界=1000 > 1 → 必须进编码路径（字节为 0 不能短路放行）
    out = truncate_to_context_window(msgs, context_window=1000, max_tokens=None)
    assert calls["n"] >= 1  # 进了编码，证明上界计入了每条开销
    assert len(out) < N  # 且确实被截断


# ---------------------------------------------------------------------------
# F5: tiktoken hard dependency declared (正例 / 正例)
# ---------------------------------------------------------------------------


def test_tiktoken_importable_in_venv():
    import tiktoken

    assert hasattr(tiktoken, "get_encoding")


def test_pyproject_declares_tiktoken():
    p = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"
    text = p.read_text(encoding="utf-8")
    assert "tiktoken>=0.14.0" in text
