# SG-0 验证子 agent 落码复核报告

> 角色：验证子 agent（仅改 `tests/`，不动 `src/`），主 agent 负责集成验收。
> 状态：**✅ 已验收闭合（2026-09-21）** —— 用例 23 → **27 条**（主 agent 加固 4 条守卫），
> mq3 全量 **`1052 passed / 0 failed / 11 deselected`**，覆盖率 **`4115 语句 / 0 未覆盖 = 100%`**（64.3s）。
> 过程中修掉 **1 个 23 条用例全都抓不到的真缺陷**（`_log_attempts` 漏 `await`）+ **11 处测试侧「靠猜接口」的错配**（见 §8、§9）。
>
> ⚠️ 作废记录：首次 mq3 全量跑测曾把该机压垮（2 核 / 3.9G 内存 / **无 swap**，跑了 **50 分 17 秒** → 用户态 fork 卡死，
> sshd 不出 banner、tailscaled 死掉，而 nginx 仍能应答）。该次结果为 `16 failed / 1028 passed / 99%`，**不作为验收证据**；
> 机器负载回落后以 `nice -n 19 ionice -c 3` 重跑，得上述数字。

---

## 1. 用例落地清单（23 条 = 6+4+5+2+3+3）

| 功能 | 类别 | 用例 | 落点文件 | 就绪 |
|---|---|---|---|---|
| F1 | 正例 | `test_call_attempts_table_created` | `tests/test_db_new.py:230` | ✅ |
| F1 | 正例 | `test_call_attempts_index_created` | `tests/test_db_new.py:244` | ✅ |
| F1 | 边界 | `test_legacy_db_upgrade_creates_table_and_keeps_data` | `tests/test_db_new.py:251` | ✅ |
| F2 | 正例 | `test_create_call_attempts_batch_insert` | `tests/test_db_new.py:271` | ✅ |
| F2 | 边界 | `test_create_call_attempts_empty_list` | `tests/test_db_new.py:300` | ✅ |
| F2 | 反例 | `test_create_call_attempts_allows_null_attribution` | `tests/test_db_new.py:315` | ✅ |
| F3 | 正例 | `test_call_llm_returns_result_and_none_error` | `tests/test_pipeline_base.py:580` | ✅ |
| F3 | 反例 | `test_call_llm_returns_error_instead_of_swallowing` | `tests/test_pipeline_base.py:597` | ✅ |
| F3 | 边界 | `test_call_llm_max_retries_one_calls_upstream_once` | `tests/test_pipeline_base.py:615` | ✅ |
| F3 | 正例 | `test_call_llm_second_attempt_succeeds` | `tests/test_pipeline_base.py:636` | ✅ |
| F4/F5 | 正例(G1) | `test_retry_success_writes_attempts_row` | `tests/test_core_runtime.py` | ✅ |
| F4/F5 | 正例(G3) | `test_backup_group_attempts_recorded` | `tests/test_core_runtime.py` | ✅ |
| F4/F5 | 正例 | `test_all_fail_records_every_attempt` | `tests/test_core_runtime.py` | ✅ |
| F4/F5 | 边界 | `test_no_failed_attempt_writes_no_rows` | `tests/test_core_runtime.py` | ✅ |
| F4/F5 | 反例 | `test_attempts_write_failure_does_not_break_request` | `tests/test_core_runtime.py` | ✅ |
| F6 | 正例(G2) | `test_non_stream_error_row_has_attribution` | `tests/test_core_runtime.py` | ✅ |
| F6 | 正例(G2) | `test_all_endpoints_fail_emits_error_sse`（§3.1 `:208` 归属断言） | `tests/test_stream_fallback.py:189` | ✅ |
| F7 | 正例(G4) | `test_empty_stream_falls_back_to_next`（扩写 + cooldown 守卫） | `tests/test_stream_fallback.py:215` | ✅ |
| F7 | 正例(G4) | `test_stream_timeout_from_request_overrides_default`（扩写 + cooldown 守卫） | `tests/test_stream_fallback.py:358` | ✅ |
| F7 | 反例 | `test_empty_stream_dead_branch_removed`（源码死代码 + `# UNCOVERED` 断言） | `tests/test_stream_fallback.py:408` | ✅ |
| F8 | 正例 | `test_admin_query_attempts_by_request_id` | `tests/test_admin_api.py` | ✅ |
| F8 | 正例 | `test_admin_query_attempts_by_model_id` | `tests/test_admin_api.py` | ✅ |
| F8 | 边界 | `test_admin_query_attempts_empty_result` | `tests/test_admin_api.py` | ✅ |

**合计：正例 14 / 反例 4 / 边界 5 = 23 条。**

> 关于 F4：SG-0_tests.md §1 落点表把 F4 列在 `test_langgraph_engine.py`，但 §2 用例明细里 F4 无独立用例——F4「图内 `RouteState.attempts` 收集」与 F5「驱动落库」合并为 T5.1–T5.5，全部落在 `test_core_runtime.py`（驱动层端到端验证收集+落库）。严格按权威的 23 条明细落点，F4 不单列图级单测。

---

## 2. 旧用例改动（回归面）

### 2.1 §3.1 两处 `model_id` 断言
- `tests/test_stream_fallback.py:208-209`：`test_all_endpoints_fail_emits_error_sse` 的 `error_log[0]["model_id"] is None` → `== ep2.model_id` 且 `provider_id == ep2.detail.provider_id`（F6 归属 = 最后尝试端点）。
- `tests/test_stream_fallback.py:280`：`test_route_failure_emits_error_sse` 保持 `model_id is None` 不动（选择阶段失败无端点可归属，是 F6 反例守卫）。

### 2.2 §3.2 `call_llm` 签名迁移（实测远大于文档估计）
> 文档写「7 文件、约 21 处」。按「落码前全仓 grep、以 grep 结果为准」，实测如下：

| 文件 | 改动点 | 数量 |
|---|---|---|
| `tests/test_langgraph_engine.py` | patch 点（`:89/:110/:129/:187/:213`）+ side_effect 函数（`:274/:299`） | 7 |
| `tests/test_pipeline_engine.py` | patch 点（`:332/:354/:374/:442/:473/:505/:540/:556/:591/:609/:634/:654/:687/:721`）+ `call_llm_side_effect`/`check_call` 函数体（8 处） | 21 |
| `tests/test_pipeline_base.py` | mock patch（2）+ 直接调用（7） | 9 |
| `tests/test_pipeline_strategies.py` | patch 点（5）+ `fake_call_llm` 函数体（1） | 6 |
| `tests/test_langgraph_strategy.py` | patch 点（`:194/:217/:240/:300/:361/:388/:414/:441/:462/:482` 等 13 处，含 `responses`/`call_mock` 重赋值） | 13 |
| `tests/test_p0_1_cache_convergence.py` | 直接调用（`:115/:316`） | 2 |
| `tests/test_stream_fallback.py` | 无 `call_llm` patch（仅 §3.1 env 扩展捕获 `_log_attempts`） | 0 |

**合计 ≈ 58 处**（文档估 ~21，低估约 2.7×）。全部改为返回 `(dict|None, Exception|None)` 元组；直接调用处补 `result, err = await call_llm(...)` + `assert err is None`。已 `py_compile` 全部通过。

---

## 3. 覆盖率与证据

- **目标模块（6 个）**：`botflow.storage.db`、`botflow.storage.models`、`botflow.pipeline._shared`、`botflow.pipeline.langgraph_engine`、`botflow.core`、`botflow.admin_api`。
- **覆盖率数字（已产出）**：**`4115 语句 / 0 未覆盖 / 100%`**；`1052 passed, 0 failed, 11 deselected in 64.30s`。
  6 个目标模块逐项全绿：`storage.db` 593、`storage.models` 135、`pipeline._shared` 51、`pipeline.langgraph_engine` 200、`core` 704、`admin_api` 253。
  首跑为 **99%**（`core.py` 152-156 / 182-184、`db.py` 931-938 共 **14 行**未覆盖）→ 已补 **4 条**用例闭合（§9.3）。
- **证据主机**：**Linux mq3**（`ssh qbot@100.88.88.88`），隔离副本 `/tmp/copy` + `uv venv /tmp/x --python 3.13`：
  `PYTHONPATH=/tmp/copy/src /tmp/x/bin/python -m pytest tests/ --cov=botflow --cov-report=term-missing --timeout=180 -p no:cacheprovider`
  ⚠️ **绝不在部署目录 `/srv/botflow` 跑 pytest**；本机 Windows 跑不了 async（事件循环/loopback 被禁），且沙箱对 `pypi.org` 与清华镜像均不可达。
- **`# UNCOVERED`**：未新增；既有 `core.py:1146-1147` 死代码 `# UNCOVERED` 由 `test_empty_stream_dead_branch_removed`（T7.3）断言删除，覆盖改由 T7.1/T7.2 真实用例承担。

---

## 4. 文档/代码矛盾（已发现）

1. **§3.2 波及面低估**：文档「7 文件 ~21 处」，实测 ~58 处。已按 grep 全量迁移。
2. **T3.2 两文档冲突**：`SG-0_features.md:125` 写「`err.status_code == 500`」，但 `SG-0_tests.md` §2 明确警告「不要断言 `err.status_code`，除非先确认 `ProviderError` 确有该属性」。**以 `SG-0_tests.md` 为准**——`test_call_llm_returns_error_instead_of_swallowing` 只断言 `isinstance(err, ProviderError)` + `"HTTP 500" in str(err)`，不碰 `status_code`。
3. **F4 落点二义**：§1 映射表说 F4→`test_langgraph_engine.py`，§2 用例明细无 F4 独立用例（合并入 T5.1–T5.5→`test_core_runtime.py`）。**以权威 23 条明细为准**，F4 不单列。
4. **§0.1 / §0.2 口径纠偏（已被 features 回改）**：`CallAttempt` 是 Pydantic `BaseModel` 非 dataclass；G4「空流/首超时不记冷却」不成立（死代码+无留痕，冷却已记）。F7 改为「删死代码 + 补留痕」。

---

## 5. 保留的关键守卫（防回退）

| 守卫 | 位置 | 状态 |
|---|---|---|
| T3.2 异常不再被吞 | `tests/test_pipeline_base.py:597` | ✅ |
| T5.1 + T5.5 组合（重试仍留痕 + 留痕坏不影响主链） | `tests/test_core_runtime.py` | ✅ |
| §3.1 第二行归属仍 `None`（选择阶段失败不硬塞模型） | `tests/test_stream_fallback.py:280` | ✅ |
| T7.1 / T7.2 冷却计数 `== 1`（不双重计数） | `tests/test_stream_fallback.py:233/:377` | ✅ |

---

## 6. 未完事项 / 待编码子 agent 对齐

1. **`src/` 实现未落地**（最大阻塞）：需新增/修改——`call_attempts` 表 + 索引 + 旧库升级、Pydantic `CallAttempt` 模型、`db.create_call_attempts` 批量写入（executemany）、`call_llm` 返回 `tuple[dict|None, Exception|None]`、`RouteState.attempts` 收集、`core._log_attempts`（与 `_log_call` 同构）、F6 非流式/流式错误行归属、`core.py:1146-1147` 死代码删除、F8 admin 只读查询端点。
2. **接口假设（测试按 spec 契约编写，待实现对齐）**：
   - `core._log_attempts(attempts: list[dict]) -> None`（模块级，monkeypatch 捕获）。
   - 驱动从 route 结果取 `attempts` 字段转发给 `_log_attempts`（测试中 `_AttemptEngine.route` 返回 `{"choices":…, "attempts":[…]}`）。
   - F8 端点假定 `GET /admin/attempts?request_id=&model_id=`，返回 `{"success": True, "attempts":[…]}`（无匹配返回空列表非 404）。**若编码子 agent 选不同路径，需在联合运行时对齐**。
3. **覆盖率 100% 未验证**：实现落地后于 mq3 跑全量，确认 6 模块 100% 且无新增 `# UNCOVERED`。
4. **Windows 同步子集不可独立验收**：因依赖 async 驱动与真实 `call_attempts` 写库，必须在 mq3 跑异步全量。

---

## 7. 结论

27 条用例已落进 6 个既有测试文件（**0 新增文件**，AGENTS.md 规则 6），§3.1/§3.2 回归面已全量处理（§3.2 实测 **~58 处**，远超文档估计的 21 处）。
4 个关键守卫齐备，**未新增任何 `# UNCOVERED`**，并删除了既有死代码标记（`core.py` 旧 `if gen is None: break`）。
实现由编码子 agent 落地、主 agent 集成验收修掉 1 个真缺陷 + 11 处测试错配，**已在 mq3 产出全量 async + 覆盖率证据：1052 passed / 0 failed / 100%**。
**本任务已闭合，无遗留未闭合项。**

---

## 8. 主 agent 集成验收记录（运行证据待补）

> 编码/验证两个子 agent 并行完成后，按 `AGENTS.md`「全部子任务完成后（主 agent 验收）」做了集成验收。
> **下列改动由主 agent 落地**，不属于两个子 agent 的产出。

### 8.1 修复 1 个测试抓不到的真缺陷：`_log_attempts` 漏 `await`

`core._log_attempts` 内 `_log_writer.log_attempt(entry)` **少了 `await`**（`log_attempt` 是 `async def`），
协程永不执行 → **attempts 在生产路径永不落库**。对照 `_log_call`（`core.py:725`）用的是 `await _log_writer.log(...)`。
→ 已在 `core.py:762` 补 `await`。

**为什么 23 条用例抓不到**：测试 monkeypatch 的是 `core._log_attempts` **本身**（只断言「被调用过、参数对不对」），
抓不到「调用进去了但内部没落库」；且 `pyproject.toml` **未设 `filterwarnings = error`**，
未 await 的 `RuntimeWarning` 不会让测试失败。**这类缺陷只能靠人读代码抓，是本任务最值得记录的一处。**

### 8.2 补一条硬约束的漏洞：`_log_attempts` 直写兜底分支无保护

`_log_writer` 为 `None` 时的直写兜底 `await _get_db().create_call_attempts(entries)` **原本没有 try**，
违反 `SG-0_features.md §2` 硬约束 3（留痕失败绝不影响主链路）。已把整个函数体包进 `try/except Exception` + `log.error`。
按 `SG-0_tests.md §2` 的要求，**吞错发生在 `_log_attempts` 内部**，驱动不再包第二层；T5.5 据此改写为打桩**底层 writer**。

### 8.3 测试侧 4 处「靠猜接口」的错配（已修，脚本化替换并断言命中数 = 1）

| # | 错配 | 事实（以源码为准） | 修法 |
|---|---|---|---|
| 1 | `_AttemptEngine.route` 把 attempts 放在键 `"attempts"` | 实现用 **`"_attempts"`**（`langgraph_engine.py:580` 写、`core.py:1045` pop；与既有 `_routing` 同风格，`_` 前缀是内部键约定，必须 pop 掉才不泄漏给客户端） | stub 改 `result["_attempts"]` |
| 2 | 用 `result["status"]="error"` 表达失败 | 失败路径是**异常驱动**：`core.py:1068` 用 `getattr(e, "attempts")`，由 `_raise_routing_error` 把 attempts / used ids 挂在异常上 | error 模式 stub 改为 **raise** 并携带 `.attempts` / `.used_model_id` / `.used_provider_id`；全失败断言 `status_code == 502`（非 200） |
| 3 | `_drive` 用**同步 lambda** 打桩 `_log_attempts` | 驱动是 `await _log_attempts(...)` → await 非可等待对象 → `TypeError` 被错误路径吞掉 → 502 | 改为 `async def _capture(rows)` |
| 4 | T5.5 用 `caplog` 断言 `log.error` | **loguru 不向 stdlib `logging` 传播**（`common/logger.py` 直接 `logger.add(sys.stderr)`）→ `caplog.records` 恒为空 | 改为 `monkeypatch.setattr(core, "log", MagicMock())` 并断言 `fake_log.error.called`；打桩目标移到**底层 writer** |

> 第 2 条同时是**反例守卫**的落点：`test_stream_fallback.py` 里「选择阶段失败（无端点被调用）」仍必须断言
> `model_id is None` —— F6 只填「确实尝试过」的归属，不得无脑塞最后一个模型。

### 8.4 文档同步（主 agent）

`docs/pipeline-single-graph-design.md §3.6` 有**两处残留**与 §1.1 的更正矛盾（G4 行写「空流 / 首超时不记冷却」、
设计点 4 写「既 `cooldown.record_failure` 也进 attempts」）→ 均已改为「**冷却现状已在记，不得新增 `record_failure`**（会双重计数）」，
并指向 `SG-0_features.md §1.1`。

### 8.5 运行证据状态（已闭合）

| 项 | 状态 |
|---|---|
| 本机（Windows） | ❌ 跑不了 async；沙箱对 `pypi.org` 与清华镜像**均不可达** → 本机不产出验收证据 |
| mq3 | ✅ **已产出**：`1052 passed / 0 failed / 11 deselected in 64.30s`，覆盖率 `4115 / 0 miss / 100%` |
| 生产节点 | 故意不使用（根分区 95% 满 + 生产机） |
| **覆盖率数字** | ✅ **100%（0 未覆盖）** —— 已闭合 |

补充事实：首次跑测（`16 failed / 1028 passed / 99%`，50 分 17 秒）**已作废**——
它既跑在「`core.py:762` 补 `await` 之前」的副本上，又把 mq3 压到用户态无法响应。
判定 mq3 故障用了分层探测：ICMP 通 + TCP `:22` 握手成功但 **5 分钟不出 banner**，而 **nginx 能应答 301** ——
nginx worker 是 master **预派生**的（不 fork），sshd 每连接**必须 fork** → 指向 **fork/进程创建被阻塞**（2 核 / 3.9G / 无 swap），
而非网络问题或机器未启动。`uptime` 显示 **up 20 天**（从未重启），load 15m=32.01 → 1m=0.23（尖峰已退），与上述推断一致。

---

## 9. 第二轮：11 处测试侧错配 + 覆盖率收口（主 agent）

> 第一轮聚焦复测把失败从 **16 收敛到 11**（我本地修的 5 个已转绿）。
> 11 个的根因**全在测试侧**（实现是对的），逐条以源码为准修正 —— 全部用一次性 Python 脚本替换并**断言命中数**，防静默漏改。

### 9.1 五类错配

| 根因（源码事实） | 涉及 | 修法 |
|---|---|---|
| `_log_call` 用**关键字传参**（`core.py:1053/1077/1292/1341`），测试读 `call.args[0]` → 恒空 | `test_core_runtime` 3 条 | 改读 `call.kwargs` |
| `route()` 成功时内嵌 `_attempts`（`langgraph_engine.py:580`），整体相等断言失败 | `test_pipeline_engine` 6 条 | 加 `_without_attempts()` 助手，6 处替换 |
| `call_attempts` 有 `id INTEGER PRIMARY KEY AUTOINCREMENT`（与 `call_logs` 同约定），测试断言列集合**精确相等** | `test_call_attempts_table_created` | 改 `required <= col_names` + `"id" in col_names` |
| `create_call_attempts` 走 `conn.executemany`（`db.py:827`），测试却打桩 `db.execute_write` → **打空** | `test_create_call_attempts_batch_insert` | 改打桩**连接**的 `executemany`（`_ConnSpy`），断言 `executemany_calls == 1` 且 `rows_per_call == [3]` |
| 空列表用例同样打桩 `execute_write` → 断言恒真（**空壳测试**） | `test_create_call_attempts_empty_list` | 改打桩 `_ensure_connection`，断言短路返回 `0` 且**不开连接** |

### 9.2 加固 4 条守卫用例（补上「23 条抓不到漏 `await`」的缺口）

`test_core_runtime.py::TestAttemptLogging` 新增：

| 用例 | 作用 |
|---|---|
| `test_log_attempts_actually_reaches_writer` | **真实执行** `_log_attempts`，断言 writer 收到 `CallAttempt`（漏 `await` 时协程不执行、`seen` 为空 → 必红） |
| `test_log_attempts_falls_back_to_direct_db_write` | `_log_writer=None` 时的直写兜底分支 |
| `test_log_attempts_empty_list_is_noop` | 空列表短路 |
| `test_log_attempts_swallows_writer_failure` | 留痕失败不上抛（硬约束 3） |

### 9.3 覆盖率收口（首跑 99% → 100%）

首跑 14 行未覆盖，**全在新增的 SG-0 生产路径上**：

| 位置 | 为什么没被覆盖 | 补法 |
|---|---|---|
| `core.py:152-156` | `CallLogWriter.log_attempt` **本体从未真实执行** —— 守卫用例与 T5.5 都把它整个打桩掉了（**这正是"漏 await"能溜过去的那一层**） | `test_core_units.py` 新增 2 条：缓冲后刷盘、满缓冲自动刷盘 |
| `core.py:182-184` | `_flush_unlocked()` 的 attempt 批量写 + 其 `except`（缓冲恒空 → 分支不达） | 同上 + `test_call_log_writer_flush_attempts_error_logged` |
| `db.py:931-938` | `query_call_attempts` 的 `provider_id` / `group_id` / `error_type` 三个过滤分支（T8.x 只走过 `request_id` / `model_id`） | `test_db_new.py` 新增 `test_query_call_attempts_extra_filters` |

### 9.4 守卫与红线复核（逐条确认在位）

| 守卫 | 位置 | 状态 |
|---|---|---|
| T3.2 异常不再被吞 | `tests/test_pipeline_base.py:597` | ✅ |
| T5.1 + T5.5 组合 | `tests/test_core_runtime.py` | ✅ |
| §3.1 第二行归属仍 `None`（选择阶段失败不硬塞模型） | `tests/test_stream_fallback.py:280` | ✅ |
| 冷却计数 `== 1`（**不得双重计数**） | `tests/test_stream_fallback.py:168/211/225/233/334/369/377` | ✅ |
| 死代码标记已删 | `core.py` 旧 `if gen is None: break` 及其 `# UNCOVERED` | ✅ 已删 |
| 未新增 `# UNCOVERED` | `src/` 现存 10 处**全部为既有**（旧库迁移路径 ×6、抽象方法占位 ×1、不可达分支 ×1） | ✅ |

### 9.5 交付物

- `tests/`：3 个文件修正 + 8 条新用例（4 条守卫 + 4 条覆盖率）→ 目标模块 **100%**。
- 脚本（可复查）：`.workbuddy/tmp/fix_sg0_tests3.py`、`add_sg0_guards.py`、`close_coverage.py`、`sg0_full.sh`。
