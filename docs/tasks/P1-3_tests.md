# P1-3 测试用例文档：3 个内建策略

> 测试文件：`tests/test_pipeline_strategies.py`
> 覆盖：RandomWeightsStrategy (7) + RoundRobinStrategy (6) + SequentialStrategy (5) = 18 个测试场景

---

## 测试基础设施

所有策略的 `select_endpoints()` 签名相同：`(messages, db, cooldown, group_id, ...)`。测试需要 mock `db`（`Database`）和 `cooldown`（`CooldownManager`），以及构造 `ModelEndpoint` 对象。

### Mock/Fixture 设计

```python
# tests/conftest.py 或 test 文件内部

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from botflow.router import CooldownManager, ModelEndpoint
from botflow.storage.models import GroupModelWithDetails


def make_endpoint(
    model_id: int = 1,
    weight: float = 1.0,
    context_window: int = 8192,
    max_retries: int = 3,
    cooldown_failure_threshold: int = 3,
    cooldown_seconds: int = 60,
    provider_id: int = 1,
    model_name: str = "test-model",
) -> ModelEndpoint:
    """构造一个 ModelEndpoint 用于测试。"""
    detail = GroupModelWithDetails(
        id=model_id,
        group_id=1,
        model_id=model_id,
        weight=weight,
        is_enabled=True,
        model_name=model_name,
        display_name=model_name,
        provider_id=provider_id,
        provider_name="test-provider",
        provider_type="openai",
        max_retries=max_retries,
        cooldown_seconds=cooldown_seconds,
        cooldown_failure_threshold=cooldown_failure_threshold,
        context_window=context_window,
    )
    mock_provider = MagicMock()
    return ModelEndpoint(detail, mock_provider)


def make_mock_db(endpoints: list[ModelEndpoint]) -> MagicMock:
    """构造 mock Database，get_group_models 返回 endpoints 的 detail。"""
    db = AsyncMock()
    db.get_group_models.return_value = [ep.detail for ep in endpoints]
    db.get_provider.return_value = MagicMock(id=1, is_enabled=True)
    return db
```

### patch 加载

由于 `load_endpoints`、`filter_available`、`truncate_messages` 都从 `_shared` 导入，测试中通过 `patch` 替换它们以隔离测试目标。

```python
@pytest.fixture
def cooldown():
    return CooldownManager()
```

---

## 1. RandomWeightsStrategy 测试

### 1.1 select_endpoints 返回 RouteResult

**场景**：传入 2 个可用 endpoints，验证返回类型和字段。

```python
@pytest.mark.asyncio
async def test_random_weights_select_returns_route_result():
    """select_endpoints 返回 RouteResult，包含 endpoints、messages 等字段。"""
    from botflow.pipeline.strategies import RandomWeightsStrategy
    from botflow.pipeline.base import RouteResult

    ep1 = make_endpoint(model_id=1, weight=1.0)
    ep2 = make_endpoint(model_id=2, weight=1.0)
    strategy = RandomWeightsStrategy(params={})
    db = make_mock_db([ep1, ep2])
    cooldown = CooldownManager()
    messages = [{"role": "user", "content": "hello"}]

    with patch("botflow.pipeline.strategies.load_endpoints", new_callable=AsyncMock, return_value=[ep1, ep2]), \
         patch("botflow.pipeline.strategies.filter_available", return_value=[ep1, ep2]), \
         patch("botflow.pipeline.strategies.truncate_messages", return_value=messages):
        result = await strategy.select_endpoints(messages, db, cooldown, group_id=1)

    assert isinstance(result, RouteResult)
    assert len(result.endpoints) == 2
    assert result.messages == messages
    assert result.temperature is None
    assert result.max_tokens is None
    assert result.extra_kwargs == {}
```

### 1.2 weighted random 选择分布正确

**场景**：高权重模型被选为首选的频率显著更高（1000 次采样统计）。

```python
@pytest.mark.asyncio
async def test_random_weights_distribution():
    """权重 3:1 的模型，首选频率应接近 75%。"""
    from botflow.pipeline.strategies import RandomWeightsStrategy

    ep_heavy = make_endpoint(model_id=1, weight=3.0, model_name="heavy")
    ep_light = make_endpoint(model_id=2, weight=1.0, model_name="light")
    strategy = RandomWeightsStrategy(params={})
    db = make_mock_db([ep_heavy, ep_light])
    cooldown = CooldownManager()
    messages = [{"role": "user", "content": "hello"}]

    counts = {1: 0, 2: 0}
    N = 1000
    with patch("botflow.pipeline.strategies.load_endpoints", new_callable=AsyncMock, return_value=[ep_heavy, ep_light]), \
         patch("botflow.pipeline.strategies.filter_available", return_value=[ep_heavy, ep_light]), \
         patch("botflow.pipeline.strategies.truncate_messages", return_value=messages):
        for _ in range(N):
            result = await strategy.select_endpoints(messages, db, cooldown, group_id=1)
            counts[result.endpoints[0].model_id] += 1

    # ep_heavy 应被选为首选约 75%（容差 65%-85%）
    ratio = counts[1] / N
    assert 0.65 < ratio < 0.85, f"Heavy model ratio {ratio:.2f} not in expected range"
```

### 1.3 cooldown 中的模型被跳过

**场景**：3 个模型，1 个在 cooldown，select_endpoints 不应返回该模型。

```python
@pytest.mark.asyncio
async def test_random_weights_skips_cooldown():
    """cooldown 中的模型不应出现在返回的 endpoints 中。"""
    from botflow.pipeline.strategies import RandomWeightsStrategy

    ep1 = make_endpoint(model_id=1, weight=1.0)
    ep2 = make_endpoint(model_id=2, weight=1.0)
    ep3 = make_endpoint(model_id=3, weight=1.0)
    strategy = RandomWeightsStrategy(params={})
    db = make_mock_db([ep1, ep2, ep3])
    cooldown = CooldownManager()
    messages = [{"role": "user", "content": "hello"}]

    # ep2 在 cooldown
    available = [ep1, ep3]

    with patch("botflow.pipeline.strategies.load_endpoints", new_callable=AsyncMock, return_value=[ep1, ep2, ep3]), \
         patch("botflow.pipeline.strategies.filter_available", return_value=available), \
         patch("botflow.pipeline.strategies.truncate_messages", return_value=messages):
        result = await strategy.select_endpoints(messages, db, cooldown, group_id=1)

    returned_ids = {ep.model_id for ep in result.endpoints}
    assert 2 not in returned_ids
    assert len(result.endpoints) == 2
```

### 1.4 全部 cooldown → NoAvailableModelError

**场景**：所有模型都在 cooldown，应抛出 `NoAvailableModelError`。

```python
@pytest.mark.asyncio
async def test_random_weights_all_cooldown_raises():
    """所有模型 cooldown 时抛出 NoAvailableModelError。"""
    from botflow.pipeline.strategies import RandomWeightsStrategy
    from botflow.common.exceptions import NoAvailableModelError

    ep1 = make_endpoint(model_id=1, weight=1.0)
    strategy = RandomWeightsStrategy(params={})
    db = make_mock_db([ep1])
    cooldown = CooldownManager()
    messages = [{"role": "user", "content": "hello"}]

    with patch("botflow.pipeline.strategies.load_endpoints", new_callable=AsyncMock, return_value=[ep1]), \
         patch("botflow.pipeline.strategies.filter_available", return_value=[]):
        with pytest.raises(NoAvailableModelError, match="all models on cooldown"):
            await strategy.select_endpoints(messages, db, cooldown, group_id=1)
```

### 1.5 context window 截断生效

**场景**：验证 `truncate_messages` 被调用时传入了正确的参数。

```python
@pytest.mark.asyncio
async def test_random_weights_truncation():
    """truncate_messages 被调用，传入 available endpoints 和 max_tokens。"""
    from botflow.pipeline.strategies import RandomWeightsStrategy

    ep1 = make_endpoint(model_id=1, weight=1.0, context_window=4096)
    strategy = RandomWeightsStrategy(params={})
    db = make_mock_db([ep1])
    cooldown = CooldownManager()
    messages = [{"role": "user", "content": "x" * 10000}]
    truncated = [{"role": "user", "content": "x" * 4000}]

    with patch("botflow.pipeline.strategies.load_endpoints", new_callable=AsyncMock, return_value=[ep1]), \
         patch("botflow.pipeline.strategies.filter_available", return_value=[ep1]) as mock_filter, \
         patch("botflow.pipeline.strategies.truncate_messages", return_value=truncated) as mock_truncate:
        result = await strategy.select_endpoints(messages, db, cooldown, group_id=1, max_tokens=2000)

    mock_truncate.assert_called_once_with(messages, [ep1], 2000)
    assert result.messages == truncated
```

### 1.6 execute 成功（select + call_llm）

**场景**：execute 调用 select_endpoints 后，对第一个 endpoint 调用 call_llm 成功。

```python
@pytest.mark.asyncio
async def test_random_weights_execute_success():
    """execute 选择 endpoint 并成功调用 LLM。"""
    from botflow.pipeline.strategies import RandomWeightsStrategy

    ep1 = make_endpoint(model_id=1, weight=1.0)
    strategy = RandomWeightsStrategy(params={})
    db = make_mock_db([ep1])
    cooldown = CooldownManager()
    messages = [{"role": "user", "content": "hello"}]
    llm_response = {"choices": [{"message": {"content": "hi"}}]}

    with patch("botflow.pipeline.strategies.load_endpoints", new_callable=AsyncMock, return_value=[ep1]), \
         patch("botflow.pipeline.strategies.filter_available", return_value=[ep1]), \
         patch("botflow.pipeline.strategies.truncate_messages", return_value=messages), \
         patch("botflow.pipeline.strategies.call_llm", new_callable=AsyncMock, return_value=llm_response) as mock_call:
        result = await strategy.execute(messages, db, cooldown, group_id=1)

    assert result == llm_response
    mock_call.assert_called_once()
```

### 1.7 execute 全失败 → ProviderError

**场景**：所有 endpoint 的 call_llm 都返回 None，应抛出 `ProviderError`。

```python
@pytest.mark.asyncio
async def test_random_weights_execute_all_fail():
    """所有 endpoint 调用失败时抛出 ProviderError。"""
    from botflow.pipeline.strategies import RandomWeightsStrategy
    from botflow.common.exceptions import ProviderError

    ep1 = make_endpoint(model_id=1, weight=1.0)
    ep2 = make_endpoint(model_id=2, weight=1.0)
    strategy = RandomWeightsStrategy(params={})
    db = make_mock_db([ep1, ep2])
    cooldown = CooldownManager()
    messages = [{"role": "user", "content": "hello"}]

    with patch("botflow.pipeline.strategies.load_endpoints", new_callable=AsyncMock, return_value=[ep1, ep2]), \
         patch("botflow.pipeline.strategies.filter_available", return_value=[ep1, ep2]), \
         patch("botflow.pipeline.strategies.truncate_messages", return_value=messages), \
         patch("botflow.pipeline.strategies.call_llm", new_callable=AsyncMock, return_value=None):
        with pytest.raises(ProviderError, match="All endpoints failed"):
            await strategy.execute(messages, db, cooldown, group_id=1)
```

---

## 2. RoundRobinStrategy 测试

### 2.1 select_endpoints 按顺序选择

**场景**：3 个模型（weight 均为 1.0），第一次请求选第 1 个（index 0）。

```python
@pytest.mark.asyncio
async def test_round_robin_first_selection():
    """首次请求选择第一个 endpoint（index 0）。"""
    from botflow.pipeline.strategies import RoundRobinStrategy

    ep1 = make_endpoint(model_id=1, weight=1.0, model_name="model-A")
    ep2 = make_endpoint(model_id=2, weight=1.0, model_name="model-B")
    ep3 = make_endpoint(model_id=3, weight=1.0, model_name="model-C")
    strategy = RoundRobinStrategy(params={})
    db = make_mock_db([ep1, ep2, ep3])
    cooldown = CooldownManager()
    messages = [{"role": "user", "content": "hello"}]

    # 清除类计数器确保从 0 开始
    RoundRobinStrategy._counters.clear()

    with patch("botflow.pipeline.strategies.load_endpoints", new_callable=AsyncMock, return_value=[ep1, ep2, ep3]), \
         patch("botflow.pipeline.strategies.filter_available", return_value=[ep1, ep2, ep3]), \
         patch("botflow.pipeline.strategies.truncate_messages", return_value=messages):
        result = await strategy.select_endpoints(messages, db, cooldown, group_id=1)

    assert result.endpoints[0].model_id == 1
```

### 2.2 第二请求选下一个

**场景**：连续两次请求，第二次选第 2 个模型。

```python
@pytest.mark.asyncio
async def test_round_robin_second_selection():
    """第二次请求选择第二个 endpoint（index 1）。"""
    from botflow.pipeline.strategies import RoundRobinStrategy

    ep1 = make_endpoint(model_id=1, weight=1.0)
    ep2 = make_endpoint(model_id=2, weight=1.0)
    ep3 = make_endpoint(model_id=3, weight=1.0)
    strategy = RoundRobinStrategy(params={})
    db = make_mock_db([ep1, ep2, ep3])
    cooldown = CooldownManager()
    messages = [{"role": "user", "content": "hello"}]

    RoundRobinStrategy._counters.clear()

    with patch("botflow.pipeline.strategies.load_endpoints", new_callable=AsyncMock, return_value=[ep1, ep2, ep3]), \
         patch("botflow.pipeline.strategies.filter_available", return_value=[ep1, ep2, ep3]), \
         patch("botflow.pipeline.strategies.truncate_messages", return_value=messages):
        await strategy.select_endpoints(messages, db, cooldown, group_id=1)  # idx 0 -> model 1
        result2 = await strategy.select_endpoints(messages, db, cooldown, group_id=1)  # idx 1 -> model 2

    assert result2.endpoints[0].model_id == 2
```

### 2.3 计数器溢出保护

**场景**：手动设置计数器到极大值，验证不会溢出或异常。

```python
@pytest.mark.asyncio
async def test_round_robin_counter_overflow_protection():
    """计数器接近溢出边界时仍正常工作。"""
    from botflow.pipeline.strategies import RoundRobinStrategy

    ep1 = make_endpoint(model_id=1, weight=1.0)
    ep2 = make_endpoint(model_id=2, weight=1.0)
    strategy = RoundRobinStrategy(params={})
    db = make_mock_db([ep1, ep2])
    cooldown = CooldownManager()
    messages = [{"role": "user", "content": "hello"}]

    # 设置计数器到 1999（len=2, 2*1000=2000, 下一次 2000 % 2000 = 0）
    RoundRobinStrategy._counters[1] = 1999

    with patch("botflow.pipeline.strategies.load_endpoints", new_callable=AsyncMock, return_value=[ep1, ep2]), \
         patch("botflow.pipeline.strategies.filter_available", return_value=[ep1, ep2]), \
         patch("botflow.pipeline.strategies.truncate_messages", return_value=messages):
        result = await strategy.select_endpoints(messages, db, cooldown, group_id=1)

    # 1999 % 2 = 1 -> 选 ep2 (index 1)
    assert result.endpoints[0].model_id == 2
    # 计数器归零: (1999+1) % 2000 = 0
    assert RoundRobinStrategy._counters[1] == 0
```

### 2.4 单模型 group

**场景**：只有 1 个模型，每次请求都选它。

```python
@pytest.mark.asyncio
async def test_round_robin_single_model():
    """单模型 group，每次请求都选唯一模型。"""
    from botflow.pipeline.strategies import RoundRobinStrategy

    ep1 = make_endpoint(model_id=1, weight=1.0)
    strategy = RoundRobinStrategy(params={})
    db = make_mock_db([ep1])
    cooldown = CooldownManager()
    messages = [{"role": "user", "content": "hello"}]

    RoundRobinStrategy._counters.clear()

    with patch("botflow.pipeline.strategies.load_endpoints", new_callable=AsyncMock, return_value=[ep1]), \
         patch("botflow.pipeline.strategies.filter_available", return_value=[ep1]), \
         patch("botflow.pipeline.strategies.truncate_messages", return_value=messages):
        for _ in range(5):
            result = await strategy.select_endpoints(messages, db, cooldown, group_id=1)
            assert result.endpoints[0].model_id == 1
            assert len(result.endpoints) == 1
```

### 2.5 cooldown 中的模型跳过

**场景**：3 个模型中 1 个在 cooldown，轮询只在剩余 2 个间切换。

```python
@pytest.mark.asyncio
async def test_round_robin_skips_cooldown():
    """cooldown 模型被过滤后，轮询在剩余模型间进行。"""
    from botflow.pipeline.strategies import RoundRobinStrategy

    ep1 = make_endpoint(model_id=1, weight=1.0)
    ep2 = make_endpoint(model_id=2, weight=1.0)  # cooldown
    ep3 = make_endpoint(model_id=3, weight=1.0)
    strategy = RoundRobinStrategy(params={})
    db = make_mock_db([ep1, ep2, ep3])
    cooldown = CooldownManager()
    messages = [{"role": "user", "content": "hello"}]

    RoundRobinStrategy._counters.clear()
    available = [ep1, ep3]  # ep2 filtered

    with patch("botflow.pipeline.strategies.load_endpoints", new_callable=AsyncMock, return_value=[ep1, ep2, ep3]), \
         patch("botflow.pipeline.strategies.filter_available", return_value=available), \
         patch("botflow.pipeline.strategies.truncate_messages", return_value=messages):
        r1 = await strategy.select_endpoints(messages, db, cooldown, group_id=1)
        r2 = await strategy.select_endpoints(messages, db, cooldown, group_id=1)

    assert r1.endpoints[0].model_id == 1
    assert r2.endpoints[0].model_id == 3
    # 第三次应回到 ep1
    with patch("botflow.pipeline.strategies.load_endpoints", new_callable=AsyncMock, return_value=[ep1, ep2, ep3]), \
         patch("botflow.pipeline.strategies.filter_available", return_value=available), \
         patch("botflow.pipeline.strategies.truncate_messages", return_value=messages):
        r3 = await strategy.select_endpoints(messages, db, cooldown, group_id=1)
    assert r3.endpoints[0].model_id == 1
```

### 2.6 execute 成功

**场景**：RoundRobinStrategy 的 execute 通过基类默认实现成功返回。

```python
@pytest.mark.asyncio
async def test_round_robin_execute_success():
    """execute 选择 endpoint 并成功调用 LLM。"""
    from botflow.pipeline.strategies import RoundRobinStrategy

    ep1 = make_endpoint(model_id=1, weight=1.0)
    strategy = RoundRobinStrategy(params={})
    db = make_mock_db([ep1])
    cooldown = CooldownManager()
    messages = [{"role": "user", "content": "hello"}]
    llm_response = {"choices": [{"message": {"content": "hi"}}]}

    RoundRobinStrategy._counters.clear()

    with patch("botflow.pipeline.strategies.load_endpoints", new_callable=AsyncMock, return_value=[ep1]), \
         patch("botflow.pipeline.strategies.filter_available", return_value=[ep1]), \
         patch("botflow.pipeline.strategies.truncate_messages", return_value=messages), \
         patch("botflow.pipeline.strategies.call_llm", new_callable=AsyncMock, return_value=llm_response) as mock_call:
        result = await strategy.execute(messages, db, cooldown, group_id=1)

    assert result == llm_response
    mock_call.assert_called_once()
```

---

## 3. SequentialStrategy 测试

### 3.1 select_endpoints 按 weight 降序排列

**场景**：3 个模型 weight 分别为 1.0, 3.0, 2.0，返回顺序应为 3.0 → 2.0 → 1.0。

```python
@pytest.mark.asyncio
async def test_sequential_weight_descending_order():
    """endpoints 按 weight 降序排列。"""
    from botflow.pipeline.strategies import SequentialStrategy

    ep_low = make_endpoint(model_id=1, weight=1.0, model_name="low")
    ep_high = make_endpoint(model_id=2, weight=3.0, model_name="high")
    ep_mid = make_endpoint(model_id=3, weight=2.0, model_name="mid")
    strategy = SequentialStrategy(params={})
    db = make_mock_db([ep_low, ep_high, ep_mid])
    cooldown = CooldownManager()
    messages = [{"role": "user", "content": "hello"}]

    with patch("botflow.pipeline.strategies.load_endpoints", new_callable=AsyncMock, return_value=[ep_low, ep_high, ep_mid]), \
         patch("botflow.pipeline.strategies.filter_available", return_value=[ep_low, ep_high, ep_mid]), \
         patch("botflow.pipeline.strategies.truncate_messages", return_value=messages):
        result = await strategy.select_endpoints(messages, db, cooldown, group_id=1)

    ids = [ep.model_id for ep in result.endpoints]
    assert ids == [2, 3, 1], f"Expected [2, 3, 1] (weight desc), got {ids}"
```

### 3.2 第一个可用 endpoint 排在最前

**场景**：验证 weight 最高的 endpoint 是 `result.endpoints[0]`。

```python
@pytest.mark.asyncio
async def test_sequential_highest_weight_first():
    """weight 最高的 endpoint 排在首位。"""
    from botflow.pipeline.strategies import SequentialStrategy

    ep1 = make_endpoint(model_id=1, weight=5.0)
    ep2 = make_endpoint(model_id=2, weight=1.0)
    strategy = SequentialStrategy(params={})
    db = make_mock_db([ep1, ep2])
    cooldown = CooldownManager()
    messages = [{"role": "user", "content": "hello"}]

    with patch("botflow.pipeline.strategies.load_endpoints", new_callable=AsyncMock, return_value=[ep1, ep2]), \
         patch("botflow.pipeline.strategies.filter_available", return_value=[ep1, ep2]), \
         patch("botflow.pipeline.strategies.truncate_messages", return_value=messages):
        result = await strategy.select_endpoints(messages, db, cooldown, group_id=1)

    assert result.endpoints[0].model_id == 1
    assert result.endpoints[0].detail.weight == 5.0
```

### 3.3 execute 成功（第一个就成功）

**场景**：第一个 endpoint 调用成功，不尝试后续。

```python
@pytest.mark.asyncio
async def test_sequential_execute_first_success():
    """第一个 endpoint 成功，不尝试后续。"""
    from botflow.pipeline.strategies import SequentialStrategy

    ep1 = make_endpoint(model_id=1, weight=3.0)
    ep2 = make_endpoint(model_id=2, weight=1.0)
    strategy = SequentialStrategy(params={})
    db = make_mock_db([ep1, ep2])
    cooldown = CooldownManager()
    messages = [{"role": "user", "content": "hello"}]
    llm_response = {"choices": [{"message": {"content": "hi"}}]}

    with patch("botflow.pipeline.strategies.load_endpoints", new_callable=AsyncMock, return_value=[ep1, ep2]), \
         patch("botflow.pipeline.strategies.filter_available", return_value=[ep1, ep2]), \
         patch("botflow.pipeline.strategies.truncate_messages", return_value=messages), \
         patch("botflow.pipeline.strategies.call_llm", new_callable=AsyncMock, return_value=llm_response) as mock_call:
        result = await strategy.execute(messages, db, cooldown, group_id=1)

    assert result == llm_response
    # call_llm 只调用了一次（第一个就成功）
    assert mock_call.call_count == 1
```

### 3.4 execute 第一个失败跳到第二个

**场景**：第一个 endpoint 调用返回 None，第二个成功。

```python
@pytest.mark.asyncio
async def test_sequential_execute_fallback_to_second():
    """第一个失败，跳到第二个成功。"""
    from botflow.pipeline.strategies import SequentialStrategy

    ep1 = make_endpoint(model_id=1, weight=3.0)
    ep2 = make_endpoint(model_id=2, weight=1.0)
    strategy = SequentialStrategy(params={})
    db = make_mock_db([ep1, ep2])
    cooldown = CooldownManager()
    messages = [{"role": "user", "content": "hello"}]
    llm_response = {"choices": [{"message": {"content": "hi"}}]}

    # call_llm: 第一次 None, 第二次成功
    call_count = 0
    async def fake_call_llm(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return None
        return llm_response

    with patch("botflow.pipeline.strategies.load_endpoints", new_callable=AsyncMock, return_value=[ep1, ep2]), \
         patch("botflow.pipeline.strategies.filter_available", return_value=[ep1, ep2]), \
         patch("botflow.pipeline.strategies.truncate_messages", return_value=messages), \
         patch("botflow.pipeline.strategies.call_llm", side_effect=fake_call_llm) as mock_call:
        result = await strategy.execute(messages, db, cooldown, group_id=1)

    assert result == llm_response
    assert mock_call.call_count == 2
```

### 3.5 全部失败 → ProviderError

**场景**：所有 endpoint 调用都返回 None，抛出 `ProviderError`。

```python
@pytest.mark.asyncio
async def test_sequential_execute_all_fail():
    """所有 endpoint 失败时抛出 ProviderError。"""
    from botflow.pipeline.strategies import SequentialStrategy
    from botflow.common.exceptions import ProviderError

    ep1 = make_endpoint(model_id=1, weight=2.0)
    ep2 = make_endpoint(model_id=2, weight=1.0)
    strategy = SequentialStrategy(params={})
    db = make_mock_db([ep1, ep2])
    cooldown = CooldownManager()
    messages = [{"role": "user", "content": "hello"}]

    with patch("botflow.pipeline.strategies.load_endpoints", new_callable=AsyncMock, return_value=[ep1, ep2]), \
         patch("botflow.pipeline.strategies.filter_available", return_value=[ep1, ep2]), \
         patch("botflow.pipeline.strategies.truncate_messages", return_value=messages), \
         patch("botflow.pipeline.strategies.call_llm", new_callable=AsyncMock, return_value=None):
        with pytest.raises(ProviderError, match="All endpoints failed"):
            await strategy.execute(messages, db, cooldown, group_id=1)
```

---

## 4. 注册表测试

### 4.1 策略注册到 STRATEGY_REGISTRY

**场景**：导入 `strategies.py` 后，3 个策略都在注册表中。

```python
def test_all_strategies_registered():
    """导入 strategies.py 后，3 个内建策略都注册到 STRATEGY_REGISTRY。"""
    from botflow.pipeline.base import STRATEGY_REGISTRY
    from botflow.pipeline.strategies import (
        RandomWeightsStrategy,
        RoundRobinStrategy,
        SequentialStrategy,
    )

    assert STRATEGY_REGISTRY["random_weights"] is RandomWeightsStrategy
    assert STRATEGY_REGISTRY["round_robin"] is RoundRobinStrategy
    assert STRATEGY_REGISTRY["sequential"] is SequentialStrategy
```

### 4.2 `__init__.py` 导出更新

**场景**：从 `botflow.pipeline` 可以直接导入 3 个策略类。

```python
def test_pipeline_init_exports():
    """botflow.pipeline 导出 3 个策略类。"""
    from botflow.pipeline import (
        RandomWeightsStrategy,
        RoundRobinStrategy,
        SequentialStrategy,
    )

    assert RandomWeightsStrategy is not None
    assert RoundRobinStrategy is not None
    assert SequentialStrategy is not None
```

---

## 测试覆盖汇总

| 测试编号 | 测试场景 | 策略 | 类型 |
|---------|---------|------|------|
| 1.1 | select_endpoints 返回 RouteResult | RandomWeights | 正例 |
| 1.2 | weighted random 分布正确 | RandomWeights | 正例 |
| 1.3 | cooldown 模型被跳过 | RandomWeights | 正例 |
| 1.4 | 全部 cooldown → NoAvailableModelError | RandomWeights | 反例 |
| 1.5 | context window 截断生效 | RandomWeights | 正例 |
| 1.6 | execute 成功 | RandomWeights | 正例 |
| 1.7 | execute 全失败 → ProviderError | RandomWeights | 反例 |
| 2.1 | 按顺序选择（首次） | RoundRobin | 正例 |
| 2.2 | 第二请求选下一个 | RoundRobin | 正例 |
| 2.3 | 计数器溢出保护 | RoundRobin | 边界 |
| 2.4 | 单模型 group | RoundRobin | 边界 |
| 2.5 | cooldown 模型跳过 | RoundRobin | 正例 |
| 2.6 | execute 成功 | RoundRobin | 正例 |
| 3.1 | 按 weight 降序排列 | Sequential | 正例 |
| 3.2 | 最高 weight 排最前 | Sequential | 正例 |
| 3.3 | execute 第一个成功 | Sequential | 正例 |
| 3.4 | execute 第一个失败跳第二个 | Sequential | 正例 |
| 3.5 | 全部失败 → ProviderError | Sequential | 反例 |
| 4.1 | 策略注册到 STRATEGY_REGISTRY | 注册表 | 正例 |
| 4.2 | __init__.py 导出 | 导出 | 正例 |

共 **20** 个测试场景（18 策略 + 2 注册/导出），覆盖正例、反例、边界值。
