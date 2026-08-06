"""Tests for streaming fallback: _stream_common tries endpoints in order.

Covers design.md error policy: 可重试错误重试后 fallback，不可重试错误立即 fallback，
以及首 chunk 之前的失败才会回退（已开始的流失败直接上报给客户端）。
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from botflow import core
from botflow.common.exceptions import AllModelsCooldownError, ProviderError
from botflow.router import CooldownManager, ModelEndpoint
from botflow.storage.models import GroupModelWithDetails


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class StubProvider:
    """chat_stream yields behavior items; Exception items are raised."""

    def __init__(self, behavior: list) -> None:
        self.behavior = behavior
        self.calls = 0

    async def chat_stream(self, *args, **kwargs):
        self.calls += 1
        result = self.behavior[min(self.calls - 1, len(self.behavior) - 1)]
        if isinstance(result, Exception):
            raise result
        for item in result:
            if isinstance(item, Exception):
                raise item
            yield item


def _make_endpoint(model_id: int, provider: StubProvider, max_retries: int = 3) -> ModelEndpoint:
    detail = GroupModelWithDetails(
        id=model_id,
        group_id=1,
        model_id=model_id,
        weight=1.0,
        is_enabled=True,
        model_name=f"model-{model_id}",
        display_name=f"model-{model_id}",
        provider_id=1,
        provider_name="test-provider",
        provider_type="openai",
        max_retries=max_retries,
        cooldown_seconds=60,
        cooldown_failure_threshold=3,
    )
    return ModelEndpoint(detail, provider)


class StubRouter:
    def __init__(self, endpoints: list[ModelEndpoint], route_error: Exception | None = None, fallback_group_id: int | None = None) -> None:
        self.endpoints = endpoints
        self.route_error = route_error
        self.fallback_group_id = fallback_group_id
        self.cooldown = CooldownManager()
        self.db = None

    async def route(self, **kwargs):
        if self.route_error:
            raise self.route_error
        return {
            "endpoints": self.endpoints,
            "group_id": 1,
            "messages": kwargs["messages"],
            "temperature": kwargs.get("temperature"),
            "max_tokens": kwargs.get("max_tokens"),
            "kwargs": {},
        }


def _serialize(chunk: dict) -> tuple[list[str], dict | None]:
    return [f"data: {chunk['content']}\n\n"], None


async def _collect(agen) -> list[str]:
    return [line async for line in agen]


@pytest.fixture
def env(monkeypatch):
    logs: list[dict] = []

    async def fake_log_call(**kwargs):
        logs.append(kwargs)

    monkeypatch.setattr(core, "_log_call", fake_log_call)
    return logs


def _setup(monkeypatch, router: StubRouter, logs: list[dict]) -> None:
    monkeypatch.setattr(
        core,
        "_get_extra_route_params",
        AsyncMock(return_value=(1, router, {})),
    )


# ---------------------------------------------------------------------------
# Fallback behavior
# ---------------------------------------------------------------------------


async def test_fallback_to_next_endpoint_on_pre_stream_failure(monkeypatch, env):
    """不可重试错误（400）→ 立即 fallback 到下一个模型。"""
    ep1 = _make_endpoint(1, StubProvider([ProviderError("OpenAICompat stream failed: HTTP 400 Bad Request")]))
    ep2 = _make_endpoint(2, StubProvider([[{"content": "a"}, {"content": "b"}]]))
    router = StubRouter([ep1, ep2])
    _setup(monkeypatch, router, env)

    out = await _collect(core._stream_common({"model": "x", "messages": []}, _serialize))

    assert out == ["data: a\n\n", "data: b\n\n", "data: [DONE]\n\n"]
    assert router.cooldown.get_failure_count(1, ep1.model_id) == 1  # 失败模型进入冷却计数
    assert router.cooldown.get_failure_count(1, ep2.model_id) == 0
    assert [entry for entry in env if entry["status"] == "success"][0]["model_id"] == ep2.model_id
    assert not any(entry["status"] == "error" for entry in env)


async def test_first_endpoint_used_when_healthy(monkeypatch, env):
    """首个模型正常 → 直接使用，不触发 fallback。"""
    ep1 = _make_endpoint(1, StubProvider([[{"content": "hello"}]]))
    ep2 = _make_endpoint(2, StubProvider([[{"content": "unused"}]]))
    router = StubRouter([ep1, ep2])
    _setup(monkeypatch, router, env)

    out = await _collect(core._stream_common({"model": "x", "messages": []}, _serialize))

    assert out == ["data: hello\n\n", "data: [DONE]\n\n"]
    assert ep2.provider.calls == 0
    assert [entry for entry in env if entry["status"] == "success"][0]["model_id"] == ep1.model_id
    assert router.cooldown.get_failure_count(1, ep1.model_id) == 0


async def test_all_endpoints_fail_emits_error_sse(monkeypatch, env):
    """全部模型失败 → 向上游返回 SSE error 事件。"""
    err = ProviderError("OpenAICompat stream failed: HTTP 400 Bad Request")
    ep1 = _make_endpoint(1, StubProvider([err]))
    ep2 = _make_endpoint(2, StubProvider([ProviderError("OpenAICompat stream failed: HTTP 500")]))
    router = StubRouter([ep1, ep2])
    _setup(monkeypatch, router, env)
    monkeypatch.setattr(core, "exponential_backoff", AsyncMock())

    out = await _collect(core._stream_common({"model": "x", "messages": []}, _serialize))

    assert out[-1] == "data: [DONE]\n\n"
    error_line = next(line for line in out if line.startswith("data: {") and '"error"' in line)
    assert '"type": "server_error"' in error_line
    assert "HTTP 500" in error_line  # 暴露的是最后一个模型的错误
    error_log = [entry for entry in env if entry["status"] == "error"]
    assert len(error_log) == 1
    assert error_log[0]["model_id"] is None
    assert error_log[0]["error_message"] == "OpenAICompat stream failed: HTTP 500"
    # 两个模型都被计入失败
    assert router.cooldown.get_failure_count(1, ep1.model_id) == 1
    assert router.cooldown.get_failure_count(1, ep2.model_id) == 1


async def test_empty_stream_falls_back_to_next(monkeypatch, env):
    """空流（无任何 chunk）视为失败，回退下一个模型。"""
    ep1 = _make_endpoint(1, StubProvider([[]]))  # 立刻 StopAsyncIteration
    ep2 = _make_endpoint(2, StubProvider([[{"content": "ok"}]]))
    router = StubRouter([ep1, ep2])
    _setup(monkeypatch, router, env)

    out = await _collect(core._stream_common({"model": "x", "messages": []}, _serialize))

    assert out == ["data: ok\n\n", "data: [DONE]\n\n"]
    assert router.cooldown.get_failure_count(1, ep1.model_id) == 1
    assert [entry for entry in env if entry["status"] == "success"][0]["model_id"] == ep2.model_id


async def test_retryable_error_retries_same_endpoint(monkeypatch, env):
    """可重试错误（5xx）→ 指数退避后重试同一模型，不立即 fallback。"""
    ep1 = _make_endpoint(1, StubProvider([ProviderError("OpenAICompat request failed: HTTP 500"), [{"content": "a"}, {"content": "b"}]]), max_retries=3)
    ep2 = _make_endpoint(2, StubProvider([[{"content": "unused"}]]))
    router = StubRouter([ep1, ep2])
    _setup(monkeypatch, router, env)
    monkeypatch.setattr(core, "exponential_backoff", AsyncMock())

    out = await _collect(core._stream_common({"model": "x", "messages": []}, _serialize))

    assert out == ["data: a\n\n", "data: b\n\n", "data: [DONE]\n\n"]
    assert ep1.provider.calls == 2
    assert ep2.provider.calls == 0
    assert router.cooldown.get_failure_count(1, ep1.model_id) == 0  # 成功后清零
    assert [entry for entry in env if entry["status"] == "success"][0]["model_id"] == ep1.model_id


async def test_mid_stream_failure_not_retried(monkeypatch, env):
    """流已开始后中途失败 → 不回退，直接上报 SSE error。"""
    ep1 = _make_endpoint(1, StubProvider([[{"content": "a"}, ProviderError("connection reset")]]))
    ep2 = _make_endpoint(2, StubProvider([[{"content": "b"}]]))
    router = StubRouter([ep1, ep2])
    _setup(monkeypatch, router, env)

    out = await _collect(core._stream_common({"model": "x", "messages": []}, _serialize))

    assert ep2.provider.calls == 0  # 绝不回退
    assert out[0] == "data: a\n\n"
    assert out[-1] == "data: [DONE]\n\n"
    error_log = [entry for entry in env if entry["status"] == "error"]
    assert len(error_log) == 1
    assert error_log[0]["model_id"] == ep1.model_id  # 记录实际使用的模型
    assert "connection reset" in error_log[0]["error_message"]


async def test_route_failure_emits_error_sse(monkeypatch, env):
    """路由层失败（如全部模型冷却）→ SSE error。"""
    router = StubRouter([], route_error=AllModelsCooldownError("Group 1: all models are on cooldown"))
    _setup(monkeypatch, router, env)

    out = await _collect(core._stream_common({"model": "x", "messages": []}, _serialize))

    assert out[-1] == "data: [DONE]\n\n"
    assert any('"type": "server_error"' in line for line in out)
    assert [entry for entry in env if entry["status"] == "error"][0]["model_id"] is None


async def test_stream_options_forwarded_to_provider(monkeypatch, env):
    """kwargs（如 stream_options）在回退路径上原样传给每个候选模型。"""
    ep1 = _make_endpoint(1, StubProvider([ProviderError("HTTP 400")]))
    ep2 = _make_endpoint(2, StubProvider([[{"content": "hi"}]]))
    router = StubRouter([ep1, ep2])
    _setup(monkeypatch, router, env)
    router.route_kwargs = {"stream_options": {"include_usage": True}}

    original_route = router.route

    async def route_with_kwargs(**kwargs):
        result = await original_route(**kwargs)
        result["kwargs"] = router.route_kwargs
        return result

    router.route = route_with_kwargs

    out = await _collect(core._stream_common({"model": "x", "messages": []}, _serialize))
    assert out == ["data: hi\n\n", "data: [DONE]\n\n"]


async def test_usage_chunk_recorded_in_success_log(monkeypatch, env):
    """携带 usage 的 chunk → 成功日志记录 usage。"""
    ep1 = _make_endpoint(1, StubProvider([[{"content": "a", "usage": {"prompt_tokens": 5}}]]))
    router = StubRouter([ep1])
    _setup(monkeypatch, router, env)

    def serialize_with_usage(chunk):
        return [f"data: {chunk['content']}\n\n"], chunk.get("usage")

    out = await _collect(core._stream_common({"model": "x", "messages": []}, serialize_with_usage))

    assert out == ["data: a\n\n", "data: [DONE]\n\n"]
    success_log = [entry for entry in env if entry["status"] == "success"][0]
    assert success_log["usage"] == {"prompt_tokens": 5}


async def test_fallback_group_used_when_all_endpoints_fail(monkeypatch, env):
    """组内全部模型失败 → 降级到 fallback group 重试（mimo 超时切 deepseek 的场景）。"""
    ep1 = _make_endpoint(1, StubProvider([ProviderError("Model model-1 timed out waiting for first chunk")]))
    ep2 = _make_endpoint(2, StubProvider([[{"content": "fallback-ok"}]]))
    router = StubRouter([ep1], fallback_group_id=4)
    _setup(monkeypatch, router, env)
    monkeypatch.setattr(core, "exponential_backoff", AsyncMock())

    # fallback group 的 router：endpoints 成功
    async def fallback_route(**kwargs):
        return {
            "endpoints": [ep2],
            "group_id": 4,
            "messages": kwargs["messages"],
            "temperature": kwargs.get("temperature"),
            "max_tokens": kwargs.get("max_tokens"),
            "kwargs": {},
        }

    fallback_router = StubRouter([ep2], fallback_group_id=None)
    fallback_router.route = fallback_route
    monkeypatch.setattr(core, "GroupRouter", lambda *a, **kw: fallback_router)

    out = await _collect(core._stream_common({"model": "x", "messages": []}, _serialize))

    assert out == ["data: fallback-ok\n\n", "data: [DONE]\n\n"]
    success_log = [entry for entry in env if entry["status"] == "success"]
    assert success_log[0]["model_id"] == ep2.model_id
    assert not any(entry["status"] == "error" for entry in env)
    # 主 group 的模型被计入失败
    assert router.cooldown.get_failure_count(1, ep1.model_id) == 1


async def test_fallback_group_failure_emits_error_sse(monkeypatch, env):
    """主组与 fallback group 都失败 → 上报 SSE error。"""
    ep1 = _make_endpoint(1, StubProvider([ProviderError("HTTP 400")]))
    ep2 = _make_endpoint(2, StubProvider([ProviderError("HTTP 500")]))
    router = StubRouter([ep1], fallback_group_id=4)
    _setup(monkeypatch, router, env)
    monkeypatch.setattr(core, "exponential_backoff", AsyncMock())

    async def fallback_route(**kwargs):
        return {
            "endpoints": [ep2],
            "group_id": 4,
            "messages": kwargs["messages"],
            "temperature": kwargs.get("temperature"),
            "max_tokens": kwargs.get("max_tokens"),
            "kwargs": {},
        }

    fallback_router = StubRouter([ep2], fallback_group_id=None)
    fallback_router.route = fallback_route
    monkeypatch.setattr(core, "GroupRouter", lambda *a, **kw: fallback_router)

    out = await _collect(core._stream_common({"model": "x", "messages": []}, _serialize))

    assert out[-1] == "data: [DONE]\n\n"
    error_line = next(line for line in out if line.startswith("data: {") and '"error"' in line)
    assert '"type": "server_error"' in error_line
    error_log = [entry for entry in env if entry["status"] == "error"]
    assert len(error_log) == 1
    # 暴露 fallback group 最后一个模型的错误
    assert "HTTP 500" in error_log[0]["error_message"]


async def test_stream_timeout_from_request_overrides_default(monkeypatch, env):
    """请求可自定义首 chunk 超时；超时后回退下一个模型。"""
    ep1 = _make_endpoint(1, StubProvider([TimeoutError()]))  # 直接抛超时
    ep2 = _make_endpoint(2, StubProvider([[{"content": "ok"}]]))
    router = StubRouter([ep1, ep2])
    _setup(monkeypatch, router, env)
    monkeypatch.setattr(core, "exponential_backoff", AsyncMock())

    out = await _collect(core._stream_common({"model": "x", "messages": [], "stream_timeout": 0.1}, _serialize))

    assert out == ["data: ok\n\n", "data: [DONE]\n\n"]
    assert router.cooldown.get_failure_count(1, ep1.model_id) == 1


async def test_serialize_error_emits_error_sse_no_fallback(monkeypatch, env):
    """序列化异常（botflow 侧 chunk 形状问题）→ SSE error，且不回退到其他模型。"""
    ep1 = _make_endpoint(1, StubProvider([[{"content": "a"}]]))

    def bad_serialize(chunk):
        raise ValueError("unexpected chunk shape")

    router = StubRouter([ep1])
    _setup(monkeypatch, router, env)

    out = await _collect(core._stream_common({"model": "x", "messages": []}, bad_serialize))

    assert out[-1] == "data: [DONE]\n\n"
    assert any('"type": "server_error"' in line for line in out)
    error_log = [entry for entry in env if entry["status"] == "error"]
    assert len(error_log) == 1
    assert error_log[0]["model_id"] == ep1.model_id
