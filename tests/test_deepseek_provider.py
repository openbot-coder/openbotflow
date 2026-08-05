"""Tests for DeepSeekProvider (OpenAI-compatible client, configurable base_url)."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from botflow.common.exceptions import ProviderError
from botflow.providers.deepseek_provider import DeepSeekProvider, _DEFAULT_BASE_URL

_normalize_chat_response = DeepSeekProvider._normalize_chat_response
_normalize_stream_response = DeepSeekProvider._normalize_stream_response


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestInit:
    def test_requires_api_key(self):
        with pytest.raises(ValueError, match="api_key"):
            DeepSeekProvider(api_key="")

    def test_default_base_url(self):
        p = DeepSeekProvider(api_key="sk-x")
        assert p.base_url == _DEFAULT_BASE_URL
        assert str(p._client.base_url) == _DEFAULT_BASE_URL

    def test_custom_base_url(self):
        p = DeepSeekProvider(api_key="sk-x", base_url="https://opencode.ai/zen/go/v1/")
        assert p.base_url == "https://opencode.ai/zen/go/v1"
        assert "opencode.ai" in str(p._client.base_url)

    def test_timeout_from_extra_config(self):
        p = DeepSeekProvider(api_key="sk-x", extra_config={"timeout": 30.0})
        assert p._client.timeout == 30.0


# ---------------------------------------------------------------------------
# _build_chat_kwargs whitelist
# ---------------------------------------------------------------------------


class TestBuildChatKwargs:
    def _provider(self):
        return DeepSeekProvider(api_key="sk-x")

    def test_whitelisted_and_special_kwargs_forwarded(self):
        p = self._provider()
        kw = p._build_chat_kwargs(
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.5,
            max_tokens=100,
            stream=True,
            tools=[{"type": "function"}],
            tool_choice="auto",
            response_format={"type": "json_object"},
            stop=["END"],
            top_p=0.9,
        )
        assert kw["model"] == "deepseek-v4-flash"
        assert kw["stream"] is True
        assert kw["temperature"] == 0.5
        assert kw["max_tokens"] == 100
        assert kw["tools"] == [{"type": "function"}]
        assert kw["tool_choice"] == "auto"
        assert kw["response_format"] == {"type": "json_object"}
        assert kw["stop"] == ["END"]
        assert kw["top_p"] == 0.9

    def test_unknown_kwargs_dropped(self):
        p = self._provider()
        kw = p._build_chat_kwargs(
            model="m",
            messages=[],
            temperature=None,
            max_tokens=None,
            stream=False,
            evil_param="x",
            extra_headers={"X": "1"},
        )
        assert "evil_param" not in kw
        assert "extra_headers" not in kw
        assert "temperature" not in kw  # None 不发送
        assert "max_tokens" not in kw

    def test_non_stream_messages_passthrough(self):
        p = self._provider()
        msgs = [{"role": "user", "content": "hi"}]
        kw = p._build_chat_kwargs(model="m", messages=msgs, temperature=None, max_tokens=None, stream=False)
        assert kw["messages"] is msgs  # 原样透传，不改写


# ---------------------------------------------------------------------------
# Response normalization (reasoning_content)
# ---------------------------------------------------------------------------


class TestNormalize:
    def test_content_empty_falls_back_to_reasoning(self):
        data = {
            "choices": [{"message": {"role": "assistant", "content": "", "reasoning_content": "thinking..."}}],
        }
        out = _normalize_chat_response(data, "deepseek-v4-flash")
        msg = out["choices"][0]["message"]
        assert msg["content"] == "thinking..."
        assert msg["reasoning_content"] is None

    def test_reasoning_kept_when_content_present(self):
        data = {
            "choices": [{"message": {"role": "assistant", "content": "answer", "reasoning_content": "thinking..."}}],
        }
        out = _normalize_chat_response(data, "m")
        msg = out["choices"][0]["message"]
        assert msg["content"] == "answer"
        assert msg["reasoning_content"] == "thinking..."

    def test_stream_chunk_keeps_reasoning_separate(self):
        data = {"choices": [{"delta": {"content": "", "reasoning_content": "thinking..."}}]}
        out = _normalize_stream_response(data, "m")
        assert out["model"] == "m"
        assert out["choices"][0]["delta"]["reasoning_content"] == "thinking..."

    def test_no_choices_structure(self):
        assert _normalize_chat_response({}, "m")["choices"] == []


# ---------------------------------------------------------------------------
# chat / chat_stream with mocked client
# ---------------------------------------------------------------------------


class TestCalls:
    def _provider(self, create_mock):
        p = DeepSeekProvider(api_key="sk-x", base_url="https://opencode.ai/zen/go/v1/")
        p._client = MagicMock()
        p._client.chat.completions.create = create_mock
        return p

    @pytest.mark.asyncio
    async def test_chat_calls_create_and_normalizes(self):
        resp = MagicMock()
        resp.model_dump.return_value = {
            "id": "x",
            "model": "deepseek-v4-flash",
            "choices": [{"message": {"role": "assistant", "content": "", "reasoning_content": "think"}}],
        }
        create = AsyncMock(return_value=resp)
        p = self._provider(create)

        out = await p.chat([{"role": "user", "content": "hi"}], "deepseek-v4-flash")

        assert out["choices"][0]["message"]["content"] == "think"
        assert create.call_args.kwargs["stream"] is False
        assert create.call_args.kwargs["model"] == "deepseek-v4-flash"

    @pytest.mark.asyncio
    async def test_chat_error_wrapped(self):
        create = AsyncMock(side_effect=RuntimeError("boom"))
        p = self._provider(create)
        with pytest.raises(ProviderError, match="DeepSeek request failed: boom"):
            await p.chat([{"role": "user", "content": "hi"}], "m")

    @pytest.mark.asyncio
    async def test_chat_stream_yields_chunks(self):
        chunks = [
            MagicMock(model_dump=lambda: {"choices": [{"delta": {"content": "a", "reasoning_content": None}}]}),
            MagicMock(model_dump=lambda: {"choices": [{"delta": {"content": "b", "reasoning_content": None}}]}),
        ]

        async def _agen():
            for c in chunks:
                yield c

        create = AsyncMock(return_value=_agen())
        p = self._provider(create)

        got = [c async for c in p.chat_stream([{"role": "user", "content": "hi"}], "m")]

        assert len(got) == 2
        assert got[0]["choices"][0]["delta"]["content"] == "a"
        assert create.call_args.kwargs["stream"] is True

    @pytest.mark.asyncio
    async def test_chat_stream_skips_chunks_without_choices(self):
        async def _agen():
            yield MagicMock(model_dump=lambda: {"id": "x"})  # 无 choices，跳过

        create = AsyncMock(return_value=_agen())
        p = self._provider(create)

        got = [c async for c in p.chat_stream([{"role": "user", "content": "hi"}], "m")]
        assert got == []

    @pytest.mark.asyncio
    async def test_list_models(self):
        m1 = MagicMock()
        m1.id = "deepseek-v4-flash"
        resp = MagicMock()
        resp.data = [m1]
        p = DeepSeekProvider(api_key="sk-x")
        p._client = MagicMock()
        p._client.models.list = AsyncMock(return_value=resp)

        assert await p.list_models() == [{"id": "deepseek-v4-flash", "object": "model"}]
