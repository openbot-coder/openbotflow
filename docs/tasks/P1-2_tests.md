# P1-2 测试用例文档：Pipeline 骨架 — BaseStrategy + _shared.py

> 对应功能点文档：`docs/tasks/P1-2_features.md`
> 子任务：P1 — Pipeline 骨架
> 日期：2026-09-09

---

## 测试基础设施

```python
import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

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
from botflow.storage.db import Database
from botflow.storage.models import GroupModelWithDetails
from botflow.common.exceptions import ProviderError
```

### Fixtures

```python
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
```

---

## 一、RouteResult 测试

### TC-01：创建 RouteResult 并访问字段

**场景**：验证 NamedTuple 的字段访问正确。

```python
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
```

### TC-02：endpoints 为空列表

**场景**：策略可能返回空 endpoints（全部 cooldown），验证空列表正常。

```python
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
```

### TC-03：RouteResult 是不可变的

**场景**：NamedTuple 不可变赋值。

```python
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
```

---

## 二、StrategyError 测试

### TC-04：抛出 StrategyError

**场景**：验证可以正常抛出和捕获。

```python
def test_raise_strategy_error():
    with pytest.raises(StrategyError, match="test error"):
        raise StrategyError("test error")
```

### TC-05：StrategyError 继承关系

**场景**：StrategyError 继承 Exception，但不继承 BotflowError。

```python
def test_strategy_error_inheritance():
    from botflow.common.exceptions import BotflowError
    err = StrategyError("msg")
    assert isinstance(err, Exception)
    assert not isinstance(err, BotflowError)
```

---

## 三、BaseStrategy 测试

### TC-06：抽象类不能直接实例化

**场景**：BaseStrategy 是 ABC，缺少 `select_endpoints` 实现时不能实例化。

```python
def test_base_strategy_cannot_instantiate():
    with pytest.raises(TypeError, match="abstract method"):
        BaseStrategy(params={})
```

### TC-07：子类必须实现 select_endpoints

**场景**：只继承不实现抽象方法，仍不能实例化。

```python
class IncompleteStrategy(BaseStrategy):
    pass

def test_incomplete_strategy_cannot_instantiate():
    with pytest.raises(TypeError, match="abstract method"):
        IncompleteStrategy(params={})
```

### TC-08：完整子类可以实例化

**场景**：实现了 `select_endpoints` 的子类可以正常实例化。

```python
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
```

### TC-09：默认 execute 方法调用 select_endpoints + call_llm 循环

**场景**：execute 调用 select_endpoints，然后逐个 endpoint 尝试 call_llm，第一个成功就返回。

```python
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
        mock_call.return_value = {"choices": [{"message": {"content": "ok"}}]}
        result = await strategy.execute(
            messages=[{"role": "user", "content": "hi"}],
            db=mock_db, cooldown=cooldown, group_id=1,
        )
        assert mock_call.call_count == 1
        assert result["choices"][0]["message"]["content"] == "ok"
```

### TC-10：execute 所有 endpoint 失败时抛出 ProviderError

**场景**：所有 endpoint 的 call_llm 返回 None，execute 应抛出 ProviderError。

```python
@pytest.mark.asyncio
async def test_execute_all_fail_raises():
    strategy = TwoEndpointStrategy(params={})
    strategy._eps = [MagicMock(), MagicMock()]

    with patch("botflow.pipeline._shared.call_llm", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = None
        with pytest.raises(ProviderError, match="All endpoints failed"):
            await strategy.execute(
                messages=[], db=MagicMock(),
                cooldown=CooldownManager(), group_id=1,
            )
```

---

## 四、STRATEGY_REGISTRY 测试

### TC-11：register_strategy 注册成功

**场景**：注册新策略后可以查找到。

```python
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
```

### TC-12：重复注册同名策略报错

**场景**：同名策略第二次注册应抛出 ValueError。

```python
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
```

### TC-13：未知策略名查找返回 None

**场景**：STRATEGY_REGISTRY.get 对不存在的 key 返回 None。

```python
def test_unknown_strategy_returns_none():
    assert STRATEGY_REGISTRY.get("nonexistent_strategy_xyz") is None
```

---

## 五、load_endpoints 测试

### TC-14：正常加载 group_models

**场景**：DB 返回 enabled models + enabled providers，正确构建 ModelEndpoint 列表。

```python
@pytest.mark.asyncio
async def test_load_endpoints_normal():
    _endpoint_cache.clear()
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

    # 清理缓存
    _endpoint_cache.clear()
```

### TC-15：缓存命中（60s TTL）

**场景**：第一次加载后，60 秒内再次调用应直接返回缓存，不再查 DB。

```python
@pytest.mark.asyncio
async def test_load_endpoints_cache_hit():
    _endpoint_cache.clear()
    db = AsyncMock()

    fake_ep = [MagicMock(spec=ModelEndpoint)]
    _endpoint_cache[42] = (fake_ep, time.time())  # 注入缓存

    result = await load_endpoints(group_id=42, db=db)
    assert result is fake_ep
    db.get_group_models.assert_not_called()

    _endpoint_cache.clear()
```

### TC-16：缓存过期重新加载

**场景**：缓存超过 TTL 后应重新从 DB 加载。

```python
@pytest.mark.asyncio
async def test_load_endpoints_cache_expired():
    _endpoint_cache.clear()
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

    _endpoint_cache.clear()
```

### TC-17：group 无 model 返回空列表

**场景**：DB 返回空列表时，load_endpoints 应返回空列表。

```python
@pytest.mark.asyncio
async def test_load_endpoints_empty():
    _endpoint_cache.clear()
    db = AsyncMock()
    db.get_group_models = AsyncMock(return_value=[])

    result = await load_endpoints(group_id=99, db=db)
    assert result == []

    _endpoint_cache.clear()
```

### TC-18：provider disabled 时跳过

**场景**：model 关联的 provider is_enabled=False 时，该 endpoint 应被跳过。

```python
@pytest.mark.asyncio
async def test_load_endpoints_disabled_provider_skipped():
    _endpoint_cache.clear()
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

    _endpoint_cache.clear()
```

---

## 六、filter_available 测试

### TC-19：全部可用

**场景**：所有 endpoint 都不在 cooldown，返回全部。

```python
def test_filter_available_all_available(cooldown, sample_endpoint):
    result = filter_available([sample_endpoint], cooldown, group_id=1)
    assert len(result) == 1
    assert result[0] is sample_endpoint
```

### TC-20：部分 cooldown 过滤

**场景**：一个 endpoint 在 cooldown，另一个不在，返回未 cooldown 的。

```python
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
```

### TC-21：全部 cooldown 返回空

**场景**：所有 endpoint 都在 cooldown，返回空列表。

```python
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
```

---

## 七、call_llm 测试

### TC-22：调用成功返回结果

**场景**：provider.chat 正常返回，call_llm 返回结果，cooldown 记录 success。

```python
@pytest.mark.asyncio
async def test_call_llm_success(sample_endpoint, cooldown):
    expected = {"choices": [{"message": {"content": "ok"}}]}
    sample_endpoint.provider.chat = AsyncMock(return_value=expected)

    with patch("botflow.pipeline._shared.get_config") as mock_cfg:
        mock_cfg.return_value = MagicMock(upstream_semaphore_size=0)
        result = await call_llm(
            sample_endpoint,
            messages=[{"role": "user", "content": "hi"}],
            group_id=1,
            cooldown=cooldown,
            temperature=0.7,
            max_tokens=100,
        )

    assert result == expected
    assert cooldown.get_failure_count(group_id=1, model_id=100) == 0
```

### TC-23：调用失败重试

**场景**：第一次调用抛出可重试错误（429），第二次成功。

```python
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
        result = await call_llm(
            sample_endpoint,
            messages=[],
            group_id=1,
            cooldown=cooldown,
        )

    assert result == {"choices": []}
    assert sample_endpoint.provider.chat.call_count == 2
```

### TC-24：重试耗尽返回 None

**场景**：每次调用都失败，重试耗尽后返回 None，cooldown 记录 failure。

```python
@pytest.mark.asyncio
async def test_call_llm_retries_exhausted(sample_endpoint, cooldown):
    error = Exception("HTTP 500 Internal Server Error")
    sample_endpoint.provider.chat = AsyncMock(side_effect=error)

    with patch("botflow.pipeline._shared.get_config") as mock_cfg, \
         patch("botflow.pipeline._shared.exponential_backoff", new_callable=AsyncMock):
        mock_cfg.return_value = MagicMock(upstream_semaphore_size=0)
        result = await call_llm(
            sample_endpoint,
            messages=[],
            group_id=1,
            cooldown=cooldown,
        )

    assert result is None
    assert cooldown.get_failure_count(group_id=1, model_id=100) >= 1
```

### TC-25：cooldown 记录 success/failure

**场景**：成功调用后 failure count 重置为 0；失败调用后 failure count 递增。

```python
@pytest.mark.asyncio
async def test_call_llm_cooldown_recording(sample_endpoint, cooldown):
    # 先制造 2 次失败
    error = Exception("fail")
    sample_endpoint.provider.chat = AsyncMock(side_effect=error)

    with patch("botflow.pipeline._shared.get_config") as mock_cfg, \
         patch("botflow.pipeline._shared.exponential_backoff", new_callable=AsyncMock):
        mock_cfg.return_value = MagicMock(upstream_semaphore_size=0)
        await call_llm(sample_endpoint, messages=[], group_id=1, cooldown=cooldown)

    assert cooldown.get_failure_count(group_id=1, model_id=100) >= 1

    # 现在成功调用
    sample_endpoint.provider.chat = AsyncMock(return_value={"ok": True})
    with patch("botflow.pipeline._shared.get_config") as mock_cfg:
        mock_cfg.return_value = MagicMock(upstream_semaphore_size=0)
        await call_llm(sample_endpoint, messages=[], group_id=1, cooldown=cooldown)

    assert cooldown.get_failure_count(group_id=1, model_id=100) == 0
```

### TC-26：信号量限流

**场景**：配置 semaphore_size > 0 时，调用使用信号量。

```python
@pytest.mark.asyncio
async def test_call_llm_with_semaphore(sample_endpoint, cooldown):
    sample_endpoint.provider.chat = AsyncMock(return_value={"ok": True})
    sem = asyncio.Semaphore(2)

    with patch("botflow.pipeline._shared.get_config") as mock_cfg, \
         patch("botflow.pipeline._shared._ensure_provider_semaphore", return_value=sem):
        result = await call_llm(
            sample_endpoint, messages=[], group_id=1, cooldown=cooldown,
        )

    assert result == {"ok": True}
```

### TC-27：非可重试错误不重试

**场景**：抛出非可重试错误（如 ValueError），直接 break 不重试。

```python
@pytest.mark.asyncio
async def test_call_llm_non_retryable_error_no_retry(sample_endpoint, cooldown):
    error = ValueError("invalid parameter")
    sample_endpoint.provider.chat = AsyncMock(side_effect=error)

    with patch("botflow.pipeline._shared.get_config") as mock_cfg:
        mock_cfg.return_value = MagicMock(upstream_semaphore_size=0)
        result = await call_llm(sample_endpoint, messages=[], group_id=1, cooldown=cooldown)

    assert result is None
    assert sample_endpoint.provider.chat.call_count == 1  # 没有重试
```

---

## 八、truncate_messages 测试

### TC-28：消息未超限不截断

**场景**：消息 token 数未超限，原样返回。

```python
def test_truncate_messages_under_limit():
    msgs = [{"role": "user", "content": "hi"}]
    ep = MagicMock()
    ep.detail.context_window = 128000

    result = truncate_messages(msgs, [ep], max_tokens=1024)
    assert result == msgs
```

### TC-29：消息超限截断

**场景**：消息 token 数超限时，截断为最近的消息。

```python
def test_truncate_messages_over_limit():
    # 构造一个很长的消息列表，总 token 远超 context_window
    msgs = [{"role": "user", "content": "x" * 10000} for _ in range(20)]
    ep = MagicMock()
    ep.detail.context_window = 1000  # 非常小的 context window

    result = truncate_messages(msgs, [ep], max_tokens=100)
    assert len(result) < len(msgs)
```

### TC-30：保留 system message

**场景**：截断后 system message 仍然保留。

```python
def test_truncate_messages_preserves_system():
    msgs = [
        {"role": "system", "content": "You are a helpful assistant."},
    ] + [{"role": "user", "content": "x" * 10000} for _ in range(20)]

    ep = MagicMock()
    ep.detail.context_window = 1000

    result = truncate_messages(msgs, [ep], max_tokens=100)
    assert any(m.get("role") == "system" for m in result)
```

### TC-31：所有 endpoint context_window 为 0 不截断

**场景**：所有 endpoint 的 context_window 为 0（未知），不截断。

```python
def test_truncate_messages_no_context_window():
    msgs = [{"role": "user", "content": "hello"}]
    ep = MagicMock()
    ep.detail.context_window = 0

    result = truncate_messages(msgs, [ep], max_tokens=None)
    assert result == msgs
```

---

## 九、invalidate_endpoint_cache 测试

### TC-32：缓存被清除

**场景**：调用 invalidate_endpoint_cache 后，对应 group_id 的缓存不存在。

```python
def test_invalidate_endpoint_cache():
    _endpoint_cache[42] = (["fake"], time.time())
    invalidate_endpoint_cache(42)
    assert 42 not in _endpoint_cache
```

### TC-33：清除不存在的 group 缓存不报错

**场景**：对从未缓存过的 group_id 调用 invalidate，不应报错。

```python
def test_invalidate_nonexistent_cache():
    invalidate_endpoint_cache(99999)  # 不应抛出异常
```

---

## 十、_apply_model_extra_config 直接测试

### TC-34：strip_params 移除指定参数

**场景**：extra_config 中配置 strip_params，对应参数从 kwargs 中移除。

```python
async def test_TC34_apply_model_extra_config_strip_params():
    kwargs = {"temperature": 0.7, "reasoning_effort": "high", "reasoning_content": "some"}
    config = {"strip_params": ["reasoning_effort", "reasoning_content"]}
    result = _apply_model_extra_config(kwargs, config)
    assert "reasoning_effort" not in result
    assert "reasoning_content" not in result
    assert result["temperature"] == 0.7
```

### TC-35：空 extra_config 不改变 kwargs

**场景**：extra_config 为空字典时，kwargs 原样返回。

```python
async def test_TC35_apply_model_extra_config_empty():
    kwargs = {"temperature": 0.7, "max_tokens": 100}
    result = _apply_model_extra_config(kwargs, {})
    assert result == kwargs
```

### TC-36：reasoning_mode="off" 同时在 extra_config 和 kwargs

**场景**：extra_config 中 reasoning_mode 为 off，kwargs 中也有 reasoning_effort，应被移除。

```python
async def test_TC36_apply_model_extra_config_reasoning_mode_off():
    kwargs = {"temperature": 0.7, "reasoning_effort": "high"}
    config = {"reasoning_mode": "off"}
    result = _apply_model_extra_config(kwargs, config)
    assert "reasoning_effort" not in result
    assert result["temperature"] == 0.7
```

---

## 十一、_ensure_provider_semaphore 直接测试

### TC-37：size > 0 返回 Semaphore

**场景**：semaphore size 大于 0 时返回 asyncio.Semaphore 实例。

```python
async def test_TC37_ensure_provider_semaphore_positive_size():
    sem = _ensure_provider_semaphore(provider_id=999, size=5)
    assert isinstance(sem, asyncio.Semaphore)
```

### TC-38：size <= 0 返回 None

**场景**：semaphore size 为 0 或负数时返回 None（不限流）。

```python
async def test_TC38_ensure_provider_semaphore_unlimited():
    sem = _ensure_provider_semaphore(provider_id=998, size=0)
    assert sem is None
```

### TC-39：重复调用返回同一实例

**场景**：相同 provider_id 重复调用返回缓存的同一实例。

```python
async def test_TC39_ensure_provider_semaphore_cached():
    sem1 = _ensure_provider_semaphore(provider_id=997, size=3)
    sem2 = _ensure_provider_semaphore(provider_id=997, size=3)
    assert sem1 is sem2
```

---

## 测试覆盖矩阵

| 功能点 | 测试用例 | 正例 | 反例 | 边界 |
|--------|---------|------|------|------|
| RouteResult | TC-01, TC-02, TC-03 | ✓ | ✓(空列表) | ✓(不可变) |
| StrategyError | TC-04, TC-05 | ✓ | ✓(继承) | - |
| BaseStrategy | TC-06, TC-07, TC-08, TC-09, TC-10 | ✓(实例化/执行) | ✓(抽象/失败) | ✓(空endpoints) |
| STRATEGY_REGISTRY | TC-11, TC-12, TC-13 | ✓(注册) | ✓(重复) | ✓(未知) |
| load_endpoints | TC-14, TC-15, TC-16, TC-17, TC-18 | ✓(正常) | ✓(disabled) | ✓(空/过期) |
| filter_available | TC-19, TC-20, TC-21 | ✓(全部可用) | ✓(全部cooldown) | ✓(部分) |
| call_llm | TC-22, TC-23, TC-24, TC-25, TC-26, TC-27 | ✓(成功) | ✓(失败/重试) | ✓(信号量/非重试) |
| truncate_messages | TC-28, TC-29, TC-30, TC-31 | ✓(未超限) | ✓(超限) | ✓(system/零window) |
| invalidate_endpoint_cache | TC-32, TC-33 | ✓(清除) | - | ✓(不存在) |
| _apply_model_extra_config | TC-34, TC-35, TC-36 | ✓(strip_params) | ✓(空config) | ✓(reasoning_mode_off) |
| _ensure_provider_semaphore | TC-37, TC-38, TC-39 | ✓(正数size) | ✓(size<=0) | ✓(缓存复用) |
