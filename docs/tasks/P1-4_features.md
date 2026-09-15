# P1-4 功能点文档：PipelineEngine 骨架 + core.py 非流式接入

> 阶段：P1（PipelineEngine 骨架）+ P2（非流式接入 core.py）
> 依赖：P1-1（ModelGroup type/params）、P1-2（BaseStrategy + _shared.py）、P1-3（3 个内建策略）已完成
> 设计文档：docs/pipeline_router_design.md v2.0 第 6 节

---

## 概述

P1-4 实现 `PipelineEngine` 统一路由入口（非流式路径），并将 `core.py` 的 `_handle_chat_non_stream` 从 `GroupRouter` 切换到 `PipelineEngine`。流式路径（`_stream_common`）暂不改动，保持 GroupRouter。

---

## 功能点清单

### F1: `PipelineEngine.__init__` + db_factory + group 缓存

**文件**：`src/botflow/pipeline/engine.py`（新建）

**描述**：

创建 `PipelineEngine` 类，接受 `db_factory` 和 `cooldown` 两个构造参数：

```python
class PipelineEngine:
    def __init__(self, db_factory: Callable[[], Database], cooldown: CooldownManager):
        self._db_factory = db_factory
        self.cooldown = cooldown
        self._group_cache: dict[int, tuple[ModelGroup, float]] = {}
        self._GROUP_CACHE_TTL = 60
```

- `db_factory`：类型为 `Callable[[], Database]`，每次调用返回当前活跃的 `Database` 实例。使用工厂函数而非直接传入 `Database` 实例，避免长生命周期下 DB 连接失效。
- `cooldown`：`CooldownManager` 实例，与 `core.py` 中的全局 `_cooldown_manager` 共享。
- `_group_cache`：内存字典，key 为 `group_id`，value 为 `(ModelGroup, timestamp)` 元组。
- `_GROUP_CACHE_TTL`：缓存有效期，60 秒。

**db 属性**：

```python
@property
def db(self) -> Database:
    return self._db_factory()
```

每次访问 `self.db` 都通过工厂函数获取最新的 `Database` 实例。

**关键设计决策**：
- 不使用 async 初始化，`PipelineEngine` 的创建是同步的（仅存储引用）
- `_group_cache` 是实例级别，每个 `PipelineEngine` 实例独立缓存
- 60s TTL 是在设计文档审查中确定的（架构审查 P2 级别发现）

---

### F2: `PipelineEngine._load_group()` 带 60s TTL 缓存

**文件**：`src/botflow/pipeline/engine.py`

**描述**：

```python
async def _load_group(self, group_id: int) -> ModelGroup:
    now = time.time()
    cached = self._group_cache.get(group_id)
    if cached:
        group, ts = cached
        if now - ts < self._GROUP_CACHE_TTL:
            return group
    group = await self.db.get_group(group_id)
    if group is None:
        raise ConfigurationError(f"Group {group_id} not found")
    self._group_cache[group_id] = (group, now)
    return group
```

**行为**：
1. 先查内存缓存，命中且未过期则直接返回
2. 缓存未命中或过期，从 DB 加载
3. DB 返回 None → 抛出 `ConfigurationError`
4. 加载成功后写入缓存

**缓存更新策略**：写入即覆盖，不区分首次加载和过期重载。过期后下次访问自动刷新（lazy expiration）。

---

### F3: `PipelineEngine._create_strategy()` + 未知 type 报错

**文件**：`src/botflow/pipeline/engine.py`

**描述**：

```python
def _create_strategy(self, group: ModelGroup) -> BaseStrategy:
    strategy_cls = STRATEGY_REGISTRY.get(group.type)
    if strategy_cls is None:
        raise ConfigurationError(
            f"Unknown strategy type '{group.type}' for group '{group.name}'. "
            f"Available: {', '.join(sorted(STRATEGY_REGISTRY))}"
        )
    return strategy_cls(params=group.params)
```

**行为**：
1. 从 `STRATEGY_REGISTRY` 查找策略类
2. 未找到 → 抛出 `ConfigurationError`，错误信息包含未知 type、group name 和可用策略列表
3. 找到 → 实例化，传入 `group.params`

**关键设计决策**：
- `ConfigurationError` 不会触发 fallback（设计文档 6.3 节明确：`except ConfigurationError: raise`）
- 策略实例化是同步的（`__init__` 只存储 params）

---

### F4: `PipelineEngine.route()` non-streaming + fallback

**文件**：`src/botflow/pipeline/engine.py`

**描述**：

```python
async def route(
    self,
    group: ModelGroup,
    messages: list[dict],
    stream: bool = False,
    temperature: float | None = None,
    max_tokens: int | None = None,
    _fallback_depth: int = 0,
    _visited: set[int] | None = None,
    **kwargs,
) -> dict | StreamRouteResult:
```

**非流式路径**（P1-4 重点）：

1. 调用 `_create_strategy(group)` 获取策略实例
2. 调用 `strategy.execute(messages, db, cooldown, group.id, temperature, max_tokens, **kwargs)`
3. `execute()` 内部：`select_endpoints()` → 逐个 `call_llm()` → 返回第一个成功结果
4. 全部 endpoint 失败 → 抛出 `ProviderError`

**Fallback 逻辑**：

```python
try:
    # ... strategy.execute() 或 strategy.select_endpoints()
except (AllModelsCooldownError, NoAvailableModelError, ProviderError, StrategyError):
    if group.fallback_group_id:
        fallback_group = await self._load_group(group.fallback_group_id)
        return await self.route(
            fallback_group, messages, stream=stream,
            temperature=temperature, max_tokens=max_tokens,
            _fallback_depth=_fallback_depth + 1, _visited=_visited, **kwargs,
        )
    raise
except ConfigurationError:
    raise  # 配置错误不 fallback
```

**触发 fallback 的异常**：
- `AllModelsCooldownError`：所有模型在冷却中
- `NoAvailableModelError`：策略层报告无可用模型（与 `AllModelsCooldownError` 语义相同，都表示当前没有可用的模型端点）
- `ProviderError`：所有 endpoint 调用失败
- `StrategyError`：策略执行错误

**不触发 fallback 的异常**：
- `ConfigurationError`：配置错误直接抛出

---

### F5: Fallback 循环检测 `_visited: set`

**文件**：`src/botflow/pipeline/engine.py`

**描述**：

在 `route()` 方法顶部：

```python
if _visited is None:
    _visited = set()
if group.id in _visited:
    raise ProviderError(f"Fallback cycle detected: group {group.name} ({group.id})")
_visited.add(group.id)
```

**行为**：
- 第一次调用时 `_visited` 为 None，初始化为空 set
- 每次进入 `route()` 时将当前 `group.id` 加入 `_visited`
- 如果 `group.id` 已在 `_visited` 中，说明 fallback 链成环，抛出 `ProviderError`
- `_visited` 通过递归传递，整个 fallback 链共享同一个 set

**示例**：
```
Group A (fallback → B) → Group B (fallback → A)
route(A) → _visited={A}
  route(B) → _visited={A, B}
    route(A) → A in _visited → ProviderError: Fallback cycle detected
```

---

### F6: Fallback 深度限制 + ConfigurationError 不 fallback

**文件**：`src/botflow/pipeline/engine.py`

**描述**：

```python
if _fallback_depth > 3:
    raise ProviderError("Fallback chain too deep")
```

**深度限制**：
- 最大 fallback 深度为 3（即最多 4 层：原始 + 3 次 fallback）
- 超过限制抛出 `ProviderError`

**ConfigurationError 不 fallback**（已在 F4 中描述）：

```python
except ConfigurationError:
    raise  # 配置错误不 fallback
```

**组合防护**：
- 循环检测（F5）：防止 A→B→A 类死循环
- 深度限制（F6）：防止 A→B→C→D→... 无限链
- ConfigurationError 不 fallback：防止配置错误在 fallback 链中传播

---

### F7: `pipeline/__init__.py` 导出更新

**文件**：`src/botflow/pipeline/__init__.py`

**描述**：

在现有导出基础上新增 `PipelineEngine`：

```python
from botflow.pipeline.engine import PipelineEngine

__all__ = [
    # 现有导出
    "BaseStrategy",
    "RouteResult",
    "STRATEGY_REGISTRY",
    "StrategyError",
    "register_strategy",
    "RandomWeightsStrategy",
    "RoundRobinStrategy",
    "SequentialStrategy",
    # P1-4 新增
    "PipelineEngine",
]
```

**目的**：使 `from botflow.pipeline import PipelineEngine` 可用，供 `core.py` 导入。

---

### F8: `core.py`：`_get_engine()` 新增

**文件**：`src/botflow/core.py`

**描述**：

**新增 `_get_engine()`**：

```python
from botflow.pipeline import PipelineEngine

_engine: PipelineEngine | None = None

def _get_engine() -> PipelineEngine:
    global _engine
    if _engine is None:
        _engine = PipelineEngine(db_factory=_get_db, cooldown=_cooldown_manager)
    return _engine
```

- 单例模式，懒初始化
- `db_factory` 传入 `_get_db` 函数（非调用结果），保证每次获取最新的 DB 实例
- `cooldown` 传入全局 `_cooldown_manager`

**`_get_extra_route_params()` 保持不变**：

当前签名和返回值不变：`async def _get_extra_route_params(internal, stream=False) -> tuple[int, GroupRouter, dict]`

返回 `(group_id, GroupRouter, safe_extra)` 三元组。**不修改**此函数的返回类型或签名，因为 `_stream_common`（core.py:1060）通过解包三元组使用该函数，改签名会破坏流式路径。

**注意**：`_get_engine()` 不是 async 方法（PipelineEngine 初始化不需要 async）。

---

### F9: `core.py`：`_handle_chat_non_stream` 改用 PipelineEngine

**文件**：`src/botflow/core.py`

**描述**：

当前代码：

```python
async def _handle_chat_non_stream(internal, request, format_response):
    group_id, router, safe_extra = await _get_extra_route_params(internal)
    # ...
    result = await router.route(
        messages=internal["messages"],
        temperature=internal.get("temperature"),
        max_tokens=internal.get("max_tokens"),
        stream=False,
        **safe_extra,
    )
```

改造后：

```python
async def _handle_chat_non_stream(internal, request, format_response):
    model_name = internal.get("model", "")
    group_id = await _get_group_id({"model": model_name})
    db = _get_db()
    group = await db.get_group(group_id)
    engine = _get_engine()
    safe_extra = _filter_safe_extra(internal.get("extra", {}))
    
    result = await engine.route(
        group=group,
        messages=internal["messages"],
        temperature=internal.get("temperature"),
        max_tokens=internal.get("max_tokens"),
        stream=False,
        **safe_extra,
    )
    # ... 其余逻辑不变
```

**关键变化**：
- **不再调用 `_get_extra_route_params()`**，而是在函数内部直接获取 `engine` 和 `group`（避免修改 `_get_extra_route_params` 的返回值）
- `_get_engine()` 获取 PipelineEngine 单例
- `db.get_group(group_id)` 获取 `ModelGroup` 对象
- `_filter_safe_extra()` 过滤安全的 extra 参数
- `engine.route()` 需要传入 `group` 对象（而非之前的 `group_id`）
- `stream=False` 显式传递
- 返回值格式不变（`dict`），`_handle_chat_non_stream` 后续逻辑无需修改

**流式路径不受影响**：`_stream_common` 仍使用 `GroupRouter`，通过 `_get_router()` 和 `_get_extra_route_params()` 获取，完全不动。

---

### F10: 异常类 `ConfigurationError`

**文件**：`src/botflow/common/exceptions.py`

**描述**：

`ConfigurationError` 已存在于 `common/exceptions.py`（P1-1 已完成）：

```python
class ConfigurationError(BotflowError):
    """Raised when configuration is invalid."""
```

继承链：`ConfigurationError → BotflowError → Exception`

**在 P1-4 中的使用场景**：
- `_load_group()` 找不到 group 时抛出
- `_create_strategy()` 未知 strategy type 时抛出
- `route()` 中捕获 `ConfigurationError` 后直接 re-raise（不 fallback）

**无需修改**：该异常类已在 P1-1 中定义，P1-4 直接使用。

---

### F11: `_shared.py`：`call_llm()` 注入 `_routing` 字段

**文件**：`src/botflow/pipeline/_shared.py`

**描述**：

在 `call_llm()` 成功调用后，向返回的 `result` 字典注入 `_routing` 字段，记录实际使用的 endpoint 信息，便于调试和日志追踪：

```python
# 在 call_llm() 成功返回前注入
result["_routing"] = {
    "model_id": ep.model_id,
    "provider_id": ep.detail.provider_id,
}
return result
```

**注入时机**：LLM 调用成功、尚未返回给调用方时。

**字段含义**：
- `model_id`：实际命中的模型 ID（endpoint 对象上的 `model_id`）
- `provider_id`：实际使用的 provider ID（`ep.detail.provider_id`）

**目的**：
- `core.py` 的后续处理可从 `result["_routing"]` 获取路由决策信息，用于日志和响应头
- 避免在 `route()` 层面额外包装返回值

---

## 文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `src/botflow/pipeline/engine.py` | **新建** | PipelineEngine 类（F1-F6） |
| `src/botflow/pipeline/__init__.py` | **修改** | 新增 PipelineEngine 导出（F7） |
| `src/botflow/core.py` | **修改** | 新增 `_get_engine()`，`_handle_chat_non_stream` 改用 PipelineEngine（F8-F9） |
| `src/botflow/pipeline/_shared.py` | **修改** | `call_llm()` 注入 `_routing` 字段（F11） |

**不修改的文件**：
- `src/botflow/common/exceptions.py`：`ConfigurationError` 已存在（F10）
- `src/botflow/router.py`：`GroupRouter` 保留，流式路径仍使用
- `src/botflow/pipeline/base.py`：`BaseStrategy` + `STRATEGY_REGISTRY` 不变
- `src/botflow/pipeline/strategies.py`：3 个内建策略不变

---

## 向后兼容

| 场景 | 行为 |
|------|------|
| 非流式请求（OpenAI/Anthropic/Responses） | 切换到 PipelineEngine，行为等价 |
| 流式请求 | 仍使用 GroupRouter，无变化 |
| `_get_router()` | 保留但非流式路径不再调用 |
| `GroupRouter` 类 | 保留，流式路径和测试仍使用 |

---

## 依赖关系

```
P1-1 (ModelGroup type/params)
  ↓
P1-2 (BaseStrategy + _shared.py)
  ↓
P1-3 (3 个内建策略)
  ↓
P1-4 (PipelineEngine + core.py 非流式) ← 本文档
  ↓
P3 (core.py 流式迁移)
```
