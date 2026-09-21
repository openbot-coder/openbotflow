# SG-1 功能点：驱动四步骨架 + 图瘦身 + 传输层分离

> 任务类型：架构重构（统一流式 / 非流式的调用骨架）
> 范围（用户已拍板，见 `docs/pipeline-single-graph-design.md §11`）：
> ① 驱动四步骨架落在图外（`core.py`）；② `StateGraph` 收敛为**单次「策略执行」**（删 `_resolve_group` 与 fallback 边）；③ 流式调用链入图（新增 `try_stream` + `get_stream_writer`）；④ 传输层分离（图推 chunk，core 序列化）；⑤ 降级收紧为**白名单**；⑥ 修既有缺陷 1 / 2（流式缺组级 fallback、流式只降 1 跳）。
> 关联：`docs/pipeline-single-graph-design.md` v2 §3 §4 §8 §9；`docs/design.md §3.5 §4.1 §4.2`（已同步为目标态）
> **前置**：`SG-0`（失败留痕）建议先行 —— 它的留痕表是本次重构的验收工具。
> **不在本任务**：阶段二（`_resolve_model` 缓存）、阶段三（`langgraph` 子图）另立任务。

---

## 1. 目标与边界

| 层 | 位置 | 职责 |
|---|---|---|
| **驱动层** | 图外（`core.py`） | ① 分组 → ② 取策略模板 + 参数 → ③ 生成策略 → ④ 循环「策略执行 / 失败降级 backup 组」 |
| **执行层** | 图内（`StateGraph`） | 单次「策略执行」：选端点 → 逐端点调用（端点级重试 + 退避 + 冷却） |

**驱动管「跳哪组、试几次」；图管「这一组怎么把请求打出去」。**

---

## 2. 硬约束（违反即打回）

1. **非流式行为逐字段不变**（唯一例外：R9 白名单收紧 —— 未预期异常由「降级」变 502，已评审确认）。
2. **覆盖率 100%，不得新增 `# UNCOVERED`**（红线）。
3. **两处重试不得叠加**：端点级重试只在图内 `call_llm`（`ep.max_retries`）；组级降级只在驱动（最多 3 跳 + 环检测）。驱动**不做**端点级重试。
4. **`call_logs.error_type` 保真**：驱动不降级 / 预算耗尽时必须 `raise` 原始类型化异常（复用 `langgraph_engine._raise_routing_error`），不得压成 `ProviderError`。
5. **`docs/design.md §3.5/§4.1/§4.2` 已改写为目标态**，实现须与之一致，否则回改文档。
6. 不引入除「抬高 `langgraph` 下限」外的新依赖。

---

## 3. 功能点清单

### F1 驱动四步骨架（图外）

- 文件：`src/botflow/core.py`。
- 新增 `_drive(internal, mode)`（形态见设计文档 §3.1）：① 分组 → ②③ 建策略 → ④ `while` 循环调图 + 降级。
- 降级守卫：`visited` 集（环）、`depth >= 3`、`fallback_group_id` 为 `None` → 终止并 `raise` 原始异常。
- **非流式与流式共用这一段**：差异只在 `mode` 与出口如何送出。

### F2 图瘦身：删 `_resolve_group` 与 fallback 边

- 文件：`src/botflow/pipeline/langgraph_engine.py`。
- 删除节点 `_resolve_group`（`:135-182`）；删除 `_route_after_load`（`:324-328`）；删除 `try_call → resolve_group` 的 fallback 边（`:406-414`）。
- 保留 `_finalize_error`（`:294-317`）—— 出口写 error 供驱动读。
- `route()` / `route_stream()`（`:468-571`）合并为单入口 `run(strategy, group, mode, ...)`。

### F3 `select_endpoints` 节点

- `_load_and_select`（`:185-244`）拆开：「取模板 + 建策略」移到驱动 F1；「调 `strategy.select_endpoints()`」留作图节点。
- 失败时把**异常对象**写进 `state["error"]`（保留 3.1.0 的修复语义），并写 `state["recoverable"]`（见 F6）。
- 删除 `:206-210` 对 `langgraph` 策略的硬拒绝 —— 阶段三会把它改成子图；本任务先保留为 `ConfigurationError`（黑名单），**不提前放开**。

### F4 `try_stream` 节点

- 把 `core._stream_common` 的首 chunk 逻辑（`:1104-1144`）1:1 搬进图节点：
  - `asyncio.timeout(stream_timeout)` 取首 chunk；
  - 超时 / 空流 / 可重试异常 → `exponential_backoff` 后重试；
  - 逐块迭代用 `langgraph.config.get_stream_writer()` 推 `{"chunk": chunk}`；
  - `request.is_disconnected()` 用 `GraphContext.request`（新增字段）。
- `stream_started=True` 之后失败 → 只报错不降级（R5）。
- 逐次失败 append 到 `state["attempts"]`（SG-0 F4）。

### F5 传输层分离

- 驱动侧消费 `engine.stream_events(...)`（`astream(stream_mode=["custom", "values"])`）：
  - `custom` → 取 chunk → `chunk["model"] = model_name` → `serialize(chunk)` → `yield`；
  - `values` → `final_state`（取 `used_model_id` / `provider_id` / `error`）。
- **4 种协议共用这段**（openai / completions / anthropic / responses），`serialize` 由各协议提供。

### F6 降级白名单（`recoverable`）

- 白名单：`AllModelsCooldownError`、`NoAvailableModelError`、`ProviderError ∧ is_retryable_error(e)`、以及**端点全失败状态**。
- 黑名单：`ConfigurationError`、`StrategyError`、`HTTPException`、`asyncio.CancelledError`、**其它未列出的 `Exception`**。
- 白名单外的失败一律 `recoverable=False`（上抛 + 留痕），见设计文档 §3.5。
- 依赖 SG-0 F3 把 `call_llm` 的 `last_error` 暴露出来，否则拿不到 `error_type`。

### F7 两条路径归一

- `_handle_chat_non_stream`（`:959-1021`）与 `_stream_common`（`:1061-1240`）改为共用 F1 的驱动骨架。
- 落库、计时、`HTTPException(404/502)` 映射留在 core（协议层职责）。

### F8 删除重复实现

- 删除 `_stream_common` 的端点重试环（`:1104-1144`）与一次性 `fallback_attempted` 降级（`:1204-1219`）。
- 删除 `engine._load_group()` 的外部调用（`:1214`）—— 组加载归驱动。
- 删除 `core.py:1147` 的 `# UNCOVERED`（随 SG-0 F7 一并处理）。

### F9 依赖下限

- 文件：`pyproject.toml:26`。**已完成**：`langgraph>=1.0`。

### F10 文档同步

- **已完成**：`docs/design.md §3.5/§4.1/§4.2`、`docs/pipeline_router_design.md §9`。
- 待办：实现落地后核对设计文档 §2「现状」章节是否仍准确（本任务会改变「现状」）。

---

## 4. 测试用例清单

> 编码子 agent 实现，验证子 agent 落测试。类型三档：正例 / 反例 / 边界值。

### F1 驱动骨架

| 编号 | 类型 | 用例 | 预期 |
|---|---|---|---|
| T1.1 | 正例 | 主组首次成功 | 只调图 1 次、不降级 |
| T1.2 | **正例（缺陷 1）** | 主组全冷却 → backup 组成功 | **流式**同样降级成功（现状为 502） |
| T1.3 | 正例 | backup 组也全冷却 | 终止并 `raise` 原始 `AllModelsCooldownError` |
| T1.4 | 反例 | 降级环（A→B→A） | `visited` 拦住，终止 |
| T1.5 | 边界 | 降级深度 = 3 | 第 3 跳仍执行；第 4 跳终止 |
| T1.6 | 反例 | `fallback_group_id=None` | 立即终止，不尝试降级 |
| T1.7 | 边界 | backup 组不存在 | `ConfigurationError`（黑名单）→ 上抛，不继续降级 |

### F2 / F3 图瘦身

| 编号 | 类型 | 用例 | 预期 |
|---|---|---|---|
| T2.1 | **反例（关键）** | 断言 `_resolve_group` 已从模块移除 | `not hasattr(engine, "_resolve_group")` |
| T2.2 | 正例 | 图的节点集合 | 仅 `select_endpoints` / `try_call` / `try_stream` / `finalize_error` |
| T2.3 | 正例 | 成功出口 | `state["result"]` 非空、`error` 为空 |
| T2.4 | 正例 | 失败出口 | `state["error"]` 非空、`recoverable` 已写入 |

### F4 / F5 流式入图 + 传输层

| 编号 | 类型 | 用例 | 预期 |
|---|---|---|---|
| T4.1 | 正例 | 首 chunk 正常 | 逐块经 `writer()` 推出，`serialize` 收到全部 chunk |
| T4.2 | 正例 | 首 chunk 超时 → 重试成功 | 第 2 次成功，无异常外泄 |
| T4.3 | 正例 | 首 chunk 空流 | 换下一端点；该端点记 cooldown failure |
| T4.4 | 反例 | 首 chunk 抛不可重试错误（400） | 不重试，换端点 |
| T4.5 | **反例（R5）** | 已推 chunk 后失败（`stream_started=True`） | **只报错，不降级**（不调 backup 组） |
| T4.6 | 边界 | 客户端断开（`is_disconnected` → True） | 中止迭代并 `aclose()` provider 生成器 |
| T4.7 | 边界 | `get_stream_writer()` 在非流式上下文被调用 | `try/except RuntimeError` 兜底，不炸（R1） |
| T5.1 | 正例 | 4 种协议各一条流式 | `serialize` 被正确调用，`[DONE]` 信号在最末 |

### F6 白名单

| 编号 | 类型 | 用例 | 预期 |
|---|---|---|---|
| T6.1 | 正例 | `AllModelsCooldownError` | `recoverable=True` → 驱动降级 |
| T6.2 | 正例 | `NoAvailableModelError` | `recoverable=True` |
| T6.3 | 正例 | `ProviderError` + 503 | `recoverable=True` |
| T6.4 | **反例（R9）** | 未知策略名 `ConfigurationError` | `recoverable=False` → 直接 502，**不降级** |
| T6.5 | **反例（R9）** | `select_endpoints` 抛未预期 `TypeError` | `recoverable=False` → 502 且**留痕**（SG-0） |
| T6.6 | 反例 | `StrategyError` | `recoverable=False` |

### F7 / F8 归一与去重

| 编号 | 类型 | 用例 | 预期 |
|---|---|---|---|
| T7.1 | 正例 | 非流式同参数 | 响应体与改动前**逐字段一致**（成功路径） |
| T7.2 | 反例 | 断言 `_stream_common` 无独立重试环 | 源码不含 `for attempt in range(attempts)` |
| T7.3 | 反例 | 断言 `fallback_attempted` 已删除 | 全仓 grep 无残留 |
| T7.4 | 正例 | `call_logs` 落库字段 | `status` / `model_id` / `provider_id` / `duration_ms` / tokens 与改动前一致 |

**用例总数**：**29**（正例 15 / 反例 10 / 边界 4 —— 逐条点数：7 + 4 + 8 + 6 + 4 = 29）。

> 注：此处先前误写为「27（正例 15 / 反例 8 / 边界 4）」，按上表逐条点数更正。
> 落点与断言以 `SG-1_tests.md` 为准（该文档为 30 条，比本文多一条防回退守卫 `T6.7`）。

---

## 5. 明确不做（不改什么）

| # | 不做 | 理由 |
|---|---|---|
| 1 | 不做阶段二（`_resolve_model` 入图 + 缓存） | 独立任务；本任务不动每请求 2 次查库 |
| 2 | 不做阶段三（`langgraph` 策略重写为子图） | 独立任务；本任务保留 `:206` 的拒绝（改为黑名单语义） |
| 3 | 不放开 `params` 在服务路径的作用 | 依赖阶段三 |
| 4 | 不改 schema（除 SG-0 的 `call_attempts`） | 本任务无 DB 变更 |
| 5 | 不改 4 种协议的 `serialize` 实现 | 传输层只改「谁调用 serialize」，不改协议本身 |
| 6 | 不改 `admin_api`（除 SG-0 F8 的查询端点） | — |

---

## 6. 风险与前置

1. **`astream` 在 uvicorn 下未验证**（R3）：合并前必须在 mq3 + 生产各跑一次真实流式。
2. **`get_stream_writer()` 上下文约束**（R1）：只在 stream 上下文可用，节点内必须 `try/except RuntimeError`。
3. **R4 行为变更**（流式降级 1 跳 → 3 跳）：已评审接受。
4. **R9 行为变更**（白名单收紧 → 未预期异常 502）：已评审接受；黑名单失败**必须留痕**。
5. **本机 Windows 跑不了 async 测试**：async 证据只能取 Linux 远端（mq3 `/tmp` 隔离副本）。
6. **前置**：强烈建议 SG-0 先行 —— 重构中「失败留痕前后逐条一致」是最有力的回归证据。
