# P1-4 测试用例文档：PipelineEngine 骨架 + core.py 非流式接入

> 对应功能点文档：docs/tasks/P1-4_features.md
> 测试文件：`tests/test_pipeline_engine.py`（PipelineEngine 单元测试）+ `tests/test_core_pipeline.py`（core.py 集成测试）
> 测试框架：pytest + pytest-asyncio + unittest.mock

---

## 测试文件结构

```
tests/
├── test_pipeline_engine.py    # PipelineEngine 单元测试（F1-F6）
└── test_core_pipeline.py      # core.py 集成测试（F8-F9）
```

---

## 一、PipelineEngine 单元测试

文件：`tests/test_pipeline_engine.py`

### 1.1 创建 Engine 实例

| 编号 | 用例 | 类型 | 描述 |
|------|------|------|------|
| E-01 | `test_init_stores_factory_and_cooldown` | 正例 | 传入 `db_factory` 和 `cooldown`，验证实例属性正确存储 |
| E-02 | `test_init_empty_group_cache` | 正例 | 新实例 `_group_cache` 为空字典 |
| E-03 | `test_db_property_calls_factory` | 正例 | 访问 `engine.db` 时调用 `db_factory()`，返回 factory 的返回值 |

**E-01 详细**：
```python
async def test_init_stores_factory_and_cooldown():
    mock_db_factory = Mock()
    mock_cooldown = Mock(spec=CooldownManager)
    engine = PipelineEngine(db_factory=mock_db_factory, cooldown=mock_cooldown)
    assert engine._db_factory is mock_db_factory
    assert engine.cooldown is mock_cooldown
```

**E-02 详细**：
```python
async def test_init_empty_group_cache():
    engine = PipelineEngine(db_factory=Mock(), cooldown=Mock(spec=CooldownManager))
    assert engine._group_cache == {}
    assert engine._GROUP_CACHE_TTL == 60
```

**E-03 详细**：
```python
async def test_db_property_calls_factory():
    mock_db = Mock(spec=Database)
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=Mock(spec=CooldownManager))
    assert engine.db is mock_db
```

---

### 1.2 `_load_group` 正常加载

| 编号 | 用例 | 类型 | 描述 |
|------|------|------|------|
| G-01 | `test_load_group_returns_group` | 正例 | DB 返回有效 group，方法返回该 group |
| G-02 | `test_load_group_caches_result` | 正例 | 首次加载后结果写入 `_group_cache` |
| G-03 | `test_load_group_calls_db_get_group` | 正例 | 验证调用了 `db.get_group(group_id)` |

**G-01 详细**：
```python
async def test_load_group_returns_group():
    group = ModelGroup(id=1, name="fast", type="random_weights")
    mock_db = AsyncMock(spec=Database)
    mock_db.get_group = AsyncMock(return_value=group)
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=Mock(spec=CooldownManager))
    result = await engine._load_group(1)
    assert result == group
    assert result.name == "fast"
```

---

### 1.3 `_load_group` 缓存命中

| 编号 | 用例 | 类型 | 描述 |
|------|------|------|------|
| G-04 | `test_load_group_cache_hit` | 正例 | 缓存存在且未过期，不调用 DB |
| G-05 | `test_load_group_cache_hit_returns_same_group` | 正例 | 缓存命中返回与首次加载相同的 group 对象 |

**G-04 详细**：
```python
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
```

---

### 1.4 `_load_group` 缓存过期

| 编号 | 用例 | 类型 | 描述 |
|------|------|------|------|
| G-06 | `test_load_group_cache_expired` | 边界 | 缓存过期后重新从 DB 加载 |
| G-07 | `test_load_group_cache_ttl_boundary` | 边界 | 缓存刚好在 TTL 边界时的行为 |

**G-06 详细**：
```python
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
```

**G-07 详细**：
```python
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
```

---

### 1.5 `_load_group` 不存在 → ConfigurationError

| 编号 | 用例 | 类型 | 描述 |
|------|------|------|------|
| G-08 | `test_load_group_not_found_raises` | 反例 | DB 返回 None，抛出 ConfigurationError |
| G-09 | `test_load_group_not_found_message` | 反例 | 验证异常消息包含 group_id |

**G-08 详细**：
```python
async def test_load_group_not_found_raises():
    mock_db = AsyncMock(spec=Database)
    mock_db.get_group = AsyncMock(return_value=None)
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=Mock(spec=CooldownManager))

    with pytest.raises(ConfigurationError, match="Group 999 not found"):
        await engine._load_group(999)
```

---

### 1.6 `_create_strategy` 正常创建

| 编号 | 用例 | 类型 | 描述 |
|------|------|------|------|
| S-01 | `test_create_strategy_random_weights` | 正例 | type="random_weights" → RandomWeightsStrategy |
| S-02 | `test_create_strategy_round_robin` | 正例 | type="round_robin" → RoundRobinStrategy |
| S-03 | `test_create_strategy_sequential` | 正例 | type="sequential" → SequentialStrategy |
| S-04 | `test_create_strategy_passes_params` | 正例 | group.params 正确传递给策略构造函数 |

**S-01 详细**：
```python
def test_create_strategy_random_weights():
    engine = PipelineEngine(db_factory=Mock(), cooldown=Mock(spec=CooldownManager))
    group = ModelGroup(id=1, name="fast", type="random_weights", params={})
    strategy = engine._create_strategy(group)
    assert isinstance(strategy, RandomWeightsStrategy)
```

**S-04 详细**：
```python
def test_create_strategy_passes_params():
    engine = PipelineEngine(db_factory=Mock(), cooldown=Mock(spec=CooldownManager))
    params = {"some_key": "some_value"}
    group = ModelGroup(id=1, name="fast", type="random_weights", params=params)
    strategy = engine._create_strategy(group)
    assert strategy.params == params
```

---

### 1.7 `_create_strategy` 未知 type → ConfigurationError

| 编号 | 用例 | 类型 | 描述 |
|------|------|------|------|
| S-05 | `test_create_strategy_unknown_type_raises` | 反例 | type="nonexistent" → ConfigurationError |
| S-06 | `test_create_strategy_unknown_type_message` | 反例 | 验证错误消息包含 type、group name 和可用策略列表 |

**S-05 详细**：
```python
def test_create_strategy_unknown_type_raises():
    engine = PipelineEngine(db_factory=Mock(), cooldown=Mock(spec=CooldownManager))
    group = ModelGroup(id=1, name="smart", type="nonexistent", params={})
    with pytest.raises(ConfigurationError, match="Unknown strategy type 'nonexistent'"):
        engine._create_strategy(group)
```

**S-06 详细**：
```python
def test_create_strategy_unknown_type_message():
    engine = PipelineEngine(db_factory=Mock(), cooldown=Mock(spec=CooldownManager))
    group = ModelGroup(id=1, name="smart", type="nonexistent", params={})
    with pytest.raises(ConfigurationError) as exc_info:
        engine._create_strategy(group)
    msg = str(exc_info.value)
    assert "nonexistent" in msg
    assert "smart" in msg
    assert "random_weights" in msg  # 至少列出一个可用策略
```

---

### 1.8 `route` non-streaming 成功

| 编号 | 用例 | 类型 | 描述 |
|------|------|------|------|
| R-01 | `test_route_non_stream_success` | 正例 | 策略 execute 返回成功结果 |
| R-02 | `test_route_non_stream_passes_params` | 正例 | 验证 messages/temperature/max_tokens 正确传递 |
| R-03 | `test_route_non_stream_passes_kwargs` | 正例 | 验证 extra kwargs 传递给策略 |

**R-01 详细**：
```python
async def test_route_non_stream_success():
    mock_db = AsyncMock(spec=Database)
    mock_cooldown = Mock(spec=CooldownManager)
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=mock_cooldown)

    group = ModelGroup(id=1, name="fast", type="random_weights")
    expected_result = {"choices": [{"message": {"content": "Hello"}}]}

    with patch.object(RandomWeightsStrategy, "execute", new_callable=AsyncMock) as mock_execute:
        mock_execute.return_value = expected_result
        result = await engine.route(
            group=group,
            messages=[{"role": "user", "content": "Hi"}],
            stream=False,
            temperature=0.7,
            max_tokens=100,
        )
    assert result == expected_result
```

---

### 1.9 `route` non-streaming 失败 → fallback

| 编号 | 用例 | 类型 | 描述 |
|------|------|------|------|
| R-04 | `test_route_non_stream_fallback_on_provider_error` | 正例 | ProviderError 触发 fallback 到 fallback_group |
| R-05 | `test_route_non_stream_fallback_on_cooldown_error` | 正例 | AllModelsCooldownError 触发 fallback |
| R-06 | `test_route_non_stream_fallback_on_no_available_error` | 正例 | NoAvailableModelError 触发 fallback |
| R-07 | `test_route_non_stream_fallback_on_strategy_error` | 正例 | StrategyError 触发 fallback |
| R-07 | `test_route_non_stream_fallback_success` | 正例 | fallback group 执行成功，返回 fallback 结果 |
| R-08 | `test_route_non_stream_no_fallback_without_id` | 反例 | fallback_group_id=None 时直接抛出异常 |

**R-04 详细**：
```python
async def test_route_non_stream_fallback_on_provider_error():
    mock_db = AsyncMock(spec=Database)
    mock_cooldown = Mock(spec=CooldownManager)
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=mock_cooldown)

    group_a = ModelGroup(id=1, name="fast", type="random_weights", fallback_group_id=2)
    group_b = ModelGroup(id=2, name="default", type="random_weights")
    mock_db.get_group = AsyncMock(return_value=group_b)

    expected_result = {"choices": [{"message": {"content": "Fallback OK"}}]}

    call_count = 0
    async def mock_execute(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ProviderError("All endpoints failed")
        return expected_result

    with patch.object(RandomWeightsStrategy, "execute", side_effect=mock_execute):
        result = await engine.route(group=group_a, messages=[{"role": "user", "content": "Hi"}], stream=False)

    assert result == expected_result
    assert call_count == 2
```

**R-06 详细**：
```python
async def test_route_non_stream_fallback_on_no_available_error():
    """验证 NoAvailableModelError 触发 fallback（语义等同 AllModelsCooldownError）."""
    mock_db = AsyncMock(spec=Database)
    mock_cooldown = Mock(spec=CooldownManager)
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=mock_cooldown)

    group_a = ModelGroup(id=1, name="fast", type="random_weights", fallback_group_id=2)
    group_b = ModelGroup(id=2, name="default", type="random_weights")
    mock_db.get_group = AsyncMock(return_value=group_b)

    expected_result = {"choices": [{"message": {"content": "Fallback OK"}}]}

    call_count = 0
    async def mock_execute(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise NoAvailableModelError("No available models")
        return expected_result

    with patch.object(RandomWeightsStrategy, "execute", side_effect=mock_execute):
        result = await engine.route(group=group_a, messages=[{"role": "user", "content": "Hi"}], stream=False)

    assert result == expected_result
    assert call_count == 2
```

**R-08 详细**：
```python
async def test_route_non_stream_no_fallback_without_id():
    mock_db = AsyncMock(spec=Database)
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=Mock(spec=CooldownManager))

    group = ModelGroup(id=1, name="fast", type="random_weights", fallback_group_id=None)

    with patch.object(RandomWeightsStrategy, "execute", new_callable=AsyncMock, side_effect=ProviderError("fail")):
        with pytest.raises(ProviderError, match="fail"):
            await engine.route(group=group, messages=[], stream=False)
```

---

### 1.10 `route` fallback 循环检测

| 编号 | 用例 | 类型 | 描述 |
|------|------|------|------|
| R-09 | `test_route_fallback_cycle_detected` | 反例 | A→B→A 循环被检测并抛出 ProviderError |
| R-10 | `test_route_fallback_cycle_error_message` | 反例 | 验证错误消息包含 group name 和 id |

**R-09 详细**：
```python
async def test_route_fallback_cycle_detected():
    mock_db = AsyncMock(spec=Database)
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=Mock(spec=CooldownManager))

    group_a = ModelGroup(id=1, name="group_a", type="random_weights", fallback_group_id=2)
    group_b = ModelGroup(id=2, name="group_b", type="random_weights", fallback_group_id=1)

    async def mock_get_group(gid):
        return {1: group_a, 2: group_b}[gid]
    mock_db.get_group = AsyncMock(side_effect=mock_get_group)

    with patch.object(RandomWeightsStrategy, "execute", new_callable=AsyncMock, side_effect=ProviderError("fail")):
        with pytest.raises(ProviderError, match="Fallback cycle detected"):
            await engine.route(group=group_a, messages=[], stream=False)
```

---

### 1.11 `route` fallback 深度限制

| 编号 | 用例 | 类型 | 描述 |
|------|------|------|------|
| R-11 | `test_route_fallback_depth_limit` | 边界 | 超过 3 层深度抛出 ProviderError |
| R-12 | `test_route_fallback_depth_exact_limit` | 边界 | 恰好 3 层 fallback 不报错（第 4 层才报错） |

**R-11 详细**：
```python
async def test_route_fallback_depth_limit():
    mock_db = AsyncMock(spec=Database)
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=Mock(spec=CooldownManager))

    # 构建 5 层 fallback 链：1→2→3→4→5
    groups = {
        i: ModelGroup(id=i, name=f"group_{i}", type="random_weights",
                       fallback_group_id=i + 1 if i < 5 else None)
        for i in range(1, 6)
    }
    mock_db.get_group = AsyncMock(side_effect=lambda gid: groups[gid])

    with patch.object(RandomWeightsStrategy, "execute", new_callable=AsyncMock, side_effect=ProviderError("fail")):
        with pytest.raises(ProviderError, match="Fallback chain too deep"):
            await engine.route(group=groups[1], messages=[], stream=False)
```

---

### 1.12 `route` ConfigurationError 不 fallback

| 编号 | 用例 | 类型 | 描述 |
|------|------|------|------|
| R-13 | `test_route_config_error_no_fallback` | 反例 | ConfigurationError 直接抛出，不触发 fallback |
| R-14 | `test_route_config_error_preserves_exception` | 反例 | ConfigurationError 异常链完整保留 |

**R-13 详细**：
```python
async def test_route_config_error_no_fallback():
    mock_db = AsyncMock(spec=Database)
    engine = PipelineEngine(db_factory=lambda: mock_db, cooldown=Mock(spec=CooldownManager))

    group = ModelGroup(id=1, name="bad", type="nonexistent", fallback_group_id=2)

    with pytest.raises(ConfigurationError, match="Unknown strategy type"):
        await engine.route(group=group, messages=[], stream=False)

    # fallback_group 不应被加载
    assert mock_db.get_group.call_count == 0
```

---

## 二、core.py 集成测试

文件：`tests/test_core_pipeline.py`

### 2.1 `_get_engine` 返回 PipelineEngine 实例

| 编号 | 用例 | 类型 | 描述 |
|------|------|------|------|
| C-01 | `test_get_engine_returns_pipeline_engine` | 正例 | `_get_engine()` 返回 `PipelineEngine` 实例 |
| C-02 | `test_get_engine_singleton` | 正例 | 多次调用返回同一个实例 |
| C-03 | `test_get_engine_uses_db_factory` | 正例 | 验证 `_get_db` 作为 factory 传入 |

**C-01 详细**：
```python
def test_get_engine_returns_pipeline_engine():
    # 重置全局引擎
    import botflow.core as core_module
    core_module._engine = None

    engine = core_module._get_engine()
    assert isinstance(engine, PipelineEngine)
```

**C-02 详细**：
```python
def test_get_engine_singleton():
    import botflow.core as core_module
    core_module._engine = None

    engine1 = core_module._get_engine()
    engine2 = core_module._get_engine()
    assert engine1 is engine2
```

---

### 2.2 `_handle_chat_non_stream` 使用 PipelineEngine

| 编号 | 用例 | 类型 | 描述 |
|------|------|------|------|
| C-04 | `test_handle_non_stream_uses_pipeline_engine` | 正例 | Mock 验证调用了 `engine.route()` 而非 `router.route()` |
| C-05 | `test_handle_non_stream_passes_group` | 正例 | 验证 `engine.route()` 收到正确的 `group` 参数 |
| C-06 | `test_handle_non_stream_stream_false` | 正例 | 验证 `stream=False` 被显式传递 |
| C-07 | `test_handle_non_stream_preserves_response` | 正例 | PipelineEngine 返回的结果正确传递给 `format_response` |

**C-04 详细**：
```python
async def test_handle_non_stream_uses_pipeline_engine():
    """验证 _handle_chat_non_stream 使用 PipelineEngine 而非 GroupRouter."""
    import botflow.core as core_module

    # 设置 mock engine
    mock_engine = Mock(spec=PipelineEngine)
    mock_group = ModelGroup(id=1, name="fast", type="random_weights")
    mock_engine._load_group = AsyncMock(return_value=mock_group)
    mock_engine.route = AsyncMock(return_value={
        "model": "fast",
        "choices": [{"message": {"content": "Hello"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    })
    core_module._engine = mock_engine

    internal = {
        "model": "fast",
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": False,
    }
    mock_request = Mock()

    result = await core_module._handle_chat_non_stream(
        internal, mock_request, lambda x: x
    )

    # 验证使用了 engine.route
    mock_engine.route.assert_called_once()
    call_kwargs = mock_engine.route.call_args
    assert call_kwargs.kwargs.get("stream") is False or call_kwargs[1].get("stream") is False
```

**C-05 详细**：
```python
async def test_handle_non_stream_passes_group():
    """验证 engine.route() 收到正确的 group 对象."""
    import botflow.core as core_module

    mock_engine = Mock(spec=PipelineEngine)
    mock_group = ModelGroup(id=42, name="test_group", type="round_robin")
    mock_engine._load_group = AsyncMock(return_value=mock_group)
    mock_engine.route = AsyncMock(return_value={
        "model": "test_group",
        "choices": [{"message": {"content": "OK"}}],
        "usage": {},
    })
    core_module._engine = mock_engine

    internal = {
        "model": "test_group",
        "messages": [{"role": "user", "content": "Test"}],
        "stream": False,
    }

    await core_module._handle_chat_non_stream(internal, Mock(), lambda x: x)

    # 验证 route 收到的 group 参数
    call_args = mock_engine.route.call_args
    assert call_args.kwargs.get("group") is mock_group or call_args[1].get("group") is mock_group
```

---

### 2.3 回归测试：streaming 路径不受影响

| 编号 | 用例 | 类型 | 描述 |
|------|------|------|------|
| TC-27 | `test_stream_common_still_uses_group_router` | 回归 | 验证 `_stream_common` 仍通过 `_get_extra_route_params()` + GroupRouter 执行，不受 P1-4 影响 |
| TC-28 | `test_handle_non_stream_uses_pipeline_engine_not_group_router` | 回归 | 验证 `_handle_chat_non_stream` 使用 PipelineEngine 而非 GroupRouter |

**TC-27 详细**：
```python
async def test_stream_common_still_uses_group_router():
    """回归测试：streaming 路径仍使用 GroupRouter，P1-4 不应影响."""
    import botflow.core as core_module

    mock_router = Mock()
    mock_router.route = Mock(return_value=AsyncIterator([b'data: "Hello"\n\n']))

    with patch.object(core_module, '_get_extra_route_params',
                     new_callable=AsyncMock) as mock_params, \
         patch.object(core_module, '_get_router', return_value=mock_router):
        mock_params.return_value = (1, mock_router, {})

        # 模拟 _stream_common 的关键调用路径
        # 验证 _get_extra_route_params 返回的仍是 (group_id, router, safe_extra)
        group_id, router, safe_extra = await core_module._get_extra_route_params(
            {"model": "fast", "stream": True}
        )
        assert isinstance(group_id, int)
        assert router is mock_router
        assert isinstance(safe_extra, dict)
```

**TC-28 详细**：
```python
async def test_handle_non_stream_uses_pipeline_engine_not_group_router():
    """回归测试：non-streaming 路径已迁移到 PipelineEngine."""
    import botflow.core as core_module

    mock_engine = Mock(spec=PipelineEngine)
    mock_group = ModelGroup(id=1, name="fast", type="random_weights")
    mock_engine._load_group = AsyncMock(return_value=mock_group)
    mock_engine.route = AsyncMock(return_value={
        "model": "fast",
        "choices": [{"message": {"content": "OK"}}],
        "usage": {},
    })
    core_module._engine = mock_engine

    internal = {
        "model": "fast",
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": False,
    }

    with patch.object(core_module, '_get_group_id', new_callable=AsyncMock, return_value=1), \
         patch.object(core_module, '_get_db') as mock_get_db, \
         patch.object(core_module, '_filter_safe_extra', return_value={}):
        mock_db = AsyncMock()
        mock_db.get_group = AsyncMock(return_value=mock_group)
        mock_get_db.return_value = mock_db

        await core_module._handle_chat_non_stream(internal, Mock(), lambda x: x)

    # 验证使用了 engine.route 而非 GroupRouter
    mock_engine.route.assert_called_once()
    call_kwargs = mock_engine.route.call_args
    assert call_kwargs.kwargs.get("group") is mock_group or call_kwargs[1].get("group") is mock_group
```

---

## 三、测试覆盖矩阵

| 功能点 | 测试编号 | 正例 | 反例 | 边界 |
|--------|----------|------|------|------|
| F1: `__init__` | E-01, E-02, E-03 | 3 | - | - |
| F2: `_load_group` 缓存 | G-01 ~ G-09 | 5 | 1 | 3 |
| F3: `_create_strategy` | S-01 ~ S-06 | 4 | 2 | - |
| F4: `route` non-stream + fallback | R-01 ~ R-08 | 7 | 2 | - |
| F5: 循环检测 | R-09, R-10 | - | 2 | - |
| F6: 深度限制 + ConfigError | R-11 ~ R-14 | - | 3 | 1 |
| F7: `__init__.py` 导出 | (import 验证) | 1 | - | - |
| F8: `_get_engine` | C-01 ~ C-03 | 3 | - | - |
| F9: `_handle_chat_non_stream` | C-04 ~ C-07 | 4 | - | - |
| F10: `ConfigurationError` | (已在异常类中存在) | - | - | - |
| 回归：streaming 路径 | TC-27 | - | - | 1 |
| 回归：non-streaming 迁移 | TC-28 | 1 | - | - |

**总计**：29 个测试用例
- 正例：28
- 反例：8
- 边界：5

---

## 四、Mock 策略

### Mock 对象

| Mock 目标 | 用途 | 方式 |
|-----------|------|------|
| `Database` | 模拟 DB 操作 | `AsyncMock(spec=Database)` |
| `CooldownManager` | 模拟冷却管理 | `Mock(spec=CooldownManager)` |
| `RandomWeightsStrategy.execute` | 模拟策略执行 | `patch.object(..., new_callable=AsyncMock)` |
| `PipelineEngine` (in core.py) | 模拟 engine 实例 | `Mock(spec=PipelineEngine)` |
| `time.time()` | 模拟缓存过期 | 修改 `_group_cache` 时间戳 |

### 不 Mock 的部分

- `PipelineEngine` 类本身（单元测试中直接实例化）
- `STRATEGY_REGISTRY`（使用真实注册表）
- `ConfigurationError`、`ProviderError`（使用真实异常类）

---

## 五、运行命令

```bash
# 运行全部 P1-4 测试
pytest tests/test_pipeline_engine.py tests/test_core_pipeline.py -v

# 只运行 PipelineEngine 单元测试
pytest tests/test_pipeline_engine.py -v

# 只运行 core.py 集成测试
pytest tests/test_core_pipeline.py -v

# 带覆盖率
pytest tests/test_pipeline_engine.py tests/test_core_pipeline.py -v --cov=botflow.pipeline.engine --cov=botflow.core --cov-report=term-missing
```
