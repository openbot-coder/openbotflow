# 流式 call_logs tokens 恒 NULL 回归：功能点与测试用例

> 任务类型：bug 修复（SG-1 重构回归，3 处微改）
> 范围：仅 `src/botflow/core.py::_drive_stream` 的 usage 累积与成功落库
> 关联：`git show 29c2c0f~1:src/botflow/core.py`（旧 usage 语义锚点）、
> `docs/tasks/SG-1_features.md`（F7 统一驱动重构）

---

## 1. 功能点描述

### 1.1 Bug 背景

流式（SSE）调用成功后，`call_logs` 的 tokens 四列
（`prompt_tokens` / `completion_tokens` / `cache_tokens` / `total_tokens`）**恒为 NULL**，
而同一上游、同一模型的非流式调用 tokens 正常。管理端「用量统计 / 日汇总」因此对
流式流量全部记 0（`storage/daily_summary.py:47-49` 求和时 `l.prompt_tokens or 0`）。

### 1.2 根因

SG-1 把端点重试环从 `_stream_common` 迁进统一驱动 `_drive_stream` 时，
**usage 累积逻辑没有跟着迁**（`src/botflow/core.py::_drive_stream`）：

- 循环里 `lines, _usage = _serialize(chunk)` 照旧把 usage 从序列化器解出来，
  但 `_usage` 此后再未被读取——**取到了却从未使用**；
- 成功落库那行硬编码 `usage=None`：

```python
await _log_call(..., status="success", duration_ms=duration, usage=None)
```

`_log_call`（`core.py:725-728`）在 `usage` 为 falsy 时四列全写 NULL，于是流式成功行
tokens 恒空。非流式 `_drive_chat` 走的是 `usage=result.get("usage", {})`
（`core.py:1116`），完全不受影响——这也解释了「只有流式坏」的定向现象。

### 1.3 修复方案（3 处 diff 摘要，均在 `core.py::_drive_stream`）

| # | 位置 | 改动 |
|---|------|------|
| ① | while 循环内、`final_state = None` 之后（`core.py:1186`） | 新增 `usage_final: dict | None = None` —— **每组尝试开始时重置** |
| ② | chunk 分支，`lines, _usage = _serialize(chunk)` 之后（`core.py:1205-1206`） | 新增 `if _usage: usage_final = _usage` —— 取**末次非空** usage |
| ③ | 成功路径 `_log_call(...)`（`core.py:1226`） | `usage=None` → `usage=usage_final` |

与旧代码（`git show 29c2c0f~1:src/botflow/core.py`）逐条对齐：

```
旧 core.py:1177  usage_final = None                ←→ 新 ①（同在 while 循环内，降级重置）
旧 core.py:1275  if usage: usage_final = usage     ←→ 新 ②（同为 truthy 才覆盖）
旧 core.py:1300  usage=usage_final                 ←→ 新 ③
```

语义等价性（已核对）：

- **取末次非空**：`if _usage:` 为真才覆盖 → 中间 chunk 带 usage、后续 chunk
  `usage=None`（多数 chunk）不会把已累积值冲掉；空 dict `{}` 同样不覆盖（旧代码
  `if usage:` 亦然）。
- **每组重置**：① 在 `while True:` 体内 → 降级到备份组时 `usage_final` 归零，
  最终落库的是**成功那一组**自己的 usage，不会串到失败组的残留值。
- **错误路径不变**：降级耗尽（`core.py:1246`）与非预期异常（`core.py:1269`）两条
  错误落库仍是硬编码 `usage=None`，本次修复未触碰。

### 1.4 数据形态核对（serialize → _log_call 三处吻合）

`serialize(chunk) -> (sse_lines, usage | None)`，usage 均为**内部统一形态**
（`prompt_tokens` / `completion_tokens` / `cache_tokens` / `total_tokens`）：

| 序列化器 | 取值 | 形态 |
|----------|------|------|
| `_openai_serialize`（`core.py:1309-1313`） | `sse_data.get("usage")`，透传自 `chunk["usage"]`（`protocol_adapter.py:193-194`） | 内部 usage dict |
| `_anthropic_serialize`（`core.py:1316-1327`） | `chunk.get("usage")`（SSE 事件里的 `input_tokens` 是对外协议，不回流） | 内部 usage dict |
| `_stream_responses` 的 `_responses_serialize_wrap`（`core.py:1403-1421`） | `chunk.get("usage")` | 内部 usage dict |
| 默认 `lambda`（`core.py:1180`，serialize=None 时） | 恒 `None` | 落库 NULL（预期） |

消费端 `_log_call`（`core.py:725-728`）只认 `usage.get("prompt_tokens" /
"completion_tokens" / "cache_tokens" / "total_tokens")`。上游 provider 产出的
chunk usage 已是该形态（`providers/openai_compat.py:218-222`、
`providers/anthropic_provider.py:185-188`、`providers/google_provider.py:193-196`），
**两端吻合**。`cache_tokens` 在 anthropic/openai 均有值，修复后该列一并恢复。

### 1.5 影响面

- **仅流式成功路径**（`_drive_stream` 的 `status="success"` 落库行）受益；
  非流式 `_drive_chat`、两条错误落库、`call_attempts` 留痕均不变。
- **历史 NULL 行不可恢复**：usage 来源只有当时流里的 chunk，事后无从回填；
  修复只对**新产生**的流式调用生效。
- 序列化器本身未改，SSE 响应字节不变（对外协议零影响）。
- 客户端中途断连的行：沿旧语义走成功落库，usage 为断连前已累积的末次非空值。

---

## 2. 测试用例清单

统一约定：流式事件序列必须含 `("state", {"recoverable": False, ...})` 终态，
否则 `_drive_stream` 会把成功流当组级失败去降级（见 `test_core_endpoints.py:88-90` 注释）。
断言一律读 `core._log_call` 捕获到的 **kwargs**（`_log_call` 是 `**kwargs` 调用，
`call.args` 恒空）。

| # | 用例名（建议） | 断言点 | 建议测试文件 |
|---|----------------|--------|--------------|
| 1 | `test_stream_success_logs_usage_from_chunk` | **正例**：流式成功且某 chunk 带 usage → `_log_call` 收到 `status="success"` 且 `kwargs["usage"] == 该 usage dict`（逐键：`prompt_tokens/completion_tokens/cache_tokens/total_tokens`）；不再断言 `usage is None` | `tests/test_core_runtime.py`（`_drive_setup` + 捕获 kwargs） |
| 2 | `test_stream_last_chunk_usage_wins` | **正例**：多 chunk，仅末尾 chunk 带 usage（模拟 `stream_options.include_usage` 的 usage-only 终局 chunk）→ 落库 usage **等于末尾那个**（取末次非空，不是首次、不是累加） | `tests/test_core_runtime.py` |
| 3 | `test_stream_no_usage_logs_null` | **反例**：全流无 usage（所有 chunk 的 serialize 返回 `None`）→ `kwargs["usage"] is None`（`_log_call` 四列写 NULL，与修复前行为一致，防「无 usage 时写脏值」） | `tests/test_core_runtime.py` |
| 4 | `test_stream_mid_usage_not_cleared_by_later_null` | **边界**：中间 chunk 带 usage、其后 chunk `usage=None` → 落库仍是**中间那个** usage（`if _usage:` 不允许 None 覆盖已累积值） | `tests/test_core_runtime.py` |
| 5 | `test_stream_fallback_resets_usage_per_group` | **边界**：主组降级到备份组 → 断言两组各 `run(mode="stream")` 一次、最终落库 usage 是**成功组（备份组）**的 usage；主组若曾产出带 usage 的 chunk 也**不得**残留（`usage_final` 在 while 内重置） | `tests/test_core_runtime.py`（亦可用 `tests/test_stream_fallback.py` 的 `env` fixture：主组 `error=AllModelsCooldownError` + 备份组 `register_group(2, events=[...])`） |
| 6 | `test_stream_error_paths_keep_usage_none` | **反例**：① 降级耗尽（主组 `AllModelsCooldownError` 且无备份）② 未预期异常（`ValueError`）→ 两条 `status="error"` 行的 `kwargs["usage"] is None`（错误路径不被本修复带偏） | `tests/test_core_runtime.py`——扩展既有 `test_drive_stream_exhausted_fallback_leaves_trace` / `test_drive_stream_unexpected_error_leaves_trace_and_closes`（各加一行 usage 断言即可，或新增独立用例） |
| 7 | （回归锚点，引用已有测试，无需新写） | **非流式 usage 路径不受影响**：`tests/test_core_units.py::test_log_call_uses_writer`（`_log_call` 消费 usage dict 写四列）、`tests/test_core_endpoints.py::test_chat_completions_non_stream` + `test_non_stream_response_byte_identical`（stub 响应含 `usage: {prompt_tokens:1, completion_tokens:1, total_tokens:2}`，端点路径字节级不变） | 已有测试，跑通即为锚 |

可选端到端补强（真实 DB 落库，而非 mock kwargs）：
在 `tests/test_core_endpoints.py` 的 `client` fixture 下发 `stream:true` 请求，用
`core._db.get_call_logs_for_day(...)`/`query_call_logs` 读回该行，断言
`prompt_tokens is not None`——直接复现「tokens 恒 NULL」的原始症状，作为
**症状级回归钉**。

---

## 3. 测试基础设施参考

### 3.1 首选：`tests/test_core_runtime.py`（monkeypatch 捕获 `_log_call` kwargs）

- **`_DriverEngine`**（`test_core_runtime.py:1326`）：单入口桩
  `run(strategy, group, mode, **kw)`；`_run_behavior[gid]` 是
  `async fn(s, g, m) -> result | raise` 的工厂。**流式用例**让工厂返回一个
  **async gen 对象**（`engine.run` 是 `await behavior(...)`，故工厂体内
  `async def gen(): ...; return gen()`），gen 产出 `("chunk", c)` / `("state", s)` 事件。
- **`_make_group(gid, type_, fallback_gid)`**（`:1371`）：组 1 挂
  `fallback_gid=2`、组 2 作备份，即可构造用例 5 的降级链。
- **`_drive_setup(monkeypatch, engine, primary, backup, attempts)`**
  （`:1375`）：一次性接好 `_get_extra_route_params` / `_load_group` /
  `_log_call`(AsyncMock) / `_log_attempts`。**用例 1-5 建议直接
  `monkeypatch.setattr(core, "_log_call", _capture)`（`async def _capture(**kwargs)`）
  覆盖它装的 AsyncMock**，随后
  `async for _ in core._drive({"model": "x", "messages": []}, mode="stream", serialize=...)`
  消费完流，再断言 `captured[-1]["usage"]`。
- **serialize 注入**：`core._drive(..., mode="stream", serialize=my_serialize)`，
  自定义 `def my_serialize(chunk) -> tuple[list[str], dict | None]` 精确控制
  每个 chunk 的 usage 返回值（用例 1-4 的关键旋钮）；也可直接传
  `core._openai_serialize` 走真实序列化链。
- 既有可参照用例：`:1547` `test_drive_stream_unexpected_error_leaves_trace_and_closes`、
  `:1585` `test_drive_stream_exhausted_fallback_leaves_trace`（正是用例 6 的骨架）。

### 3.2 备选：`tests/test_stream_fallback.py`（组级降级语义最全）

- **`StubEngine.register_group(gid, events=[("chunk", c), ("state", s)], error=exc)`**
  （`:136-159`）：按组 id 注册事件序列或抛错，`run_calls` 记录 `(gid, mode)`。
- **`env` fixture**（`:94-108`）：`_LogCapture` 同时捕获 `_log_call`（list 项，
  即 kwargs dict）与 `_log_attempts`（`.attempts`），用例 5 降级断言最顺手。
- 既有 `test_driver_group_fallback_works_for_stream`（`:178`）即降级骨架，扩 usage 断言即可。

### 3.3 端到端：`tests/test_core_endpoints.py`（TestClient + 真实 DB）

- **`StubEngine`**（`:52-91`）：`run(mode="stream")` 返回 `("chunk", ...)` +
  `("state", {"recoverable": False, ...})` 事件序列；`client` fixture 建真实
  sqlite（`tmp_path`）并把 `_log_call` 指向真实写库（**未**打桩时）。
- 用途：症状级回归——请求后直接查 `call_logs` 行，断言 tokens 四列非 NULL。
  注意该文件默认 fixture **未**打桩 `_log_call`（真实落库），而
  `tests/test_core_runtime.py:694` 的同名 `client` fixture **有** `AsyncMock` 打桩，
  两处别搞混。

---

## 4. 验证口径

本机 Windows 跑不了 async 测试，用例留待 CI/目标环境执行。核对基线：

- 修复 3 处均已落地（`core.py:1186 / 1205-1206 / 1226`）；
- 与旧代码三条语义逐条等价（重置时机、truthy 覆盖、成功落库）；
- 错误路径两处仍 `usage=None`（`core.py:1246 / 1269`）；
- 非流式 `core.py:1116` 未被触碰。
