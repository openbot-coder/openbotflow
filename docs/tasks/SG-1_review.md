# SG-1 验证报告（实现已落地 + 单元 / 外部回归验收）

> 验证子 agent 职责：只改 `tests/` 与本文档，不改 `src/`（实现缺陷只写入本报告）。
>
> **状态：已落地并验收通过。**
>
> | 项 | 结果 |
> |---|---|
> | 实现提交 | `29c2c0f`（11 files，+2434 / −1439），已 push `origin/main`（`8acd179..29c2c0f`） |
> | 部署验证机 | mq3 `/srv/botflow`，`git merge --ff-only origin/main` → HEAD=`29c2c0f`、DIRTY=0 |
> | 探活 | `sudo supervisorctl restart botflow` 后 `/health`=200（RUNNING）；**回滚点 `3f82234`** |
> | 单元证据（Linux/mq3 全量） | **1076 passed / 0 failed**；四目标模块 **100%**；**未新增任何 `# UNCOVERED`** |
> | 外部回归（mq3 真服务） | **E1–E10 = 23/23 PASS**；另记 1 项**既有缺陷 AD-1**（非回归，见 §6） |
>
> 与上一版（「实现未落地」）的差异：全部「就绪未跑」用例已随实现落地并通过；§5 原有 5 条遗留风险
> 中 4 条已消解、1 条转为 §6 的新形态（legacy `route()` 双实现）。

---

## 1. 用例总数与分类（对齐 `SG-1_tests.md` §2，共 30 条）

| 功能点 | 条数 | 正例 | 反例 | 边界 |
|---|---|---|---|---|
| F1 驱动四步骨架 | 7 (T1.1–T1.7) | T1.1,T1.2 | T1.4,T1.6 | T1.5,T1.7 |
| F2/F3 图瘦身·select_endpoints | 4 (T2.1–T2.4) | T2.2,T2.3,T2.4 | T2.1 | — |
| F4 try_stream 节点 | 7 (T4.1–T4.7) | T4.1,T4.2,T4.3 | T4.4,T4.5 | T4.6,T4.7 |
| F5 传输层分离 | 1 (T5.1) | T5.1 | — | — |
| F6 降级白名单 | 7 (T6.1–T6.7) | T6.1,T6.2,T6.3 | T6.4,T6.5,T6.6 | T6.7 |
| F7/F8 归一与去重 | 4 (T7.1–T7.4) | T7.1,T7.4 | T7.2,T7.3 | — |
| **合计** | **30** | **15** | **10** | **5** |

> 另：`test_driver_group_fallback_works_for_stream_non_stream_counterpart`（T1.2 的非流式对照）作为额外 1 条，不计入 30。
> 本次落地另新增 **G1–G12** 共 12 条真实分支补洞用例（详见 §3.3），全量用例 1052 → **1076**。

### 落点分布（0 新文件，全部进 8 个既有文件）
- `tests/test_langgraph_engine.py`：T2.1–T2.4、T4.1–T4.7、T6.1–T6.3、T6.6、T6.7、**G1–G7**
- `tests/test_stream_fallback.py`：T1.2（+ 非流式对照）、T7.2、T7.3
- `tests/test_core_runtime.py`：T1.1、T1.3、T1.4、T1.5、T1.6、T1.7、T6.4、T6.5、T7.4、**G8–G12**
- `tests/test_core_endpoints.py`：T5.1、T7.1
- 其余文件（pipeline_uncovered / pipeline_engine / router / router_full）承载 §3 旧用例改写，不新增 T 编号

---

## 2. §3 旧用例处理清单（全部完成，且随实现落地通过）

### §3.1 语义反转 / 重写（6 项，全完成）
| 文件 | 处理 |
|---|---|
| `test_langgraph_engine.py` `test_route_stream_selects_endpoints_only` | 改名 `test_graph_stream_invokes_provider`：断言 `try_stream` 在图内调 provider 并经 `writer()` 推 chunk。**旧名已消失** |
| `test_langgraph_engine.py` `test_route_all_endpoints_fail_finalize_error` | 去掉 `"No fallback group available"` 断言，改断言图出口（`error` 非空 / `recoverable` 已写） |
| `test_pipeline_engine.py` `test_stream_common_still_uses_pipeline_engine` | 改写为 `test_stream_common_uses_driver`：断言 `_stream_common` 委托 `core._drive` |
| `test_pipeline_uncovered.py` `test_load_and_select_short_circuits_when_fatal_error_present` | 删除（`fatal_error` 退出历史）→ T6.4/T6.5 覆盖 |
| `test_pipeline_uncovered.py` `test_load_and_select_rejects_langgraph_strategy` | 删除 → 迁 T6.4（驱动步骤②拒绝） |
| `test_pipeline_uncovered.py` `test_route_after_call_fatal_error_routes_to_error` | 删除（`_route_after_call` 的 fatal_error 分支消失） |

### §3.2 图内 → 驱动搬迁（删除原处，在 F1 重建；7 组，全完成）
- `test_langgraph_engine.py` 3 项（`test_route_fallback_group_succeeds` / `_cycle_detected` / `_depth_limit`）→ 迁 T1.2/T1.4/T1.5
- `test_pipeline_uncovered.py` 4 项（`resolve_group_*` / `fallback_group_not_found_is_fatal`）→ 迁 T1.4/T1.7
- `test_pipeline_engine.py` **R-04~R-08（原 lines 428–622，6 条图内 fallback 用例）整段删除** → 迁 T1.1/T1.3/T1.6
- `test_router_full.py:413-440`（`test_route_stream_all_cooldown_fallback`）→ 转为 NOTE（组级 fallback 已迁驱动）
- `test_router.py:314-333` `test_route_stream_all_on_cooldown_raises_typed_error` → docstring 更新为「`route_stream` 合并进 `run(mode="stream")`」，断言 `AllModelsCooldownError` 不变

### §3.3 调用缝变更（route_stream → run / stream_events；全完成）
- `test_stream_fallback.py` `StubEngine`：按 `group.id` 返回 `("chunk", c)`/`("state", s)` 事件序列（`run` / `stream_events`）
- `test_core_runtime.py` `_StubEngine` / `_StreamEngine` / `_AttemptEngine`：删除 `route_stream`，补齐 `run` / `stream_events`
- `test_core_endpoints.py` `StubEngine`：同上
- `test_router.py` / `test_router_full.py` 流式选端点用例：`engine.route_stream(...)` → `engine.run(group=..., mode="stream", ...)`，选端点断言经 spy `RandomWeightsStrategy.select_endpoints` 捕获 `RouteResult`

---

## 3. 覆盖率验收与证据

- 目标模块 **100%**：`botflow.core`（686 行）、`botflow.pipeline.langgraph_engine`（296 行）、
  `botflow.pipeline.engine`（33 行）、`botflow.pipeline._shared`（51 行）——**0 未覆盖行**。
- **未新增任何 `# UNCOVERED` / `# pragma: no cover`**（AGENTS.md 红线）。
  - 落地过程中 SG-1 曾引入 1 个 `# pragma: no cover - defensive`（`_handle_chat_non_stream` 的 except 分支）；
    因 HEAD 版 `core.py` 该标记数为 0 且该分支**可达**，已移除并用 G12 真测。
  - 另删除 2 处**不可达死代码**而非用注释掩盖：`core._chain_first`（逐 chunk 迭代迁入图后无调用点）、
    `langgraph_engine._run_one` 尾部 `return {}  # unreachable`（`_raise_routing_error` 是 NoReturn）。
- 证据主机：Linux（mq3）+ 隔离 venv（`uv venv /tmp/x --python 3.13`），
  `PYTHONPATH=src pytest tests/ -m "not integration" --cov=botflow --cov-report=term-missing`。
  本机 Windows 跑不了 async 测试（loopback 被禁 → 挂死），不经手「全部通过」结论。
- 全量结果：**1076 passed / 0 failed**（SG-0 为 1052 passed，本次 +24 条）。

### 3.1 落地期间修掉的 6 个真实测试缺陷（非实现缺陷）

| # | 用例 | 症状 | 根因与修法 |
|---|---|---|---|
| 1 | T4.x / `test_graph_stream_invokes_provider` | 断言 `pushed[i]["choices"]` 取不到 | `try_stream` 的 `writer()` 推的是 `{"chunk": chunk}` **包装**；改 `pushed[i]["chunk"]["choices"]`（4 处） |
| 2 | T4.6 断连 | `provider.aclose_called` 永假 | 真实 provider 的 `chat_stream` 是**异步生成器函数**，`aclose()` 关的是生成器对象、不是 provider 方法；改断言 `streams[0].closed` |
| 3 | T4.5 | fake 表达不出「同流中途失败」 | 第二个元素只在第二次调用才用；改 `[[chunk, ProviderError(...)]]`（同一列表内 Exception 项即中途抛出） |
| 4 | T4.7 | 假覆盖 | 本版 LangGraph 在 `ainvoke` 下 `get_stream_writer()` **不抛** `RuntimeError`；改为显式注入 `_raising_writer()` |
| 5 | `_StreamingProvider` | 与真实 impl 不符 | 重写为 `_TrackedStream`（支持 `__aiter__`/`__anext__`/`aclose`）+ `chat_stream` 返回该对象 |
| 6 | 新增用例断言 | 覆盖率假缺口 494-498 / 378 | 上表 3/4 修好后自然消解 |

### 3.2 落地期间修掉的真实现回归（1 处）

**`core._drive_stream` 两条失败分支丢失失败留痕**——违反 `docs/pipeline-single-graph-design.md` §3.6
「黑名单失败必须留痕」。HEAD 版 `_stream_common` 有 `_log_attempts` + `_log_call(status="error")`，
SG-1 改写后这两条分支只把留痕 append 到内存 list 就 `return`，导致流式失败**在 `call_logs` 里静默消失**。
已恢复为与 `_drive_chat` 对称的留痕，并补 G9 / G10 真测（`test_drive_stream_unexpected_error_leaves_trace_and_closes`、
`test_drive_stream_exhausted_fallback_leaves_trace`）。

### 3.3 G1–G12 真实分支补洞（逐行定性：真分支补测 / 死代码删除）

| 用例 | 覆盖点 |
|---|---|
| G1 `test_build_strategy_rejects_langgraph_strategy` | `_build_strategy` 拒绝 `langgraph` type → `ConfigurationError` |
| G2 `test_try_call_preserves_preexisting_typed_error` | 节点级 `try_call` 保留 `state["error"]` typed cause |
| G3 `test_try_stream_all_timeouts_exhaust_endpoint_then_falls_through` | 单端点用满 `max_retries` 后落到全失败出口 |
| G4 `test_try_stream_retryable_error_retries_then_succeeds` | HTTP 503 可重试 + `backoff.assert_awaited_once()` |
| G5 `test_stream_events_uses_real_langgraph_custom_channel` | 不 stub `get_stream_writer`，走真实 custom-stream 链路 |
| G6 `test_run_stream_raises_on_group_level_failure` | 直接驱动 `_run_stream` 触发 `NoAvailableModelError` |
| G7 `test_run_chat_mode_returns_result_dict` | `run(mode="chat")` 返回图结果 + `_attempts` |
| G8 `test_load_group_delegates_to_active_engine` | `core._load_group` 委派引擎 `_load_group` |
| G9 `test_drive_stream_unexpected_error_leaves_trace_and_closes` | §3.2 回归修复的流式留痕 |
| G10 `test_drive_stream_exhausted_fallback_leaves_trace` | 降级耗尽留痕 |
| G11 `test_handle_chat_non_stream_reraises_http_exception` | `HTTPException` 原样透传 |
| G12 `test_handle_chat_non_stream_wraps_unexpected_error_as_502` | `ProviderError` 包成 502（替代被移除的 pragma 掩码） |

---

## 4. 外部回归矩阵 E1–E10（mq3 真服务，HEAD=`29c2c0f`）

探针：`_e_probe.py`（本机 Write → `scp` → `python3`，凭据取被测机 `.env` + `data/botflow.db` 的 `config.llm_key`，
运行期只驻内存、不打印不落盘）。E9/E10 临时分组统一 `sg1_e*` 前缀，跑完校验清理（实测无残留）。

**结果：23/23 PASS**（23 = 各 case 的 HTTP + 语义断言计数）。`[DONE]` 位置、行序、`model` 字段全部符合。

| # | 协议 | 模式 | 实测（关键值） | 判定 |
|---|---|---|---|---|
| E1 | openai | 非流式 | `object=chat.completion` `model=fast-text` `finish=stop` `content='hi'` `usage={p:79,c:2,t:81,cache:0}` | PASS |
| E2 | openai | 流式 | 5 个 SSE block / 4 个 JSON chunk / `[DONE]@4`(末位) / `models={'fast-text'}` / role chunk 4 / finish chunk 1 | PASS |
| E3 | completions | 非流式 | `object=chat.completion` `model=fast-text` `finish=stop` `content='hi'` | PASS ⚠️见下 |
| E4 | completions | 流式 | 5 lines / `[DONE]@4`(末位) / 4 chunk / 含 `delta.content` | PASS ⚠️见下 |
| E5 | anthropic | 非流式 | `type=message` `role=assistant` `model=fast-text` `stop_reason=stop` `text='hi'` `usage` 齐 | PASS |
| E6 | anthropic | 流式 | `message_start` + `content_block_delta` 齐全 | PASS（主链路）⚠️AD-1 |
| E7 | responses | 非流式 | `object=response` `status=completed` `model=fast-text` `text='hi'` | PASS |
| E8 | responses | 流式 | `response.created`+`in_progress`+`output_text.delta`，**不发 `[DONE]`** | PASS（主链路）⚠️AD-1 |
| E9-1 | 降级 | 流式 | a→b→c，仅 c 有模型 → **2 跳降级成功**，`chars=2` | PASS |
| E9-2 | 降级 | 流式 | cap0→cap1→cap2（3 组用满），cap3 有模型但**未被尝试** → error SSE + 无正文 + `[DONE]` | PASS |
| E9-3 | 降级 | 流式 | cyc0→cyc1→cyc0（visited=2 < 3，排除 cap 干扰）→ **环检测**立即收流 | PASS |
| E10 | 行为变更 | 非流式 | 未知 `group.type` → **HTTP 502** + `detail` 齐；`call_logs.error_type=ConfigurationError`（`/admin/logs` id=61） | PASS |

> ⚠️ **E3/E4 的端点契约澄清**：`/v1/completions` 是**兼容端点**——它接受 `prompt` 但复用
> `internal_to_openai` / `_stream_openai`，因此返回 **`chat.completion` / chat delta 形状**，而**不是**
> `text_completion`（`choices[0].text`）。已核对 `8acd179` 的同名端点源码与 `29c2c0f` **逐字相同**
> （SG-1 未改动），故按实测契约断言，非回归。
>
> ⚠️ **E6/E8 的「齐全」判据自上线以来从未满足**（见 §6 AD-1）：终止事件缺失。SG-1 前后一致 → 非回归。

### 4.1 E9 / E10 改动前后对照（评审已接受的行为变更，勿当 bug 修回）

| 项 | SG-0（旧 `route_stream` 内联） | SG-1（驱动 `_drive`） | 线上实测 |
|---|---|---|---|
| E9 组级降级跳数 | 组内用 `fallback_attempted` 单标志位 → **只允许 1 跳** | `visited` 集合 + `_MAX_FALLBACK_GROUPS=3` → **最多 3 组**（主 + 2 备份）；`fb in visited` 环检测；`len(visited)>=3` 上限 | a→b→c **2 跳成功**；3 组用满后第 4 组不再尝试 |
| E10 未知 `group.type` | `_resolve_group` 仍解析出该组 → 进入调用 → 触发降级 | `_build_strategy` 直接 `ConfigurationError` → **非流式 502**（不再降级） | 502 + `call_logs.error_type=ConfigurationError` |

> 注：`_MAX_FALLBACK_GROUPS = 3` 的语义是「**累计尝试组数**上限 3」，与 `SG-1_features.md` 的
> 「最多 3 跳」措辞存在 1 的偏差（3 组 = 2 跳）。本报告以**实测行为**为准：主 + 2 备份 = 3 组。

---

## 5. 必须保留的 6 条关键守卫（防回退）—— 全部落码并通过

| 守卫 | 用例 | 所在文件 |
|---|---|---|
| T1.2（流式能降级，缺陷 1 修复） | `test_driver_group_fallback_works_for_stream` | test_stream_fallback.py |
| T4.5（已推 chunk 后不降级） | `test_try_stream_after_first_chunk_failure_not_recoverable` | test_langgraph_engine.py |
| T1.3（上抛原始类型化异常） | `test_driver_backup_also_cooldown_raises_original_error` | test_core_runtime.py |
| T6.7（recoverable 键恒存在） | `test_recoverable_key_always_present_on_failure` | test_langgraph_engine.py |
| T7.3（fallback_attempted 全仓零残留） | `test_fallback_attempted_symbol_fully_removed` | test_stream_fallback.py |
| T2.3（成功分支显式 error:None） | `test_graph_success_exit_writes_result_only` | test_langgraph_engine.py |

---

## 6. 本次新发现：AD-1（**既有缺陷**，非 SG-1 回归）

### 6.1 现象

`POST /v1/messages`（anthropic）与 `POST /v1/responses`（responses）在 **`stream: true`** 时，
正文 chunk 之后会插入一条 `data: {"error": {"message": "'NoneType' object has no attribute 'get'", ...}}`，
且**终止事件永不出现**：

- anthropic：缺 `message_delta` / `message_stop`（且 `message_start` **重复 2 次**、无 `content_block_stop`）；
- responses：缺 `response.completed`。

对客户端而言这是「**流到一半报错**」，属对公 API 的功能性缺陷。非流式（E5/E7）与 openai 流式（E2/E4）**不受影响**
（`internal_chunk_to_openai_sse` 用 `if usage:` 判真值，天然避开）。

### 6.2 根因

```python
# src/botflow/protocol_adapter.py（anthropic 分支 line 292 / responses 分支 line 630）
usage = chunk.get("usage", {})          # ← chunk 显式携带 "usage": None 时，默认值不生效
        "input_tokens": usage.get("prompt_tokens", 0),   # ← AttributeError: 'NoneType'
```

所有 provider 的正常 chunk 一律带 `"usage": None`（`providers/base.py:92`、
`openai_compat.py:258`、`anthropic_provider.py:206`、`google_provider.py:217`），只有带 `finish_reason`
的收尾 chunk 才会走进这段 `if finish_reason:` 分支 → **必然崩溃**。

最小修法（2 处，各 1 个 token）：`usage = chunk.get("usage") or {}`。

### 6.3 判定为「既有缺陷」的取证（三重）

1. **源码字节相同**：`protocol_adapter.py` 在 `8acd179`（SG-0）与 `29c2c0f`（SG-1）的 blob **完全一致**
   —— `6384ae60c64bcb9be7f1f639a2e01242569a3d58`；`git diff --stat 8acd179 29c2c0f -- src/botflow/providers/` 为空。
2. **旧实现同样兜底为 error SSE**：SG-0 的 `_stream_common`（old core.py:1272）在 `serialize(chunk)`
   外层 `except Exception: log.error(...); raise`，并被同函数的外层 `except Exception as e:`（old core.py:1334）
   捕获 → `_log_call(status="error", error_type="AttributeError")` + error SSE + `done_signal`。
   即 SG-0 也是「正文 → error 事件 → `[DONE]`」，与 SG-1 现象**逐条一致**。
3. **生产环境（基线）实测复现** ✅：生产 `api.vxquant.com` 部署于 `eb81d1d`（= v3.0.0 + docs，
   **早于 SG-0**），其 `/v1/messages`、`/v1/responses` 流式原始 SSE 与 mq3 结构**完全相同**
   （609 / 670 字节，事件序列逐行一致）→ 天然 A/B 基线，确认**非回归**。

### 6.4 影响面（生产库实测，只读）

| 指标 | 值 |
|---|---|
| `call_logs` 总量 / 时间跨度 | 61,958 行，2026-07-08 → 2026-09-21 |
| 部署版本 | `eb81d1d`（v3.0.0 线，**无 SG-0 / SG-1**） |
| **历史 `error_type='AttributeError'` 行数** | **0**（本次探针上线前） |
| openai 系成功率 | 61,053 success / 1,116 error（≈98.2% 成功；error 以 `AllModelsCooldownError` 344、`ProviderError` 189、`NameError` 356 为主） |

→ 结论：AD-1 是**长期潜伏但零触发**的缺陷 —— 说明至今**没有任何客户端使用 anthropic / responses 的流式端点**
（任何一次调用都必然留下 1 条 AttributeError，历史为 0）。一旦有客户端启用该端点，将 **100% 失败**。
建议单独立项（属对外协议正确性，不属 SG-1 重构范围）。

---

## 7. 遗留风险与建议

1. **AD-1（既有，建议单独立项修复）**：见 §6。最小修法 `usage = chunk.get("usage") or {}`（2 处），
   另需一并修 `message_start` 重复（`internal_chunk_to_anthropic_sse` 用 `delta.get("role") == "assistant"`
   判首次，但统一 chunk 恒带 `role=assistant`，文档注释本身已承认该判据不可靠）与补齐
   `content_block_stop` / `message_stop`。**建议不改在 SG-1 里**，以保持 `29c2c0f`「纯重构」的语义边界。

2. **legacy `route()` 形成第二份降级实现**：`LangGraphEngine.route`（line 817）+ `PipelineEngine.route`
   在 `src/` **已无任何调用方**（`grep '\.route(' src/` 仅命中自身），跨组降级/环检测/深度限制因此存在
   **第二份实现**（对应 `test_pipeline_engine.py` 的 R-09~R-12）。这部分仍有 100% 覆盖、测试全绿，
   但存在**逻辑漂移**与双份维护风险。建议：删除该兼容入口并把 R-09~R-12 迁到驱动层 T1.x；
   若因对外约定需保留，应在 docstring 显式标 `# legacy`。**（本次未动 `src/`，故未处理。）**

3. **文档漂移（2 处，随 AD-1 一并订正为宜）**：
   - `src/botflow/pipeline/engine.py:3` 仍写「对外保持 `route()` / `route_stream()` 接口不变」，
     但 `route_stream` 在 `src/` 已**不存在**（全仓仅此 docstring 提及）。
   - 同文件类 docstring 称「`cooldown` 属性供 `core._stream_common()` 调用 `cooldown.record_success()`」，
     实际 `_stream_common` 已退化为 ~10 行转发，`record_success` 由**图内**记录。

4. **§5 旧遗留风险的消解情况**：原第 3 条（`route_stream` 仍存在于 `src/`）→ 已消解（仅剩 docstring 提及）；
   原第 4 条（流式选端点断言依赖 spy）→ 已随实现落地验证生效；原第 5 条（入参形状按契约推测）
   → 契约吻合，T1.x/T6.x 无需微调；原第 1 条（T6.4/T6.5 落点）→ 已按文档落在驱动层。

5. **生产尚未部署 SG-1**：生产 `eb81d1d` 早于 SG-0。`SG-1_tests.md` §4 要求「上生产后再跑一遍」，
   本次仅在 mq3 完成外部回归。生产部署属**跨两个提交（含 SG-0 行为变更）的公网发布**，
   需单独批准并准备回滚预案（mq3 回滚点 `3f82234`）。

### 7.1 处置决策（2026-09-22 确认）

| 事项 | 决策 | 说明 |
|---|---|---|
| **AD-1** 修复 | **只记录，暂不修** | §6 的取证与影响面已足够后人接手（重现脚本、根因行号、最小修法、影响面数据齐备）。**不修在 SG-1 内**，以保持 `29c2c0f`「纯重构、行为不变」的语义边界干净——若在此追加修复，该提交将同时含重构与行为修正，回滚粒度会变粗 |
| **生产部署 SG-1** | **暂不部署，待单独批准** | 生产保持 `eb81d1d`。部署会一次跨 SG-0 + SG-1（含「降级 1→3 跳」「未知 type 改 502」两条行为变更），属公网发布；需单独排期 + 回滚预案后再执行 |

> 副作用提示：因 AD-1 暂不修、生产暂不部署，**AD-1 在生产上仍会按原样复现**（§6.3 实测已确认）；
> 由于历史触发为 0 次，风险窗口仅在「有客户端首次使用 anthropic / responses 流式端点」时打开——
> 建议在客户端接入这两个端点**之前**排期修复。

---

## 8. 验证结论

- 30 条 T 用例 + T1.2 非流式对照 + 12 条 G 用例全部落码并通过；§3 三大类旧用例处理全部完成；6 条关键守卫就位。
- 单元证据：**1076 passed / 0 failed**，四目标模块 **100%**，未新增 `# UNCOVERED`／`# pragma: no cover`。
- 外部回归：**E1–E10 = 23/23 PASS**；E9/E10 两条已接受的行为变更已附改动前后对照（§4.1）。
- 发现并定性 1 项**既有缺陷 AD-1**（三重取证 + 生产基线实测），**非 SG-1 回归**；另有 1 项双实现风险、
  2 处文档漂移，均记于 §7。
- **SG-1 的「纯重构、行为不变」边界成立**（唯一的刻意行为变更 = E9 降级跳数、E10 未知 type 502，两条均已评审接受）。
- 处置决策：**AD-1 只记录暂不修**（§7.1）、**生产暂不部署待单独批准**（§7.1）；两者均不影响 SG-1 的验收结论。
