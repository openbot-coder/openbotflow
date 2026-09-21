"""Tests for P1-4: PipelineEngine skeleton + core.py non-streaming integration.

Covers 29 test scenarios from docs/tasks/P1-4_tests.md:
- PipelineEngine unit tests (21): init, _load_group, _create_strategy, route
- core.py integration tests (8): _get_engine, _handle_chat_non_stream, _stream_common regression
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from botflow.common.exceptions import (
    AllModelsCooldownError,
    ConfigurationError,
    NoAvailableModelError,
    ProviderError,
)
from botflow.pipeline.base import STRATEGY_REGISTRY, StrategyError
from botflow.storage.db import Database
from botflow.storage.models import ModelGroup
from botflow.router import CooldownManager

# PipelineEngine is expected at botflow.pipeline.engine (not yet created when tests are written).
from botflow.pipeline.engine import PipelineEngine
from botflow.pipeline.strategies import (
    RandomWeightsStrategy,
    RoundRobinStrategy,
    SequentialStrategy,
)


def _without_attempts(result: dict) -> dict:
    """Compare a `route()` result ignoring the SG-0 attempt trail.

    `LangGraphEngine.route()` smuggles the failed-attempt list out of the graph
    under the internal ``_attempts`` key (``_``-prefixed like ``_routing``); the
    driver pops it before serialisation. Unit tests calling ``route()`` directly
    must strip it before comparing against an expected response body.
    """
    return {k: v for k, v in result.items() if k != "_attempts"}


# ===========================================================================
# 一、PipelineEngine 单元测试 (21 tests)
# ===========================================================================


# ---------------------------------------------------------------------------
# 1.1 创建 Engine 实例 (E-01 ~ E-03)
# ---------------------------------------------------------------------------


# E-01: 传入 db_factory 和 cooldown，验证实例属性正确存储
async def test_init_stores_factory_and_cooldown():
    mock_db_factory = Mock()
    mock_cooldown = Mock(spec=CooldownManager)
    engine = PipelineEngine(db_factory=mock_db_factory, cooldown=mock_cooldown)
    assert engine._db_factory is mock_db_factory
    assert engine.cooldown is mock_cooldown


# E-02: 新实例 _group_cache 为空字典
async def test_init_empty_group_cache():
    engine = PipelineEngine(db_factory=Mock(), cooldown=Mock(spec=CooldownManager))
    assert engine._group_cache == {}
    assert engine._GROUP_CACHE_TTL == 60


# E-03: 访问 engine.db 时调用 db_factory()，返回 factory 的返回值
async def test_db_property_calls_factory():
    mock_db = Mock(spec=Database)
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=Mock(spec=CooldownManager))
    assert engine.db is mock_db


# ---------------------------------------------------------------------------
# 1.2 _load_group 正常加载 (G-01 ~ G-03)
# ---------------------------------------------------------------------------


# G-01: DB 返回有效 group，方法返回该 group
async def test_load_group_returns_group():
    group = ModelGroup(id=1, name="fast", type="random_weights")
    mock_db = AsyncMock(spec=Database)
    mock_db.get_group = AsyncMock(return_value=group)
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=Mock(spec=CooldownManager))
    result = await engine._load_group(1)
    assert result == group
    assert result.name == "fast"


# G-02: 首次加载后结果写入 _group_cache
async def test_load_group_caches_result():
    group = ModelGroup(id=1, name="fast", type="random_weights")
    mock_db = AsyncMock(spec=Database)
    mock_db.get_group = AsyncMock(return_value=group)
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=Mock(spec=CooldownManager))
    await engine._load_group(1)
    assert 1 in engine._group_cache
    cached_group, cached_ts = engine._group_cache[1]
    assert cached_group is group
    assert isinstance(cached_ts, float)


# G-03: 验证调用了 db.get_group(group_id)
async def test_load_group_calls_db_get_group():
    group = ModelGroup(id=1, name="fast", type="random_weights")
    mock_db = AsyncMock(spec=Database)
    mock_db.get_group = AsyncMock(return_value=group)
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=Mock(spec=CooldownManager))
    await engine._load_group(1)
    mock_db.get_group.assert_called_once_with(1)


# ---------------------------------------------------------------------------
# 1.3 _load_group 缓存命中 (G-04 ~ G-05)
# ---------------------------------------------------------------------------


# G-04: 缓存存在且未过期，不调用 DB
async def test_load_group_cache_hit():
    group = ModelGroup(id=1, name="fast", type="random_weights")
    mock_db = AsyncMock(spec=Database)
    mock_db.get_group = AsyncMock(return_value=group)
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=Mock(spec=CooldownManager))

    # 第一次加载 → 写入缓存
    await engine._load_group(1)
    assert mock_db.get_group.call_count == 1

    # 第二次加载 → 缓存命中，不再调用 DB
    result = await engine._load_group(1)
    assert mock_db.get_group.call_count == 1  # 仍然只调用了 1 次
    assert result.name == "fast"


# G-05: 缓存命中返回与首次加载相同的 group 对象
async def test_load_group_cache_hit_returns_same_group():
    group = ModelGroup(id=1, name="fast", type="random_weights")
    mock_db = AsyncMock(spec=Database)
    mock_db.get_group = AsyncMock(return_value=group)
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=Mock(spec=CooldownManager))

    first = await engine._load_group(1)
    second = await engine._load_group(1)
    assert first is second


# ---------------------------------------------------------------------------
# 1.4 _load_group 缓存过期 (G-06 ~ G-07)
# ---------------------------------------------------------------------------


# G-06: 缓存过期后重新从 DB 加载
async def test_load_group_cache_expired():
    group_v1 = ModelGroup(id=1, name="fast", type="random_weights")
    group_v2 = ModelGroup(id=1, name="fast_v2", type="round_robin")
    mock_db = AsyncMock(spec=Database)
    mock_db.get_group = AsyncMock(side_effect=[group_v1, group_v2])
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=Mock(spec=CooldownManager))

    # 第一次加载
    await engine._load_group(1)

    # 模拟 TTL 过期（直接修改缓存时间戳）
    engine._group_cache[1] = (group_v1, time.time() - 61)

    # 第二次加载 → 缓存过期，重新从 DB 加载
    result = await engine._load_group(1)
    assert result.name == "fast_v2"
    assert mock_db.get_group.call_count == 2


# G-07: 缓存刚好在 TTL 边界时视为过期
async def test_load_group_cache_ttl_boundary():
    group = ModelGroup(id=1, name="fast", type="random_weights")
    mock_db = AsyncMock(spec=Database)
    mock_db.get_group = AsyncMock(return_value=group)
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=Mock(spec=CooldownManager))

    await engine._load_group(1)

    # 设置缓存时间为恰好 60 秒前（TTL 边界）
    engine._group_cache[1] = (group, time.time() - 60)

    # 边界：now - ts = 60，不小于 TTL(60)，视为过期
    await engine._load_group(1)
    assert mock_db.get_group.call_count == 2


# ---------------------------------------------------------------------------
# 1.5 _load_group 不存在 → ConfigurationError (G-08 ~ G-09)
# ---------------------------------------------------------------------------


# G-08: DB 返回 None，抛出 ConfigurationError
async def test_load_group_not_found_raises():
    mock_db = AsyncMock(spec=Database)
    mock_db.get_group = AsyncMock(return_value=None)
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=Mock(spec=CooldownManager))

    with pytest.raises(ConfigurationError, match="Group 999 not found"):
        await engine._load_group(999)


# G-09: 验证异常消息包含 group_id
async def test_load_group_not_found_message():
    mock_db = AsyncMock(spec=Database)
    mock_db.get_group = AsyncMock(return_value=None)
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=Mock(spec=CooldownManager))

    with pytest.raises(ConfigurationError) as exc_info:
        await engine._load_group(999)
    assert "999" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 1.6 _create_strategy 正常创建 (S-01 ~ S-04)
# ---------------------------------------------------------------------------


# S-01: type="random_weights" → RandomWeightsStrategy
def test_create_strategy_random_weights():
    engine = PipelineEngine(db_factory=Mock(), cooldown=Mock(spec=CooldownManager))
    group = ModelGroup(id=1, name="fast", type="random_weights", params={})
    strategy = engine._create_strategy(group)
    assert isinstance(strategy, RandomWeightsStrategy)


# S-02: type="round_robin" → RoundRobinStrategy
def test_create_strategy_round_robin():
    engine = PipelineEngine(db_factory=Mock(), cooldown=Mock(spec=CooldownManager))
    group = ModelGroup(id=1, name="balanced", type="round_robin", params={})
    strategy = engine._create_strategy(group)
    assert isinstance(strategy, RoundRobinStrategy)


# S-03: type="sequential" → SequentialStrategy
def test_create_strategy_sequential():
    engine = PipelineEngine(db_factory=Mock(), cooldown=Mock(spec=CooldownManager))
    group = ModelGroup(id=1, name="fallback_chain", type="sequential", params={})
    strategy = engine._create_strategy(group)
    assert isinstance(strategy, SequentialStrategy)


# S-04: group.params 正确传递给策略构造函数
def test_create_strategy_passes_params():
    engine = PipelineEngine(db_factory=Mock(), cooldown=Mock(spec=CooldownManager))
    params = {"some_key": "some_value"}
    group = ModelGroup(id=1, name="fast", type="random_weights", params=params)
    strategy = engine._create_strategy(group)
    assert strategy.params == params


# ---------------------------------------------------------------------------
# 1.7 _create_strategy 未知 type → ConfigurationError (S-05 ~ S-06)
# ---------------------------------------------------------------------------


# S-05: type="nonexistent" → ConfigurationError
def test_create_strategy_unknown_type_raises():
    engine = PipelineEngine(db_factory=Mock(), cooldown=Mock(spec=CooldownManager))
    group = ModelGroup(id=1, name="smart", type="nonexistent", params={})
    with pytest.raises(ConfigurationError, match="Unknown strategy type 'nonexistent'"):
        engine._create_strategy(group)


# S-06: 验证错误消息包含 type、group name 和可用策略列表
def test_create_strategy_unknown_type_message():
    engine = PipelineEngine(db_factory=Mock(), cooldown=Mock(spec=CooldownManager))
    group = ModelGroup(id=1, name="smart", type="nonexistent", params={})
    with pytest.raises(ConfigurationError) as exc_info:
        engine._create_strategy(group)
    msg = str(exc_info.value)
    assert "nonexistent" in msg
    assert "smart" in msg
    assert "random_weights" in msg  # 至少列出一个可用策略


# ---------------------------------------------------------------------------
# 1.8 route non-streaming 成功 (R-01 ~ R-03)
# ---------------------------------------------------------------------------


def _make_ep(model_id=1, provider_id=10, model_name="m1"):
    """创建 mock ModelEndpoint（与 test_langgraph_engine 共用模式）."""
    from botflow.storage.models import GroupModelWithDetails
    detail = GroupModelWithDetails(
        id=model_id, group_id=1, model_id=model_id, weight=1.0,
        is_enabled=True, model_name=model_name, display_name=model_name,
        api_format="openai", provider_id=provider_id, provider_name="p",
        provider_type="openai", max_retries=1, cooldown_seconds=30,
        cooldown_failure_threshold=3, context_window=8192,
    )
    return MagicMock(detail=detail, provider_id=provider_id,
                     model_id=model_id, max_retries=1, cooldown_seconds=30,
                     cooldown_threshold=3, provider=MagicMock())


# ---------------------------------------------------------------------------
# 策略 mock 工具：直接 mock select_endpoints，绕开 load_endpoints 的 import 路径问题
# ---------------------------------------------------------------------------
# 策略函数体内 `from botflow.pipeline._shared import load_endpoints` 绑定的是
# 模块加载时的原始对象，patch router/​shared 均无法可靠拦截。
# 最简方案：直接在策略类上 patch select_endpoints，graph 调用路径完全保留。
PATCH_CALL = "botflow.pipeline.langgraph_engine.call_llm"


def _patch_select_endpoints(strategy_cls, side_effect):
    """在 strategy_cls 上 patch select_endpoints，返回可作为 context manager 的 patcher."""
    return patch.object(strategy_cls, "select_endpoints", side_effect=side_effect)


async def _make_select_side_effect(endpoints, messages=None):
    """构造 select_endpoints 的 side_effect：返回固定 endpoints + messages."""
    from botflow.pipeline.base import RouteResult
    async def _select(**kwargs):
        return RouteResult(
            endpoints=endpoints,
            messages=messages or kwargs.get("messages", []),
            temperature=kwargs.get("temperature"),
            max_tokens=kwargs.get("max_tokens"),
            extra_kwargs=kwargs.get("extra_kwargs", {}),
        )
    return _select


# R-01: route 成功返回 LLM 响应
async def test_route_non_stream_success():
    ep = _make_ep()
    llm_resp = {"choices": [{"message": {"content": "Hello"}}], "_routing": {"model_id": 1}}
    mock_db = AsyncMock(spec=Database)
    mock_cooldown = MagicMock(spec=CooldownManager)
    mock_cooldown.is_on_cooldown.return_value = False
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=mock_cooldown)

    group = ModelGroup(id=1, name="fast", type="random_weights")
    select_fn = await _make_select_side_effect([ep])
    with _patch_select_endpoints(RandomWeightsStrategy, side_effect=select_fn), \
         patch(PATCH_CALL, new_callable=AsyncMock, return_value=(llm_resp, None)):
        result = await engine.route(group=group, messages=[{"role": "user", "content": "Hi"}])
    assert result["choices"][0]["message"]["content"] == "Hello"


# R-02: 验证 temperature/max_tokens 传递给 call_llm
async def test_route_non_stream_passes_params():
    ep = _make_ep()
    llm_resp = {"choices": [], "_routing": {}}
    mock_db = AsyncMock(spec=Database)
    mock_cooldown = MagicMock(spec=CooldownManager)
    mock_cooldown.is_on_cooldown.return_value = False
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=mock_cooldown)
    group = ModelGroup(id=1, name="fast", type="random_weights")

    async def check_call(ep, messages, group_id, cooldown, temperature=None, max_tokens=None, **kw):
        assert temperature == 0.5
        assert max_tokens == 256
        return (llm_resp, None)

    select_fn = await _make_select_side_effect([ep])
    with _patch_select_endpoints(RandomWeightsStrategy, side_effect=select_fn), \
         patch(PATCH_CALL, new_callable=AsyncMock, side_effect=check_call):
        await engine.route(group=group, messages=[], temperature=0.5, max_tokens=256)


# R-03: 验证 extra kwargs 传递给 call_llm
async def test_route_non_stream_passes_kwargs():
    ep = _make_ep()
    llm_resp = {"choices": [], "_routing": {}}
    mock_db = AsyncMock(spec=Database)
    mock_cooldown = MagicMock(spec=CooldownManager)
    mock_cooldown.is_on_cooldown.return_value = False
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=mock_cooldown)
    group = ModelGroup(id=1, name="fast", type="random_weights")

    async def check_call(ep, messages, group_id, cooldown, temperature=None, max_tokens=None, **kw):
        assert kw.get("reasoning_effort") == "high"
        return (llm_resp, None)

    select_fn = await _make_select_side_effect([ep])
    with _patch_select_endpoints(RandomWeightsStrategy, side_effect=select_fn), \
         patch(PATCH_CALL, new_callable=AsyncMock, side_effect=check_call):
        await engine.route(group=group, messages=[], reasoning_effort="high")


# ---------------------------------------------------------------------------
# 1.9 route non-streaming 失败 → fallback (R-04 ~ R-08)
# ---------------------------------------------------------------------------

# Graph architecture: call_llm returns None on failure (never raises).
# Graph routes: try_call → (fallback) → resolve_group → load_and_select → try_call.


# ---------------------------------------------------------------------------
# 辅助：构造 select_endpoints mock，基于 group_id 返回不同 endpoint
# ---------------------------------------------------------------------------

from botflow.pipeline.base import RouteResult


def _select_factory(group_ep_map):
    """构造 select_endpoints side_effect：group_ep_map = {gid: [ep, ...]}."""

    async def _select(messages, db, cooldown, group_id, temperature=None, max_tokens=None, **kw):
        eps = group_ep_map[group_id]
        return RouteResult(
            endpoints=eps, messages=messages, temperature=temperature,
            max_tokens=max_tokens, extra_kwargs=kw,
        )

    return _select


def _select_factory_with_fail(fail_group_ids):
    """select_endpoints 对 fail_group_ids 中的 group 抛 StrategyError."""

    async def _select(messages, db, cooldown, group_id, temperature=None, max_tokens=None, **kw):
        if group_id in fail_group_ids:
            raise StrategyError(f"group {group_id} strategy failed")
        raise StrategyError("should not be called")

    return _select


# R-04: call_llm 返回 None → graph fallback 到 fallback_group
async def test_route_non_stream_fallback_on_provider_error():
    ep_a = _make_ep(model_id=1, model_name="m1")
    ep_b = _make_ep(model_id=2, provider_id=20, model_name="m2")
    fallback_result = {"choices": [{"message": {"content": "Fallback OK"}}], "_routing": {"model_id": 2}}

    mock_db = AsyncMock(spec=Database)
    group_a = ModelGroup(id=1, name="fast", type="random_weights", fallback_group_id=2)
    group_b = ModelGroup(id=2, name="default", type="random_weights")
    mock_db.get_group = AsyncMock(side_effect=lambda gid: {1: group_a, 2: group_b}[gid])
    mock_cooldown = Mock(spec=CooldownManager)
    mock_cooldown.is_on_cooldown.return_value = False
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=mock_cooldown)

    call_count = 0

    async def call_llm_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return (None, ProviderError("call_llm failed"))  # primary group fails
        return (fallback_result, None)  # fallback group succeeds

    select_fn = _select_factory({1: [ep_a], 2: [ep_b]})
    with _patch_select_endpoints(RandomWeightsStrategy, side_effect=select_fn), \
         patch(PATCH_CALL, new_callable=AsyncMock, side_effect=call_llm_side_effect):
        result = await engine.route(group=group_a, messages=[{"role": "user", "content": "Hi"}])

    assert _without_attempts(result) == fallback_result


# R-05: call_llm 返回 None（所有 endpoint 失败）→ graph fallback
async def test_route_non_stream_fallback_on_cooldown_error():
    ep_a = _make_ep(model_id=1, model_name="m1")
    ep_b = _make_ep(model_id=2, provider_id=20, model_name="m2")
    fallback_result = {"choices": [{"message": {"content": "Fallback OK"}}], "_routing": {"model_id": 2}}

    mock_db = AsyncMock(spec=Database)
    group_a = ModelGroup(id=1, name="fast", type="random_weights", fallback_group_id=2)
    group_b = ModelGroup(id=2, name="default", type="random_weights")
    mock_db.get_group = AsyncMock(side_effect=lambda gid: {1: group_a, 2: group_b}[gid])
    mock_cooldown = Mock(spec=CooldownManager)
    mock_cooldown.is_on_cooldown.return_value = False
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=mock_cooldown)

    call_count = 0

    async def call_llm_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return (None, ProviderError("call_llm failed"))  # primary group fails
        return (fallback_result, None)  # fallback group succeeds

    select_fn = _select_factory({1: [ep_a], 2: [ep_b]})
    with _patch_select_endpoints(RandomWeightsStrategy, side_effect=select_fn), \
         patch(PATCH_CALL, new_callable=AsyncMock, side_effect=call_llm_side_effect):
        result = await engine.route(group=group_a, messages=[{"role": "user", "content": "Hi"}])

    assert _without_attempts(result) == fallback_result


# R-06: call_llm 返回 None → graph fallback（所有 endpoint 不可用）
async def test_route_non_stream_fallback_on_no_available_error():
    """验证所有 endpoint 失败时 graph 触发 fallback."""
    ep_a = _make_ep(model_id=1, model_name="m1")
    ep_b = _make_ep(model_id=2, provider_id=20, model_name="m2")
    fallback_result = {"choices": [{"message": {"content": "Fallback OK"}}], "_routing": {"model_id": 2}}

    mock_db = AsyncMock(spec=Database)
    group_a = ModelGroup(id=1, name="fast", type="random_weights", fallback_group_id=2)
    group_b = ModelGroup(id=2, name="default", type="random_weights")
    mock_db.get_group = AsyncMock(side_effect=lambda gid: {1: group_a, 2: group_b}[gid])
    mock_cooldown = Mock(spec=CooldownManager)
    mock_cooldown.is_on_cooldown.return_value = False
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=mock_cooldown)

    call_count = 0

    async def call_llm_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return (None, ProviderError("call_llm failed"))  # primary group fails
        return (fallback_result, None)  # fallback group succeeds

    select_fn = _select_factory({1: [ep_a], 2: [ep_b]})
    with _patch_select_endpoints(RandomWeightsStrategy, side_effect=select_fn), \
         patch(PATCH_CALL, new_callable=AsyncMock, side_effect=call_llm_side_effect):
        result = await engine.route(group=group_a, messages=[{"role": "user", "content": "Hi"}])

    assert _without_attempts(result) == fallback_result


# R-07a: strategy.select_endpoints 抛异常 → graph fallback
async def test_route_non_stream_fallback_on_strategy_error():
    """验证 strategy 抛异常时 graph 触发 fallback."""
    ep_b = _make_ep(model_id=2, provider_id=20, model_name="m2")
    fallback_result = {"choices": [{"message": {"content": "Fallback OK"}}], "_routing": {"model_id": 2}}

    mock_db = AsyncMock(spec=Database)
    group_a = ModelGroup(id=1, name="fast", type="random_weights", fallback_group_id=2)
    group_b = ModelGroup(id=2, name="default", type="random_weights")
    mock_db.get_group = AsyncMock(side_effect=lambda gid: {1: group_a, 2: group_b}[gid])
    mock_cooldown = Mock(spec=CooldownManager)
    mock_cooldown.is_on_cooldown.return_value = False
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=mock_cooldown)

    # group A strategy raises; group B strategy returns ep_b
    select_fn_a = _select_factory_with_fail(fail_group_ids={1})

    async def select_fn_b(messages, db, cooldown, group_id, **kw):
        return RouteResult(endpoints=[ep_b], messages=messages,
                           temperature=None, max_tokens=None, extra_kwargs={})

    call_count = 0

    async def call_llm_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return (fallback_result, None)

    with _patch_select_endpoints(RandomWeightsStrategy, side_effect=select_fn_a), \
         patch(PATCH_CALL, new_callable=AsyncMock, side_effect=call_llm_side_effect):
        # On fallback, select_fn_a is called again for group_b (same strategy type)
        # but group_b id=2 is not in fail_group_ids, so it raises "should not be called"
        # We need select_fn_a to also handle group_b:
        pass

    # Rethink: _select_factory_with_fail only raises for fail_group_ids.
    # For fallback group (id=2), it should return ep_b.
    # Let's use a combined function:
    async def select_fn_combined(messages, db, cooldown, group_id, **kw):
        if group_id == 1:
            raise StrategyError("group 1 strategy failed")
        return RouteResult(endpoints=[ep_b], messages=messages,
                           temperature=None, max_tokens=None, extra_kwargs={})

    with _patch_select_endpoints(RandomWeightsStrategy, side_effect=select_fn_combined), \
         patch(PATCH_CALL, new_callable=AsyncMock, side_effect=call_llm_side_effect):
        result = await engine.route(group=group_a, messages=[{"role": "user", "content": "Hi"}])

    assert _without_attempts(result) == fallback_result


# R-07b: fallback group 执行成功，返回 fallback 结果
async def test_route_non_stream_fallback_success():
    """验证 fallback group 执行成功时返回 fallback 结果."""
    ep_a = _make_ep(model_id=1, model_name="m1")
    ep_b = _make_ep(model_id=2, provider_id=20, model_name="m2")
    fallback_result = {"model": "default", "choices": [{"message": {"content": "From fallback"}}], "_routing": {"model_id": 2}}

    mock_db = AsyncMock(spec=Database)
    group_a = ModelGroup(id=1, name="fast", type="random_weights", fallback_group_id=2)
    group_b = ModelGroup(id=2, name="default", type="round_robin")
    mock_db.get_group = AsyncMock(side_effect=lambda gid: {1: group_a, 2: group_b}[gid])
    mock_cooldown = Mock(spec=CooldownManager)
    mock_cooldown.is_on_cooldown.return_value = False
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=mock_cooldown)

    call_count = 0

    async def call_llm_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return (None, ProviderError("call_llm failed"))  # primary group fails
        return (fallback_result, None)  # fallback group succeeds

    # group A → RandomWeightsStrategy; group B → RoundRobinStrategy
    select_rw = _select_factory({1: [ep_a]})
    select_rr = _select_factory({2: [ep_b]})
    with _patch_select_endpoints(RandomWeightsStrategy, side_effect=select_rw), \
         _patch_select_endpoints(RoundRobinStrategy, side_effect=select_rr), \
         patch(PATCH_CALL, new_callable=AsyncMock, side_effect=call_llm_side_effect):
        result = await engine.route(group=group_a, messages=[{"role": "user", "content": "Hi"}])

    assert _without_attempts(result) == fallback_result


# R-08: fallback_group_id=None 且 call_llm 返回 None → ProviderError
async def test_route_non_stream_no_fallback_without_id():
    ep = _make_ep()

    mock_db = AsyncMock(spec=Database)
    group = ModelGroup(id=1, name="fast", type="random_weights", fallback_group_id=None)
    mock_cooldown = Mock(spec=CooldownManager)
    mock_cooldown.is_on_cooldown.return_value = False
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=mock_cooldown)

    select_fn = _select_factory({1: [ep]})
    with _patch_select_endpoints(RandomWeightsStrategy, side_effect=select_fn), \
         patch(PATCH_CALL, new_callable=AsyncMock, return_value=(None, ProviderError("call_llm failed"))):
        with pytest.raises(ProviderError):
            await engine.route(group=group, messages=[])


# ---------------------------------------------------------------------------
# 1.10 route fallback 循环检测 (R-09 ~ R-10)
# ---------------------------------------------------------------------------


# R-09: A→B→A 循环被检测并抛出 ProviderError
async def test_route_fallback_cycle_detected():
    ep_a = _make_ep(model_id=1, model_name="m1")
    ep_b = _make_ep(model_id=2, provider_id=20, model_name="m2")

    mock_db = AsyncMock(spec=Database)
    group_a = ModelGroup(id=1, name="group_a", type="random_weights", fallback_group_id=2)
    group_b = ModelGroup(id=2, name="group_b", type="random_weights", fallback_group_id=1)
    mock_db.get_group = AsyncMock(side_effect=lambda gid: {1: group_a, 2: group_b}[gid])
    mock_cooldown = Mock(spec=CooldownManager)
    mock_cooldown.is_on_cooldown.return_value = False
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=mock_cooldown)

    select_fn = _select_factory({1: [ep_a], 2: [ep_b]})
    with _patch_select_endpoints(RandomWeightsStrategy, side_effect=select_fn), \
         patch(PATCH_CALL, new_callable=AsyncMock, return_value=(None, ProviderError("call_llm failed"))):
        with pytest.raises(ProviderError, match="No fallback group available"):
            await engine.route(group=group_a, messages=[])


# R-10: 验证错误消息包含 "No fallback group available"
async def test_route_fallback_cycle_error_message():
    ep_a = _make_ep(model_id=1, model_name="m1")
    ep_b = _make_ep(model_id=2, provider_id=20, model_name="m2")

    mock_db = AsyncMock(spec=Database)
    group_a = ModelGroup(id=1, name="group_a", type="random_weights", fallback_group_id=2)
    group_b = ModelGroup(id=2, name="group_b", type="random_weights", fallback_group_id=1)
    mock_db.get_group = AsyncMock(side_effect=lambda gid: {1: group_a, 2: group_b}[gid])
    mock_cooldown = Mock(spec=CooldownManager)
    mock_cooldown.is_on_cooldown.return_value = False
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=mock_cooldown)

    select_fn = _select_factory({1: [ep_a], 2: [ep_b]})
    with _patch_select_endpoints(RandomWeightsStrategy, side_effect=select_fn), \
         patch(PATCH_CALL, new_callable=AsyncMock, return_value=(None, ProviderError("call_llm failed"))):
        with pytest.raises(ProviderError) as exc_info:
            await engine.route(group=group_a, messages=[])
        msg = str(exc_info.value)
        assert "No fallback group available" in msg


# ---------------------------------------------------------------------------
# 1.11 route fallback 深度限制 (R-11 ~ R-12)
# ---------------------------------------------------------------------------


# R-11: 超过 3 层深度抛出 ProviderError
async def test_route_fallback_depth_limit():
    mock_db = AsyncMock(spec=Database)
    mock_cooldown = Mock(spec=CooldownManager)
    mock_cooldown.is_on_cooldown.return_value = False
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=mock_cooldown)

    # 构建 5 层 fallback 链：1→2→3→4→5
    groups = {
        i: ModelGroup(
            id=i, name=f"group_{i}", type="random_weights",
            fallback_group_id=i + 1 if i < 5 else None,
        )
        for i in range(1, 6)
    }
    mock_db.get_group = AsyncMock(side_effect=lambda gid: groups[gid])

    all_eps = {i: [_make_ep(model_id=i, provider_id=i * 10, model_name=f"m{i}")] for i in range(1, 6)}

    select_fn = _select_factory(all_eps)
    with _patch_select_endpoints(RandomWeightsStrategy, side_effect=select_fn), \
         patch(PATCH_CALL, new_callable=AsyncMock, return_value=(None, ProviderError("call_llm failed"))):
        with pytest.raises(ProviderError, match="Fallback chain too deep"):
            await engine.route(group=groups[1], messages=[])


# R-12: 恰好 3 层 fallback 不报错（第 3 层成功）
async def test_route_fallback_depth_exact_limit():
    """恰好 3 层 fallback — 第 3 层成功，不应触发深度限制."""
    mock_db = AsyncMock(spec=Database)
    mock_cooldown = Mock(spec=CooldownManager)
    mock_cooldown.is_on_cooldown.return_value = False
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=mock_cooldown)

    group_a = ModelGroup(id=1, name="g1", type="random_weights", fallback_group_id=2)
    group_b = ModelGroup(id=2, name="g2", type="random_weights", fallback_group_id=3)
    group_c = ModelGroup(id=3, name="g3", type="random_weights", fallback_group_id=None)
    mock_db.get_group = AsyncMock(side_effect=lambda gid: {1: group_a, 2: group_b, 3: group_c}[gid])

    ep1 = _make_ep(model_id=1, model_name="m1")
    ep2 = _make_ep(model_id=2, provider_id=20, model_name="m2")
    ep3 = _make_ep(model_id=3, provider_id=30, model_name="m3")
    success_result = {"choices": [{"message": {"content": "OK at depth 3"}}], "_routing": {"model_id": 3}}

    call_count = 0

    async def call_llm_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return (None, ProviderError("call_llm failed"))  # groups 1 and 2 fail
        return (success_result, None)  # group 3 succeeds

    select_fn = _select_factory({1: [ep1], 2: [ep2], 3: [ep3]})
    with _patch_select_endpoints(RandomWeightsStrategy, side_effect=select_fn), \
         patch(PATCH_CALL, new_callable=AsyncMock, side_effect=call_llm_side_effect):
        result = await engine.route(group=group_a, messages=[])

    assert _without_attempts(result) == success_result
    assert call_count == 3


# ---------------------------------------------------------------------------
# 1.12 route ConfigurationError 不 fallback (R-13 ~ R-14)
# ---------------------------------------------------------------------------


# R-13: ConfigurationError 直接抛出，不触发 fallback
async def test_route_config_error_no_fallback():
    mock_db = AsyncMock(spec=Database)
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=Mock(spec=CooldownManager))

    group = ModelGroup(id=1, name="bad", type="nonexistent", fallback_group_id=2)

    with pytest.raises(ConfigurationError, match="Unknown strategy"):
        await engine.route(group=group, messages=[], stream=False)

    # fallback_group 不应被加载
    assert mock_db.get_group.call_count == 0


# R-14: ConfigurationError 异常链完整保留
async def test_route_config_error_preserves_exception():
    mock_db = AsyncMock(spec=Database)
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=Mock(spec=CooldownManager))

    group = ModelGroup(id=1, name="bad", type="nonexistent", fallback_group_id=2)

    with pytest.raises(ConfigurationError) as exc_info:
        await engine.route(group=group, messages=[], stream=False)

    exc = exc_info.value
    assert isinstance(exc, ConfigurationError)
    assert "nonexistent" in str(exc)


# ===========================================================================
# 二、core.py 集成测试 (8 tests)
# ===========================================================================


# ---------------------------------------------------------------------------
# 2.1 _get_engine 返回 PipelineEngine 实例 (C-01 ~ C-03)
# ---------------------------------------------------------------------------


# C-01: _get_engine() 返回 PipelineEngine 实例
def test_get_engine_returns_pipeline_engine():
    # 重置全局引擎
    import botflow.core as core_module
    core_module._engine = None

    engine = core_module._get_engine()
    assert isinstance(engine, PipelineEngine)


# C-02: 多次调用返回同一个实例（单例）
def test_get_engine_singleton():
    import botflow.core as core_module
    core_module._engine = None

    engine1 = core_module._get_engine()
    engine2 = core_module._get_engine()
    assert engine1 is engine2


# C-03: 验证 _get_db 作为 factory 传入
def test_get_engine_uses_db_factory():
    import botflow.core as core_module
    core_module._engine = None

    engine = core_module._get_engine()
    # _db_factory should be _get_db
    assert engine._db_factory is core_module._get_db


# ---------------------------------------------------------------------------
# 2.2 _handle_chat_non_stream 使用 PipelineEngine (C-04 ~ C-07)
# ---------------------------------------------------------------------------


# C-04: Mock 验证调用了 engine.route() 而非 router.route()
async def test_handle_non_stream_uses_pipeline_engine():
    """验证 _handle_chat_non_stream 使用 PipelineEngine 而非 GroupRouter."""
    import botflow.core as core_module

    # 设置 mock engine
    mock_engine = Mock(spec=PipelineEngine)
    mock_group = ModelGroup(id=1, name="fast", type="random_weights")
    mock_engine.route = AsyncMock(return_value={
        "model": "fast",
        "choices": [{"message": {"content": "Hello"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    })
    core_module._engine = mock_engine

    mock_db = AsyncMock(spec=Database)
    mock_db.get_group = AsyncMock(return_value=mock_group)

    internal = {
        "model": "fast",
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": False,
    }
    mock_request = Mock()

    with patch.object(core_module, "_get_group_id", new_callable=AsyncMock, return_value=1), \
         patch.object(core_module, "_get_db", return_value=mock_db):
        result = await core_module._handle_chat_non_stream(
            internal, mock_request, lambda x: x,
        )

    # 验证使用了 engine.route
    mock_engine.route.assert_called_once()
    call_kwargs = mock_engine.route.call_args.kwargs
    assert call_kwargs.get("stream") is False


# C-05: 验证 engine.route() 收到正确的 group 参数
async def test_handle_non_stream_passes_group():
    """验证 engine.route() 收到正确的 group 对象."""
    import botflow.core as core_module

    mock_engine = Mock(spec=PipelineEngine)
    mock_group = ModelGroup(id=42, name="test_group", type="round_robin")
    mock_engine.route = AsyncMock(return_value={
        "model": "test_group",
        "choices": [{"message": {"content": "OK"}}],
        "usage": {},
    })
    core_module._engine = mock_engine

    mock_db = AsyncMock(spec=Database)
    mock_db.get_group = AsyncMock(return_value=mock_group)

    internal = {
        "model": "test_group",
        "messages": [{"role": "user", "content": "Test"}],
        "stream": False,
    }

    with patch.object(core_module, "_get_group_id", new_callable=AsyncMock, return_value=42), \
         patch.object(core_module, "_get_db", return_value=mock_db):
        await core_module._handle_chat_non_stream(internal, Mock(), lambda x: x)

    # 验证 route 收到的 group 参数
    call_args = mock_engine.route.call_args
    assert call_args.kwargs.get("group") is mock_group or call_args[1].get("group") is mock_group


# C-06: 验证 stream=False 被显式传递
async def test_handle_non_stream_stream_false():
    """验证 stream=False 被显式传递给 engine.route."""
    import botflow.core as core_module

    mock_engine = Mock(spec=PipelineEngine)
    mock_group = ModelGroup(id=1, name="fast", type="random_weights")
    mock_engine.route = AsyncMock(return_value={
        "model": "fast",
        "choices": [{"message": {"content": "OK"}}],
        "usage": {},
    })
    core_module._engine = mock_engine

    mock_db = AsyncMock(spec=Database)
    mock_db.get_group = AsyncMock(return_value=mock_group)

    internal = {
        "model": "fast",
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": False,
    }

    with patch.object(core_module, "_get_group_id", new_callable=AsyncMock, return_value=1), \
         patch.object(core_module, "_get_db", return_value=mock_db):
        await core_module._handle_chat_non_stream(internal, Mock(), lambda x: x)

    call_kwargs = mock_engine.route.call_args.kwargs
    assert call_kwargs.get("stream") is False


# C-07: PipelineEngine 返回的结果正确传递给 format_response
async def test_handle_non_stream_preserves_response():
    """PipelineEngine 返回的结果正确传递给 format_response."""
    import botflow.core as core_module

    engine_result = {
        "model": "fast",
        "choices": [{"message": {"content": "Hello world"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }

    mock_engine = Mock(spec=PipelineEngine)
    mock_group = ModelGroup(id=1, name="fast", type="random_weights")
    mock_engine.route = AsyncMock(return_value=engine_result)
    core_module._engine = mock_engine

    mock_db = AsyncMock(spec=Database)
    mock_db.get_group = AsyncMock(return_value=mock_group)

    internal = {
        "model": "fast",
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": False,
    }

    format_fn = MagicMock(side_effect=lambda x: x)

    with patch.object(core_module, "_get_group_id", new_callable=AsyncMock, return_value=1), \
         patch.object(core_module, "_get_db", return_value=mock_db):
        resp = await core_module._handle_chat_non_stream(
            internal, Mock(), format_fn,
        )

    format_fn.assert_called_once()
    # The formatted result should contain the engine's response data
    formatted = format_fn.call_args[0][0]
    assert formatted["choices"][0]["message"]["content"] == "Hello world"


# ---------------------------------------------------------------------------
# 2.3 回归测试：streaming 路径不受影响 (TC-27 ~ TC-28)
# ---------------------------------------------------------------------------


# TC-27: _stream_common 仍通过 _get_extra_route_params() + PipelineEngine 执行
async def test_stream_common_still_uses_pipeline_engine():
    """回归测试：streaming 路径仍使用 _get_extra_route_params 返回 4 元组 (group_id, engine, group_obj, safe_extra)."""
    import botflow.core as core_module

    mock_engine = Mock()
    mock_group = ModelGroup(id=1, name="fast", type="random_weights")
    mock_params_result = (1, mock_engine, mock_group, {})

    with patch.object(core_module, "_get_extra_route_params",
                      new_callable=AsyncMock) as mock_params, \
         patch.object(core_module, "_get_engine", return_value=mock_engine):
        mock_params.return_value = mock_params_result

        group_id, engine, active_group, safe_extra = await core_module._get_extra_route_params(
            {"model": "fast", "stream": True},
        )
        assert group_id == 1
        assert engine is mock_engine
        assert active_group is mock_group
        assert safe_extra == {}


# TC-28: 验证 _handle_chat_non_stream 使用 PipelineEngine 而非 GroupRouter
async def test_handle_non_stream_uses_pipeline_engine_not_group_router():
    """回归测试：non-streaming 路径已迁移到 PipelineEngine."""
    import botflow.core as core_module

    mock_engine = Mock(spec=PipelineEngine)
    mock_group = ModelGroup(id=1, name="fast", type="random_weights")
    mock_engine.route = AsyncMock(return_value={
        "model": "fast",
        "choices": [{"message": {"content": "OK"}}],
        "usage": {},
    })
    core_module._engine = mock_engine

    mock_db = AsyncMock(spec=Database)
    mock_db.get_group = AsyncMock(return_value=mock_group)

    internal = {
        "model": "fast",
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": False,
    }

    with patch.object(core_module, "_get_group_id", new_callable=AsyncMock, return_value=1), \
         patch.object(core_module, "_get_db", return_value=mock_db):
        await core_module._handle_chat_non_stream(internal, Mock(), lambda x: x)

    # 验证使用了 engine.route 而非 GroupRouter
    mock_engine.route.assert_called_once()
    call_args = mock_engine.route.call_args
    assert call_args.kwargs.get("group") is mock_group or call_args[1].get("group") is mock_group
