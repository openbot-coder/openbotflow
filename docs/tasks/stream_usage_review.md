# 流式 usage 丢失回归：验证子 agent 审核结论

> 审核对象：`docs/tasks/stream_usage_features.md`（功能点 + 测试用例清单）
> 对照实现：`src/botflow/core.py::_drive_stream`（修复后版本）
> 审核角色：验证子 agent（不改 `src/`，只审核 + 写测试）

---

## 1. 审核结论：**通过**

清单描述的 3 处修复全部落地，行号与文档一致，语义与文档所述等价，未发现需要打回的问题。

| 清单项 | 实现位置 | 核对结果 |
|--------|----------|----------|
| ① `usage_final: dict \| None = None` 每组重置 | `core.py:1186`，在 `while True:` 体内、`final_state = None` 之后 | ✅ 位置正确（循环体内 → 降级到备份组时会重置） |
| ② `if _usage: usage_final = _usage` 末次非空 | `core.py:1205-1206`，`lines, _usage = _serialize(chunk)` 之后 | ✅ truthy 守卫正确（`None` / `{}` 不覆盖） |
| ③ 成功路径 `usage=usage_final` | `core.py:1226`（`_log_call(..., status="success", ..., usage=usage_final)`） | ✅ 已替换原硬编码 `usage=None` |
| 错误路径不被带偏（降级耗尽） | `core.py:1246` 仍 `usage=None` | ✅ 未触碰 |
| 错误路径不被带偏（未预期异常） | `core.py:1269` 仍 `usage=None` | ✅ 未触碰 |
| 非流式不受影响 | `core.py:1116` `usage=result.get("usage", {})` | ✅ 未触碰 |
| 消费端 `_log_call` 形态 | `core.py:725-728` 认 `prompt_tokens/completion_tokens/cache_tokens/total_tokens`，falsy usage → 四列 NULL | ✅ 与 serialize 产出的内部 usage 形态吻合（文档 §1.4 成立） |

补充核对：

- 默认序列化器（`serialize=None`）在 `core.py:1180` 为 `lambda c: ([json.dumps(c)], None)` → 恒 `None`，落库 NULL 属预期，与清单用例 3 一致。
- `_drive` 的 stream 分支仅转发参数（`core.py` `_drive` → `_drive_stream(internal, serialize, done_signal, request)`），serialize 注入缝存在，测试可用 `core._drive(..., mode="stream", serialize=...)` 精确控制 usage。
- 文档 §1.3 的旧代码三条锚点（`git show 29c2c0f~1:...`）我未逐字回放旧文件，但新实现的三条语义（循环内重置 / truthy 覆盖 / 成功落库）互相自洽且与「取末次非空」描述一致 —— 这一条按**实现侧自洽**放行，旧代码逐字对比以编码子 agent 已完成的核对为准。

未发现清单与实现的矛盾，也未发现清单遗漏的 usage 相关分支（`_drive_chat`、两条错误落库、attempt 留痕均与 usage 无关或已明确不改）。

---

## 2. 测试用例落点

依据审核通过的清单 §2 编写（新增于 `tests/test_core_runtime.py`，monkeypatch 捕获 `core._log_call` kwargs）：

| 清单 # | 用例名 | 状态 |
|--------|--------|------|
| 1 | `test_stream_success_logs_usage_from_chunk` | 新增 |
| 2 | `test_stream_last_chunk_usage_wins` | 新增 |
| 3 | `test_stream_no_usage_logs_null` | 新增 |
| 4 | `test_stream_mid_usage_not_cleared_by_later_null` | 新增 |
| 5 | `test_stream_fallback_resets_usage_per_group` | 新增 |
| 6 | `test_stream_error_paths_keep_usage_none` | 新增（合写两条错误路径：降级耗尽 + ValueError） |
| 7 | 回归锚点（非流式 usage） | **引用已有测试，未新写**：<br>`tests/test_core_units.py::test_log_call_uses_writer`、<br>`tests/test_core_endpoints.py::test_chat_completions_non_stream`、<br>`tests/test_core_endpoints.py::test_non_stream_response_byte_identical` |

既有错误留痕用例 `test_drive_stream_unexpected_error_leaves_trace_and_closes`（`test_core_runtime.py:1547`）与 `test_drive_stream_exhausted_fallback_leaves_trace`（`:1585`）断言的是 status/error_type/model_id，**未**断言 usage；为不动既有用例，用例 6 以独立新用例覆盖两条错误路径的 `usage is None`（与清单「或新增独立用例」的许可一致）。

---

## 3. 测试执行记录

### 3.1 本机 Windows 同步子集冒烟

（见下方「执行结果」小节，命令：`PYTHONPATH=src .venv/Scripts/pytest.exe tests/test_core_runtime.py -m "not asyncio" -q`）

### 3.2 Linux 远端 mq3 全量（async 证据）

（见下方「执行结果」小节：打包 → `/tmp/bt_venv` 安装 → `pytest tests/ --cov=botflow`）

### 执行结果

#### 本机 Windows 同步子集冒烟

- 命令：`PYTHONPATH=src .venv/Scripts/pytest.exe tests/test_core_runtime.py -m "not asyncio" -q -p no:cacheprovider --basetemp=.workbuddy/tmp/bt14`
- 结果：**1 passed**（收集 76 项，含本任务新增 6 项，全部被 `-m "not asyncio"` 正确 deselect 为 async 用例）。
- 两个本机环境限制（取证记录，非本次改动引入）：
  1. **pytest 收尾挂死**：不加 `--basetemp` 时 sessionfinish 的 numbered-tmp 目录 `shutil.rmtree` 卡死（faulthandler 栈：`_pytest/tmpdir.py:340 → pathlib.rm_rf`）。加 `--basetemp` 后正常退出。
  2. **依赖 `client` fixture 的同步用例也挂**：`tests/test_core_runtime.py:696` 的 `asyncio.new_event_loop()` 卡在 `socket._fallback_socketpair`（本机 loopback 被禁，与 async 用例同一根因）。故 `TestEndpointBranches` / `TestAttemptLogging` 两个类本机不可执行，已 deselect；其余同步用例 1 passed / 75 deselected（0 failed）。
- 新增 6 个用例在本机无法执行（async），按既定做法取 Linux 证据。

#### Linux 远端 mq3 全量（async 证据）

- 链路：本地 tar 打包（含 README.md）→ scp → `/tmp/bt_copy` 解包 → `uv venv /tmp/bt_venv --python 3.13` → `pip install -e` → 全量 pytest。
- 命令：`cd /tmp/bt_copy && PYTHONPATH=src /tmp/bt_venv/bin/pytest tests/ --cov=botflow --cov-report=term -q -p no:cacheprovider`
- **结果：1234 passed / 0 failed / 23 deselected（integration），耗时 127s。**
- **覆盖率：TOTAL 4430 行，missed 0，100%**（其中 `src/botflow/core.py` 690 行 / missed 0 / 100%）——达到 AGENTS.md 的 100% 放行门槛。
- 对照基线（修复前全量 1223 passed / 4427 行 0 missed）：passed +11（本任务新增 6 个 usage 用例 + 编码侧其它已入库用例），行数 +3（修复新增的 `usage_final` 相关行），仍 0 missed。
- 新增用例单跑取证（`-k usage -v`）：6 passed，含：
  `test_stream_success_logs_usage_from_chunk` / `test_stream_last_chunk_usage_wins` /
  `test_stream_no_usage_logs_null` / `test_stream_mid_usage_not_cleared_by_later_null` /
  `test_stream_fallback_resets_usage_per_group` / `test_stream_error_paths_keep_usage_none`。

#### 遗留风险

- 「症状级回归钉」（清单 §2 末尾的**可选**端到端用例：真实 DB 落库读回 `prompt_tokens is not None`）**未写**——按清单属可选项；如需可在 `tests/test_core_endpoints.py` 的 `client` fixture（真实 sqlite，未 mock `_log_call`）上补一条。
- 历史 NULL 行不可回填（文档 §1.5 已述，属预期，非风险缺口）。
