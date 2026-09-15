# openbotflow 核心路由迁移至 LangGraph 改动方案（async 架构）

> 目标：核心路由由手写循环 → LangGraph StateGraph 声明式驱动，全链路 async，
> 非流式/流式统一走 `ainvoke()`。
>
> 涉及文件：
> - `src/botflow/pipeline/langgraph_engine.py`（核心引擎，新建）
> - `src/botflow/pipeline/engine.py`（代理层，已改造）
> - `src/botflow/core.py`（路由调用链，无需修改）

---

## 1. 架构总览

```
请求入口（/v1/chat/completions、/v1/messages、/v1/responses）
  │
  ├─ 非流式  →  core._handle_chat_non_stream()
  │               → PipelineEngine.route()            [async]
  │                  → LangGraphEngine.route()        [async]
  │                     → await self._graph.ainvoke(...)
  │                        图内：resolve_group → load_and_select → try_call
  │                                 → (success | next_ep | fallback | error)
  │                        LLM 调用在图节点 _try_call 内直接 await
  │
  └─ 流式    →  core._stream_common()
                  → PipelineEngine.route_stream()     [async]
                     → LangGraphEngine.route_stream()  [async]
                        → await self._graph.ainvoke(...)   [stream=True，走到 load 就 END]
                  → 拿到 ordered endpoints
                  → for ep in endpoints:
                       ep.provider.chat_stream(...)   # 真正出 token
                        → _serialize(chunk) → SSE yield
```

**关键设计原则**：
- 非流式：重试、降级、cooldown 全部在图内闭环，`route()` 一次 `await ainvoke()` 返回最终结果
- 流式：图只负责选端点（`stream=True` 短路），token 迭代由 `core._stream_common` 直接驱动，避免把 SSE 生成器塞进图节点
- 全链路 async，无后台线程 hack，在 uvicorn event loop 下原生运行

---

## 2. 文件改动明细

### 2.1 `langgraph_engine.py`（核心引擎）

#### 2.1.1 节点设计（4 个，全部 `async def`）

**`_resolve_group(state, config)`**
- 首次（`_initialized=False`）：通过初始 group，记录 `visited_groups`
- 后续（fallback）：`await ctx.db.get_group(fallback_gid)` 加载 fallback group
- 循环检测 + 深度限制（max 3）

**`_load_and_select(state, config)`**
- `await load_endpoints(group.id, ctx.db)` → 过滤 cooldown → 按策略排序
- 无可用端点 → raise `NoAvailableModelError` / `AllModelsCooldownError`
- `truncate_messages()` 截断超长消息

**`_try_call(state, config)`**
- `await call_llm(ep, truncated, group_id, cooldown, ...)`
- `call_llm` 内部已处理 per-endpoint 重试 + cooldown + 信号量
- 成功 → 填充 `result`、`used_model_id`、`used_provider_id`
- 失败 → `current_ep_idx += 1`，填 `error`

**`_finalize_error(state, config)`**
- 所有端点失败后执行，返回 `{"error": {"message": ..., "type": "server_error"}}`

#### 2.1.2 条件路由

**`_route_after_load(state)`**
```
stream=True  → "done"（图 END，token 迭代交给 core._stream_common）
stream=False → "select"（进 try_call）
```

**`_route_after_call(state)`**
```
result 存在          → "success"（END）
idx < len(endpoints) → "next_ep"（回 try_call）
有 fallback_group_id → "fallback"（回 resolve_group）
其他                 → "error"（进 finalize_error）
```

#### 2.1.3 图结构

```
START
  │
  ▼
resolve_group ──▶ load_and_select ──▶ _route_after_load
                                          │
                              ┌───────────┴───────────┐
                              │ stream=True            │ stream=False
                              ▼                        ▼
                             END                   try_call
                                                    │
                                          _route_after_call
                                                  │
                               ┌────────┬────────┼────────┐
                               ▼        ▼        ▼        ▼
                             END    next_ep  fallback  error
                                     │         │             │
                                     └─────────┘             ▼
                                              (next_ep 回     finalize_error → END
                                               try_call,
                                               fallback 回
                                               resolve_group)
```

#### 2.1.4 `RouteState`（TypedDict）

| 字段 | 类型 | 用途 |
|------|------|------|
| `messages` | `list[dict]` | 原始消息 |
| `group` | `ModelGroup` | 当前 group（首次传入，后续由 resolve_group 更新） |
| `strategy_name` | `str` | 策略类型 |
| `endpoints` | `list[ModelEndpoint]` | 按策略排序后的端点队列 |
| `current_ep_idx` | `int` | 当前正在尝试的端点索引 |
| `truncated_messages` | `list[dict]` | 截断后的消息 |
| `fallback_group_id` | `int \| None` | 下一个 fallback group |
| `fallback_depth` | `int` | fallback 跳数（max 3） |
| `visited_groups` | `list[int]` | 循环检测 |
| `result` | `dict \| None` | LLM 响应（成功时填充） |
| `error` | `str \| None` | 错误信息 |
| `stream` | `bool` | True 时图只选端点 |
| `_initialized` | `bool` | 标记是否首次 resolve_group 通过 |

#### 2.1.5 `LangGraphEngine` 公开 API

| 方法 | 签名 | 说明 |
|------|------|------|
| `route()` | `async def route(group, messages, stream=False, temperature=None, max_tokens=None, **kwargs) -> dict` | 非流式：完整图执行 |
| `route_stream()` | `async def route_stream(group, messages, temperature=None, max_tokens=None, **kwargs) -> dict` | 流式：只选端点 |
| `_load_group()` | `async def _load_group(group_id: int) -> ModelGroup` | DB 缓存（60s TTL） |
| `cooldown` | property | 供 `core._stream_common` 调 `record_success/failure` |

`route()` 内部 `stream=False` → 图完整跑到 LLM 调用结束
`route_stream()` 内部 `stream=True` → 图在 `load_and_select` 后 END

---

### 2.2 `engine.py`（代理层，无需修改）

```python
class PipelineEngine:
    def __init__(self, db_factory, cooldown):
        self._inner = LangGraphEngine(db_factory, cooldown)
        self.cooldown = cooldown          # 暴露给 core._stream_common
        self._db_factory = db_factory     # 向后兼容（测试直接访问）
        self._group_cache = self._inner._group_cache
        self._GROUP_CACHE_TTL = self._inner._GROUP_CACHE_TTL

    def _create_strategy(self, group): ...   # 向后兼容（测试）
    async def _load_group(self, gid): ...    # 代理到 inner
    async def route(self, group, messages, ...): ...   # 代理到 inner.route()
    async def route_stream(self, group, messages, ...): ...  # 代理到 inner.route_stream()
```

`PipelineEngine` 的公开 API 与原版完全一致，`core.py` 无需修改即可工作。

---

### 2.3 `core.py`（路由调用链，无需修改）

调用点（已核对）：
- `L973`：`_handle_chat_non_stream` → `engine.route(group=..., stream=False, ...)`
- `L1094`：`_stream_common` → `engine.route_stream(group=active_group, ...)`
- `L1215`：`_stream_common` fallback → `engine._load_group(fallback_gid)`

`route_stream` 返回格式（与原版一致）：
```python
{
    "endpoints": [ModelEndpoint, ...],
    "group_id": int,
    "messages": [...],          # 截断后的消息
    "temperature": float|None,
    "max_tokens": int|None,
    "kwargs": {...},
    "fallback_group_id": int|None,
}
```

---

## 3. 关键设计决策

| 决策 | 说明 |
|------|------|
| **全 async + `ainvoke()`** | 与 uvicorn event loop 原生兼容，无后台线程 hack，代码更干净 |
| **流式不跑 LLM** | `stream=True` 时图在 `load_and_select` 后直接 END，token 迭代留在 `core._stream_common` |
| **fallback 循环在图内** | 非流式：`_route_after_call` 检测 `fallback_group_id` 回 `resolve_group`，最多 3 层 |
| **`PipelineEngine` API 不变** | 代理层保持原接口，`core.py` 零修改 |

> **关于之前的 ainvoke 死锁**：
> 在 Windows/Python 3.13 上，直接在脚本中 `asyncio.run(c.ainvoke())` 会死锁。
> 根因推测是 LangSmith tracing 初始化在错误线程运行，或 aiosqlite 连接绑定主线程。
> 在 uvicorn（生产环境）下，event loop 正确初始化，`ainvoke()` 应正常工作。
> 测试时如遇死锁，设置 `LANGSMITH_TRACING=false` 或 `LANGCHAIN_TRACING_V2=false` 绕过。

---

## 4. 已知风险 & 待验证

| 风险 | 影响 | 缓解 |
|------|------|------|
| aiosqlite 连接跨线程 | ainvoke 在 uvicorn loop 下运行，aiosqlite 连接创建在同一 loop → 正常 | 多 worker（`uvicorn --workers N`）每个 worker 独立 loop，无跨线程问题 |
| 测试死锁 | 单元测试脚本直接跑 ainvoke 可能触发 LangSmith tracing 问题 | 测试前设 `LANGSMITH_TRACING=false`，或用 `pytest-asyncio` 的 event loop fixture |
| 测试 patch 方式 | 旧测试 patch `RandomWeightsStrategy.execute`，新实现节点直接调 `call_llm` | 需更新 `test_pipeline_engine.py` 中 R-01~R-14 的 patch 目标 |

---

## 5. 测试验证计划

```bash
cd E:/src/openbotflow

# 1. 安装依赖（需网络）
uv sync

# 2. 设置环境变量绕过 LangSmith tracing（如死锁）
set LANGSMITH_TRACING=false
set LANGCHAIN_TRACING_V2=false

# 3. 跑核心测试
uv run pytest tests/test_pipeline_engine.py -v

# 4. 跑全量 pipeline 测试
uv run pytest tests/test_pipeline_migration.py -v

# 5. 跑 router + stream 测试
uv run pytest tests/test_router.py tests/test_router_full.py tests/test_stream_fallback.py -v
```

**需更新测试**：
- `test_pipeline_engine.py` R-01~R-14：patch 目标从 `Strategy.execute` 改为 `call_llm`（或直接 mock `load_endpoints` + `call_llm`）
- 新增：stream + fallback 集成测试

---

## 6. 文件变更清单

| 文件 | 状态 | 改动 |
|------|------|------|
| `src/botflow/pipeline/langgraph_engine.py` | ✅ 完成 | 核心引擎：4 个 async nodes、条件路由、`LangGraphEngine`（`ainvoke` 执行） |
| `src/botflow/pipeline/engine.py` | ✅ 完成 | 代理层：`PipelineEngine` 包裹 `LangGraphEngine`，API 不变 |
| `src/botflow/core.py` | ✅ 无需改 | 调用点 `engine.route()` / `engine.route_stream()` 接口不变 |
| `tests/test_pipeline_engine.py` | ⚠️ 需更新 | patch 目标从 Strategy → `call_llm` / `load_endpoints` |
