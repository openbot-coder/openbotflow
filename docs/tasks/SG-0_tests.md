# SG-0 测试用例落点清单

> 本任务为**验证子 agent 的落码依据**：编码子 agent 不写 `tests/` 下的测试代码（那是验证子 agent 的职责）。
> 本文档把 `SG-0_features.md` 的每条用例映射到**落点文件、用例名、断言要点、覆盖功能点与类别**，
> 供验证子 agent 平行落码；并逐条列出**被本次改动打破的既有用例**（这是本任务最大的回归面）。
>
> 关联：`docs/tasks/SG-0_features.md`、`docs/pipeline-single-graph-design.md §3.6 §8.5`
> 前置：无。SG-0 可独立落地与验收。

---

## 0. 落码前必读：两处口径修正（对着代码实测，已回改 features）

### 0.1 `CallAttempt` 是 **Pydantic `BaseModel`**，不是 dataclass

`SG-0_features.md` F2 原文写「`src/botflow/storage/models.py`（dataclass）」—— **与仓库事实不符**。
`models.py:8` 只有 `from pydantic import BaseModel, Field`，全文件 12 个模型（`Provider` / `Model` / `ModelGroup` /
`CallLog` / `ApiKey` / …）**无一是 dataclass**，`CallLog` 定义为 `models.py:90 class CallLog(BaseModel)`。

→ `CallAttempt` **必须写成 `BaseModel`**（与 `CallLog` 同构，字段默认值风格一致），否则 `db.py` 的
`_row_to_*` 反序列化与 `model_dump()` 出口全部对不上。features 已同步更正。

### 0.2 G4「空流 / 首 chunk 超时不记冷却」**不成立** —— 真实问题是死代码 + 无留痕

features §1 的 G4 写：「`core.py:1125-1129` 的 `break` 绕过 `:1196-1202`（`record_failure`）」。
**逐行追一遍控制流即知此说不成立：**

```
core.py:1104  for ep in route_result["endpoints"]:
1105              for attempt in range(attempts):
1118              except asyncio.TimeoutError:  → 1122 aclose → 1124 break   ─┐
1125              except StopAsyncIteration:    → 1128 gen=None → 1129 break  ─┤ 退出的是
1141-1144         except Exception: ... continue / break                      ─┘ attempt 内层循环
1146              if gen is None:
1147                  break  # ← 永远到不了（三个 except 分支都已 break）
1196          # All attempts on this endpoint failed
1197          engine.cooldown.record_failure(...)   ← 上面 break 之后**会落到这里**
```

`break` 退出的是**内层 attempt 循环**，控制流随即落到 `:1197` 的 `record_failure`。

**佐证（仓库内可验证，无需跑测试）**：既有用例
`tests/test_stream_fallback.py:203` `assert engine.cooldown.get_failure_count(1, ep1.model_id) == 1`
（`test_empty_stream_falls_back_to_next`）与 `:340`（`test_stream_timeout_from_request_overrides_default`）
**今天就在断言「空流 / 首超时都记了冷却」，且全量 1024 passed 全绿**。若 G4 的「不记冷却」成立，
这两条用例早已失败。

→ **F7 的真实内容改为两条**：① 删除 `core.py:1146-1147` 这段不可达代码及其 `# UNCOVERED`（AGENTS.md 规则 4「优先删除」）；
② 让空流 / 首超时**产出 attempt 留痕**（并入 F5 的统一通道，`stage="stream"`）。
**不新增冷却调用**——已有行为正确，重复加会变成双重计数（`cooldown_failure_threshold` 提前触发）。

---

## 1. 落点约定

**0 个新测试文件**（AGENTS.md 规则 6「文件越少越好」）。全部落进既有 6 个文件：

| 功能点 | 落点文件 | 为什么是它 |
|---|---|---|
| F1 / F2 建表 · 索引 · 批量写入 | `tests/test_db_new.py` | 该文件已是「new `botflow.storage.db` methods」的归口（`TestApiKeys` / `TestCallLogNewFields`） |
| F3 `call_llm` 暴露 `last_error` | `tests/test_pipeline_base.py` | 该文件已有「七、`call_llm` 测试（TC-22 ~ TC-27）」专章 |
| F4 图内 `RouteState.attempts` 收集 | `tests/test_langgraph_engine.py` | 已有的全图级用例都在此（`_try_call` 经真实图跑） |
| F5 驱动落库（成功也写） | `tests/test_core_runtime.py` | 驱动（非流式）在 `core`，该文件已是 core runtime 归口 |
| F6 归属修正 | 非流式 → `test_core_runtime.py`；流式 → `tests/test_stream_fallback.py` | 两条路径各自已有断言点 |
| F7 空流 / 首超时 | `tests/test_stream_fallback.py` | 两个相关既有用例就在此（`:193`、`:329`） |
| F8 admin 只读查询 | `tests/test_admin_api.py` | 唯一使用 `TestClient(app)` 的 admin 归口 |

**必须新增的 monkeypatch 点（对编码子 agent 的接口要求）**：

- 驱动侧新增模块级 `core._log_attempts(attempts: list[dict]) -> None`，与 `_log_call` **同构**
  （同样从 `_request_ctx` 取 `request_id`，同样走 `_log_writer` 的批量缓冲通道）。
  理由：T5.1–T5.5 需要在不碰真库的前提下捕获「写了什么」；`_log_call` 已是这个形状，复用同一约定即可。
- `RouteState` 新增 `attempts: list[dict]`（F4），**图只带出、不落库**。

**可复用的既有 helper**（不要另起）：

| helper | 位置 | 用途 |
|---|---|---|
| `db` fixture（`tmp_path` + `initialize` + `close`） | `tests/test_db_new.py:14-19` | F1/F2 全部用例 |
| `Database.execute_read(sql, params) -> list[sqlite3.Row]` | `src/botflow/storage/db.py:269` | 查 `sqlite_master` / `PRAGMA table_info` |
| `Database.execute_write(sql, params) -> int` | `src/botflow/storage/db.py:262` | T2.1 的「一次 `executemany`」spy 目标 |
| `_make_endpoint` / `_make_group` / `StubProvider` | `tests/test_stream_fallback.py:25-67` | F5/F6/F7 的端点与组构造 |
| `env` fixture（`monkeypatch.setattr(core, "_log_call", fake)`） | `tests/test_stream_fallback.py:115-123` | 直接扩展为同时捕获 `_log_attempts` |

---

## 2. 用例 → 落点映射

### F1 / F2 建表与写入（6 条）

| 编号 | 类别 | 用例名 | 落点文件 | 断言要点 |
|---|---|---|---|---|
| T1.1 | 正例 | `test_call_attempts_table_created` | `tests/test_db_new.py` | `await db.execute_read("SELECT name FROM sqlite_master WHERE type='table' AND name='call_attempts'")` 命中 1 行；`PRAGMA table_info(call_attempts)` 的列名**集合** == `{request_id, group_id, model_id, provider_id, stage, endpoint_idx, attempt_no, error_type, error_message, duration_ms, created_at}`（用 set 比较，不假设列序） |
| T1.2 | 正例 | `test_call_attempts_index_created` | 同上 | `sqlite_master` 中 `type='index' AND name='idx_call_attempts_request'` 命中 1 行 |
| T1.3 | 边界 | `test_legacy_db_upgrade_creates_table_and_keeps_data` | 同上 | 先用标准库 `sqlite3` **预建**只含 `call_logs` 的旧库并插 1 行 → `Database(path).initialize()` → ① `call_attempts` 已建；② 预插的 `call_logs` 行**仍在**（证明 `CREATE TABLE IF NOT EXISTS` 未破坏既有数据） |
| T2.1 | 正例 | `test_create_call_attempts_batch_insert` | 同上 | `await db.create_call_attempts([a1, a2, a3])` → `execute_read("SELECT * FROM call_attempts")` 得 3 行且字段值逐条一致；**同时** spy `db.execute_write` 断言**只被调用 1 次**（证明走 `executemany`，而非逐行 round-trip） |
| T2.2 | 边界 | `test_create_call_attempts_empty_list` | 同上 | `await db.create_call_attempts([])` 不抛错；表内 0 行；`execute_write` **未被调用** |
| T2.3 | 反例 | `test_create_call_attempts_allows_null_attribution` | 同上 | `model_id=None, provider_id=None`（select 阶段失败的形态）可写入；读回仍为 `None`；`NOT NULL` 约束**不得**加在这两列上 |

> T2.1 的 spy 目标按实现取：若 `create_call_attempts` 内部走的是别的私有 helper，就 spy 那一个。
> 断言的是「**一次批量调用**」这个事实，不是具体函数名。

### F3 `call_llm` 暴露 `last_error`（4 条）

> **签名已定：返回 `tuple[dict | None, Exception | None]`**（不是 out 参数）。
> 理由见 §3.2 —— out 参数会让「忘了传 sink 的调用者」重新落回**静默吞异常**，正是本任务要消灭的 bug 类。

| 编号 | 类别 | 用例名 | 落点文件 | 断言要点 |
|---|---|---|---|---|
| T3.1 | 正例 | `test_call_llm_returns_result_and_none_error` | `tests/test_pipeline_base.py` | `result, err = await call_llm(ep_ok, ...)` → `err is None`；`result["_routing"] == {"model_id": ep.model_id, "provider_id": ep.detail.provider_id}`（`_routing` 注入点未被搬迁） |
| T3.2 | **反例（关键）** | `test_call_llm_returns_error_instead_of_swallowing` | 同上 | provider 每次抛 `ProviderError("...HTTP 500")` → `result is None` **且** `err is not None`；断言 `err` **是同一个异常实例**（`err is raised_instance`）或至少 `str(err)` 与抛出文本一致。⚠️ **不要断言 `err.status_code`**，除非先确认 `botflow/common/exceptions.py` 的 `ProviderError` 确有该属性 —— 以实际定义为准 |
| T3.3 | 边界 | `test_call_llm_max_retries_one_calls_upstream_once` | 同上 | `ep.max_retries=1` + 可重试失败 → provider 被调用**恰好 1 次**；`err` 非空（不因 `attempts<max_retries` 判定而误多试） |
| T3.4 | 正例 | `test_call_llm_second_attempt_succeeds` | 同上 | 第 1 次抛可重试异常、第 2 次成功（`exponential_backoff` 打成 `AsyncMock`）→ `(result, None)`；provider 被调用 2 次 |

### F4 / F5 收集与落库 —— 本任务核心验收（5 条）

`stage` 取值以 `docs/pipeline-single-graph-design.md §3.6` 的 DDL 定义为准；下表只断言「有值且与场景相符」。

| 编号 | 类别 | 用例名 | 落点文件 | 断言要点 |
|---|---|---|---|---|
| T5.1 | **正例（G1 关键）** | `test_retry_success_writes_attempts_row` | `tests/test_core_runtime.py` | 非流式：`ep1` 全失败 → `ep2` 成功（`_log_call` / `_log_attempts` 均用 `monkeypatch.setattr(core, ...)` 捕获）。断言 ① `call_logs` 侧只有 **1 条 `success`**（「一次请求一行」语义未变）；② `call_attempts` 侧 **≥1 行**且 `model_id == ep1.model_id`、`provider_id == ep1.detail.provider_id`、`error_type` 非空。**这是「重试了也要记录」的直接落点** |
| T5.2 | **正例（G3 关键）** | `test_backup_group_attempts_recorded` | 同上 | 主组全失败 → backup 组成功 → 捕获到的 attempts **同时含两组**：按 `group_id` 去重 == `{primary.id, backup.id}`（跨组累加，不能只留最后一组） |
| T5.3 | 正例 | `test_all_fail_records_every_attempt` | 同上 | 全失败 → ① `call_logs` 侧 **1 条 `error`**；② attempts 覆盖每个端点的每次**实际**尝试：`{(endpoint_idx, attempt_no)}` 与预期集合相等（不可重试错误只 1 次尝试，故**不断言固定条数**，用集合） |
| T5.4 | 边界 | `test_no_failed_attempt_writes_no_rows` | 同上 | 首端点一次成功、全程无失败 → `_log_attempts` **未被调用**，或收到空列表；表内 0 行（不产生噪音） |
| T5.5 | 反例 | `test_attempts_write_failure_does_not_break_request` | 同上 | `db.create_call_attempts` 抛 `RuntimeError`（或 `_log_attempts` 内部抛）→ 请求**仍正常返回**（响应体与成功路径一致），只 `log.error`；`caplog` 断言有 error 记录。⚠️ 这条守的是 features §2 硬约束 3 |

> `_log_attempts` 的落库是 fire-and-forget 观测代码：其内部异常必须在**它自己**捕获并 `log.error`，
> 不允许冒泡到驱动 —— 否则 T5.5 只能在驱动里再包一层 try，变成两处防御。

### F6 归属修正（2 条）

| 编号 | 类别 | 用例名 | 落点文件 | 断言要点 |
|---|---|---|---|---|
| T6.1 | **正例（G2 关键）** | `test_non_stream_error_row_has_attribution` | `tests/test_core_runtime.py` | 非流式全失败 → `_log_call` 捕获到的 `status="error"` 记录 `model_id` / `provider_id` **均非 `None`**，且等于**最后一次尝试**的端点（`ep_last`） |
| T6.2 | **正例（G2 关键）** | `test_stream_error_row_has_attribution` | `tests/test_stream_fallback.py` | 流式全失败 → 同上（改写既有 `test_all_endpoints_fail_emits_error_sse:170` 的 `:187` 断言，见 §3.1） |

### F7 空流 / 首超时（3 条，口径已修正）

| 编号 | 类别 | 用例名 | 落点文件 | 断言要点 |
|---|---|---|---|---|
| T7.1 | 正例 | `test_empty_stream_records_attempt` | `tests/test_stream_fallback.py` | 扩写既有 `test_empty_stream_falls_back_to_next`：**保留**其 `cooldown.get_failure_count == 1` 断言（现状已正确，是回归守卫），**新增** attempts 侧断言 —— `ep1` 有一条 `stage` 属 stream 类、`error_message` 含 `"empty stream"` |
| T7.2 | 正例 | `test_first_chunk_timeout_records_attempt` | 同上 | 扩写既有 `test_stream_timeout_from_request_overrides_default`：同上，`error_message` 含 `"timed out waiting for first chunk"`；且**冷却计数仍为 1**（守卫「不得重复计数」） |
| T7.3 | 反例 | `test_empty_stream_dead_branch_removed` | 同上（或与源码断言用例同处） | 读 `src/botflow/core.py` 文本断言：`"if gen is None:"` 紧随的 `break` 段**已删**；且该文件在 `_stream_common` 范围内**不含 `# UNCOVERED`**。参照 `tests/test_context.py` 的 `test_no_cjk_ratio_refs_in_src` 写法（`Path(...).read_text()` + 子串断言） |

### F8 查询端点（3 条）

| 编号 | 类别 | 用例名 | 落点文件 | 断言要点 |
|---|---|---|---|---|
| T8.1 | 正例 | `test_admin_query_attempts_by_request_id` | `tests/test_admin_api.py` | 沿用该文件的 `TestClient(app)` + admin key 既有 fixture；预插 2 个 request 的 attempts → 按 `request_id` 查只返回该 request 的行 |
| T8.2 | 正例 | `test_admin_query_attempts_by_model_id` | 同上 | 按 `model_id` 查只返回该模型的行 |
| T8.3 | 边界 | `test_admin_query_attempts_empty_result` | 同上 | 无匹配 → **200 + 空列表**（`{"success": True, "attempts": []}`），**不是 404** |

---

## 3. 旧用例处理（验证子 agent 必做）

### 3.1 直接断言 `model_id is None` 的两处（F6 会打破其中之一）

| 文件:行 | 现状 | 改动后 | 处理 |
|---|---|---|---|
| `tests/test_stream_fallback.py:187` | `assert error_log[0]["model_id"] is None`（`test_all_endpoints_fail_emits_error_sse`：`ep1` 400 / `ep2` 500，两个端点**都尝试过**） | F6 后归属 = 最后一次尝试的端点 | **改为 `== ep2.model_id`**（并同步断言 `provider_id == ep2.detail.provider_id`） |
| `tests/test_stream_fallback.py:251` | `assert [...][0]["model_id"] is None`（`test_route_failure_emits_error_sse`：`route_stream` 直接抛 `AllModelsCooldownError`，**无任何端点被调用**） | 无端点可归属，仍为 `None` | **保持 `None` 不动** —— 这条是 F6 的**反例守卫**：F6 只填「确实尝试过」的归属，不得给选择阶段失败硬塞模型 |

### 3.2 ⚠️ `call_llm` 签名变更的波及面：**7 个文件、约 21 处 patch 点**

这是 SG-0 最大的回归面。**必须逐处改，漏一处就是运行期炸**（`tuple` 被当 `dict` 用），
且 `AsyncMock` 默认返回的 `MagicMock` 是**真值**，不解包会静默走成功分支。

| 文件 | 需改的位置 | 现状 | 改为 |
|---|---|---|---|
| `tests/test_langgraph_engine.py` | `:89, :110, :129, :165, :187, :213, :274, :299`（8 处 `patch("botflow.pipeline.langgraph_engine.call_llm", ...)`） | `return_value=None` / `return_value=llm_resp` / `side_effect=[None, resp_b]` / `fake_call_llm` 返回裸 dict | `(None, ProviderError(...))` / `(llm_resp, None)` / `side_effect=[(None, err), (resp_b, None)]` / `return (resp, None)` |
| `tests/test_langgraph_engine.py:232` | `new_callable=AsyncMock` 仅用于 `assert_not_called()` | 无返回值 | **不改**（不消费返回值） |
| `tests/test_pipeline_engine.py` | `:442, :473, :505, :540, :556, :591, :721`（7 处，常量 `PATCH_CALL = "botflow.pipeline.langgraph_engine.call_llm"` 见 `:298`） | 同上 | 同上 |
| `tests/test_pipeline_base.py` | `:224, :240`（`mock_call` patch）+ `:456, :482, :503, :524, :532, :545, :561`（7 处**直接调用** `await call_llm(...)` 并断言返回值） | 直接调用单值解包 | patch 处给 `(resp, None)`；直接调用处改 `result, err = await call_llm(...)`，并**顺手补 `err is None`** |
| `tests/test_pipeline_strategies.py` | `:88`（`_PATCH_CALL_LLM` 常量）+ `:457`（`fake_call_llm`）+ `:467`（patch 点） | `fake_call_llm` 返回裸值 | 返回 `(resp, None)` |
| `tests/test_langgraph_strategy.py` | `:76`（`_PATCH_CALL_LLM`）+ 其消费处 | 同上 | 同上 |
| `tests/test_p0_1_cache_convergence.py` | `:115, :316`（直接 `await call_llm(...)`） | 单值解包 | 改解包 + 补 `err is None` |

**落码前先全仓 grep 一遍，以 grep 结果为准**：

```
call_llm          # 全部调用/打桩点
_PATCH_CALL_LLM   # 两个策略测试文件的间接打桩常量
PATCH_CALL        # test_pipeline_engine.py 的常量
```

**不受影响的文件**（不要顺手改）：
`tests/test_pipeline_uncovered.py`（不碰 `call_llm`）、
`tests/test_router.py` / `tests/test_router_full.py` / `tests/test_core_endpoints.py` /
`tests/test_core_runtime.py` / `tests/test_pipeline_engine.py:952`（均经
`_get_extra_route_params` / `route_stream` 打桩，不直连 `call_llm`）。

### 3.3 无需改动、但需回归确认的既有用例

| 文件:用例 | 为什么它在本任务中重要 |
|---|---|
| `test_stream_fallback.py::test_first_endpoint_used_when_healthy` | 成功路径的锚：F5 加入 attempts 写入后，**不得**因此多写行或改变 SSE 输出 |
| `test_stream_fallback.py::test_mid_stream_failure_not_retried` | `:238` 已断言中途失败的归属是 `ep1`；F6 落地后应**继续成立**（中途失败时 `used_ep` 已设） |
| `test_stream_fallback.py::test_usage_chunk_recorded_in_success_log` | F5 动的是「成功路径也要写 attempts」，这条守「成功路径的 `call_logs` 语义不变」 |
| `test_db_new.py::TestCallLogNewFields` | F1/F2 在 `db.py` 加表，须证明 `call_logs` 既有字段与写入路径未被波及 |
| `test_core_runtime.py` 的断连 / fallback 加载失败用例 | F5 在驱动里插入落库调用，须证明异常路径的既有行为不变 |

---

## 4. 覆盖率验收（给验证子 agent）

- 目标模块 **100%**：`botflow.storage.db`、`botflow.storage.models`、`botflow.pipeline._shared`、
  `botflow.pipeline.langgraph_engine`、`botflow.core`、`botflow.admin_api`。
- **不得新增任何 `# UNCOVERED`**（AGENTS.md 红线）。F7 的方向相反：**删掉一处**既有 `# UNCOVERED`
  （`core.py:1147` 的不可达 `break`）。
- 本机 Windows 跑不了 async 测试 → async 证据取 Linux（mq3 `/tmp` 隔离副本）：
  `uv venv /tmp/x --python 3.13` → `pip install -e /tmp/copy pytest pytest-asyncio` →
  `PYTHONPATH=/tmp/copy/src pytest`。**绝不在部署目录跑 pytest**。
- 跑测命令（AGENTS.md 规范）：`PYTHONPATH=src python -m pytest tests/ --cov=botflow --cov-report=term -m "not integration"`。

**必须保留的关键守卫（防回退）**：

1. **T3.2**（异常不再被吞）—— 它是 F6 白名单判定与 attempts 留痕的共同前置；改回 `return None` 就会复活 G1。
2. **T5.1 + T5.5 组合** —— 前者证明「重试成功也留痕」，后者证明「留痕坏了不影响主链路」。
   两条必须都在，否则修复会从一个极端滑到另一个极端。
3. **§3.1 第二行**（选择阶段失败归属仍为 `None`）—— 防止 F6 被实现成「无脑塞最后一个模型」。
4. **T7.1 / T7.2 的冷却计数 == 1** —— 防止 §0.2 的误读被真的实现成「再补一次 `record_failure`」而双重计数。

**用例总数：23**（正例 14 / 反例 4 / 边界值 5 —— 与 `SG-0_features.md` §4 修正后的数字一致）。

> 注：`SG-0_features.md` §4 原写「21（正例 12 / 反例 4 / 边界 5）」。按该节表格逐条点数实为
> **23 条**（6 + 4 + 5 + 2 + 3 + 3 = 23），分类为 正例 14 / 反例 4 / 边界 5。features 已同步更正。
