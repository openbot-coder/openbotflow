# Pipeline Router 重构设计文档

> 版本：2.0 | 日期：2026-09-01
> 状态：审查完成，待实施
> 审查者：架构审查 + 兼容性审查 + 实现审查（3 轮交叉审查）

## 1. 目标

将 botflow 的路由引擎从硬编码的 `GroupRouter` 重构为可扩展的 **Pipeline Engine**，通过 `group.type` 字段选择路由策略，`group.params` 携带策略配置。

**核心原则**：
- 原有 `random_weights` 行为 100% 不变
- 新增路由策略只需实现一个 `Strategy` 接口
- 不引入重型依赖（LangGraph 作为可选的 `langgraph` 类型策略）
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

现有 `group_models` 关联表保持不动。`random_weights`、`round_robin`、`sequential` 等策略仍然从 `group_models` 读取模型列表和权重。`langgraph` 策略可以不用 `group_models`，而是通过 `params.graph.nodes` 引用其他 group。

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

# 可选策略（langgraph 未安装时静默跳过）
def _register_optional():
    try:
        from botflow.pipeline.langgraph_strategy import LangGraphStrategy
        register_strategy("langgraph", LangGraphStrategy)
    except ImportError:
        pass

_register_optional()
```

---

## 4. 各策略详细设计

### 4.1 `random_weights`（现有逻辑，零改动）

**行为**：与当前 `GroupRouter._route_non_stream` 完全一致。

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

**实现**：核心逻辑从 `GroupRouter._route_non_stream` 搬过来，变为 `select_endpoints()` 的实现。

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

### 4.4 `langgraph`（自定义图路由，P5）

**行为**：执行用户定义的 StateGraph，图中的节点可以是 LLM 调用、条件分支、数据变换等。

**params**：携带完整的 LangGraph 图定义。

```json
{
  "name": "smart-router",
  "type": "langgraph",
  "params": {
    "entry": "classify",
    "nodes": {
      "classify": {
        "type": "llm_call",
        "group": "fast",
        "system_prompt": "判断用户意图。只输出一个词：code、search 或 chat。",
        "input_key": "user_message",
        "output_key": "intent"
      },
      "code_handler": {
        "type": "llm_call",
        "group": "code-expert",
        "input_key": "messages"
      },
      "chat_handler": {
        "type": "llm_call",
        "group": "default",
        "input_key": "messages"
      }
    },
    "edges": [
      {"from": "classify", "to": "code_handler", "condition": "intent == 'code'"},
      {"from": "classify", "to": "chat_handler", "condition": "default"}
    ]
  }
}
```

**图节点类型**：

| node.type | 作用 | 参数 |
|-----------|------|------|
| `llm_call` | 调用指定 group 的 LLM | `group`, `system_prompt?`, `input_key`, `output_key` |
| `condition` | 基于状态值分支 | `input_key`, `branches: [{when, to}]` |
| `parallel` | 并行调用多个节点，合并结果 | `targets: [node_name]`, `merge: "concat"\|"join"` |
| `transform` | 数据变换函数 | `function: "truncate"\|"compress"\|"format"`, `args` |

**条件表达式安全求值**（不用 `eval`，用 `ast.parse` + 白名单 operator）：

```python
import ast
import operator

SAFE_OPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
}

def safe_eval_condition(expr: str, state: dict) -> bool:
    tree = ast.parse(expr, mode='eval')
    if not isinstance(tree.body, ast.Compare):
        raise ValueError(f"Condition must be a simple comparison: {expr}")
    cmp = tree.body
    if not isinstance(cmp.left, ast.Name):
        raise ValueError(f"Left side must be a variable name: {expr}")
    value = state.get(cmp.left.id)
    for op, comparator in zip(cmp.ops, cmp.comparators):
        if type(op) not in SAFE_OPS:
            raise ValueError(f"Unsupported operator: {type(op).__name__}")
        if not isinstance(comparator, (ast.Constant, ast.Str)):
            raise ValueError(f"Right side must be a literal: {expr}")
        right = comparator.value if isinstance(comparator, ast.Constant) else comparator.s
        if not SAFE_OPS[op](str(value) if value is not None else "", right):
            return False
    return True
```

**未安装 langgraph 时**：在注册时静默跳过，`GET /admin/strategies` 返回的列表不包含 `langgraph`。

---

## 5. `_shared.py` — 共享基础设施

所有共享函数为**独立的 async 函数**（不是 BaseStrategy 的方法），策略通过参数调用。

**从 `router.py` 搬过来的全局状态**：

```python
# pipeline/_shared.py

# --- 从 router.py 搬过来，原封不动 ---
_provider_semaphores: dict[int, asyncio.Semaphore | None] = {}
_endpoint_cache: dict[int, tuple[list[ModelEndpoint], float]] = {}
_ENDPOINT_CACHE_TTL = 60

# provider 缓存也搬过来
_provider_cache: dict[tuple[int, str], tuple[BaseProvider, float]] = {}
_PROVIDER_CACHE_TTL = 300
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

    从 GroupRouter._attempt_call 1:1 搬过来。
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
- provider 信号量 `_provider_semaphores` 必须在 `_shared.py`，不能在 strategy 里——它是全局跨 group 共享的
- `_endpoint_cache` 也搬到 `_shared.py`，与 provider 缓存一起管理
- `call_llm` 是独立函数，测试时不需要实例化 strategy

---

## 6. `PipelineEngine` — 统一入口

### 6.1 架构

```
请求
  │
  ▼
PipelineEngine.route(group, messages, stream=False, ...)
  │
  ├─ 查策略注册表 → strategy_cls = STRATEGY_REGISTRY[group.type]
  │
  ├─ strategy = strategy_cls(group.params)
  │
  ├─ if stream:
  │     return await strategy.select_endpoints(messages, ...)  → StreamRouteResult
  │   else:
  │     return await strategy.execute(messages, ...)           → dict
  │
  └─ 失败时 → engine 层 fallback（跨策略类型）
```

### 6.2 代码结构

```
src/botflow/
├── router.py                     # 保留：CooldownManager, ModelEndpoint,
│                                 #        weighted_random_select/order, retry 工具函数
│                                 #        （GroupRouter 类标记 @deprecated）
│
├── pipeline/
│   ├── __init__.py               # 导出 PipelineEngine
│   ├── engine.py                 # PipelineEngine（统一入口）
│   ├── base.py                   # BaseStrategy ABC + RouteResult + STRATEGY_REGISTRY
│   ├── _shared.py                # load_endpoints, call_llm, filter_available, etc.
│   ├── strategies.py             # RandomWeightsStrategy + RoundRobinStrategy + SequentialStrategy
│   └── langgraph_strategy.py     # LangGraphStrategy（P5，可选依赖）
```

**文件数从 9 个精简到 7 个**：3 个内建策略合并到 `strategies.py`（总共不到 100 行），`graph_types.py` 延迟到 P5。

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
    ) -> dict | StreamRouteResult:
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

**当前 `core.py` `_stream_common` 的流式 fallback 逻辑（110+ 行）分两阶段迁移**：

**第一阶段（P2）**：非流式先迁移，流式暂保持 GroupRouter
```
core.py _handle_chat_non_stream → PipelineEngine.route(stream=False)
core.py _stream_common → 仍用 GroupRouter（暂时）
```

**第二阶段（P3）**：流式迁移到 PipelineEngine
```
core.py _stream_common → PipelineEngine.route(stream=True) → select_endpoints()
                        → 逐个 endpoint 尝试（逻辑从 _stream_common 搬到 engine 或保持在 core.py）
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
  params: '{"entry":"classify"}' # 新增，JSON 字符串（与现有 query param 风格一致）
  fallback_group_id: null
```

**params 传递方式**：用 JSON 字符串（与现有 admin API 的 query param 风格一致），后端 `json.loads()` 解析。不改为 Pydantic Body（避免破坏现有客户端）。

### 7.2 更新 Group

```
PATCH /admin/groups/{id}
  # 可更新 type 和 params
  type: "round_robin"
  params: '{}'
```

### 7.3 获取 Group

```
GET /admin/groups/{id}/details
  返回中包含 type 和 params 字段（model_dump() 自动包含）
```

### 7.4 新增：策略类型查询

```
GET /admin/strategies
  返回支持的策略类型列表及说明：
  [
    {"type": "random_weights", "name": "加权随机", "params_schema": {}},
    {"type": "round_robin", "name": "轮询", "params_schema": {}},
    {"type": "sequential", "name": "顺序降级", "params_schema": {}},
    {"type": "langgraph", "name": "自定义图", "params_schema": "...", "available": true/false}
  ]
```

`available` 字段根据 langgraph 是否安装动态返回。

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
| `test_router_full.py` | **不改**：`GroupRouter` 保留 |
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
| `langgraph` | 可选 | 仅 `langgraph` 类型策略需要 |
| `langchain-core` | 可选 | langgraph 的依赖 |

```toml
# pyproject.toml
[project.optional-dependencies]
pipeline = ["langgraph>=0.2", "langchain-core>=0.3"]
```

安装方式：`pip install botflow[pipeline]`

---

## 10. 实施阶段

| 阶段 | 内容 | 验证标准 | 风险 |
|------|------|---------|------|
| **P1** | DB migration + Model 变更 + pipeline/ 目录 + BaseStrategy + _shared.py + PipelineEngine 骨架 | 现有测试全部通过（不改 core.py，不改任何行为） | 低 |
| **P2** | RandomWeightsStrategy + core.py **非流式**接入 | test_router.py 纯函数不变；新增 test_strategy_random_weights.py；非流式路径改用 PipelineEngine；流式路径**暂不改** | 低 |
| **P3** | core.py **流式**路径迁移到 PipelineEngine | 所有 streaming 测试通过；_stream_common 不再直接引用 GroupRouter | 中 |
| **P4** | RoundRobinStrategy + SequentialStrategy | 新增测试覆盖轮询、顺序降级、部分失败 | 低 |
| **P5** | Admin API 支持 type/params + /admin/strategies | 创建 round_robin group → 路由成功；修改 group type 热切换生效 | 低 |
| **P6** | LangGraphStrategy | 依赖 langgraph 的集成测试 | 中 |
| **P7** | 清理 GroupRouter（标记 deprecated，保留兼容） | 所有测试通过 | 低 |

**P1-P5 为第一期**，不引入任何新依赖，只重构内部结构。
**P6 为第二期**，按需引入 langgraph。
**P7 为清理期**，在第二期稳定后执行。

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
src/botflow/router.py                    # GroupRouter 标记 @deprecated（P7）
pyproject.toml                           # optional-dependencies pipeline
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
| `tests/test_strategy_random_weights.py` | 行为与现有 `test_router.py` 完全一致 |
| `tests/test_strategy_round_robin.py` | 顺序轮询、计数器溢出、单模型 group |
| `tests/test_strategy_sequential.py` | 顺序降级、部分失败、全部失败 |
| `tests/test_pipeline_migration.py` | DB migration 正确性、旧数据兼容、params JSON 解析 |
| `tests/test_shared.py` | call_llm、load_endpoints、filter_available 独立测试 |

### 第二期测试（P6）

| 测试文件 | 覆盖 |
|---------|------|
| `tests/test_pipeline_langgraph.py` | LangGraph 集成（需安装 langgraph） |

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
- Admin API `params` 用 JSON 字符串传递（与现有 query param 风格一致）
- Fallback 需检测循环（visited set）
- langgraph group 在 `find_groups_by_model_name` 中不可见（预期行为）

### 实现审查关键发现
- provider 信号量 `_provider_semaphores` 必须在 `_shared.py`（跨 group 共享）
- context_window 截断放在 `_shared.py` 的 `truncate_messages()` 函数中
- PipelineEngine 用 db_factory 而非 db 实例（避免重连失效）
- Streaming 路径分两阶段迁移（P2 非流式，P3 流式）
- round_robin 计数器加溢出保护
- endpoint_cache 在 Admin 修改 group_models 后需 invalidation
