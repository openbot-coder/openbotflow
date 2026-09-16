# Pipeline Router 重构设计文档

> 版本：2.0 | 日期：2026-09-01
> 状态：已实施并随 v3.0.0 发布（本文档为设计与实现记录，非待实施计划）
> 审查者：架构审查 + 兼容性审查 + 实现审查（3 轮交叉审查）

## 1. 目标

将 botflow 的路由引擎从硬编码的 `GroupRouter` 重构为可扩展的 **Pipeline Engine**，通过 `group.type` 字段选择路由策略，`group.params` 携带策略配置。

**核心原则**：
- 原有 `random_weights` 行为 100% 不变
- 新增路由策略只需实现一个 `Strategy` 接口
- 不引入重型依赖（LangGraph 作为独立的 `langgraph` 类型策略，实施时为 P6 硬依赖，见第 9 节）
- 所有策略共享 cooldown、retry、fallback、context truncation、call logging

---

## 2. 数据模型变更

### 2.1 `model_groups` 表新增字段

```sql
-- 新库：直接在 CREATE_TABLES_SQL 中定义
CREATE TABLE IF NOT EXISTS model_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL DEFAULT 'random_weights',
    params TEXT NOT NULL DEFAULT '{}',
    is_enabled INTEGER NOT NULL DEFAULT 1,
    fallback_group_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 旧库迁移（与现有 db.py 迁移模式一致：先试 SELECT，失败则 ALTER）
```

| 新字段 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `type` | TEXT | `random_weights` | 路由策略类型 |
| `params` | TEXT (JSON) | `{}` | 策略参数，结构由 type 决定 |

**迁移代码**（在 `db.py initialize()` 中，与现有 `context_window`/`api_format` 迁移模式一致）：

```python
try:
    await self._conn.execute("SELECT type FROM model_groups LIMIT 1")
except sqlite3.OperationalError:  # UNCOVERED: 旧库迁移路径
    await self._conn.execute(
        "ALTER TABLE model_groups ADD COLUMN type TEXT NOT NULL DEFAULT 'random_weights'"
    )
    await self._conn.execute(
        "ALTER TABLE model_groups ADD COLUMN params TEXT NOT NULL DEFAULT '{}'"
    )
```

### 2.2 Python Model 变更

```python
class ModelGroup(BaseModel):
    id: int = 0
    name: str
    description: str = ""
    type: str = "random_weights"    # 新增
    params: dict = Field(default_factory=dict)  # 新增
    is_enabled: bool = True
    fallback_group_id: Optional[int] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
```

### 2.3 `db.py` 必须同步更新的位置

| 位置 | 当前状态 | 需要的改动 |
|------|---------|-----------|
| `CREATE_TABLES_SQL` | 缺 `type`/`params` | 加入两列定义 |
| `_row_to_group()` | 未解析 `type`/`params` | 加入解析，`params` 需 `json.loads` + 防御性 try/except |
| `create_group()` INSERT | 缺新列 | 补全 `type`, `params` 列 |
| `_GROUP_UPDATE_COLUMNS` | 白名单缺字段 | 加入 `"type"`, `"params"` |
| `update_group()` | 无特殊处理 | `params` 为 dict 时自动 `json.dumps` |
| `update_group_raw()` | 签名缺参数 | 加 `type="random_weights"`, `params=None` 默认值 |
| `list_groups_with_models()` | 返回 dict 缺字段 | 补全 `type`, `params` |

**params JSON 防御性解析**：

```python
def _row_to_group(self, row: sqlite3.Row) -> ModelGroup:
    params_raw = row["params"]
    try:
        params = json.loads(params_raw) if params_raw else {}
    except (json.JSONDecodeError, TypeError):
        params = {}
    return ModelGroup(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        type=row["type"],
        params=params,
        is_enabled=bool(row["is_enabled"]),
        fallback_group_id=row["fallback_group_id"],
    )
```

### 2.4 `group_models` 表不变

现有 `group_models` 关联表保持不动。`random_weights`、`round_robin`、`sequential` 等策略仍然从 `group_models` 读取模型列表和权重。`langgraph` 策略可以不用 `group_models`，而是通过 `params.nodes` 中各节点的 `group_id` 引用其他 group。

**注意**：`langgraph` 类型的 group 在 `find_groups_by_model_name()` 中不可见（因为没有 `group_models` 关联）。`_get_group_id()` 的 step 2 无法匹配 langgraph 组，需要通过 step 1（精确 group name 匹配）路由。这是预期行为，应在文档中说明。

---

## 3. 策略接口

### 3.1 `BaseStrategy` 抽象基类

**设计原则**：strategy 只负责「选择 endpoint」，不负责「调用 LLM」。调用由 `_shared.py` 的独立函数完成。streaming 和 non-streaming 复用同一个 `select_endpoints()` 方法。

```python
from typing import NamedTuple

class RouteResult(NamedTuple):
    """策略选择结果：候选 endpoints + 准备好的 messages。"""
    endpoints: list[ModelEndpoint]   # 按优先级排序的候选列表
    messages: list[dict]             # 截断后的 messages
    temperature: float | None
    max_tokens: int | None
    extra_kwargs: dict


class BaseStrategy(ABC):
    """所有路由策略的基类。

    策略只做「选择」，不做「调用」。
    _shared.py 提供 call_llm() 等工具函数。
    """

    def __init__(self, params: dict):
        self.params = params

    @abstractmethod
    async def select_endpoints(
        self,
        messages: list[dict],
        db: Database,
        cooldown: CooldownManager,
        group_id: int,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs,
    ) -> RouteResult:
        """选择候选 endpoints 并准备调用参数。

        返回 RouteResult，包含按优先级排序的 endpoints 列表和截断后的 messages。
        strategy 不调用 LLM，只做选择。
        """
        ...

    async def execute(
        self,
        messages: list[dict],
        db: Database,
        cooldown: CooldownManager,
        group_id: int,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs,
    ) -> dict:
        """非流式便捷方法：select + 逐个尝试 call_llm，返回第一个成功的结果。"""
        result = await self.select_endpoints(messages, db, cooldown, group_id, temperature, max_tokens, **kwargs)
        for ep in result.endpoints:
            resp = await call_llm(ep, result.messages, group_id, cooldown,
                                  result.temperature, result.max_tokens, **result.extra_kwargs)
            if resp is not None:
                return resp
        raise ProviderError(f"All endpoints failed in strategy for group {group_id}")
```

**关键设计决策**：
- `db` 和 `cooldown` 通过方法参数传入，不在 `__init__` 中存储（避免 db 实例过期问题）
- `group_id` 在方法参数中传递（strategy 需要知道 group_id 用于 cooldown key，但不存为实例属性）
- `select_endpoints()` 是核心抽象，streaming 和 non-streaming 都用它
- `execute()` 是 default implementation（select + call 循环），简单策略不需要覆盖

### 3.2 策略注册表

```python
STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {}

def register_strategy(name: str, cls: type[BaseStrategy]) -> None:
    if name in STRATEGY_REGISTRY:
        raise ValueError(f"Strategy '{name}' already registered")
    STRATEGY_REGISTRY[name] = cls

# 内建策略
register_strategy("random_weights", RandomWeightsStrategy)
register_strategy("round_robin", RoundRobinStrategy)
register_strategy("sequential", SequentialStrategy)

# langgraph 策略：langgraph 为硬依赖，直接注册，无 ImportError 兜底
from botflow.pipeline.langgraph_strategy import LangGraphStrategy
register_strategy("langgraph", LangGraphStrategy)
```

---

## 4. 各策略详细设计

### 4.1 `random_weights`（现有逻辑，零改动）

**行为**：与原 `GroupRouter._route_non_stream` 完全一致——该类已删除，逻辑现为 `RandomWeightsStrategy`。

```
Load endpoints → Filter cooldown → Weighted random select → Context truncation
```

**params**：不需要 params（从 `group_models` 读权重）。

```json
{
  "name": "fast",
  "type": "random_weights",
  "params": {}
}
```

**实现**：核心逻辑已从 `GroupRouter._route_non_stream` 搬至 `strategies.py`，变为 `select_endpoints()` 的实现；`GroupRouter` 类本身已删除。

### 4.2 `round_robin`（轮询）

**行为**：按固定顺序轮询 group_models 中的模型。用内存计数器 + group_id 做 key，每次请求递增。

**params**：不需要 params。

```json
{
  "name": "balanced",
  "type": "round_robin",
  "params": {}
}
```

**边界情况**：
- 重启后计数器归零，从第一个模型重新开始（可接受，round_robin 不保证精确均匀）
- 单 worker 模式下无问题（当前 botflow 用 `uvicorn.Config(loop="asyncio")` 单 worker）
- 计数器加溢出保护：`(idx + 1) % (len(endpoints) * 1000)`

### 4.3 `sequential`（顺序降级，原 `fallback_chain`）

**重命名原因**：`fallback_chain` 与 engine 层的 group-level `fallback_group_id` 概念混淆。改为 `sequential` 明确表示"按固定顺序尝试"。

**行为**：按 `group_models` 中的顺序（weight 降序）依次尝试，第一个成功就返回。

**params**：不需要 params。

```json
{
  "name": "reliable",
  "type": "sequential",
  "params": {}
}
```

### 4.4 `langgraph`（自定义图路由，P6）

**行为**：执行 `params` 中定义的有向图，每个节点是一次 LLM 调用，由边（可选条件）决定流转顺序。

**params**：携带图定义（顶层 `nodes` / `edges` / `entry` / `final`，没有 `graph` 包装层）。

```json
{
  "name": "smart-router",
  "type": "langgraph",
  "params": {
    "entry": "analyze",
    "final": "respond",
    "nodes": {
      "analyze": {
        "prompt": "分析以下内容: {messages}",
        "group_id": null
      },
      "respond": {
        "prompt": "基于分析结果回复: {state}",
        "group_id": null
      }
    },
    "edges": [["analyze", "respond"]]
  }
}
```

**节点与边的字段**：

| 字段 | 作用 |
|------|------|
| `nodes.<name>.prompt` | 该节点的 prompt 模板，支持 `{messages}` 与 `{state}` 两个占位符（未知占位符报 `ConfigurationError`） |
| `nodes.<name>.group_id` | 该节点使用的 group；`null` 表示沿用当前 group |
| `edges` | 边列表，元素为 `[from, to]` 或 `[from, to, condition]`；节点可有 0 / 1 / 多条出边 |
| `entry` | 起始节点名，必须存在于 `nodes` |
| `final` | 哪个节点的输出作为最终响应；缺省时取最后一个无出边的节点 |

**执行语义**：

- 节点只有一种形态：调用该节点 group 的一个可用 endpoint（`filter_available` + `truncate_messages` + `call_llm`）
- 节点输出写入 state（key 为节点名），可被后续节点的 `{state}` 引用
- 多条出边时按 `condition` 做**子串匹配**（`condition in 上一节点输出`）选择下一个节点；全部不匹配则走第一条（默认分支）
- `to` 可为 `__end__` / `END` 结束图；节点无出边即结束
- `MAX_STEPS = 50` 防环，超出报 `StrategyError`
- 条件不做表达式求值（没有 `eval` / AST 白名单），仅子串匹配

**langgraph 为硬依赖**（见第 9 节）：`LangGraphStrategy` 在 `langgraph_strategy.py` 被 import 时无条件注册，`GET /admin/strategies` 始终包含 `langgraph`。

---

## 5. `_shared.py` — 共享基础设施

所有共享函数为**独立的 async 函数**（不是 BaseStrategy 的方法），策略通过参数调用。

**全局状态（缓存/信号量）的单一事实源是 `router.py`**，`_shared.py` 只做 re-export：

```python
# pipeline/_shared.py — 自 router.py 导入，不重复定义
from botflow.router import (
    _provider_semaphores,
    _endpoint_cache,
    _ENDPOINT_CACHE_TTL,   # 60
    _provider_cache,
    _PROVIDER_CACHE_TTL,   # 300
)
```

**共享函数**：

```python
async def load_endpoints(group_id: int, db: Database) -> list[ModelEndpoint]:
    """从 DB 加载 group 的所有 enabled endpoints（带缓存）。"""
    # 1:1 复用现有 _endpoint_cache 逻辑和 _get_cached_provider

def filter_available(
    endpoints: list[ModelEndpoint],
    cooldown: CooldownManager,
    group_id: int,
) -> list[ModelEndpoint]:
    """过滤掉 cooldown 中的 endpoints。"""

async def call_llm(
    ep: ModelEndpoint,
    messages: list[dict],
    group_id: int,
    cooldown: CooldownManager,
    temperature: float | None = None,
    max_tokens: int | None = None,
    **kwargs,
) -> dict | None:
    """调用单个 endpoint，带 retry + cooldown + 信号量管理。

    从 GroupRouter._attempt_call 1:1 搬过来（该类已删除）。
    信号量是全局跨 group 共享的——同一个 provider 跨 group 限流。
    """
    kwargs = _apply_model_extra_config(kwargs, ep.detail.extra_config)
    sem = _ensure_provider_semaphore(ep.detail.provider_id, get_config().upstream_semaphore_size)

    last_error = None
    for attempt in range(ep.max_retries):
        try:
            async with sem or _noop_asynccontext():
                result = await ep.provider.chat(
                    messages=messages, model=ep.detail.model_name,
                    temperature=temperature, max_tokens=max_tokens, **kwargs,
                )
            cooldown.record_success(group_id, ep.model_id)
            return result
        except Exception as e:
            last_error = e
            if is_retryable_error(e) and attempt < ep.max_retries - 1:
                await exponential_backoff(attempt)
                continue
            break

    cooldown.record_failure(group_id, ep.model_id, ep.cooldown_threshold, ep.cooldown_seconds)
    return None

def truncate_messages(
    messages: list[dict],
    endpoints: list[ModelEndpoint],
    max_tokens: int | None,
) -> list[dict]:
    """Context window 截断（取 available endpoints 最小 context_window）。"""

def invalidate_endpoint_cache(group_id: int) -> None:
    """Admin 修改 group_models 后调用，清除缓存。"""
    _endpoint_cache.pop(group_id, None)
```

**关键决策**：
- provider 信号量 `_provider_semaphores` 定义在 `router.py`（`_shared.py` re-export），不能在 strategy 里——它是全局跨 group 共享的
- `_endpoint_cache` 同样定义在 `router.py`，与 `_provider_cache` 一起管理，`_shared.py` 只 re-export
- `call_llm` 是独立函数，测试时不需要实例化 strategy

---

## 6. `PipelineEngine` — 统一入口

### 6.1 架构

```
请求
  │
  ▼
PipelineEngine.route(group, messages, ...)          # 非流式 → dict
PipelineEngine.route_stream(group, messages, ...)   # 流式 → 候选 endpoints dict
  │
  ├─ 查策略注册表 → strategy_cls = STRATEGY_REGISTRY[group.type]
  │
  ├─ strategy = strategy_cls(group.params) → 由图选出候选 endpoints
  │
  ├─ 非流式：strategy.execute(messages, ...)                     → dict
  │  流式：{endpoints, group_id, messages, temperature, max_tokens, kwargs, fallback_group_id}
  │
  └─ 失败时 → engine 层 fallback（跨策略类型）
```

### 6.2 代码结构

```
src/botflow/
├── router.py                     # 保留：CooldownManager, ModelEndpoint,
│                                 #        weighted_random_select/order, retry 工具函数、
│                                 #        endpoint/provider 缓存与信号量
│                                 #        （GroupRouter 类已删除）
│
├── pipeline/
│   ├── __init__.py               # 导出 PipelineEngine
│   ├── engine.py                 # PipelineEngine（统一入口，代理到 LangGraphEngine）
│   ├── langgraph_engine.py       # LangGraphEngine：StateGraph 节点 + 重试/降级生命周期
│   ├── base.py                   # BaseStrategy ABC + RouteResult + STRATEGY_REGISTRY
│   ├── _shared.py                # load_endpoints, call_llm, filter_available, etc.
│   ├── strategies.py             # RandomWeightsStrategy + RoundRobinStrategy + SequentialStrategy
│   └── langgraph_strategy.py     # LangGraphStrategy（P6，硬依赖）
```

**文件数从 9 个精简到 7 个**：3 个内建策略合并到 `strategies.py`（总共不到 100 行），`graph_types.py` 延迟到 P6。

### 6.3 PipelineEngine 实现

```python
class PipelineEngine:
    def __init__(self, db_factory: Callable[[], Database], cooldown: CooldownManager):
        self._db_factory = db_factory
        self.cooldown = cooldown
        self._group_cache: dict[int, tuple[ModelGroup, float]] = {}
        self._GROUP_CACHE_TTL = 60

    @property
    def db(self) -> Database:
        return self._db_factory()

    async def _load_group(self, group_id: int) -> ModelGroup:
        """加载 group（带缓存）。"""
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

    def _create_strategy(self, group: ModelGroup) -> BaseStrategy:
        strategy_cls = STRATEGY_REGISTRY.get(group.type)
        if strategy_cls is None:
            raise ConfigurationError(
                f"Unknown strategy type '{group.type}' for group '{group.name}'. "
                f"Available: {', '.join(sorted(STRATEGY_REGISTRY))}"
            )
        return strategy_cls(params=group.params)

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
    ) -> dict:
        """统一路由入口。"""
        if _fallback_depth > 3:
            raise ProviderError("Fallback chain too deep")
        if _visited is None:
            _visited = set()
        if group.id in _visited:
            raise ProviderError(f"Fallback cycle detected: group {group.name} ({group.id})")
        _visited.add(group.id)

        strategy = self._create_strategy(group)

        try:
            if stream:
                return await strategy.select_endpoints(
                    messages, self.db, self.cooldown, group.id, temperature, max_tokens, **kwargs
                )
            else:
                return await strategy.execute(
                    messages, self.db, self.cooldown, group.id, temperature, max_tokens, **kwargs
                )
        except (AllModelsCooldownError, ProviderError, StrategyError):
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

### 6.4 core.py 改动

```python
# 之前
from botflow.router import GroupRouter

async def _get_router(group_id: int) -> GroupRouter:
    db = _get_db()
    group = await db.get_group(group_id)
    fallback_group_id = group.fallback_group_id if group else None
    return GroupRouter(group_id=group_id, db=db, cooldown_manager=_cooldown_manager, fallback_group_id=fallback_group_id)

# 之后
from botflow.pipeline import PipelineEngine

_engine: PipelineEngine | None = None

def _get_engine() -> PipelineEngine:
    global _engine
    if _engine is None:
        _engine = PipelineEngine(db_factory=_get_db, cooldown=self._cooldown_manager)
    return _engine

# route() 调用处改为：
engine = _get_engine()
group = await engine._load_group(group_id)
result = await engine.route(group=group, messages=..., temperature=..., stream=...)
```

**注意**：`_get_engine()` 不需要 async（PipelineEngine 初始化不需要 async），用 db_factory 而非 db 实例避免重连失效。

### 6.5 Streaming 路径处理

**`core.py` `_stream_common` 的流式 fallback 逻辑（110+ 行）分两阶段迁移（两阶段均已完成）**：

**第一阶段（P2）**：非流式先迁移，流式暂保持 GroupRouter（中间状态，已被 P3 取代）
```
core.py _handle_chat_non_stream → PipelineEngine.route(stream=False)
core.py _stream_common → GroupRouter（仅限第一阶段，该类现已删除）
```

**第二阶段（P3）**：流式迁移到 PipelineEngine
```
core.py _stream_common → PipelineEngine.route_stream() → 候选 endpoints dict
                        → 逐个 endpoint 尝试（逻辑仍留在 core.py）
```

**流式中的 cooldown 记录**：`_stream_common` 中的 `cooldown.record_success/failure` 需要通过 engine 的 cooldown manager 访问。

### 6.6 `_get_group_id` 不改

`_get_group_id()` 只负责 "请求模型名 → group_id" 的解析，跟路由策略无关。保持不变。

**注意**：`_get_group_id` 内部的 `list_groups()` 和 `engine._load_group()` 会有两次 DB 查询，但 SQLite WAL 模式下这是可接受的（同步读操作，微秒级）。

---

## 7. Admin API 变更

### 7.1 创建 Group

```
POST /admin/groups
  name: "smart-router"
  description: "Intent-based routing"
  type: "langgraph"              # 新增，可选，默认 "random_weights"
  params: {"entry": "classify"}  # 新增，JSON 对象（Pydantic body 字段）
  fallback_group_id: null
```

**params 传递方式**：Pydantic request body 模型（`CreateGroupReq.params: Optional[dict]`），直接传 JSON 对象。create 端点用 `Body(embed=True)`，payload 形如 `{"req": {...}}`；PATCH 用扁平的 `UpdateGroupReq`。

### 7.2 更新 Group

```
PATCH /admin/groups/{id}
  # 可更新 type 和 params
  type: "round_robin"
  params: {}
```

### 7.3 获取 Group

```
GET /admin/groups/{id}/details
  返回中包含 type 和 params 字段（model_dump() 自动包含）
```

### 7.4 新增：策略类型查询

```
GET /admin/strategies
  返回已注册的策略类型名（字典序）：
  {"success": true, "strategies": ["langgraph", "random_weights", "round_robin", "sequential"]}
```

---

## 8. 向后兼容

### 8.1 现有 API 行为不变

| 场景 | 行为 |
|------|------|
| 现有 group（无 type 字段） | DB migration 默认 `type=random_weights`，行为不变 |
| 现有 `group_models` 关联表 | 所有策略共用，`langgraph` 类型可不使用 |
| 现有 `_get_group_id()` 路由 | 不变：model name → group name → group id |
| 现有 call_logs | 不变：pipeline engine 仍记录 group_id, model_id, provider_id |
| 现有 `/v1/models` 端点 | 不变：`langgraph` 类型 group 显示空 model_names |

### 8.2 现有测试

| 测试文件 | 影响 |
|---------|------|
| `test_router.py` | **不改**：`weighted_random_select` 等纯函数保留 |
| `test_router_full.py` | **已改**：标题为「Full coverage tests for the routing engine (PipelineEngine + helpers)」，改为构造 `PipelineEngine` |
| `test_group_routing.py` | **不改**：集成测试（random_weights 行为不变） |
| `test_admin_api.py` | **需新增**：type/params 的 CRUD 用例 |
| `test_db.py` | **需修改**：断言 `group.type` 和 `group.params` |
| `test_group_compat_and_models.py` | **需新增**：langgraph group 的路由行为 |

### 8.3 Fallback 机制

`fallback_group_id` 在所有策略中通用，且**可跨策略类型**：

```
请求 → smart-router (langgraph)
         │
         └─ 全部失败 → fallback → default (random_weights)
```

**防护措施**：
- 深度限制：`_fallback_depth > 3` 停止
- 循环检测：`_visited: set[int]` 防止 A→B→A 循环
- 配置错误不 fallback：`ConfigurationError` 直接抛出

**建议**：fallback group 应引用不同的模型集合，避免循环调用已失败的模型。

---

## 9. 依赖

| 依赖 | 必须/可选 | 说明 |
|------|----------|------|
| `langgraph>=0.2.0` | **必须** | 路由图引擎与 `langgraph` 类型策略都依赖它 |
| `langchain-core` | 传递引入 | 由 `langgraph` 依赖链带入 |

实施时按决策改为 hard 依赖：安装即用，不再「未安装则静默跳过」——`pipeline/__init__.py` 无条件 import `LangGraphStrategy`。

```toml
# pyproject.toml
dependencies = [
    ...
    "langgraph>=0.2.0",
]

[project.optional-dependencies]
deepseek = ["deepseek>=1.0.0"]   # 唯一保留的 extra
```

安装方式：`pip install botflow`（无需 extra）

---

## 10. 实施阶段

| 阶段 | 内容 | 验证标准 | 风险 |
|------|------|---------|------|
| **P1** | DB migration + Model 变更 + pipeline/ 目录 + BaseStrategy + _shared.py + PipelineEngine 骨架 | 现有测试全部通过（不改 core.py，不改任何行为） | 低 |
| **P2** | RandomWeightsStrategy + core.py **非流式**接入 | test_router.py 纯函数不变；新增 tests/test_pipeline_strategies.py；非流式路径改用 PipelineEngine；流式路径**暂不改** | 低 |
| **P3** | core.py **流式**路径迁移到 PipelineEngine | 所有 streaming 测试通过；_stream_common 不再直接引用 GroupRouter | 中 |
| **P4** | RoundRobinStrategy + SequentialStrategy | 新增测试覆盖轮询、顺序降级、部分失败 | 低 |
| **P5** | Admin API 支持 type/params + /admin/strategies | 创建 round_robin group → 路由成功；修改 group type 热切换生效 | 低 |
| **P6** | LangGraphStrategy | 依赖 langgraph 的集成测试 | 中 |
| **P7** | 清理 GroupRouter（已完成：类删除，`router.py` 仅剩基础设施） | 所有测试通过 | 低 |

**P1-P5 为第一期**，不引入任何新依赖，只重构内部结构。
**P6 为第二期**，引入 langgraph（hard 依赖）。
**P7 为清理期**（已完成），在第二期稳定后执行。

---

## 11. 文件变更清单

### 新增文件

```
src/botflow/pipeline/__init__.py         # 导出 PipelineEngine
src/botflow/pipeline/engine.py           # PipelineEngine
src/botflow/pipeline/base.py             # BaseStrategy + RouteResult + STRATEGY_REGISTRY
src/botflow/pipeline/_shared.py          # load_endpoints, call_llm, filter_available, etc.
src/botflow/pipeline/strategies.py       # RandomWeightsStrategy + RoundRobinStrategy + SequentialStrategy
src/botflow/pipeline/langgraph_strategy.py # LangGraphStrategy (P6)

docs/pipeline_router_design.md           # 本文档
```

### 修改文件

```
src/botflow/storage/models.py            # ModelGroup 加 type, params
src/botflow/storage/db.py                # migration + CRUD 支持新字段（7 处改动）
src/botflow/core.py                      # _get_router → PipelineEngine（非流式 P2，流式 P3）
src/botflow/admin_api.py                 # group CRUD 支持 type/params + /admin/strategies
src/botflow/router.py                    # 删除 GroupRouter，仅保留基础设施（P7）
pyproject.toml                           # 新增 langgraph 硬依赖
```

### 不变文件

```
src/botflow/protocol_adapter.py          # 协议适配层
src/botflow/auth.py                      # 鉴权中间件
src/botflow/config.py                    # 配置管理
src/botflow/common/context.py            # Context truncation
src/botflow/providers/*.py               # Provider 实现
```

---

## 12. 测试计划

### 第一期测试（P1-P5）

| 测试文件 | 覆盖 |
|---------|------|
| `tests/test_pipeline_engine.py` | PipelineEngine 分发逻辑、strategy 注册、未知 type 报错、fallback 循环检测 |
| `tests/test_pipeline_strategies.py` | 三个内建策略：random_weights 行为与原 `test_router.py` 一致、顺序轮询与计数器溢出、顺序降级与部分/全部失败 |
| `tests/test_pipeline_migration.py` | DB migration 正确性、旧数据兼容、params JSON 解析 |
| `tests/test_pipeline_base.py` | BaseStrategy、call_llm、load_endpoints、filter_available 独立测试 |
| `tests/test_router_full.py` | 纯函数与基础设施（endpoint cache、retry 等），见 8.2 |

### 第二期测试（P6）

| 测试文件 | 覆盖 |
|---------|------|
| `tests/test_langgraph_engine.py` | LangGraphEngine 非流式/流式路由、fallback 循环检测、kwargs 透传 |

---

## 13. 审查记录

### 架构审查关键发现
- **P0**：execute/select 分离，streaming 不走统一 execute
- **P0**：_call_llm 应为 _shared.py 独立函数，不在 BaseStrategy 上
- **P1**：3 个内建策略合并到 strategies.py（不到 100 行）
- **P1**：FallbackChainStrategy 改名 SequentialStrategy（避免概念混淆）
- **P2**：group 缓存 60s TTL
- **P2**：graph_types.py 延迟到 P6（YAGNI）

### 兼容性审查关键发现
- `_row_to_group` 必须解析 `type`/`params`（否则所有读取 group 的地方丢失新字段）
- `create_group` INSERT 必须包含新列
- `CREATE_TABLES_SQL` 必须同步更新
- `_GROUP_UPDATE_COLUMNS` 白名单必须扩展
- Admin API `params` 用 JSON 字符串传递（与现有 query param 风格一致）（实施时改为 Pydantic body 模型，见 7.1）
- Fallback 需检测循环（visited set）
- langgraph group 在 `find_groups_by_model_name` 中不可见（预期行为）

### 实现审查关键发现
- provider 信号量 `_provider_semaphores` 必须在 `_shared.py`（跨 group 共享）（实施时保留在 `router.py`，由 `_shared.py` re-export，见第 5 节）
- context_window 截断放在 `_shared.py` 的 `truncate_messages()` 函数中
- PipelineEngine 用 db_factory 而非 db 实例（避免重连失效）
- Streaming 路径分两阶段迁移（P2 非流式，P3 流式）
- round_robin 计数器加溢出保护
- endpoint_cache 在 Admin 修改 group_models 后需 invalidation
