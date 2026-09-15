"""Tests for ``extra_config["headers"]`` support in the openai_compat provider.

Covers docs/tasks/P8-1_headers_features.md:

- F1 ``extra_config["headers"]`` is passed to the upstream request as
  ``extra_headers`` (and is ``None`` when unconfigured, preserving the old
  behaviour for every provider that does not use it).
- F2 the ``{conversation_id}`` placeholder resolves to a stable digest of the
  conversation, so upstreams like OpenCode Go see one session per conversation
  instead of one shared by all traffic.
- F3 backwards compatibility: response handling and error wrapping unchanged.

A fake client is injected in place of ``provider._client`` so no network call
happens; the fake records the kwargs of every ``create()`` call.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from botflow.common.exceptions import ProviderError
from botflow.providers.openai_compat import (
    OpenAICompatProvider,
    _apply_headers,
    _conversation_id,
)

_CHAT_DATA = {
    "id": "chatcmpl-1",
    "model": "m",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}

_CHUNK_DATA = {
    "id": "chatcmpl-1",
    "choices": [
        {"index": 0, "delta": {"role": "assistant", "content": "hi"}, "finish_reason": None}
    ],
}


class _Recorded:
    """Return value whose ``model_dump()`` is the raw payload."""

    def __init__(self, data):
        self._data = data

    def model_dump(self):
        return self._data


class _AsyncIter:
    def __init__(self, items):
        self._items = items

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for item in self._items:
            yield item


class _FakeCompletions:
    def __init__(self, response, error=None):
        self.calls: list[dict] = []
        self._response = response
        self._error = error

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


class _FakeClient:
    def __init__(self, response, error=None):
        self.completions = _FakeCompletions(response, error)
        self.chat = SimpleNamespace(completions=self.completions)

    @property
    def last_headers(self):
        return self.completions.calls[-1].get("extra_headers")


def _provider(headers=None, **extra):
    cfg = dict(extra)
    if headers is not None:
        cfg["headers"] = headers
    p = OpenAICompatProvider(
        api_key="k", base_url="https://test.example.com/v1", extra_config=cfg
    )
    return p


def _install(provider, response=None, error=None):
    client = _FakeClient(response, error)
    provider._client = client
    return client


USER = [{"role": "user", "content": "hello"}]


# ---------------------------------------------------------------------------
# F1 — header passthrough
# ---------------------------------------------------------------------------


class TestHeaderPassthrough:
    @pytest.mark.asyncio
    async def test_chat_passes_headers(self):
        p = _provider(headers={"X-A": "1"})
        c = _install(p, _Recorded(_CHAT_DATA))
        await p.chat(USER, "m")
        assert c.last_headers == {"X-A": "1"}

    @pytest.mark.asyncio
    async def test_chat_stream_passes_headers(self):
        p = _provider(headers={"X-A": "1"})
        c = _install(p, _AsyncIter([_Recorded(_CHUNK_DATA)]))
        async for _ in p.chat_stream(USER, "m"):
            pass
        assert c.last_headers == {"X-A": "1"}

    @pytest.mark.asyncio
    async def test_non_str_values_coerced(self):
        p = _provider(headers={"X-N": 42, "X-B": True})
        c = _install(p, _Recorded(_CHAT_DATA))
        await p.chat(USER, "m")
        assert c.last_headers == {"X-N": "42", "X-B": "True"}

    @pytest.mark.asyncio
    async def test_multiple_headers_all_kept(self):
        p = _provider(headers={"A": "1", "B": "2", "C": "3"})
        c = _install(p, _Recorded(_CHAT_DATA))
        await p.chat(USER, "m")
        assert c.last_headers == {"A": "1", "B": "2", "C": "3"}

    @pytest.mark.asyncio
    async def test_unconfigured_is_none(self):
        p = _provider()
        c = _install(p, _Recorded(_CHAT_DATA))
        await p.chat(USER, "m")
        assert c.last_headers is None

    @pytest.mark.asyncio
    async def test_explicit_none_headers(self):
        p = _provider(headers=None)
        c = _install(p, _Recorded(_CHAT_DATA))
        await p.chat(USER, "m")
        assert c.last_headers is None

    @pytest.mark.asyncio
    async def test_empty_dict_headers(self):
        p = _provider(headers={})
        c = _install(p, _Recorded(_CHAT_DATA))
        await p.chat(USER, "m")
        assert c.last_headers is None

    @pytest.mark.asyncio
    async def test_non_dict_headers_ignored(self):
        """A malformed config must not crash the request path."""
        p = _provider(headers=["not", "a", "dict"])
        c = _install(p, _Recorded(_CHAT_DATA))
        await p.chat(USER, "m")
        assert c.last_headers is None

    def test_extra_config_default_is_empty(self):
        p = OpenAICompatProvider(api_key="k")
        assert _apply_headers(p.extra_config, USER) is None


# ---------------------------------------------------------------------------
# F2 — {conversation_id} placeholder
# ---------------------------------------------------------------------------


class TestConversationIdPlaceholder:
    def test_single_placeholder_substituted(self):
        out = _apply_headers({"headers": {"x-s": "{conversation_id}"}}, USER)
        assert out["x-s"] == _conversation_id(USER)
        assert len(out["x-s"]) == 32
        assert "{" not in out["x-s"] and "}" not in out["x-s"]

    def test_surrounding_text_preserved(self):
        out = _apply_headers({"headers": {"x-s": "sess-{conversation_id}-x"}}, USER)
        assert out["x-s"] == "sess-%s-x" % _conversation_id(USER)

    def test_stable_across_turns(self):
        """Turn 5 is turn 1 with more messages appended — same session id."""
        turn1 = [{"role": "user", "content": "hello"}]
        turn5 = turn1 + [
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "more"},
            {"role": "assistant", "content": "sure"},
        ]
        assert _conversation_id(turn1) == _conversation_id(turn5)

    def test_different_conversations_differ(self):
        a = [{"role": "user", "content": "hello"}]
        b = [{"role": "user", "content": "goodbye"}]
        assert _conversation_id(a) != _conversation_id(b)

    def test_system_prompt_distinguishes(self):
        """Same first user message under a different system prompt is distinct."""
        a = [{"role": "system", "content": "agent A"}, {"role": "user", "content": "hi"}]
        b = [{"role": "system", "content": "agent B"}, {"role": "user", "content": "hi"}]
        assert _conversation_id(a) != _conversation_id(b)

    def test_system_prompt_does_not_collapse(self):
        """A shared system prompt alone must not make all sessions identical."""
        sys_msg = {"role": "system", "content": "you are a coding agent"}
        a = [sys_msg, {"role": "user", "content": "one"}]
        b = [sys_msg, {"role": "user", "content": "two"}]
        assert _conversation_id(a) != _conversation_id(b)

    def test_no_user_message(self):
        out = _apply_headers({"headers": {"x-s": "{conversation_id}"}}, [{"role": "system", "content": "s"}])
        assert len(out["x-s"]) == 32

    def test_empty_messages(self):
        out = _apply_headers({"headers": {"x-s": "{conversation_id}"}}, [])
        assert len(out["x-s"]) == 32

    def test_multimodal_first_user_matches_plain_text(self):
        """List content hashes the same as its extracted plain text."""
        multimodal = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
                ],
            }
        ]
        assert _conversation_id(multimodal) == _conversation_id(
            [{"role": "user", "content": "look"}]
        )

    def test_none_content(self):
        out = _apply_headers({"headers": {"x-s": "{conversation_id}"}}, [{"role": "user", "content": None}])
        assert len(out["x-s"]) == 32

    def test_static_header_kept_alongside_placeholder(self):
        out = _apply_headers(
            {"headers": {"x-s": "{conversation_id}", "User-Agent": "botflow/1.0"}}, USER
        )
        assert out["User-Agent"] == "botflow/1.0"
        assert out["x-s"] == _conversation_id(USER)

    def test_unclosed_brace_not_replaced(self):
        out = _apply_headers({"headers": {"x-s": "{conversation_id"}}, USER)
        assert out["x-s"] == "{conversation_id"

    def test_non_placeholder_value_untouched(self):
        out = _apply_headers({"headers": {"x-s": "static-session"}}, USER)
        assert out["x-s"] == "static-session"


# ---------------------------------------------------------------------------
# F3 — backwards compatibility
# ---------------------------------------------------------------------------


class TestBackwardsCompatible:
    @pytest.mark.asyncio
    async def test_chat_unified_output(self):
        p = _provider()
        _install(p, _Recorded(_CHAT_DATA))
        result = await p.chat(USER, "m")
        assert result["choices"][0]["message"]["content"] == "ok"
        assert result["usage"]["total_tokens"] == 2

    @pytest.mark.asyncio
    async def test_chat_stream_yields_chunks(self):
        p = _provider()
        _install(p, _AsyncIter([_Recorded(_CHUNK_DATA)]))
        chunks = [c async for c in p.chat_stream(USER, "m")]
        assert len(chunks) == 1
        assert chunks[0]["choices"][0]["delta"]["content"] == "hi"

    @pytest.mark.asyncio
    async def test_chat_error_wrapped(self):
        p = _provider()
        _install(p, error=RuntimeError("boom"))
        with pytest.raises(ProviderError, match="boom"):
            await p.chat(USER, "m")

    @pytest.mark.asyncio
    async def test_chat_stream_error_wrapped(self):
        p = _provider()
        _install(p, error=RuntimeError("boom"))
        with pytest.raises(ProviderError, match="boom"):
            async for _ in p.chat_stream(USER, "m"):
                pass

    @pytest.mark.asyncio
    async def test_stream_options_default_injected(self):
        """Pre-existing stream_options behaviour is untouched by the change."""
        p = _provider()
        c = _install(p, _AsyncIter([_Recorded(_CHUNK_DATA)]))
        async for _ in p.chat_stream(USER, "m"):
            pass
        assert c.completions.calls[-1]["stream_options"] == {"include_usage": True}
