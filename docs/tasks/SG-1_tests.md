# SG-1 测试用例落点清单

> 本任务为**验证子 agent 的落码依据**：编码子 agent 不写 `tests/` 下的测试代码（那是验证子 agent 的职责）。
> 本文档把 `SG-1_features.md` 的每条用例映射到**落点文件、用例名、断言要点、覆盖功能点与类别**，
> 并逐条列出**被本次重构打破的既有用例**（§3）与**必须跑的外部回归矩阵**（§4）。
>
> 关联：`docs/tasks/SG-1_features.md`、`docs/pipeline-single-graph-design.md` v2 §3 §4 §8 §9、
> `docs/design.md §3.5 §4.1 §4.2`（已同步为目标态）
> 前置：**SG-0 先行**。SG-0 产出的 `call_attempts` 表是本次重构最有力的回归证据
> ——「重构前后失败留痕逐条一致」是 T7.1 / T7.4 的核心手段。

---

## 0. 落码前必读：用例总数修正

`SG-1_features.md` §4 原写「27（正例 15 / 反例 8 / 边界 4）」。按该节表格逐条点数实为
**29 条**（7 + 4 + 8 + 6 + 4 = 29），分类为 **正例 15 / 反例 10 / 边界 4**。features 已同步更正。
本文档 §2 是 29 条的展开版，**以本文档为准**。

---

## 1. 落点约定

**0 个新测试文件**（AGENTS.md 规则 6）。落进既有 8 个文件：

| 功能点 | 落点文件 | 为什么是它 |
|---|---|---|
| F1 驱动四步骨架 | `tests/test_core_runtime.py`（新增驱动用例）+ `tests/test_stream_fallback.py`（流式降级 T1.2） | 驱动在图外=core；流式的既有断言点在此 |
| F2 / F3 图瘦身 · `select_endpoints` | `tests/test_langgraph_engine.py` + `tests/test_pipeline_uncovered.py` | 全图级用例在 former，节点级在 latter |
| F4 `try_stream` 节点 | `tests/test_langgraph_engine.py` | 需真实图上下文 + `get_stream_writer` |
| F5 传输层分离 | `tests/test_stream_fallback.py`（4 协议 SSE 形状）+ `tests/test_core_endpoints.py`（`TestClient` 端到端） | — |
| F6 降级白名单 | `tests/test_langgraph_engine.py`（图出口）+ `tests/test_core_runtime.py`（驱动是否降级） | `recoverable` 在图内写、在驱动消费，两侧各测一半 |
| F7 / F8 归一与去重 | `tests/test_pipeline_engine.py`（`_stream_common` 打桩用例重写）+ 源码断言用例 | — |

**接口约定（对编码子 agent 的要求，测试依赖它）**：

| 符号 | 形状 | 用途 |
|---|---|---|
| `core._drive(internal, mode) -> ...` | 图外四步骨架；`mode ∈ {"chat", "stream"}` | T1.x 的直接被测对象 |
| `engine.run(strategy, group, mode, **kw)` | 替代 `route()` / `route_stream()` 的**单入口** | 图侧唯一入口 |
| `engine.stream_events(...)` | `astream(stream_mode=["custom", "values"])` 的包装，产出 `("chunk", c)` / `("state", s)` | F5 传输层 |
| `state["recoverable"]: bool` | 图出口必写（成功与失败都写） | T6.x |
| `state["attempts"]: list[dict]` | 图只带出、不落库（SG-0 F4 已建） | 与 SG-0 复用 |
| `GraphContext.request` | 新增字段，仅 `try_stream` 用来 `is_disconnected()` | T4.6 |

**必须在 `try_stream` 内做的兜底**（T4.7 测它）：

```python
try:
    writer = get_stream_writer()
except RuntimeError:                      # 非流式上下文（ainvoke）下无 writer
    writer = None                         # ← 必须落到这条，不得让异常冒泡
```

---

## 2. 用例 → 落点映射

### F1 驱动骨架（7 条）

| 编号 | 类别 | 用例名 | 落点文件 | 断言要点 |
|---|---|---|---|---|
| T1.1 | 正例 | `test_driver_primary_group_success_no_fallback` | `tests/test_core_runtime.py` | 主组首次成功 → `engine.run` 被调用**恰好 1 次**；`_load_group` **未被调用**（没走备份组）；落库 1 条 `success` |
| T1.2 | **正例（缺陷 1 核心）** | `test_driver_group_fallback_works_for_stream` | `tests/test_stream_fallback.py` | 主组全冷却（`run` 抛 `AllModelsCooldownError`）→ 备份组可用 → **流式同样降级成功**，SSE 正常收尾 `[DONE]`。⚠️ **改动前此场景返回 502**，是本任务的行为修复；建议同时写一条非流式对照（同一 stub，仅 `mode` 不同） |
| T1.3 | 正例 | `test_driver_backup_also_cooldown_raises_original_error` | `tests/test_core_runtime.py` | 主组与备份组全冷却 → **`raise` 的是原始 `AllModelsCooldownError`**，`type(e).__name__ == "AllModelsCooldownError"`（**不是 `ProviderError`**）。这条守 features §2 硬约束 4 —— `call_logs.error_type` 保真 |
| T1.4 | 反例 | `test_driver_cycle_guard_stops_loop` | 同上 | 组链 A→B→A：`run` 最多被调用 **2 次**（A、B），第 3 次因 `A ∈ visited` 被拦，随即终止 |
| T1.5 | 边界 | `test_driver_depth_limit_three` | 同上 | 4 级链：第 3 跳**仍执行**（`run` 调用 3 次），第 4 跳终止。⚠️ 必须构造「第 3 跳恰好执行 / 第 4 跳恰好终止」两侧，否则 `>= 3` 写成 `> 3` 也不会被发现 |
| T1.6 | 反例 | `test_driver_no_backup_group_terminates_immediately` | 同上 | `fallback_group_id is None` → 不尝试降级（`_load_group` 未被调用）、立即上抛 |
| T1.7 | 边界 | `test_driver_backup_group_missing_raises_configuration_error` | 同上 | 备份组 id 指向不存在的组（`_load_group` 抛 `ConfigurationError`）→ **上抛且不再继续降级**（黑名单语义，非白名单） |

> T1.4 / T1.5 / T1.6 / T1.7 是**图内既有能力向驱动的搬迁**：它们在
> `tests/test_langgraph_engine.py:175-217` 与 `tests/test_pipeline_uncovered.py:114-124`
> 已有等价实现，删除原处后必须在本组重建（见 §3.2）。

### F2 / F3 图瘦身 · `select_endpoints` 节点（4 条）

| 编号 | 类别 | 用例名 | 落点文件 | 断言要点 |
|---|---|---|---|---|
| T2.1 | **反例（关键）** | `test_resolve_group_removed_from_module` | `tests/test_langgraph_engine.py` | `not hasattr(langgraph_engine, "_resolve_group")`；同时断言 `_route_after_load` 亦不存在（`_route_after_call` 若保留，另测其新语义） |
| T2.2 | 正例 | `test_graph_node_set_is_minimal` | 同上 | 编译后的图节点集合 == `{select_endpoints, try_call, try_stream, finalize_error}`（用 `set(graph.get_graph().nodes)` 或对 `_build_graph` 暴露的常量断言；**不得**遗留 `resolve_group`、`load_and_select`） |
| T2.3 | 正例 | `test_graph_success_exit_writes_result_only` | 同上 | 成功：`state["result"]` 非空、`state["error"]` 为 `None`（⚠️ **成功分支必须显式写 `error: None`** —— 这正是 v1 发现、v2 靠「每次全新 `ainvoke`」消除的旧缺陷；写成回归守卫） |
| T2.4 | 正例 | `test_graph_failure_exit_writes_error_and_recoverable` | 同上 | 失败：`state["error"]` 非空**且** `"recoverable" in state`（两种取值都算通过，只要键存在且为 `bool`） |

> T2.3 的显式 `error: None` 不因「每次全新 `ainvoke`」而多余：`finalize_error` 与
> `select_endpoints` 在**同一张图内**仍会合并 state，`error` 键一旦写入就会留在
> `values` 流里被 F5 的 `stream_events` 读到，进而让驱动误判「这次失败了」。

### F4 `try_stream` 节点（7 条）

| 编号 | 类别 | 用例名 | 落点文件 | 断言要点 |
|---|---|---|---|---|
| T4.1 | 正例 | `test_try_stream_pushes_chunks_via_writer` | `tests/test_langgraph_engine.py` | provider 正常吐 N 个 chunk → 经 `writer()` 推出的 payload **恰好 N 个**且顺序一致；`state["attempts"]` 为空（无失败尝试） |
| T4.2 | 正例 | `test_try_stream_retries_after_first_chunk_timeout` | 同上 | 第 1 次 `__anext__` 超时、第 2 次成功 → 无异常外泄，chunk 正常推出；`state["attempts"]` 含 1 条超时记录（`exponential_backoff` 打成 `AsyncMock`） |
| T4.3 | 正例 | `test_try_stream_empty_stream_moves_to_next_endpoint` | 同上 | 端点 A 空流 → 换端点 B；**A 被 `cooldown.record_failure`** 恰 1 次；`attempts` 含 A 的空流记录（`error_message` 含 `"empty stream"`） |
| T4.4 | 反例 | `test_try_stream_non_retryable_error_no_retry` | 同上 | 首 chunk 前抛 400 → **不重试**（provider 只被调用 1 次）、换下一端点 |
| T4.5 | **反例（R5）** | `test_try_stream_after_first_chunk_failure_not_recoverable` | 同上 | 已推出 ≥1 chunk 后失败 → `state["recoverable"] is False`；**驱动侧断言不调备份组**（`run` 只被调用 1 次）→ 内容无法收回，不改写已发出的 SSE |
| T4.6 | 边界 | `test_try_stream_client_disconnect_acloses_generator` | 同上 | `GraphContext.request.is_disconnected()` 返回 `True` → 中止迭代，provider 生成器 `aclose()` 被调用（spy）；不出错、不落 error 行 |
| T4.7 | 边界 | `test_try_stream_without_writer_context_does_not_raise` | 同上 | 在**非流式**上下文（`ainvoke`）下走 `try_stream`（或被直接调为普通节点）→ `get_stream_writer()` 抛 `RuntimeError` 被捕获，节点不炸（R1 兜底） |

> T4.1–T4.5 的 provider stub 沿用 `tests/test_stream_fallback.py:25-40` 的 `StubProvider`
> （`behavior: list`，元素为 `list[dict]` 或 `Exception`）——**不要另造 stub**。
> T4.1 / T4.7 需要在真实图上下文里跑，直接用 `StateGraph(RouteState)` 装单节点、
> `config={"configurable": {"ctx": ctx}}`（写法见 `tests/test_pipeline_uncovered.py:86-92`）。

### F5 传输层分离（1 条）

| 编号 | 类别 | 用例名 | 落点文件 | 断言要点 |
|---|---|---|---|---|
| T5.1 | 正例 | `test_stream_events_serialize_all_four_protocols` | `tests/test_stream_fallback.py`（参数化 4 条，或 `tests/test_core_endpoints.py` 走 `TestClient`） | 对 openai / completions / anthropic / responses **各一条**流式：① `serialize` 收到的 chunk 里 `chunk["model"]` **已被覆盖为请求的 `model` 名**（不是上游模型名）；② `[DONE]` 信号在**最末**且只出现一次；③ `final_state` 被消费到（`used_model_id` / `provider_id` 有值） |

### F6 降级白名单（6 条）

| 编号 | 类别 | 用例名 | 落点文件 | 断言要点 |
|---|---|---|---|---|
| T6.1 | 正例 | `test_recoverable_all_models_cooldown` | `tests/test_langgraph_engine.py` | `select_endpoints` 抛 `AllModelsCooldownError` → `state["recoverable"] is True` |
| T6.2 | 正例 | `test_recoverable_no_available_model` | 同上 | `NoAvailableModelError` → `recoverable is True`（`strategies.py` 的「no enabled models」与 `router.py` 的「total weight ≤ 0」两处都覆盖到，参数化即可） |
| T6.3 | 正例 | `test_recoverable_retryable_provider_error` | 同上 | `ProviderError` 且 `is_retryable_error(e) is True`（如 503）→ `recoverable is True` |
| T6.4 | **反例（R9）** | `test_not_recoverable_configuration_error_unknown_strategy` | `tests/test_core_runtime.py` | 驱动步骤②取到未知 `group.type` → `ConfigurationError` **不降级**（`run` 只被调用 1 次）→ HTTP **502**、**不是** 404 |
| T6.5 | **反例（R9）** | `test_not_recoverable_unexpected_type_error_and_logged` | 同上 | `select_endpoints` 抛未预期 `TypeError` → `recoverable is False` → 502；**同时断言 `call_attempts` 有留痕行**（SG-0）—— 黑名单失败必须留痕，否则从「掩盖 bug」变成「静默失败」 |
| T6.6 | 反例 | `test_not_recoverable_strategy_error` | `tests/test_langgraph_engine.py` | `StrategyError` → `recoverable is False` |
| T6.7 | 边界 | `test_recoverable_key_always_present_on_failure` | 同上 | 遍历 T6.1–T6.6 的失败态：**每个** `state` 都有 `recoverable` 键且为 `bool`。防止「忘了写这个键」被驱动默认成 `False`/`True` 之一而无人察觉 |

> T6.7 是 `features` 里没写、但**必须有**的一条：`recoverable` 缺键时驱动必须
> 按 `False`（保守不降级）处理；这条用例把「保守默认」钉死，避免后人在 `state.get("recoverable", True)` 上写反。

### F7 / F8 归一与去重（4 条）

| 编号 | 类别 | 用例名 | 落点文件 | 断言要点 |
|---|---|---|---|---|
| T7.1 | 正例 | `test_non_stream_response_byte_identical` | `tests/test_core_endpoints.py`（`TestClient`） | 固定请求 + 固定 stub 上游 → 响应**逐字段**与改动前的「黄金响应」一致（除 `_routing` 被 pop 后的字段集合、`created` 时间戳类字段）。用 golden dict 比较，不做「关键字段抽查」 |
| T7.2 | 反例 | `test_stream_common_has_no_own_retry_loop` | `tests/test_stream_fallback.py`（源码断言） | 读 `src/botflow/core.py` 文本：`_stream_common` 函数体内不含 `for attempt in range(`（端点重试环已迁入图）；行数从 ~180 降到 ~60 量级（断言上限 `< 90` 即可，别写死精确值） |
| T7.3 | 反例 | `test_fallback_attempted_symbol_fully_removed` | 同上 | 全仓（`src/`）grep `fallback_attempted` → **0 命中**。参照 `tests/test_context.py` 的 `test_no_cjk_ratio_refs_in_src` 写法 |
| T7.4 | 正例 | `test_call_logs_fields_unchanged` | `tests/test_core_runtime.py` | 成功路径与失败路径各一条：`status` / `model_id` / `provider_id` / `duration_ms` / `prompt_tokens` / `completion_tokens` / `total_tokens` **与改动前一致**；配合 SG-0 的 `call_attempts` 做「留痕逐条一致」比对 |

---

## 3. 旧用例处理（验证子 agent 必做 —— SG-1 的主战场）

**这次重构的破坏面远大于 SG-0**：图的重心、`route_stream` 的存在、`_stream_common` 的调用缝三处同时变。
落码前先全仓 grep：

```
_resolve_group      _route_after_load     route_stream
_stream_common      fallback_attempted    load_and_select
fatal_error         _load_group
```

### 3.1 语义反转 / 必须重写（不是删，是换断言）

| 文件:行 | 现状 | 改动后 | 处理 |
|---|---|---|---|
| `test_langgraph_engine.py:224-239` `test_route_stream_selects_endpoints_only` | 断言 `call_llm.assert_not_called()` ——「流式图**只选端点**，不调 LLM」 | F4 后 `try_stream` **在图内**调用 provider | **语义完全反转**：改写为 `test_graph_stream_invokes_provider` —— 断言 `try_stream` 被调用、chunk 经 `writer()` 推出。**旧名必须消失**，留着会误导后来人以为流式仍在图上短路 |
| `test_langgraph_engine.py:123-133` `test_route_all_endpoints_fail_finalize_error` | 断言 `"No fallback group available" in str(exc)` | 图不再有 fallback 边，该消息不再由图产生 | 改为断言图出口（`state["error"]` 非空 / `recoverable` 已写）；「无备份组」的语义搬到驱动 T1.6 |
| `test_pipeline_engine.py:951-952` `test_stream_common_still_uses_pipeline_engine` | 断言 `_stream_common` 仍经 `_get_extra_route_params()` + `PipelineEngine` 执行 | F7 后 `_stream_common` 走 `_drive` | 改写为 `test_stream_common_uses_driver`（断言 `_drive` 被调用、`_stream_common` 自身不再持有路由/重试逻辑），或直接并入 T7.1 |
| `test_pipeline_uncovered.py:132-137` `test_load_and_select_short_circuits_when_fatal_error_present` | 依赖 `state["fatal_error"]` 键 | `fatal_error` 退出历史（配置错误改由 `recoverable=False` 表达） | 删除；等价语义由 T6.4 / T6.5 在驱动层覆盖 |
| `test_pipeline_uncovered.py:140-147` `test_load_and_select_rejects_langgraph_strategy` | 断言节点 `_load_and_select` 抛 `ConfigurationError("multi-step workflow")` | 拒绝点从节点迁到**驱动步骤②**（建策略时） | 节点级用例删除 → 落成 T6.4（驱动层、未知/被拒策略）。⚠️ 本任务**保留**拒绝本身，只是换了位置（阶段三才放开） |
| `test_pipeline_uncovered.py:171-172` `test_route_after_call_fatal_error_routes_to_error` | 断言 `_route_after_call({"fatal_error": ...}) == "error"` | `fatal_error` 分支消失 | 若 `_route_after_call` 保留（改为在 `try_call`/`try_stream` 间选择），改测其**新**语义；若删除，删用例。以实现为准，不得留一个恒真的空断言 |

### 3.2 搬迁：图内 → 驱动（删原处，在 §2 F1 重建）

| 文件:行 | 现有用例 | 断言的语义 | 迁到 |
|---|---|---|---|
| `test_langgraph_engine.py:140-168` | `test_route_fallback_group_succeeds` | 主组失败 → 备份组成立 | **T1.2** |
| `test_langgraph_engine.py:175-191` | `test_route_fallback_cycle_detected` | 环检测 | **T1.4** |
| `test_langgraph_engine.py:198-217` | `test_route_fallback_depth_limit` | 深度上限 3 | **T1.5** |
| `test_pipeline_uncovered.py:100-111` | `test_resolve_group_first_pass_appends_unvisited_group` | 首轮把 group 登记进 `visited` | **T1.4**（`visited` 现由驱动维护） |
| `test_pipeline_uncovered.py:114-124` | `test_resolve_group_fallback_group_not_found_is_fatal` | 备份组不存在 → fatal | **T1.7** |
| `test_pipeline_engine.py:417-597`（R-04 ~ R-08，`PATCH_CALL` 见 `:298`） | 5 条「`call_llm` 返回 `None` → 图 fallback 到 `fallback_group`」 | 图内组级降级（**含 `:597` R-08「`fallback_group_id=None` → `ProviderError`」） | **T1.1 / T1.3 / T1.6**。R-08 与 T1.6 是同一件事，不要两条都留 |
| `test_router_full.py:413-440` | `test_route_stream_all_cooldown_fallback` / `_no_fallback_raises` | `route_stream` 内解析 fallback 组 | **T1.2 / T1.3**（并在 `route_stream` 合并后重指到 `run(mode="stream")`） |
| `test_router.py:314-333` | `test_route_stream_all_on_cooldown_raises_typed_error`（docstring 现写「Group-level fallback is owned by the caller (`core._stream_common`)」） | 冷却时抛**类型化**异常给上层 | **T1.3**；同时**更新该 docstring** —— `_stream_common` 这个名字将不再准确 |

### 3.3 调用缝变更：`route_stream` → `run` / `stream_events`（改打桩目标，不改语义）

| 文件:行 | 现有打桩点 | 改动后 | 处理 |
|---|---|---|---|
| `test_stream_fallback.py:70-104` `StubEngine` | 打桩 `route_stream()` 返回 `{"endpoints": [...], "fallback_group_id": ...}`，由 `_stream_common` 自行迭代 | 驱动改调 `engine.stream_events(...)` / `engine.run(...)` | **重写 `StubEngine`**：按 `group.id` 返回「图事件序列」（`("chunk", c)` / `("state", s)`）。这是**本任务单点最大的测试改写**。`register_group` / `_make_group` / `_make_endpoint` / `_setup` 保留 |
| `test_stream_fallback.py:115-123` `env` fixture | 只捕获 `_log_call` | F5 后驱动还写 `_log_attempts`（SG-0） | 扩展为同时捕获两者（返回 `(logs, attempts)` 或一个带两个 list 的对象）；**SG-0 已建此 fixture 的用法，勿另起** |
| `test_core_endpoints.py:53, :66` | docstring 与 `route_stream` stub | 同上 | 打桩目标改 `run` / `stream_events`；docstring 同步 |
| `test_core_runtime.py:11`（docstring）, `:667, :819`（`route_stream` stub）, `:830`（`_load_group` stub）, `:864, :879, :902`（`_stream_common` 调用） | 同上 | 同上 | 同步改；`:830` 的 `_load_group` stub **仍需要**（驱动拿它加载备份组），只是调用方从图换成驱动 |
| `test_router.py:279-311`、`test_router_full.py:397-456` | 断言 `PipelineEngine.route_stream` 的选端点顺序 / 冷却过滤 / 上下文窗口截断 | `route_stream` 合并进 `run` | 重指到 `run(mode="stream")`；**断言内容不变**（选端点语义本就应当保持） |

### 3.4 保留不动（不要顺手改）

| 文件 | 理由 |
|---|---|
| `tests/test_pipeline_engine.py:69-205`（G-01 ~ G-09，`_load_group` 全部用例） | **`_load_group` 本体保留**（含 60s 缓存）。F8 删的是「**在图内被调用**」，不是这个方法。这 9 条是缓存语义的守卫 |
| `tests/test_langgraph_engine.py:80-118, 260-301`（成功路径、`next_ep`、kwargs/temperature 透传） | 图内选端点 + 逐端点调用的语义不变，应全绿通过 —— 它们是「重构未改变调用契约」的证据 |
| `tests/test_pipeline_strategies.py`、`tests/test_pipeline_base.py`（`select_endpoints` 相关） | `BaseStrategy.select_endpoints()` 的契约（T6.1–T6.3 的异常来源）不变 |
| `tests/test_db*.py`、`tests/test_admin_api.py` | SG-1 无 schema 变更、无 admin 变更 |
| `tests/test_context.py`、`tests/test_providers_uncovered.py` 等 | 与路由链无关 |

### 3.5 已由 SG-0 处理、此处只需确认

| 项 | 确认内容 |
|---|---|
| `core.py:1146-1147` 的 `# UNCOVERED` + 不可达 `break` | SG-0 F7 已删；SG-1 只需确认它没有在搬迁 `try_stream` 时**复活** |
| `call_llm` 的 tuple 签名 | SG-0 F3 已改；SG-1 的 `try_stream` 直接消费 `(result, err)`，**不得**回退成单值 |
| `_log_attempts` | SG-0 F5 已建；SG-1 的 `try_stream` 只往 `state["attempts"]` append，落库仍由驱动统一做 |

---

## 4. 外部回归矩阵（集成测试，AGENTS.md 明确「必不可少，绝对不能跳过」）

单元测试过不了这一关：`astream` 在 uvicorn 下的行为、4 种协议的 SSE 形状、
真实流式的首 chunk 时序，都只能在真服务上验。**合并前在 mq3 跑一遍，上生产后再跑一遍。**

| # | 协议 | 模式 | 端点 | 必验 |
|---|---|---|---|---|
| E1 | openai | 非流式 | `POST /v1/chat/completions` | 响应 JSON 与改动前逐字段一致 |
| E2 | openai | 流式 | 同上 `stream=true` | SSE 行序正确、`[DONE]` 在最末、`model` 字段是请求的组名 |
| E3 | completions | 非流式 | `POST /v1/completions` | 同上 |
| E4 | completions | 流式 | 同上 | 同上 |
| E5 | anthropic | 非流式 | `POST /v1/messages` | 同上 |
| E6 | anthropic | 流式 | 同上 | SSE 事件名（`message_start` / `content_block_delta` / `message_stop`）齐全 |
| E7 | responses | 非流式 | `POST /v1/responses` | 同上 |
| E8 | responses | 流式 | 同上 | 同上 |
| E9 | 行为变更 R4 | 流式降级 | 主组人为全冷却（制造 3 级备份链） | 降级**最多 3 跳**（改动前只 1 跳）—— 这是**接受的**行为变更，需在真服务上确认链路 |
| E10 | 行为变更 R9 | 非流式 | `group.type` 设为未知值 | 返回 **502**（改动前会尝试降级）；同时 `call_logs.error_type == "ConfigurationError"` |

> **E9 / E10 是两条已被评审接受的行为变更**，不是回归失败。跑出来要记录在
> `SG-1_review.md` 里，附上改动前后对照，避免后人把它当 bug 修回去。

**执行方式**（沿用既有约定）：
- mq3：`/srv/botflow`，`git fetch` 到目标 SHA → `supervisorctl restart` → **探活 ≥ 90s**
  （⚠️ `supervisorctl restart` 返回成功 ≠ 进程已起，曾差 37s）。
- 生产 `api.vxquant.com`：`openbot@`，**远端 shell 是 fish** → 本地 Write → `scp` → `ssh host 'bash /tmp/x.sh'`。
  该机 = tailnet `100.88.88.2`，本身即天然外部客户端。
- 证据取 Linux：本机 Windows **跑不了 async 测试**，「全部通过」只能在 Linux 上得出。

---

## 5. 覆盖率验收（给验证子 agent）

- 目标模块 **100%**：`botflow.core`、`botflow.pipeline.langgraph_engine`、
  `botflow.pipeline.engine`、`botflow.pipeline._shared`。
- **不得新增任何 `# UNCOVERED`**（AGENTS.md 红线）。
- ⚠️ **覆盖率不能「继承」**：`_stream_common` 从 ~180 行降到 ~60 行，
  删掉的分支会让父模块的行数基数下降，**必须重跑并重算**，不能拿改动前的 100% 当结论。
- 跑测命令：`PYTHONPATH=src python -m pytest tests/ --cov=botflow --cov-report=term -m "not integration"`。
- 本机只跑 `-m "not asyncio"`；async 证据取 Linux：

```
uv venv /tmp/x --python 3.13
pip install -e /tmp/copy pytest pytest-asyncio
PYTHONPATH=/tmp/copy/src pytest
```

**必须保留的关键守卫（防回退）**：

1. **T1.2**（流式能降级）—— 缺陷 1 的修复凭证；删掉它，缺陷随时会回来且无人知道。
2. **T4.5**（已推 chunk 后不降级）—— 与 T1.2 方向相反，**两条必须同时存在**，
   否则「多降级」会被误实现成「中途失败也重试」，把已发出的 SSE 变成两段拼在一起的脏流。
3. **T1.3**（上抛原始类型化异常）—— 保住 `call_logs.error_type` 的运维语义。
   `AllModelsCooldownError`（稍后重试即可）与 `NoAvailableModelError`（根本没模型）必须可区分。
4. **T6.7**（`recoverable` 键恒存在）—— 防「缺键被默认值吃掉」。
5. **T7.3**（`fallback_attempted` 全仓零残留）—— 防「新旧两套降级并存」，
   那会导致同一个请求降级两次（一次在图外、一次残留逻辑）。
6. **T2.3**（成功分支显式 `error: None`）—— 防旧缺陷复活。

**用例总数：30**（正例 15 / 反例 10 / 边界值 5），外加 §4 的 **10 条外部回归（E1–E10）**，
后者不计入单元用例数。

点数：F1 七条 + F2/F3 四条 + F4 七条 + F5 一条 + F6 **七条** + F7/F8 四条 = **30**。

> 注 1：`SG-1_features.md` §4 原写「27（正例 15 / 反例 8 / 边界 4）」。按该节表格逐条点数实为
> **29**（7 + 4 + 8 + 6 + 4 = 29），分类 正例 15 / 反例 10 / 边界 4 —— features 已同步更正为 29。
> 注 2：本文档比 features **多 1 条**：**T6.7**（`recoverable` 键恒存在，边界值）。
> 它是评审后补的防回退守卫，故本文档的总数为 30，与 features 的 29 刻意不等 —— **以本文档为准**，
> features 的 §4 保持 29 不动（它就是最初的 29 条，不回填）。
