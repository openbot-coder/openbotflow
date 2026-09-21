# SG-0 功能点：失败留痕（可观测性补齐）

> 任务类型：观测性（新增 append-only 表 + 逐次尝试留痕）
> 范围（用户已拍板）：① 新增 `call_attempts` 表；② **每一次失败尝试都留一条记录**，即使该请求最终成功；③ 修正最终错误行的 `model_id` / `provider_id` 归属；④ 空流 / 首 chunk 超时并入统一失败路径（**补留痕** + 删死代码；**不补冷却**，现状已记 —— 见 §1.1）；⑤ `call_llm` 暴露 `last_error`。
> 关联：`docs/pipeline-single-graph-design.md §3.6 §8.5`、`src/botflow/core.py:1125-1240`、`src/botflow/pipeline/_shared.py:98-160`
> **定位**：本任务**先于 SG-1 落地**。它独立可测，且落地后正好成为 SG-1 图重构的验收工具（重构前后失败留痕应逐条一致）。

---

## 1. 背景：现状有 4 处失败不留库

| # | 缺口 | 位置 | 后果 |
|---|---|---|---|
| G1 | 逐次尝试失败只 `log.warning`，不落库 | `_shared.py:134-146`、`core.py:1130-1144` | **重试成功时 `call_logs` 只有一条 `success`**，失败尝试无迹可查 |
| G2 | 最终错误行丢失归属 | `core.py:1009-1010` 写死 `None`；`:1227-1228` 在 `used_ep` 未设置时同为 `None` | 失败行无法定位模型 / 供应商 |
| G3 | 降级前的组失败无痕 | `core.py:1204-1219` | 主组失败只在应用日志，DB 无行 |
| G4 | 空流 / 首 chunk 超时**不留痕**；且有一段不可达死代码 | `core.py:1125-1129`（⚠️ **冷却是记了的**，见 §1.1）；`:1146-1147` 是不可达的 `break` | 失败尝试无迹可查（同 G1 的根因）；死代码挂着 `# UNCOVERED` 需删除 |

> ⚠️ **§1.1 口径更正（对代码逐行追过，勿按旧描述实现）**
>
> 本节先前把 G4 写成「空流 / 首超时**不记冷却**，`break` 绕过了 `:1196-1202`」。**此说不成立**：
> `:1124` / `:1129` 的 `break` 退出的是**内层 `for attempt` 循环**，控制流随即落到 `:1197` 的
> `engine.cooldown.record_failure(...)` —— 冷却**已经记了**。
>
> 仓库内可直接验证的佐证：既有用例 `tests/test_stream_fallback.py:203`
> （`test_empty_stream_falls_back_to_next`）与 `:340`（`test_stream_timeout_from_request_overrides_default`）
> **今天就在断言 `get_failure_count == 1`，且全量 1024 passed 全绿**。若 G4 原描述成立，这两条早已失败。
>
> 真正的问题是两件事：① `:1146-1147` 的 `if gen is None: break` **不可达**（三个 `except` 分支都已 `break`），
> 是一段带 `# UNCOVERED` 的死代码 —— 按 AGENTS.md 规则 4「优先删除」直接删；
> ② 空流 / 首超时**没有 attempt 留痕**，这是 G1 的普遍问题，由 F5 的统一通道解决。
>
> **落地时不得新增 `record_failure` 调用** —— 会变成双重计数，使 `cooldown_failure_threshold` 提前触发。

用户原话：「错误日志好像有一些地方没有记录的，即使重试了，也要记录调用错误。」

---

## 2. 硬约束（违反即打回）

1. **`call_logs` 的「一次请求一行」语义不变**。失败尝试写**新表**，不改 `call_logs` 的 `status` 口径 —— 否则成功率统计、`/admin` 报表全部失真（生产已有 5 万+ 行）。
2. **`call_llm()` 不引入 DB 依赖**。它被策略与图共用，签名里没有 `db`；留痕数据由**图收集进 state、驱动落库**。
3. **留痕失败绝不影响主链路**。写 `call_attempts` 抛错只 `log.error`，不得 `raise`（这是观测代码，不是业务代码）。
4. **覆盖率 100%，不得新增 `# UNCOVERED`**（AGENTS.md；红线见设计文档 §10）。
5. 不改发给上游的字节、不改 `call_logs` 现有列。

---

## 3. 功能点清单

### F1 `call_attempts` 表 + 索引 + 迁移

- 文件：`src/botflow/storage/db.py`（与 `call_logs` 同处，`:95-116` 之后）。
- DDL 见 `docs/pipeline-single-graph-design.md §3.6`；字段 `request_id / group_id / model_id / provider_id / stage / endpoint_idx / attempt_no / error_type / error_message / duration_ms / created_at`。
- 索引：`idx_call_attempts_request ON call_attempts(request_id)`。
- 建表走既有 `CREATE TABLE IF NOT EXISTS` 初始化路径，**无需 ALTER**（新表，旧库首次启动即建）。

### F2 `CallAttempt` 模型 + 写入方法

- 文件：`src/botflow/storage/models.py`（**Pydantic `BaseModel`** —— 与 `CallLog`（`models.py:90`）同构；该文件 12 个模型无一是 dataclass）+ `src/botflow/storage/db.py`（`create_call_attempts(list)` 批量插入）。
- 批量插入用一次 `executemany`，避免逐行 round-trip（生产 5 万+ 行，逐行已实测 ~306/s，是既有瓶颈）。
- 写入**复用 `CallLogWriter` 的批量缓冲通道**（`buffer=100, flush=5s`）或独立 writer —— 二选一，编码时需在回报里说明选择理由。

### F3 `call_llm()` 暴露失败原因

- 文件：`src/botflow/pipeline/_shared.py` 的 `call_llm()`。
- 现状：失败时 `return None`，**异常被吞**（`:134-146`）。
- 改为返回 `tuple[dict | None, Exception | None]`，或在 `None` 时把 `last_error` 通过 out 参数 / state 回传。
- 这是 SG-1 白名单判定（`recoverable`）与留痕取 `error_type` 的**共同前置**。
- ⚠️ 所有调用点（`_shared.py` 内、`langgraph_engine._try_call:267`、`base.py:85`）需同步改。

### F4 图内尝试收集（`RouteState.attempts`）

- 文件：`src/botflow/pipeline/langgraph_engine.py`。
- `RouteState` 增加 `attempts: list[dict]`；`_try_call` / `try_stream` 每次**失败尝试** append 一条（含 `stage / endpoint_idx / attempt_no / model_id / provider_id / error_type / error_message / duration_ms`）。
- 图**不落库**，只把明细带出 state。

### F5 驱动落库（成功与失败都写）

- 文件：`src/botflow/core.py`。
- 驱动在每次「策略执行」返回后**取走** state 里的 attempts，跨 backup 组累加；在写最终 `_log_call` 的同一处批量写 `call_attempts`。
- **`status="success"` 路径也必须写** —— 这正是「重试了也要记录」的落点（G1 的验收点）。

### F6 修正最终错误行归属（G2）

- 文件：`src/botflow/core.py:1009-1010`（非流式）与 `:1227-1228`（流式）。
- `_try_call` 把**最后一次尝试**的 `model_id` / `provider_id` 写进 state，驱动用它填最终错误行，不再写死 `None`。

### F7 空流 / 首 chunk 超时并入统一失败路径（G4 —— 口径已更正，见 §1.1）

- 文件：`src/botflow/core.py:1118-1129`（迁入图后为 `try_stream` 节点）。
- ① **删除** `:1146-1147` 的不可达 `if gen is None: break` 及其 `# UNCOVERED` —— 死代码，AGENTS.md 规则 4 优先删除。
- ② 空流 / 首超时**追加 attempt 留痕**（并入 F5 的统一通道，`stage` 取 stream 类），与其它失败一致。
- ⚠️ **不新增 `cooldown.record_failure(...)`** —— 现状已经记了（§1.1），再加会双重计数。
- 该 `# UNCOVERED` 删除后，原来被它遮挡的行必须由**真实用例**覆盖（T7.1 / T7.2），不得换个标记继续掩盖。

### F8 `admin_api` 只读查询

- 文件：`src/botflow/admin_api.py`。
- 新增只读端点：按 `request_id` / `model_id` / 时间范围查询 `call_attempts`。
- 理由：没有查询入口，这张表只能手写 SQL 查，形同不存在。

---

## 4. 测试用例清单

> 本任务先由编码子 agent 实现，测试由验证子 agent 落进 `tests/`。类型三档：正例 / 反例 / 边界值。

### F1 / F2 建表与写入

| 编号 | 类型 | 用例 | 预期 |
|---|---|---|---|
| T1.1 | 正例 | 初始化库后查 `sqlite_master` | `call_attempts` 表存在，列齐全 |
| T1.2 | 正例 | 建索引断言 | `idx_call_attempts_request` 存在 |
| T1.3 | 边界 | 旧库（已有 `call_logs` 无 `call_attempts`）升级 | 启动即建表，**不破坏既有数据** |
| T2.1 | 正例 | `create_call_attempts([...])` 3 条 | 一次批量写入 3 行，字段值一致 |
| T2.2 | 边界 | `create_call_attempts([])` | 空列表不报错、不写行 |
| T2.3 | 反例 | 写入时 `model_id=None` / `provider_id=None` | 允许为 NULL（select 阶段失败无归属） |

### F3 `call_llm` 暴露 last_error

| 编号 | 类型 | 用例 | 预期 |
|---|---|---|---|
| T3.1 | 正例 | 端点首次即成功 | 返回 `(result, None)`，`result["_routing"]` 完好 |
| T3.2 | **反例（关键）** | 端点始终抛 500 | 返回 `(None, err)`，`err.status_code == 500`（异常**不再被吞**） |
| T3.3 | 边界 | `ep.max_retries=1` 且失败 | 只调用 1 次上游，`err` 非空 |
| T3.4 | 正例 | 第 2 次尝试成功 | 返回 `(result, None)`，且上游被调用 2 次 |

### F4 / F5 收集与落库（核心验收）

| 编号 | 类型 | 用例 | 预期 |
|---|---|---|---|
| T5.1 | **正例（G1 关键）** | 首端点失败 → 次端点成功 | `call_logs` 1 行 `success`；`call_attempts` ≥1 行，其 `error_type` / `model_id` / `provider_id` 齐全 |
| T5.2 | **正例（G3 关键）** | 主组全失败 → backup 组成功 | `call_attempts` 同时含主组与 backup 组的尝试 |
| T5.3 | 正例 | 全失败 | `call_logs` 1 行 `error`；`call_attempts` 覆盖每个端点每次尝试 |
| T5.4 | 边界 | 一次请求全部成功、无失败尝试 | `call_attempts` **0 行**（不产生噪音） |
| T5.5 | 反例 | `call_attempts` 写入抛错（mock `db` 故障） | 主链路**仍正常返回**，只 `log.error` |

### F6 归属修正

| 编号 | 类型 | 用例 | 预期 |
|---|---|---|---|
| T6.1 | **正例（G2 关键）** | 非流式全失败 | `call_logs` 错误行 `model_id` / `provider_id` **非 None** |
| T6.2 | **正例（G2 关键）** | 流式全失败 | 同上 |

### F7 空流 / 超时

| 编号 | 类型 | 用例 | 预期 |
|---|---|---|---|
| T7.1 | **正例（G4 关键）** | 端点返回空流 | ① attempts 有该端点行、`error_message` 含 `"empty stream"`；② **冷却计数仍为 1**（守卫「不得重复计数」，见 §1.1） |
| T7.2 | **正例（G4 关键）** | 首 chunk 超时 | 同上；`error_message` 含 `"timed out waiting for first chunk"`；冷却计数仍为 1 |
| T7.3 | 反例 | 断言 `core.py` 中 `:1146-1147` 的不可达 `break` **已删除**、该处**不再有 `# UNCOVERED`** | 死代码删除，覆盖由 T7.1 / T7.2 的真实用例承担 |

### F8 查询端点

| 编号 | 类型 | 用例 | 预期 |
|---|---|---|---|
| T8.1 | 正例 | 按 `request_id` 查 | 返回该请求全部尝试行 |
| T8.2 | 正例 | 按 `model_id` 查 | 只返回该模型的行 |
| T8.3 | 边界 | 无匹配 | 返回空列表（非 404） |

**用例总数**：**23**（正例 14 / 反例 4 / 边界 5 —— 逐条点数：6 + 4 + 5 + 2 + 3 + 3 = 23）。

> 注：此处先前误写为「21（正例 12 / 反例 4 / 边界 5）」，按上表逐条点数更正。
> 落点与断言以 `SG-0_tests.md` 为准，两份文档的条目一一对应。

---

## 5. 明确不做（不改什么）

| # | 不做 | 理由 |
|---|---|---|
| 1 | 不改 `call_logs` 的列与 `status` 口径 | 成功率统计与 `/admin` 报表依赖它（生产 5 万+ 行） |
| 2 | 不在 `call_llm()` 里查库 | 会污染共享工具的签名与依赖方向 |
| 3 | 不把失败尝试写成 `call_logs` 的行 | 会让行数膨胀数倍并破坏「一次请求一行」语义 |
| 4 | 不做聚合 / 清理任务（保留策略） | 先按现状 append-only；数据量增长后再议（measure first） |
| 5 | 不改 `cost` 计算 | `call_logs.cost` 至今为 `None`，属既有缺口，不在本任务范围 |

---

## 6. 风险与前置

1. **schema 变更**：{设计文档 §9 R8} 新增表使回滚不再是纯 `git revert`。表为 append-only 且与主链路解耦 —— 回滚时**保留不删**，旧代码不读它即无影响。
2. **写放大**：全失败请求会产生「端点数 × 尝试数」行。生产上最坏情况是主组 3 端点 × 3 次 + backup 组 3 × 3 = 18 行/请求。需在 F2 用**批量插入**；若实测放大明显，需加采样策略（待实测后再议）。
3. **F3 是破坏性签名变更**：`call_llm()` 所有调用点必须同步改，漏一处会在运行期炸（`tuple` 被当 `dict` 用）。需全仓 grep 调用点。
4. **前置**：无。SG-0 可独立于 SG-1 落地与验收。
