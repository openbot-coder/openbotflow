# P9-6 功能点/测试用例文档 —— 验证子 agent 审核报告

> 审核对象：`docs/tasks/P9-6_features.md`、`docs/tasks/P9-6_tests.md`、`src/botflow/common/context.py`（当前实现，以此为准）
> 审核人：验证子 agent（**未修改 `src/` 任何代码**，仅清理了 src 下过期的 `.cover` 注释产物）
> 环境：Windows / PowerShell / `.venv/Scripts/python.exe` / `PYTHONPATH=src`；**只跑 `tests/test_context.py`**（async 测试在本机挂死，按约束不跑 `tests/` 其余文件）
> 实测：`33 passed`；`botflow.common.context` **100%**（`59 stmts / 0 miss`）

---

## 1. 审核结论：**通过（实现正确，零阻塞）** —— 建议 2 条，已就地处理 1 条

设计主体与实现经独立实证**全部成立、零行为变化、上界不变量恒成立**。无必须改代码的阻塞项。

| 级别 | 条数 | 内容 |
|---|---|---|
| 阻塞（必改代码） | 0 | 无 |
| 建议（可改可不改） | 2 | ① 两文档「用例总数 24」计数错误（已就地更正为 22）；② src 下 22 个过期 `.cover` 注释产物含旧 `_cjk_ratio`（已删除，未入库、被 `.gitignore:59 *.cover` 忽略） |
| 通过 | 7 | 见 §2 逐项表 |

---

## 2. 逐项审核表（对应主 agent 的 7 个审核重点）

| # | 审核点 | 判定 | 依据 / 实测 |
|---|---|---|---|
| 1 | 上界不变量 `estimate_tokens(m) ≤ _token_upper_bound(m)` | **通过** | 12 个定向构造 + 200 次随机 fuzz 全部成立。关键退化用例「1000 条 `{"role":"","content":""}`」：estimate=1000 == upper=1000。证明主 agent 集成期修正的「逐条计 `_MESSAGE_OVERHEAD`」**堵死了初版缺口**——若漏加开销，该用例上界会误算为 0 而短路放行 |
| 2 | 短路边界正确性 | **通过** | 构造 `ub=105`：① `上界==limit`（`context_window=ub+1024, max_tokens=1024`）→ `estimate_tokens` 调用 **0** 次（走短路）；② `上界==limit+1`（`context_window=ub-1+1024`）→ 调用 **2** 次（进编码/二分）。用 `monkeypatch` 计数证明「没进编码路径」，非恒真断言 |
| 3 | `_cjk_ratio` 真死透 | **通过** | `hasattr(context, "_cjk_ratio")` 为 `False`；`src/` 真实 `.py` 下 grep 零引用（见 §4 清理项） |
| 4 | `encode_ordinary` 必要性 | **通过** | 实测 `enc.encode("<|endoftext|>")` 抛 `ValueError: Encountered text corresponding to disallowed special token`；`enc.encode_ordinary("<|endoftext|> world")` 正常返回 8 token。T1.5 反例护栏有效，防止有人改回 `encode()` |
| 5 | ctx=0 零行为变化 | **通过（明确结论见 §3）** | 见 §3 实证：4 组 ctx=0 时 `truncate_to_context_window` 调用次数 = **0**，因为早退在调用**之前** |
| 6 | 文档与实际实现一致 | **通过** | 函数名 `_token_upper_bound`/`estimate_tokens`/`truncate_to_context_window`/`_extract_text`/`_encoding`、常量 `_ENCODING_NAME`/`_MESSAGE_OVERHEAD`、边界描述、用例编号全部一致 |
| 7 | 有无该删没删 / 多余抽象 | **通过** | `_extract_text` 被 `estimate_tokens` 与 `_token_upper_bound` **共用**（无重复实现）；依赖仅 `tiktoken`（`pyproject.toml:28` 已声明）；单例用标准库 `lru_cache`，无手写全局、无多余 hash 缓存（v1 明确不做） |

---

## 3. 「ctx=0 零行为变化」明确结论 + 证据

**结论：成立，且是「双重保险」式的零影响。**

代码链：`pipeline/_shared.py:168` `truncate_messages` → `:174-178` 仅收集 `context_window > 0` 的 endpoint 组成 `context_windows` → `:179 if not context_windows: return messages` → `:181` 才调用 `truncate_to_context_window`。

- 对 `fast`/`free`/`smart`/`fast-text` 四组（全部 `context_window=0`），`context_windows` 必为空列表，`truncate_messages` 在 **`_shared.py:180`** 直接返回，**根本不会执行到 `_shared.py:181` 的 `truncate_to_context_window` 调用**。
- 因此本次改动（`context.py` 内的 `context_window <= 0` 早退分支 `:100-101` 与新增 `_token_upper_bound` 短路 `:108`）**对这 4 组既未被触发，也未被触及**——哪怕把 `context.py` 内改动全部还原，4 组行为也完全一致。
- 实证：mock `truncate_to_context_window`，喂入 2 个 `context_window=0` 的 endpoint → 该函数调用次数 = **0**。

---

## 4. 已实测确认 / 已处理事项

| 项 | 结果 |
|---|---|
| 上界不变量反例证伪尝试 | **未能证伪**——所有构造（空 role / 空 content / 超长 CJK / list content / 特殊 token 字面量 / 空列表 / 混合角色 / 1000 条空消息 / 随机 fuzz）均满足 `estimate ≤ upper` |
| 短路边界（T4.3 / T4.4） | 各自独立构造并 `monkeypatch` 计数，证明短路边界正确、非恒真 |
| `src/` 下 `_cjk_ratio` 残留 | 真实 `.py` 零引用；但发现 **22 个过期的 `.cover` 注释产物**（`coverage annotate` 生成）仍含旧 `def _cjk_ratio` 与 `cjk_ratio = _cjk_ratio(text)`。已用 `[System.IO.File]::Delete` 删除（被 `.gitignore:59 *.cover` 忽略，未入库，删除安全，非源码改动） |
| 文档计数错误 | `P9-6_features.md:217` 与 `P9-6_tests.md:95` 误写「24（正例 11 / 反例 6 / 边界值 7）」；逐条点表实为 **22（正例 12 / 反例 4 / 边界值 6）**，已就地更正 |

---

## 5. 测试落地说明（tests/test_context.py）

- 仅改本文件，无新建测试模块（AGENTS.md 规则 6）。
- 删除旧 3 个 `_cjk_ratio` 用例 + import 中的 `_cjk_ratio`。
- 旧 `test_estimate_tokens_english` / `test_estimate_tokens_list_content` 被 P9-6 的 T1.2 / T1.4 同名更强用例取代（避免同名函数遮蔽），故移除；其余旧用例（`_extract_text` 三例、空列表、`cjk`、`truncate_*` 六例）**保留**——它们覆盖了 `_extract_text` 的 list/None/int 分支与 `best==0` 的两条返回路径，是 100% 覆盖率的必要部分。
- 新增 22 条 P9-6 用例（T1.1–T5.2），其中 T4.3/T4.4/T4.7 用 `monkeypatch` 计数 `estimate_tokens` 调用次数证明短路边界与 `_MESSAGE_OVERHEAD` 回归守卫。

---

## 6. 额外发现（文档/仓库卫生，非代码缺陷）

1. **`.cover` 文件污染 src 树**：共 22 个（`context.cover`/`router.cover`/`db.cover` 等），是 `coverage annotate` 的过期注释输出，内容对应 tiktoken 改动前的旧代码。建议仓库纪律层面避免把 `coverage annotate` 产物留在 `src/` 下（本次已清理）。
2. **文档用例总数数字失真**：不影响实现，但验收清单数字误导，已更正。

---

## 7. 不确定项 / 需主 agent 裁决

- **生产词表预置**：`o200k_base` 首次调用需联网下载并缓存于 `TIKTOKEN_CACHE_DIR`。P9-6_features §7 风险 2 已记录，但生产环境（Linux/离线）是否已在镜像中预置该缓存，超出本验证范围，请主 agent 在集成测试中确认。
- **全项目覆盖率**：受本机 async 测试挂死限制，我仅以 `tests/test_context.py` + `--cov=botflow.common.context` 给出 100% 证据；`botflow.common.context` 之外的回归由主 agent 在 CI/集成阶段认定。

---

**审核签署**：验证子 agent ｜ 结论 = **通过（零阻塞，2 条建议已就地处理）**
