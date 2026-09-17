# P9-6 测试用例落点清单

> 本任务为**编码子 agent**，不写 `tests/` 下的测试代码（那是验证子 agent 的职责）。
> 本文档把 `P9-6_features.md` 的每条用例映射到**落点文件、用例名、断言要点、覆盖功能点与类别**，
> 供验证子 agent 平行落码。

---

## 落点约定

- **改写文件**：`tests/test_context.py`（删除旧 3 个 `_cjk_ratio` 用例 + import 里的 `_cjk_ratio`；
  其余 `_extract_text` / `truncate_*` 旧用例保留并随实现迁移）。
- **辅助**：F3 单例可直接调 `botflow.common.context._encoding()`；F5 的 `T5.1` 在测试环境内
  `import tiktoken` 即可（依赖已在 `.venv`），`T5.2` 解析 `pyproject.toml` 文本断言。
- **不新增文件**：沿用既有 `tests/test_context.py`，不引入新测试模块（文件越少越好，AGENTS.md 规则 6）。

---

## 用例 → 落点映射

### F1 `estimate_tokens` 用 tiktoken（覆盖：正/反/边界）

| 编号 | 类别 | 用例名 | 落点文件 | 断言要点 |
|---|---|---|---|---|
| T1.1 | 正例 | `test_estimate_tokens_chinese` | `tests/test_context.py` | 结果 `>0` 且等于 `len(_encoding().encode_ordinary("user")) + len(_encoding().encode_ordinary("你好世界")) + 1` |
| T1.2 | 正例 | `test_estimate_tokens_english` | 同上 | 等于 o200k_base 对 `role+text` 的真实编码计数 |
| T1.3 | 正例 | `test_estimate_tokens_empty_list` | 同上 | `estimate_tokens([]) == 0` |
| T1.4 | 正例 | `test_estimate_tokens_list_content` | 同上 | list content 经 `_extract_text` 后计数 `>0` |
| T1.5 | **反例** | `test_estimate_tokens_special_token_literal` | 同上 | 正文含 `"<|endoftext|>"` **不抛 `ValueError`**（证 `encode_ordinary`） |
| T1.6 | 边界 | `test_estimate_tokens_long_special_literal` | 同上 | 长文本内嵌多个特殊 token 字面量仍正常返回 |
| T1.7 | 边界 | `test_estimate_tokens_single_char` | 同上 | 结果 `>= _MESSAGE_OVERHEAD`（空正文也有每条开销） |

### F2 删除 `_cjk_ratio`（覆盖：反/正/边界）

| 编号 | 类别 | 用例名 | 落点文件 | 断言要点 |
|---|---|---|---|---|
| T2.1 | **反例** | `test_cjk_ratio_removed_from_module` | `tests/test_context.py` | `not hasattr(context, "_cjk_ratio")` |
| T2.2 | 正例 | `test_estimate_tokens_no_heuristic_path` | 同上 | 中英混合结果 = o200k_base 真实计数（≠旧 `4 - cjk_ratio*2` 公式的低估） |
| T2.3 | 边界 | `test_no_cjk_ratio_refs_in_src` | 同上（或独立 grep 用例） | `src/botflow` 内 grep `_cjk_ratio` 仅命中 `docs/` 与本文档，源码零残留 |

### F3 编码器惰性单例（覆盖：正/正/边界）

| 编号 | 类别 | 用例名 | 落点文件 | 断言要点 |
|---|---|---|---|---|
| T3.1 | 正例 | `test_encoding_singleton_returns_same_object` | `tests/test_context.py` | `a is b`（`a,b = _encoding(), _encoding()`） |
| T3.2 | 正例 | `test_encoding_loads_on_first_call` | 同上 | `_encoding().encode_ordinary("x")` 可调用且返回非空 |
| T3.3 | 边界 | `test_encoding_lru_cache_maxsize_one` | 同上 | 多次调用始终 `is` 同一实例 |

### F4 上界短路（覆盖：正/正/边界/边界/反/正/反）

| 编号 | 类别 | 用例名 | 落点文件 | 断言要点 |
|---|---|---|---|---|
| T4.1 | 正例 | `test_truncate_zero_window_returns_as_is` | `tests/test_context.py` | `context_window=0` 与 `-5` 均原样返回（4 个 ctx=0 组零行为变化） |
| T4.2 | 正例 | `test_truncate_upper_bound_fits` | 同上 | 短内容 + 大窗口 → `_token_upper_bound <= limit` → 原样返回 |
| T4.3 | **边界** | `test_truncate_upper_bound_equal` | 同上 | 构造 `_token_upper_bound == limit` → 走短路、原样返回（不进编码） |
| T4.4 | **边界** | `test_truncate_upper_bound_plus_one` | 同上 | 构造 `_token_upper_bound == limit + 1` → **不**短路、进编码/二分 |
| T4.5 | 反例 | `test_truncate_oversize_gets_truncated` | 同上 | 超大内容 + 小窗口 → 返回子集（`len(out) < len(in)`） |
| T4.6 | 正例 | `test_truncate_keeps_system` | 同上 | system + 多条超大 user → 输出首项为 system 且缩短 |
| T4.7 | **反例（集成期新增）** | `test_upper_bound_covers_empty_messages` | 同上 | N 条 `{"role":"","content":""}` → `_token_upper_bound >= estimate_tokens == N * _MESSAGE_OVERHEAD`，且 `limit < N` 时**不得**被字节 0 短路放行（`_MESSAGE_OVERHEAD` 计入上界的回归守卫） |

> 构造 `上界 == limit` 的取法：`limit = context_window - reserve`，取 `max_tokens` 令
> `limit == _token_upper_bound(messages)` 即可；`+1` 用 `max_tokens` 减 1。两条必须**各有一个用例**
> 证明短路边界正确，而非恒真。

### F5 硬依赖声明（覆盖：正/正）

| 编号 | 类别 | 用例名 | 落点文件 | 断言要点 |
|---|---|---|---|---|
| T5.1 | 正例 | `test_tiktoken_importable_in_venv` | `tests/test_context.py` | `import tiktoken` 不抛 `ImportError`（环境侧验收） |
| T5.2 | 正例 | `test_pyproject_declares_tiktoken` | `tests/test_context.py` | 读 `pyproject.toml`，`dependencies` 含 `tiktoken>=0.14.0` |

---

## 旧用例处理（验证子 agent 必做）

| 文件:行 | 现状 | 改动后 | 处理 |
|---|---|---|---|
| `tests/test_context.py:5-10` import 块 | 含 `from botflow.common.context import (..., _cjk_ratio)` | 模块已无 `_cjk_ratio` → 整文件 import 失败 | **删除 import 行内的 `_cjk_ratio`** |
| `tests/test_context.py:52-62` `test_cjk_ratio_*` 三个用例 | 直接调用 `_cjk_ratio` | 函数已删 | **整段删除**（由 F2/T2.1–T2.3 以正确前提覆盖） |
| `tests/test_context.py:31-49` `test_estimate_tokens_*` 四例 | 仅断言 `>0` | 实现变准后仍 `>0` | **保留**，可顺手加「等于 o200k_base 真实计数」的精确断言（T1.1/T1.2 覆盖） |
| `tests/test_context.py:65-109` `test_truncate_*` 六例 | 已覆盖零窗/拟合/带 system/仅 system/仅留最后/空 | 实现语义不变（短路+真实分词） | **保留**，其中 `test_truncate_fits`（T4.2）与零窗（`T4.1`）直接复用 |

---

## 覆盖率验收（给验证子 agent）

- `botflow.common.context` 目标 **100%**。
- 已删除的 `_cjk_ratio` 不得以任何形式复活（不写 `# UNCOVERED:` 掩盖）。
- F4 的两条边界 `上界 == limit`（T4.3）与 `上界 == limit + 1`（T4.4）**必须各有一个用例**
  以证明短路边界正确，而非恒真。
- **T4.7 是集成期修正的回归守卫**：`_token_upper_bound` 必须逐条计入 `_MESSAGE_OVERHEAD`，
  否则空 `role`/空 `content` 的退化消息会被误短路。务必保留。
- F1 的 `encode_ordinary` 反例（T1.5）是关键的「坑」护栏：务必保留，防止有人改回 `encode()`。

**用例总数：22**（正例 12 / 反例 4 / 边界值 6），与 `P9-6_features.md` §5 一致。

> 注：原文档误写为「24（正例 11 / 反例 6 / 边界值 7）」，验证子 agent 已更正为按表逐条点数所得的 22 条。
