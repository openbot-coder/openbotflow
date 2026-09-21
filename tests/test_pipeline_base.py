"""Tests for P1-2: Pipeline skeleton — BaseStrategy + _shared.py.

Covers TC-01 through TC-39 from docs/tasks/P1-2_tests.md.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from botflow.pipeline.base import (
    BaseStrategy,
    RouteResult,
    STRATEGY_REGISTRY,
    StrategyError,
    register_strategy,
)
from botflow.pipeline._shared import (
    load_endpoints,
    filter_available,
    call_llm,
    truncate_messages,
    invalidate_endpoint_cache,
    _apply_model_extra_config,
    _ensure_provider_semaphore,
    _endpoint_cache,
    _provider_semaphores,
    _ENDPOINT_CACHE_TTL,
)
from botflow.router import CooldownManager, ModelEndpoint
from botflow.storage.models import GroupModelWithDetails
from botflow.common.exceptions import ProviderError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_caches():
    """每个测试前后清空全局缓存，避免测试间污染。"""
    _endpoint_cache.clear()
    _provider_semaphores.clear()
    yield
    _endpoint_cache.clear()
    _provider_semaphores.clear()


@pytest.fixture
def cooldown():
    return CooldownManager()


@pytest.fixture
def sample_model_detail():
    """创建一个最小可用的 GroupModelWithDetails。"""
    return GroupModelWithDetails(
        id=1,
        group_id=1,
        model_id=100,
        weight=1.0,
        is_enabled=True,
        model_name="gpt-4o",
        display_name="GPT-4o",
        api_format="",
        provider_id=1,
        provider_name="openai-main",
        provider_type="openai",
        max_retries=2,
        cooldown_seconds=60,
        cooldown_failure_threshold=3,
        context_window=128000,
        proxy="",
        extra_config={},
    )


@pytest.fixture
def sample_endpoint(sample_model_detail):
    """创建一个带 mock provider 的 ModelEndpoint。"""
    mock_provider = MagicMock()
    return ModelEndpoint(sample_model_detail, mock_provider)


# ---------------------------------------------------------------------------
# 一、RouteResult 测试 (TC-01 ~ TC-03)
# ---------------------------------------------------------------------------


# TC-01: 创建 RouteResult 并访问字段
def test_route_result_fields(sample_endpoint):
    msgs = [{"role": "user", "content": "hello"}]
    result = RouteResult(
        endpoints=[sample_endpoint],
        messages=msgs,
        temperature=0.7,
        max_tokens=1024,
        extra_kwargs={"reasoning_mode": "off"},
    )
    assert result.endpoints == [sample_endpoint]
    assert result.messages == msgs
    assert result.temperature == 0.7
    assert result.max_tokens == 1024
    assert result.extra_kwargs == {"reasoning_mode": "off"}


# TC-02: endpoints 为空列表
def test_route_result_empty_endpoints():
    result = RouteResult(
        endpoints=[],
        messages=[],
        temperature=None,
        max_tokens=None,
        extra_kwargs={},
    )
    assert result.endpoints == []
    assert len(result.endpoints) == 0


# TC-03: RouteResult 是不可变的
def test_route_result_immutable(sample_endpoint):
    result = RouteResult(
        endpoints=[sample_endpoint],
        messages=[],
        temperature=None,
        max_tokens=None,
        extra_kwargs={},
    )
    with pytest.raises(AttributeError):
        result.endpoints = []


# ---------------------------------------------------------------------------
# 二、StrategyError 测试 (TC-04 ~ TC-05)
# ---------------------------------------------------------------------------


# TC-04: 抛出 StrategyError
def test_raise_strategy_error():
    with pytest.raises(StrategyError, match="test error"):
        raise StrategyError("test error")


# TC-05: StrategyError 继承关系
def test_strategy_error_inheritance():
    from botflow.common.exceptions import BotflowError
    err = StrategyError("msg")
    assert isinstance(err, Exception)
    assert not isinstance(err, BotflowError)


# ---------------------------------------------------------------------------
# 三、BaseStrategy 测试 (TC-06 ~ TC-10)
# ---------------------------------------------------------------------------


# TC-06: 抽象类不能直接实例化
def test_base_strategy_cannot_instantiate():
    with pytest.raises(TypeError, match="abstract method"):
        BaseStrategy(params={})


# TC-07: 子类必须实现 select_endpoints
class IncompleteStrategy(BaseStrategy):
    pass


def test_incomplete_strategy_cannot_instantiate():
    with pytest.raises(TypeError, match="abstract method"):
        IncompleteStrategy(params={})


# TC-08: 完整子类可以实例化
class DummyStrategy(BaseStrategy):
    async def select_endpoints(self, messages, db, cooldown, group_id,
                                temperature=None, max_tokens=None, **kwargs):
        return RouteResult(
            endpoints=[], messages=messages,
            temperature=temperature, max_tokens=max_tokens, extra_kwargs=kwargs,
        )


def test_complete_strategy_instantiation():
    s = DummyStrategy(params={"key": "value"})
    assert s.params == {"key": "value"}


# TC-09: 默认 execute 方法调用 select_endpoints + call_llm 循环
class TwoEndpointStrategy(BaseStrategy):
    async def select_endpoints(self, messages, db, cooldown, group_id,
                                temperature=None, max_tokens=None, **kwargs):
        return RouteResult(
            endpoints=self._eps, messages=messages,
            temperature=temperature, max_tokens=max_tokens, extra_kwargs=kwargs,
        )


@pytest.mark.asyncio
async def test_execute_returns_first_success():
    ep1 = MagicMock()
    ep1.detail = MagicMock()
    ep1.detail.extra_config = {}
    ep1.detail.provider_id = 1
    ep1.detail.model_name = "model-a"
    ep1.detail.context_window = 128000

    ep2 = MagicMock()
    ep2.detail = MagicMock()
    ep2.detail.extra_config = {}
    ep2.detail.provider_id = 1
    ep2.detail.model_name = "model-b"
    ep2.detail.context_window = 128000

    strategy = TwoEndpointStrategy(params={})
    strategy._eps = [ep1, ep2]

    cooldown = CooldownManager()
    mock_db = MagicMock()

    # call_llm: ep1 成功，ep2 不应被调用
    with patch("botflow.pipeline._shared.call_llm", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = ({"choices": [{"message": {"content": "ok"}}]}, None)
        result = await strategy.execute(
            messages=[{"role": "user", "content": "hi"}],
            db=mock_db, cooldown=cooldown, group_id=1,
        )
        assert mock_call.call_count == 1
        assert result["choices"][0]["message"]["content"] == "ok"


# TC-10: execute 所有 endpoint 失败时抛出 ProviderError
@pytest.mark.asyncio
async def test_execute_all_fail_raises():
    strategy = TwoEndpointStrategy(params={})
    strategy._eps = [MagicMock(), MagicMock()]

    with patch("botflow.pipeline._shared.call_llm", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = (None, ProviderError("All endpoints failed"))
        with pytest.raises(ProviderError, match="All endpoints failed"):
            await strategy.execute(
                messages=[], db=MagicMock(),
                cooldown=CooldownManager(), group_id=1,
            )


# ---------------------------------------------------------------------------
# 四、STRATEGY_REGISTRY 测试 (TC-11 ~ TC-13)
# ---------------------------------------------------------------------------


# TC-11: register_strategy 注册成功
def test_register_strategy_success():
    before = set(STRATEGY_REGISTRY.keys())
    # 创建一个临时策略类
    class TempStrategy(BaseStrategy):
        async def select_endpoints(self, *args, **kwargs):
            pass
    register_strategy("_test_temp_strategy", TempStrategy)
    assert "_test_temp_strategy" in STRATEGY_REGISTRY
    assert STRATEGY_REGISTRY["_test_temp_strategy"] is TempStrategy
    # 清理
    del STRATEGY_REGISTRY["_test_temp_strategy"]


# TC-12: 重复注册同名策略报错
def test_register_duplicate_strategy_raises():
    class DummyA(BaseStrategy):
        async def select_endpoints(self, *args, **kwargs):
            pass
    register_strategy("_test_dup", DummyA)
    try:
        with pytest.raises(ValueError, match="already registered"):
            register_strategy("_test_dup", DummyA)
    finally:
        del STRATEGY_REGISTRY["_test_dup"]


# TC-13: 未知策略名查找返回 None
def test_unknown_strategy_returns_none():
    assert STRATEGY_REGISTRY.get("nonexistent_strategy_xyz") is None


# ---------------------------------------------------------------------------
# 五、load_endpoints 测试 (TC-14 ~ TC-18)
# ---------------------------------------------------------------------------


# TC-14: 正常加载 group_models
@pytest.mark.asyncio
async def test_load_endpoints_normal():
    db = AsyncMock()

    model = GroupModelWithDetails(
        id=1, group_id=1, model_id=100, weight=1.0, is_enabled=True,
        model_name="gpt-4o", display_name="GPT-4o", api_format="",
        provider_id=1, provider_name="openai", provider_type="openai",
        max_retries=3, cooldown_seconds=60, cooldown_failure_threshold=3,
        context_window=128000, proxy="", extra_config={},
    )
    provider = MagicMock()
    provider.id = 1
    provider.provider_type = "openai"
    provider.api_key = "test-key"
    provider.base_url = "https://api.openai.com/v1"
    provider.extra_config = {}
    provider.is_enabled = True

    db.get_group_models = AsyncMock(return_value=[model])
    db.get_provider = AsyncMock(return_value=provider)

    endpoints = await load_endpoints(group_id=1, db=db)
    assert len(endpoints) == 1
    assert endpoints[0].model_id == 100


# TC-15: 缓存命中（60s TTL）
@pytest.mark.asyncio
async def test_load_endpoints_cache_hit():
    db = AsyncMock()

    fake_ep = [MagicMock(spec=ModelEndpoint)]
    _endpoint_cache[42] = (fake_ep, time.time())  # 注入缓存

    result = await load_endpoints(group_id=42, db=db)
    assert result is fake_ep
    db.get_group_models.assert_not_called()


# TC-16: 缓存过期重新加载
@pytest.mark.asyncio
async def test_load_endpoints_cache_expired():
    db = AsyncMock()

    # 注入一个已过期的缓存（TTL 60s，设为 61 秒前）
    old_ep = [MagicMock(spec=ModelEndpoint)]
    _endpoint_cache[42] = (old_ep, time.time() - 61)

    model = GroupModelWithDetails(
        id=2, group_id=42, model_id=200, weight=1.0, is_enabled=True,
        model_name="gpt-4o-mini", display_name="Mini", api_format="",
        provider_id=1, provider_name="openai", provider_type="openai",
        max_retries=3, cooldown_seconds=60, cooldown_failure_threshold=3,
        context_window=128000, proxy="", extra_config={},
    )
    provider = MagicMock()
    provider.id = 1
    provider.provider_type = "openai"
    provider.api_key = "key"
    provider.base_url = "https://api.openai.com/v1"
    provider.extra_config = {}
    provider.is_enabled = True

    db.get_group_models = AsyncMock(return_value=[model])
    db.get_provider = AsyncMock(return_value=provider)

    result = await load_endpoints(group_id=42, db=db)
    assert len(result) == 1
    assert result[0].model_id == 200
    db.get_group_models.assert_called_once()


# TC-17: group 无 model 返回空列表
@pytest.mark.asyncio
async def test_load_endpoints_empty():
    db = AsyncMock()
    db.get_group_models = AsyncMock(return_value=[])

    result = await load_endpoints(group_id=99, db=db)
    assert result == []


# TC-18: provider disabled 时跳过
@pytest.mark.asyncio
async def test_load_endpoints_disabled_provider_skipped():
    db = AsyncMock()

    model = GroupModelWithDetails(
        id=1, group_id=1, model_id=100, weight=1.0, is_enabled=True,
        model_name="gpt-4o", display_name="GPT-4o", api_format="",
        provider_id=1, provider_name="openai", provider_type="openai",
        max_retries=3, cooldown_seconds=60, cooldown_failure_threshold=3,
        context_window=128000, proxy="", extra_config={},
    )
    provider = MagicMock()
    provider.is_enabled = False  # disabled

    db.get_group_models = AsyncMock(return_value=[model])
    db.get_provider = AsyncMock(return_value=provider)

    result = await load_endpoints(group_id=1, db=db)
    assert result == []


# ---------------------------------------------------------------------------
# 六、filter_available 测试 (TC-19 ~ TC-21)
# ---------------------------------------------------------------------------


# TC-19: 全部可用
def test_filter_available_all_available(cooldown, sample_endpoint):
    result = filter_available([sample_endpoint], cooldown, group_id=1)
    assert len(result) == 1
    assert result[0] is sample_endpoint


# TC-20: 部分 cooldown 过滤
def test_filter_available_partial_cooldown(cooldown):
    ep1 = MagicMock(spec=ModelEndpoint)
    ep1.model_id = 100

    ep2 = MagicMock(spec=ModelEndpoint)
    ep2.model_id = 200

    # 让 ep1 进入 cooldown：连续失败 3 次（threshold=3）
    for _ in range(3):
        cooldown.record_failure(group_id=1, model_id=100,
                                cooldown_failure_threshold=3, cooldown_seconds=300)

    result = filter_available([ep1, ep2], cooldown, group_id=1)
    assert len(result) == 1
    assert result[0].model_id == 200


# TC-21: 全部 cooldown 返回空
def test_filter_available_all_cooldown(cooldown):
    ep1 = MagicMock(spec=ModelEndpoint)
    ep1.model_id = 100
    ep2 = MagicMock(spec=ModelEndpoint)
    ep2.model_id = 200

    for ep in [ep1, ep2]:
        for _ in range(3):
            cooldown.record_failure(group_id=1, model_id=ep.model_id,
                                    cooldown_failure_threshold=3, cooldown_seconds=300)

    result = filter_available([ep1, ep2], cooldown, group_id=1)
    assert result == []


# ---------------------------------------------------------------------------
# 七、call_llm 测试 (TC-22 ~ TC-27)
# ---------------------------------------------------------------------------


# TC-22: 调用成功返回结果
@pytest.mark.asyncio
async def test_call_llm_success(sample_endpoint, cooldown):
    expected = {"choices": [{"message": {"content": "ok"}}]}
    sample_endpoint.provider.chat = AsyncMock(return_value=expected)

    with patch("botflow.pipeline._shared.get_config") as mock_cfg:
        mock_cfg.return_value = MagicMock(upstream_semaphore_size=0)
        result, err = await call_llm(
            sample_endpoint,
            messages=[{"role": "user", "content": "hi"}],
            group_id=1,
            cooldown=cooldown,
            temperature=0.7,
            max_tokens=100,
        )

    assert result == expected
    assert err is None
    assert cooldown.get_failure_count(group_id=1, model_id=100) == 0


# TC-23: 调用失败重试
@pytest.mark.asyncio
async def test_call_llm_retry_success(sample_endpoint, cooldown):
    error_429 = Exception("HTTP 429 Too Many Requests")
    error_429.status_code = 429

    sample_endpoint.provider.chat = AsyncMock(
        side_effect=[error_429, {"choices": []}]
    )

    with patch("botflow.pipeline._shared.get_config") as mock_cfg, \
         patch("botflow.pipeline._shared.exponential_backoff", new_callable=AsyncMock):
        mock_cfg.return_value = MagicMock(upstream_semaphore_size=0)
        result, err = await call_llm(
            sample_endpoint,
            messages=[],
            group_id=1,
            cooldown=cooldown,
        )

    assert result["choices"] == []
    assert err is None
    assert result["_routing"] == {"model_id": 100, "provider_id": 1}
    assert sample_endpoint.provider.chat.call_count == 2


# TC-24: 重试耗尽返回 None
@pytest.mark.asyncio
async def test_call_llm_retries_exhausted(sample_endpoint, cooldown):
    error = Exception("HTTP 500 Internal Server Error")
    sample_endpoint.provider.chat = AsyncMock(side_effect=error)

    with patch("botflow.pipeline._shared.get_config") as mock_cfg, \
         patch("botflow.pipeline._shared.exponential_backoff", new_callable=AsyncMock):
        mock_cfg.return_value = MagicMock(upstream_semaphore_size=0)
        result, err = await call_llm(
            sample_endpoint,
            messages=[],
            group_id=1,
            cooldown=cooldown,
        )

    assert result is None
    assert err is not None
    assert cooldown.get_failure_count(group_id=1, model_id=100) >= 1


# TC-25: cooldown 记录 success/failure
@pytest.mark.asyncio
async def test_call_llm_cooldown_recording(sample_endpoint, cooldown):
    # 先制造 2 次失败
    error = Exception("fail")
    sample_endpoint.provider.chat = AsyncMock(side_effect=error)

    with patch("botflow.pipeline._shared.get_config") as mock_cfg, \
         patch("botflow.pipeline._shared.exponential_backoff", new_callable=AsyncMock):
        mock_cfg.return_value = MagicMock(upstream_semaphore_size=0)
        _, err = await call_llm(sample_endpoint, messages=[], group_id=1, cooldown=cooldown)

    assert err is not None
    assert cooldown.get_failure_count(group_id=1, model_id=100) >= 1

    # 现在成功调用
    sample_endpoint.provider.chat = AsyncMock(return_value={"ok": True})
    with patch("botflow.pipeline._shared.get_config") as mock_cfg:
        mock_cfg.return_value = MagicMock(upstream_semaphore_size=0)
        _, err = await call_llm(sample_endpoint, messages=[], group_id=1, cooldown=cooldown)
    assert err is None

    assert cooldown.get_failure_count(group_id=1, model_id=100) == 0


# TC-26: 信号量限流
@pytest.mark.asyncio
async def test_call_llm_with_semaphore(sample_endpoint, cooldown):
    sample_endpoint.provider.chat = AsyncMock(return_value={"ok": True})
    sem = asyncio.Semaphore(2)

    with patch("botflow.pipeline._shared.get_config") as mock_cfg, \
         patch("botflow.pipeline._shared._ensure_provider_semaphore", return_value=sem):
        result, err = await call_llm(
            sample_endpoint, messages=[], group_id=1, cooldown=cooldown,
        )

    assert err is None
    assert result["ok"] is True
    assert result["_routing"] == {"model_id": 100, "provider_id": 1}


# TC-27: 非可重试错误不重试
@pytest.mark.asyncio
async def test_call_llm_non_retryable_error_no_retry(sample_endpoint, cooldown):
    error = ValueError("invalid parameter")
    sample_endpoint.provider.chat = AsyncMock(side_effect=error)

    with patch("botflow.pipeline._shared.get_config") as mock_cfg:
        mock_cfg.return_value = MagicMock(upstream_semaphore_size=0)
        result, err = await call_llm(sample_endpoint, messages=[], group_id=1, cooldown=cooldown)

    assert result is None
    assert err is not None
    assert sample_endpoint.provider.chat.call_count == 1  # 没有重试


# ---------------------------------------------------------------------------
# 七（续）、SG-0 F3：call_llm 暴露 last_error（返回 tuple[dict|None, Exception|None]）
# ---------------------------------------------------------------------------


# T3.1 正例：成功路径返回 (result, None)，且 _routing 注入点未被搬迁
async def test_call_llm_returns_result_and_none_error(sample_endpoint, cooldown):
    expected = {"choices": [{"message": {"content": "ok"}}]}
    sample_endpoint.provider.chat = AsyncMock(return_value=expected)

    with patch("botflow.pipeline._shared.get_config") as mock_cfg:
        mock_cfg.return_value = MagicMock(upstream_semaphore_size=0)
        result, err = await call_llm(
            sample_endpoint, messages=[{"role": "user", "content": "hi"}],
            group_id=1, cooldown=cooldown,
        )

    assert err is None
    assert result is expected
    assert result["_routing"] == {"model_id": 100, "provider_id": 1}


# T3.2 反例（关键）：失败路径返回 (None, err)，异常不再被吞
async def test_call_llm_returns_error_instead_of_swallowing(sample_endpoint, cooldown):
    raised = ProviderError("OpenAICompat request failed: HTTP 500")
    sample_endpoint.provider.chat = AsyncMock(side_effect=raised)

    with patch("botflow.pipeline._shared.get_config") as mock_cfg:
        mock_cfg.return_value = MagicMock(upstream_semaphore_size=0)
        result, err = await call_llm(
            sample_endpoint, messages=[], group_id=1, cooldown=cooldown,
        )

    assert result is None
    assert err is not None
    # 不假定 err 有 status_code 属性；只验证它是同一异常实例且文本一致。
    assert isinstance(err, ProviderError)
    assert "HTTP 500" in str(err)


# T3.3 边界：max_retries=1 且失败 → 只调用上游 1 次，err 非空
async def test_call_llm_max_retries_one_calls_upstream_once(cooldown):
    detail = GroupModelWithDetails(
        id=1, group_id=1, model_id=100, weight=1.0, is_enabled=True,
        model_name="gpt-4o", display_name="GPT-4o", api_format="",
        provider_id=1, provider_name="openai-main", provider_type="openai",
        max_retries=1, cooldown_seconds=60, cooldown_failure_threshold=3,
        context_window=128000, proxy="", extra_config={},
    )
    ep = ModelEndpoint(detail, MagicMock())
    ep.provider.chat = AsyncMock(side_effect=ProviderError("HTTP 500"))

    with patch("botflow.pipeline._shared.get_config") as mock_cfg:
        mock_cfg.return_value = MagicMock(upstream_semaphore_size=0)
        result, err = await call_llm(ep, messages=[], group_id=1, cooldown=cooldown)

    assert result is None
    assert err is not None
    assert ep.provider.chat.call_count == 1  # max_retries=1 → 不重试


# T3.4 正例：第 2 次尝试成功 → 返回 (result, None)，上游被调用 2 次
async def test_call_llm_second_attempt_succeeds(sample_endpoint, cooldown):
    retryable = Exception("HTTP 429 Too Many Requests")
    retryable.status_code = 429
    sample_endpoint.provider.chat = AsyncMock(
        side_effect=[retryable, {"choices": [{"message": {"content": "ok"}}]}]
    )

    with patch("botflow.pipeline._shared.get_config") as mock_cfg, \
         patch("botflow.pipeline._shared.exponential_backoff", new_callable=AsyncMock):
        mock_cfg.return_value = MagicMock(upstream_semaphore_size=0)
        result, err = await call_llm(
            sample_endpoint, messages=[], group_id=1, cooldown=cooldown,
        )

    assert err is None
    assert result["choices"][0]["message"]["content"] == "ok"
    assert sample_endpoint.provider.chat.call_count == 2


# ---------------------------------------------------------------------------
# 八、truncate_messages 测试 (TC-28 ~ TC-31)
# ---------------------------------------------------------------------------


# TC-28: 消息未超限不截断
def test_truncate_messages_under_limit():
    msgs = [{"role": "user", "content": "hi"}]
    ep = MagicMock()
    ep.detail.context_window = 128000

    result = truncate_messages(msgs, [ep], max_tokens=1024)
    assert result == msgs


# TC-29: 消息超限截断
def test_truncate_messages_over_limit():
    # 构造一个很长的消息列表，总 token 远超 context_window
    msgs = [{"role": "user", "content": "x" * 10000} for _ in range(20)]
    ep = MagicMock()
    ep.detail.context_window = 1000  # 非常小的 context window

    result = truncate_messages(msgs, [ep], max_tokens=100)
    assert len(result) < len(msgs)


# TC-30: 保留 system message
def test_truncate_messages_preserves_system():
    msgs = [
        {"role": "system", "content": "You are a helpful assistant."},
    ] + [{"role": "user", "content": "x" * 10000} for _ in range(20)]

    ep = MagicMock()
    ep.detail.context_window = 1000

    result = truncate_messages(msgs, [ep], max_tokens=100)
    assert any(m.get("role") == "system" for m in result)


# TC-31: 所有 endpoint context_window 为 0 不截断
def test_truncate_messages_no_context_window():
    msgs = [{"role": "user", "content": "hello"}]
    ep = MagicMock()
    ep.detail.context_window = 0

    result = truncate_messages(msgs, [ep], max_tokens=None)
    assert result == msgs


# ---------------------------------------------------------------------------
# 九、invalidate_endpoint_cache 测试 (TC-32 ~ TC-33)
# ---------------------------------------------------------------------------


# TC-32: 缓存被清除
def test_invalidate_endpoint_cache():
    _endpoint_cache[42] = (["fake"], time.time())
    invalidate_endpoint_cache(42)
    assert 42 not in _endpoint_cache


# TC-33: 清除不存在的 group 缓存不报错
def test_invalidate_nonexistent_cache():
    invalidate_endpoint_cache(99999)  # 不应抛出异常


# ---------------------------------------------------------------------------
# 十、_apply_model_extra_config 直接测试 (TC-34 ~ TC-36)
# ---------------------------------------------------------------------------


# TC-34: strip_params 移除指定参数
async def test_TC34_apply_model_extra_config_strip_params():
    kwargs = {"temperature": 0.7, "reasoning_effort": "high", "reasoning_content": "some"}
    config = {"strip_params": ["reasoning_effort", "reasoning_content"]}
    result = _apply_model_extra_config(kwargs, config)
    assert "reasoning_effort" not in result
    assert "reasoning_content" not in result
    assert result["temperature"] == 0.7


# TC-35: 空 extra_config 不改变 kwargs
async def test_TC35_apply_model_extra_config_empty():
    kwargs = {"temperature": 0.7, "max_tokens": 100}
    result = _apply_model_extra_config(kwargs, {})
    assert result == kwargs


# TC-36: reasoning_mode="off" 同时在 extra_config 和 kwargs
async def test_TC36_apply_model_extra_config_reasoning_mode_off():
    kwargs = {"temperature": 0.7, "reasoning_effort": "high"}
    config = {"reasoning_mode": "off"}
    result = _apply_model_extra_config(kwargs, config)
    assert "reasoning_effort" not in result
    assert result["temperature"] == 0.7


# ---------------------------------------------------------------------------
# 十一、_ensure_provider_semaphore 直接测试 (TC-37 ~ TC-39)
# ---------------------------------------------------------------------------


# TC-37: size > 0 返回 Semaphore
async def test_TC37_ensure_provider_semaphore_positive_size():
    sem = _ensure_provider_semaphore(provider_id=999, size=5)
    assert isinstance(sem, asyncio.Semaphore)


# TC-38: size <= 0 返回 None
async def test_TC38_ensure_provider_semaphore_unlimited():
    sem = _ensure_provider_semaphore(provider_id=998, size=0)
    assert sem is None


# TC-39: 重复调用返回同一实例
async def test_TC39_ensure_provider_semaphore_cached():
    sem1 = _ensure_provider_semaphore(provider_id=997, size=3)
    sem2 = _ensure_provider_semaphore(provider_id=997, size=3)
    assert sem1 is sem2
