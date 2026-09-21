"""Tests for providers 层 —— 补齐未被覆盖的构造、惰性客户端、流式与错误分支。

覆盖 anthropic / google / openai_compat / deepseek 四个 provider 的
``client`` 惰性初始化、``chat`` / ``chat_stream`` / ``list_models``
以及各 provider 私有的统一格式转换函数；另含 base 的代理客户端工厂。
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from botflow.common.exceptions import ProviderError
from botflow.providers import anthropic_provider as anthropic_module
from botflow.providers import google_provider as google_module
from botflow.providers import openai_compat as compat_module
from botflow.providers.anthropic_provider import AnthropicProvider
from botflow.providers.base import _make_http_client
from botflow.providers.deepseek_provider import DeepSeekProvider
from botflow.providers.google_provider import GoogleProvider
from botflow.providers.openai_compat import OpenAICompatProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _AsyncIterable:
    """Minimal async iterable so ``async for`` works without a real SDK."""

    def __init__(self, items) -> None:
        self._items = items

    def __aiter__(self):
        async def gen():
            for item in self._items:
                yield item

        return gen()


class _FakeModelDump:
    def __init__(self, data: dict) -> None:
        self._data = data

    def model_dump(self) -> dict:
        return self._data


# ===========================================================================
# 1. base._make_http_client
# ===========================================================================


class TestMakeHttpClient:

    def test_no_proxy_returns_none(self):
        assert _make_http_client(None) is None
        assert _make_http_client({}) is None
        assert _make_http_client({"timeout": 10}) is None

    async def test_proxy_creates_async_client(self):
        client = _make_http_client({"proxy": "http://127.0.0.1:7890", "timeout": 5})
        try:
            assert isinstance(client, httpx.AsyncClient)
        finally:
            await client.aclose()

    def test_missing_httpx_returns_none(self, monkeypatch):
        # ``sys.modules[name] = None`` makes ``import httpx`` raise ImportError.
        monkeypatch.setitem(sys.modules, "httpx", None)
        assert _make_http_client({"proxy": "http://127.0.0.1:7890"}) is None


# ===========================================================================
# 2. AnthropicProvider
# ===========================================================================


class TestAnthropicProvider:

    def test_client_is_lazily_initialized_once(self, monkeypatch):
        created = MagicMock(name="AsyncAnthropic")
        monkeypatch.setattr(anthropic_module, "AsyncAnthropic", created)
        provider = AnthropicProvider("key-1", base_url="https://api.anthropic.com/",
                                     extra_config={"timeout": 7})

        first = provider.client
        second = provider.client

        assert first is second is created.return_value
        created.assert_called_once_with(
            api_key="key-1", base_url="https://api.anthropic.com", timeout=7,
        )

    async def test_chat_success(self):
        provider = AnthropicProvider("k")
        provider._client = MagicMock()
        provider._client.messages.create = AsyncMock(return_value=_FakeModelDump({
            "id": "msg_1",
            "content": [
                {"type": "thinking", "thinking": "let me think"},
                {"type": "text", "text": "the answer"},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 4,
                      "cache_read_input_tokens": 2},
        }))

        out = await provider.chat(
            [{"role": "system", "content": "be terse"},
             {"role": "user", "content": "hi"}],
            "claude-x",
            temperature=0.3,
            max_tokens=64,
        )

        assert out["provider"] == "anthropic"
        assert out["model"] == "claude-x"
        assert out["choices"][0]["message"]["content"] == "the answer"
        assert out["choices"][0]["message"]["reasoning_content"] == "let me think"
        assert out["usage"] == {
            "prompt_tokens": 10, "completion_tokens": 4,
            "total_tokens": 14, "cache_tokens": 2,
        }
        params = provider._client.messages.create.call_args.kwargs
        assert params["system"] == "be terse"
        assert params["temperature"] == 0.3
        assert params["max_tokens"] == 64

    async def test_chat_defaults_max_tokens_and_omits_optional_params(self):
        provider = AnthropicProvider("k")
        provider._client = MagicMock()
        provider._client.messages.create = AsyncMock(return_value=_FakeModelDump({
            "id": "msg_2", "content": [{"type": "text", "text": "x"}], "usage": {},
        }))

        await provider.chat([{"role": "user", "content": "hi"}], "claude-x")

        params = provider._client.messages.create.call_args.kwargs
        assert params["max_tokens"] == 4096
        assert "system" not in params
        assert "temperature" not in params

    async def test_chat_failure_raises_provider_error(self):
        provider = AnthropicProvider("k")
        provider._client = MagicMock()
        provider._client.messages.create = AsyncMock(side_effect=RuntimeError("429"))
        with pytest.raises(ProviderError, match="Anthropic request failed"):
            await provider.chat([{"role": "user", "content": "hi"}], "claude-x")

    async def test_chat_stream_yields_chunks(self):
        provider = AnthropicProvider("k")
        provider._client = MagicMock()
        events = [
            SimpleNamespace(type="message_start",
                            message=SimpleNamespace(id="msg_9", role="assistant")),
            SimpleNamespace(type="content_block_delta",
                            delta=SimpleNamespace(text="he")),
            SimpleNamespace(type="content_block_delta",
                            delta=SimpleNamespace(text="llo")),
            SimpleNamespace(type="message_delta",
                            delta=SimpleNamespace(stop_reason="end_turn"),
                            usage=SimpleNamespace(input_tokens=1, output_tokens=2,
                                                  cache_read_input_tokens=0)),
        ]

        class _StreamCtx:
            async def __aenter__(self):
                return _AsyncIterable(events)

            async def __aexit__(self, *exc):
                return False

        provider._client.messages.stream = MagicMock(return_value=_StreamCtx())

        chunks = [c async for c in provider.chat_stream(
            [{"role": "system", "content": "be terse"},
             {"role": "user", "content": "hi"}],
            "claude-x", temperature=0.4, max_tokens=16,
        )]

        assert chunks[0]["id"] == "msg_9"
        assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
        assert chunks[1]["choices"][0]["delta"] == {"content": "he"}
        assert chunks[-1]["choices"][0]["finish_reason"] == "end_turn"
        assert chunks[-1]["usage"]["total_tokens"] == 3

        params = provider._client.messages.stream.call_args.kwargs
        assert params["system"] == "be terse"
        assert params["temperature"] == 0.4
        assert params["max_tokens"] == 16

    async def test_chat_stream_omits_optional_params_by_default(self):
        provider = AnthropicProvider("k")
        provider._client = MagicMock()

        class _StreamCtx:
            async def __aenter__(self):
                return _AsyncIterable([])

            async def __aexit__(self, *exc):
                return False

        provider._client.messages.stream = MagicMock(return_value=_StreamCtx())
        assert [c async for c in provider.chat_stream(
            [{"role": "user", "content": "hi"}], "claude-x",
        )] == []
        params = provider._client.messages.stream.call_args.kwargs
        assert params["max_tokens"] == 4096
        assert "system" not in params
        assert "temperature" not in params

    def test_extract_system_converts_multimodal_content_blocks(self):
        provider = AnthropicProvider("k")
        system, filtered = provider._extract_system([
            {"role": "system", "content": [{"type": "text", "text": "sys text"}]},
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        ])
        assert system == "sys text"
        # 非 system 消息的 list 内容会被转成 Anthropic content blocks
        assert isinstance(filtered[0]["content"], list)
        assert filtered[0]["content"][0]["type"] == "text"

    async def test_chat_stream_failure_raises_provider_error(self):
        provider = AnthropicProvider("k")
        provider._client = MagicMock()

        class _BoomCtx:
            async def __aenter__(self):
                raise RuntimeError("socket closed")

            async def __aexit__(self, *exc):
                return False

        provider._client.messages.stream = MagicMock(return_value=_BoomCtx())
        with pytest.raises(ProviderError, match="Anthropic stream failed"):
            [c async for c in provider.chat_stream(
                [{"role": "user", "content": "hi"}], "claude-x",
            )]

    async def test_list_models_returns_configured_default(self):
        provider = AnthropicProvider("k", extra_config={"default_model": "claude-3-5"})
        assert await provider.list_models() == [{"id": "claude-3-5", "object": "model"}]

    # -- _event_to_chunk --------------------------------------------------

    def _provider(self):
        return AnthropicProvider("k")

    def test_event_to_chunk_unknown_type_returns_empty_delta(self):
        chunk = self._provider()._event_to_chunk(SimpleNamespace(type="ping"), "m")
        assert chunk["choices"][0]["delta"] == {}
        assert chunk["choices"][0]["finish_reason"] is None
        assert chunk["usage"] is None

    def test_event_to_chunk_message_start(self):
        event = SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(id="msg_1", role="assistant"),
        )
        chunk = self._provider()._event_to_chunk(event, "m")
        assert chunk["id"] == "msg_1"
        assert chunk["choices"][0]["delta"] == {"role": "assistant", "content": ""}

    def test_event_to_chunk_text_delta(self):
        event = SimpleNamespace(
            type="content_block_delta", delta=SimpleNamespace(text="hi"),
        )
        chunk = self._provider()._event_to_chunk(event, "m")
        assert chunk["choices"][0]["delta"] == {"content": "hi"}

    def test_event_to_chunk_thinking_delta(self):
        event = SimpleNamespace(
            type="content_block_delta", delta=SimpleNamespace(thinking="hmm"),
        )
        chunk = self._provider()._event_to_chunk(event, "m")
        assert chunk["choices"][0]["delta"] == {"reasoning_content": "hmm"}

    def test_event_to_chunk_message_delta_with_usage(self):
        event = SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="max_tokens"),
            usage=SimpleNamespace(input_tokens=3, output_tokens=1,
                                  cache_read_input_tokens=1),
        )
        chunk = self._provider()._event_to_chunk(event, "m")
        assert chunk["choices"][0]["finish_reason"] == "max_tokens"
        assert chunk["usage"] == {
            "prompt_tokens": 3, "completion_tokens": 1,
            "total_tokens": 4, "cache_tokens": 1,
        }

    def test_event_to_chunk_message_delta_without_usage(self):
        event = SimpleNamespace(
            type="message_delta", delta=SimpleNamespace(stop_reason="end_turn"),
        )
        chunk = self._provider()._event_to_chunk(event, "m")
        assert chunk["choices"][0]["finish_reason"] == "end_turn"
        assert chunk["usage"] is None

    def test_event_to_chunk_tool_use_start(self):
        event = SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(type="tool_use", id="toolu_1", name="search"),
        )
        chunk = self._provider()._event_to_chunk(event, "m")
        assert chunk["choices"][0]["delta"]["tool_calls"] == [{
            "id": "toolu_1",
            "type": "function",
            "function": {"name": "search", "arguments": ""},
        }]

    def test_event_to_chunk_non_tool_use_block_is_ignored(self):
        event = SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(type="text", text="x"),
        )
        chunk = self._provider()._event_to_chunk(event, "m")
        assert chunk["choices"][0]["delta"] == {}


# ===========================================================================
# 3. GoogleProvider
# ===========================================================================


class TestGoogleProvider:

    def test_client_is_lazily_initialized_once(self, monkeypatch):
        created = MagicMock(name="genai.Client")
        monkeypatch.setattr(google_module.genai, "Client", created)
        provider = GoogleProvider("g-key")

        assert provider.client is provider.client is created.return_value
        created.assert_called_once_with(api_key="g-key")

    async def test_chat_success(self):
        provider = GoogleProvider("k")
        provider._client = MagicMock()
        usage = SimpleNamespace(prompt_token_count=5, candidates_token_count=3,
                               total_token_count=8, cached_content_token_count=1)
        provider._client.aio.models.generate_content = AsyncMock(
            return_value=SimpleNamespace(text="hi there", usage_metadata=usage),
        )

        out = await provider.chat(
            [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
            "gemini-x",
            temperature=0.2,
            max_tokens=32,
        )

        assert out["provider"] == "google"
        assert out["choices"][0]["message"]["content"] == "hi there"
        assert out["usage"] == {
            "prompt_tokens": 5, "completion_tokens": 3,
            "total_tokens": 8, "cache_tokens": 1,
        }

    async def test_chat_failure_raises_provider_error(self):
        provider = GoogleProvider("k")
        provider._client = MagicMock()
        provider._client.aio.models.generate_content = AsyncMock(
            side_effect=RuntimeError("quota"),
        )
        with pytest.raises(ProviderError, match="Google Gemini request failed"):
            await provider.chat([{"role": "user", "content": "hi"}], "gemini-x")

    async def test_chat_stream_yields_unified_chunks(self):
        provider = GoogleProvider("k")
        provider._client = MagicMock()
        chunks = [
            SimpleNamespace(text="he", usage_metadata=None),
            SimpleNamespace(
                text="llo",
                usage_metadata=SimpleNamespace(
                    prompt_token_count=1, candidates_token_count=2,
                    total_token_count=3, cached_content_token_count=0,
                ),
            ),
        ]
        provider._client.aio.models.generate_content_stream = AsyncMock(
            return_value=_AsyncIterable(chunks),
        )

        out = [c async for c in provider.chat_stream(
            [{"role": "user", "content": "hi"}], "gemini-x",
        )]

        assert out[0]["choices"][0]["delta"] == {"content": "he"}
        assert out[0]["usage"] is None
        assert out[1]["usage"]["total_tokens"] == 3

    async def test_chat_stream_failure_raises_provider_error(self):
        provider = GoogleProvider("k")
        provider._client = MagicMock()
        provider._client.aio.models.generate_content_stream = AsyncMock(
            side_effect=RuntimeError("stream dead"),
        )
        with pytest.raises(ProviderError, match="Google Gemini stream failed"):
            [c async for c in provider.chat_stream(
                [{"role": "user", "content": "hi"}], "gemini-x",
            )]

    async def test_list_models_success(self):
        provider = GoogleProvider("k")
        provider._client = MagicMock()
        provider._client.aio.models.list = MagicMock(return_value=_AsyncIterable(
            [SimpleNamespace(name="models/gemini-2"), SimpleNamespace(name="models/gemini-3")],
        ))
        assert await provider.list_models() == [
            {"id": "models/gemini-2", "object": "model"},
            {"id": "models/gemini-3", "object": "model"},
        ]

    async def test_list_models_failure_returns_empty(self):
        provider = GoogleProvider("k")
        provider._client = MagicMock()
        provider._client.aio.models.list = MagicMock(
            side_effect=RuntimeError("no permission"),
        )
        assert await provider.list_models() == []

    # -- _build_config ----------------------------------------------------

    def test_build_config_with_all_options(self):
        provider = GoogleProvider("k")
        config = provider._build_config(0.5, 128, system="be nice")
        assert config.system_instruction == "be nice"
        assert config.temperature == 0.5
        assert config.max_output_tokens == 128

    def test_build_config_without_optional_options(self):
        provider = GoogleProvider("k")
        config = provider._build_config(None, None)
        assert config.system_instruction is None
        assert config.temperature is None
        assert config.max_output_tokens is None

    # -- _chunk_to_unified ------------------------------------------------

    def test_chunk_to_unified_without_usage(self):
        provider = GoogleProvider("k")
        out = provider._chunk_to_unified(
            SimpleNamespace(text="hi", usage_metadata=None), "gemini-x",
        )
        assert out["choices"][0]["delta"] == {"content": "hi"}
        assert out["usage"] is None

    def test_chunk_to_unified_ignores_zero_total_tokens(self):
        provider = GoogleProvider("k")
        usage = SimpleNamespace(prompt_token_count=0, candidates_token_count=0,
                               total_token_count=0, cached_content_token_count=0)
        out = provider._chunk_to_unified(
            SimpleNamespace(text="", usage_metadata=usage), "gemini-x",
        )
        assert out["usage"] is None

    def test_chunk_to_unified_with_usage(self):
        provider = GoogleProvider("k")
        usage = SimpleNamespace(prompt_token_count=2, candidates_token_count=4,
                               total_token_count=6, cached_content_token_count=0)
        out = provider._chunk_to_unified(
            SimpleNamespace(text="x", usage_metadata=usage), "gemini-x",
        )
        assert out["usage"] == {
            "prompt_tokens": 2, "completion_tokens": 4,
            "total_tokens": 6, "cache_tokens": 0,
        }


# ===========================================================================
# 4. OpenAICompatProvider
# ===========================================================================


class TestOpenAICompatProvider:

    def test_azure_client_is_lazily_initialized(self, monkeypatch):
        created = MagicMock(name="AsyncAzureOpenAI")
        monkeypatch.setattr(compat_module, "AsyncAzureOpenAI", created)
        provider = OpenAICompatProvider(
            "az-key", base_url="https://x.openai.azure.com",
            extra_config={"mode": "azure", "api_version": "2024-06-01", "timeout": 9},
        )

        assert provider.client is provider.client is created.return_value
        created.assert_called_once_with(
            api_key="az-key",
            azure_endpoint="https://x.openai.azure.com",
            api_version="2024-06-01",
            timeout=9,
            http_client=None,
        )

    def test_openai_client_is_lazily_initialized(self, monkeypatch):
        created = MagicMock(name="AsyncOpenAI")
        monkeypatch.setattr(compat_module, "AsyncOpenAI", created)
        provider = OpenAICompatProvider(
            "", base_url="http://x", extra_config={"timeout": 7}
        )
        assert provider._client is None

        assert provider.client is provider.client is created.return_value
        created.assert_called_once()
        kwargs = created.call_args.kwargs
        assert kwargs["api_key"] == "dummy"  # empty key falls back to dummy
        assert kwargs["base_url"] == "http://x"
        assert kwargs["timeout"] == 7
        assert kwargs["http_client"] is None

    async def test_list_models_success(self):
        provider = OpenAICompatProvider("k", base_url="http://x")
        provider._client = MagicMock()
        provider._client.models.list = AsyncMock(return_value=SimpleNamespace(
            data=[_FakeModelDump({"id": "m1"}), _FakeModelDump({"id": "m2"})],
        ))
        assert await provider.list_models() == [{"id": "m1"}, {"id": "m2"}]

    async def test_list_models_failure_returns_empty(self):
        provider = OpenAICompatProvider("k", base_url="http://x")
        provider._client = MagicMock()
        provider._client.models.list = AsyncMock(side_effect=RuntimeError("nope"))
        assert await provider.list_models() == []

    def test_to_unified_forwards_tool_and_function_calls(self):
        provider = OpenAICompatProvider("k")
        out = provider._to_unified({
            "id": "c1",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "hi",
                    "tool_calls": [{"id": "t1"}],
                    "function_call": {"name": "f", "arguments": "{}"},
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3,
                      "prompt_tokens_details": {"cached_tokens": 1}},
        }, "m")

        message = out["choices"][0]["message"]
        assert message["tool_calls"] == [{"id": "t1"}]
        assert message["function_call"] == {"name": "f", "arguments": "{}"}
        assert out["usage"]["cache_tokens"] == 1

    def test_chunk_to_unified_forwards_reasoning_tool_and_function_calls(self):
        provider = OpenAICompatProvider("k")
        out = provider._chunk_to_unified({
            "id": "c1",
            "choices": [{
                "index": 0,
                "delta": {
                    "reasoning_content": "thinking",
                    "tool_calls": [{"index": 0}],
                    "function_call": {"name": "f"},
                },
                "finish_reason": None,
            }],
        }, "m")

        delta = out["choices"][0]["delta"]
        assert delta["reasoning_content"] == "thinking"
        assert delta["tool_calls"] == [{"index": 0}]
        assert delta["function_call"] == {"name": "f"}
        assert out["usage"] is None

    def test_chunk_to_unified_attaches_final_usage(self):
        provider = OpenAICompatProvider("k")
        out = provider._chunk_to_unified({
            "id": "c1",
            "choices": [],
            "usage": {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10,
                      "prompt_tokens_details": {"cached_tokens": 2}},
        }, "m")
        assert out["usage"] == {
            "prompt_tokens": 4, "completion_tokens": 6,
            "total_tokens": 10, "cache_tokens": 2,
        }


# ===========================================================================
# 5. DeepSeekProvider
# ===========================================================================


class TestDeepSeekProvider:

    async def test_chat_stream_failure_raises_provider_error(self):
        provider = DeepSeekProvider("k")
        provider._client = MagicMock()
        provider._client.chat.completions.create = AsyncMock(
            side_effect=RuntimeError("connection reset"),
        )
        with pytest.raises(ProviderError, match="DeepSeek stream failed"):
            [c async for c in provider.chat_stream(
                [{"role": "user", "content": "hi"}], "deepseek-chat",
            )]

    async def test_list_models_success(self):
        provider = DeepSeekProvider("k", base_url="http://x")
        provider._client = MagicMock()
        provider._client.models.list = AsyncMock(return_value=SimpleNamespace(
            data=[SimpleNamespace(id="deepseek-chat"), SimpleNamespace(id="deepseek-reasoner")],
        ))
        assert await provider.list_models() == [
            {"id": "deepseek-chat", "object": "model"},
            {"id": "deepseek-reasoner", "object": "model"},
        ]

    async def test_list_models_failure_returns_empty(self):
        provider = DeepSeekProvider("k", base_url="http://x")
        provider._client = MagicMock()
        provider._client.models.list = AsyncMock(side_effect=RuntimeError("401"))
        assert await provider.list_models() == []

    async def test_health_check_ok(self):
        provider = DeepSeekProvider("k", base_url="http://x")
        provider._client = MagicMock()
        provider._client.models.list = AsyncMock(return_value=SimpleNamespace(
            data=[SimpleNamespace(id="m")],
        ))
        assert await provider.health_check() == {"status": "ok", "models": 1}

    async def test_health_check_reports_error_status(self, monkeypatch):
        provider = DeepSeekProvider("k", base_url="http://x")

        async def _boom():
            raise RuntimeError("down")

        monkeypatch.setattr(provider, "list_models", _boom)
        assert await provider.health_check() == {"status": "error", "error": "down"}
