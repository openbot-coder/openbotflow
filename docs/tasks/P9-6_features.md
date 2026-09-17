# P9-6 功能点与测试用例：用 tiktoken 替换 `estimate_tokens` 启发式

> 任务类型：性能 + 精度改造（网关侧 CPU 热点消除）
> 范围（用户已拍板）：① `pyproject.toml` 加 `tiktoken` 硬依赖；② `common/context.py`
> 用真正 BPE 分词器替换 `_cjk_ratio` 启发式；③ 删除 `_cjk_ratio` 死代码；④ 用 `lru_cache`
> 惰性单例持有编码器；⑤ 用 `_token_upper_bound` 上界短路替换原 `estimate_tokens` 快路径。
> 关联：`docs/efficiency-audit-2026-09-17.md:57-59`（热点定位）、
> `docs/headroom-evaluation-2026-09-17.md:162`（tiktoken 对照表）、
> `docs/efficiency-improvement-plan-2026-09-17.md:89`（P9-6 工单）。

---

## 1. 背景：实测偏差与 CPU 热点

`common/context.py` 的 `estimate_tokens()` 用启发式（`4 - cjk_ratio*2` 字符/token）估算。
`_cjk_ratio()` 是纯 Python 逐字符 for 循环，是网关侧**最大 CPU 项**（backup 组单请求 21–142ms）。
实测对照（`docs/headroom-evaluation-2026-09-17.md:164-169`，生产真实载荷，Linux 端
`tiktoken o200k_base` 对照）：

| 载荷 | 旧 `estimate_tokens` | tiktoken `o200k_base` | 偏差 | 旧 `_cjk_ratio` 耗时 | tiktoken 耗时 |
|---|---|---|---|---|---|
| 真实 agent 请求（227K 字符） | 57,859 | 70,091 | **−17.5%** | 33.6ms | **22.9ms** |
| 中英混合 908 字符 | 276 | 563 | **−51.0%** | 0.1ms | 0.2ms |
| 合成中文 98K 字符 | 45,736 | 77,000 | **−40.6%** | 13.4ms | 15.4ms |
| 合成英文 202K 字符 | 50,527 | 37,601 | **+34.4%** | 28.7ms | **5.3ms** |

结论：

1. **更快**：tiktoken 是 Rust 扩展，英文大载荷从 28.7ms → 5.3ms；真实 agent 载荷 33.6ms → 22.9ms。
2. **更准**：旧启发式偏差 −51% ~ +34%，tiktoken 代理偏差收敛到 −17.5%（中文最差仍显著优于旧值）。
3. **业务正确性**：旧估算系统性低估中文 → backup 组（3.9% 流量）截断时**少截了**，真实超窗风险被掩盖。

用户已拍板：**改用 tiktoken**。

---

## 2. 硬约束（违反即打回）

- **绝不改变发给上游的字节**。`estimate_tokens` 只喂给截断决策；生产 `fast`/`free`/`smart`/
  `fast-text` 四组 `context_window=0`（占 96% 流量），`truncate_messages` 会早退 → 对这些流量
  **本次改动零行为变化**。
- **不引入可避免的依赖 / 不建未经请求的抽象**（AGENTS.md 规则 1、2；决策阶梯）。
- 保持 `estimate_tokens()` / `truncate_to_context_window()` / `_extract_text()` 的**签名不变**
  （外部调用方：`pipeline/_shared.py:181`、`router.py:18` 的导入、`tests/`）。

---

## 3. 修复方案

### (a) `pyproject.toml` 加 `tiktoken` 硬依赖

```toml
"tiktoken>=0.14.0",
```

**理由（写进本仓库先例）**：P6 把 `langgraph` 从「可选、未安装则静默跳过」改成硬依赖，注释明写
「使 langgraph 策略始终可用，不再『未安装则静默跳过』」。本次同理：若把 tiktoken 当可选依赖、
未安装则静默降级回旧启发式 = **精度无声退化，且无法被监控发现**，不可接受。故走 hard 依赖。

### (b) `src/botflow/common/context.py`

目标形态（与本文档一致，不自行发明结构）：

```python
from functools import lru_cache
import tiktoken

_ENCODING_NAME = "o200k_base"          # DeepSeek 未公开词表的实测最小误差代理
_MESSAGE_OVERHEAD = 1

@lru_cache(maxsize=1)
def _encoding() -> "tiktoken.Encoding":
    """共享 BPE 编码器，词表首次调用时才加载。"""
    return tiktoken.get_encoding(_ENCODING_NAME)

def _token_upper_bound(messages) -> int:
    """`estimate_tokens()` 的严格上界 = Σ(字节数 + 每条固定开销)。"""

def estimate_tokens(messages) -> int:
    """用真正的 BPE 分词器统计 token 数。"""
    enc = _encoding()
    total = 0
    for msg in messages:
        role = msg.get("role", "")
        text = _extract_text(msg.get("content", "") or "")
        total += len(enc.encode_ordinary(role)) + len(enc.encode_ordinary(text)) + _MESSAGE_OVERHEAD
    return total

def truncate_to_context_window(messages, context_window, max_tokens=None):
    if context_window <= 0:
        return messages                      # 4 个 ctx=0 组：零行为变化
    reserve = max_tokens or 1024
    limit = max(context_window - reserve, 1)
    if _token_upper_bound(messages) <= limit:  # 上界短路（正确性等价）
        return messages
    # 下面的二分查找继续用 estimate_tokens（已是真实分词）
    ...
```

要点：

- **删除 `_cjk_ratio()`**（改造后成为死代码，AGENTS.md 规则 4「优先考虑删除」）。全仓 grep
  `_cjk_ratio`：`src/botflow/common/context.py` 源码、`tests/test_context.py`（`_cjk_ratio`
  用例会在 import 阶段失败——**预期**，验证子 agent 会重写测试文件）、`docs/*.md`（历史/审计文档，
  不在本任务改动范围，不动）。`src/botflow/providers/openai_compat.py:40` 实际引用的是
  `estimate_tokens`（非 `_cjk_ratio`），无需清理。
- 用 `encode_ordinary()` 而非 `encode()`：后者遇到 `<|endoftext|>` 这类特殊 token 字面量会抛
  `ValueError`（**坑**，见 F1/T1.5）。`encode_ordinary` 把它们当普通文本，正文安全。
- 用 `functools.lru_cache`（标准库，决策阶梯第 3 级）做惰性单例，不手写全局变量。
- **上界短路的正确性等价**：`token 数 ≤ UTF-8 字节数`（每个 token 至少覆盖 1 字节），再加每条
  固定开销，即 `estimate_tokens(m) ≤ _token_upper_bound(m)`。故 `上界 ≤ limit ⇒ 不可能超限 ⇒
  直接原样返回`。边界：`上界 == limit` 走短路，`上界 == limit + 1` 才进编码路径（见 F4/T4.3–T4.4）。
- **上界必须含 `_MESSAGE_OVERHEAD`**（主 agent 集成期修正）：只累加字节数时，若 `role` 与
  `content` 同时为空（客户端漏发 `role`），字节数为 0 而 `estimate_tokens` 仍为 1/条，上界不成立
  → `_token_upper_bound` 逐条加 `_MESSAGE_OVERHEAD`，使上界严格成立。函数名据此改为语义化命名
  （原名 `_byte_size` 会误导为"仅字节数"）。
- **不加内容 hash 缓存**（v1 先不加，measure first；跳过理由在回报里向用户说明）。

---

## 4. 功能点清单

### F1 用 tiktoken 替换 `estimate_tokens` 启发式

- 文件：`src/botflow/common/context.py` 的 `estimate_tokens()`。
- 语义：对每条消息的 `role` 与正文分别用 `o200k_base` 编码并累加；每条加固定开销
  `_MESSAGE_OVERHEAD = 1`（沿用旧启发式「每消息 +1」的约定，保证截断决策量级不变）。
- 必须用 `encode_ordinary`（F1 的硬约束），禁止 `encode`（`ValueError` 风险）。
- 签名不变：`estimate_tokens(messages: list[dict]) -> int`。

### F2 删除 `_cjk_ratio` 死代码

- 文件：`src/botflow/common/context.py`（删除 `def _cjk_ratio` 整个函数）。
- 验收点：`context` 模块**不再导出** `_cjk_ratio`（`hasattr(context, "_cjk_ratio")` 为 `False`）。
  旧 `tests/test_context.py` 中 3 个 `_cjk_ratio` 用例 import 失败 = 本功能点的预期副作用
  （验证子 agent 会重写测试文件，本任务不碰 `tests/`）。
- 同步清理：全仓 grep 确认 `src/` 下无 `_cjk_ratio` 残留引用。

### F3 编码器惰性单例（`lru_cache`）

- 文件：`src/botflow/common/context.py` 的 `_encoding()`。
- 用 `@lru_cache(maxsize=1)` 包裹 `tiktoken.get_encoding(_ENCODING_NAME)`；词表在首次调用时才加载
  （首次会联网下载并缓存于 `TIKTOKEN_CACHE_DIR`）。
- 多次调用返回**同一对象**（`is` 判定）；不手写模块级全局变量。

### F4 上界短路替换原 `estimate_tokens` 快路径

- 文件：`src/botflow/common/context.py` 的 `truncate_to_context_window()`。
- `context_window <= 0` 早退分支**完全不变**（4 个 ctx=0 组零行为变化）。
- 用 `_token_upper_bound(messages) <= limit` 短路替换原 `estimate_tokens(messages) <= limit`
  快路径；只有上界超了才做真正的 BPE 编码。
- `_token_upper_bound()` = `Σ(role 字节 + 正文字节 + _MESSAGE_OVERHEAD)`，是 `estimate_tokens()`
  的**严格上界**（含每条固定开销，故对空 `role`/空 `content` 的退化消息同样成立）。
- 正确性等价边界：`上界 == limit` 走短路；`上界 == limit + 1` 才进编码/二分路径。
- 二分查找继续用 `estimate_tokens`（已是真实分词），system 消息保留逻辑不变。

### F5 `tiktoken` 硬依赖声明

- 文件：`pyproject.toml` 的 `[project].dependencies`。
- 加入 `"tiktoken>=0.14.0"`，附 P6 `langgraph` 同类先例注释（禁止静默降级）。
- 安装态验收：`import tiktoken` 在 `.venv` 中可用（F5 的运行时侧面）。

---

## 5. 测试用例清单

> 说明：本任务为**编码子 agent**，不写 `tests/` 下的测试；下表为验收清单，由验证子 agent 落进
> `tests/test_context.py`。「类型」三档：正例 / 反例 / 边界值（AGENTS.md 测试验收标准）。

### F1 `estimate_tokens` 用 tiktoken

| 编号 | 类型 | 用例名 | 输入 | 预期 |
|---|---|---|---|---|
| T1.1 | 正例 | `test_estimate_tokens_chinese` | `[{"role":"user","content":"你好世界"}]` | 返回 `> 0`；等于 `len(enc.encode_ordinary("user")) + len(enc.encode_ordinary("你好世界")) + 1`（与 `_encoding()` 同一实例核对） |
| T1.2 | 正例 | `test_estimate_tokens_english` | `[{"role":"user","content":"hello world this is a test"}]` | 返回 `> 0` 且与 o200k_base 编码计数一致 |
| T1.3 | 正例 | `test_estimate_tokens_empty_list` | `[]` | 返回 `0` |
| T1.4 | 正例 | `test_estimate_tokens_list_content` | `[{"role":"user","content":[{"type":"text","text":"hi there"}]}]` | `_extract_text` 正确提取后计数 `> 0` |
| T1.5 | **反例（关键坑）** | `test_estimate_tokens_special_token_literal` | 正文含 `"<|endoftext|>"` 字面量（如 `"hello <\|endoftext\|> world"`） | **不抛 `ValueError`**（证明用了 `encode_ordinary` 而非 `encode`）；计数正常 |
| T1.6 | 边界 | `test_estimate_tokens_long_special_literal` | 长文本内嵌多个特殊 token 字面量 | 仍正常返回、不抛异常 |
| T1.7 | 边界 | `test_estimate_tokens_single_char` | `[{"role":"u","content":"a"}]` | 返回 `>= _MESSAGE_OVERHEAD`（空正文也有每条开销） |

### F2 删除 `_cjk_ratio`

| 编号 | 类型 | 用例名 | 输入 | 预期 |
|---|---|---|---|---|
| T2.1 | **反例（关键）** | `test_cjk_ratio_removed_from_module` | `import botflow.common.context as c` | `not hasattr(c, "_cjk_ratio")` 为 `True`（死代码已删；旧测试 import 失败即此点的验收信号） |
| T2.2 | 正例 | `test_estimate_tokens_no_heuristic_path` | 中英混合消息 | 结果等于 o200k_base 真实计数（间接证明不再走 `_cjk_ratio` 的 `4 - cjk_ratio*2` 公式——旧公式对中文会显著低估） |
| T2.3 | 边界 | `test_no_cjk_ratio_refs_in_src` | `grep` `src/botflow` 不含 `_cjk_ratio` | 除 `docs/` 与旧测试外无残留引用 |

### F3 编码器惰性单例

| 编号 | 类型 | 用例名 | 输入 | 预期 |
|---|---|---|---|---|
| T3.1 | 正例 | `test_encoding_singleton_returns_same_object` | 连续两次 `_encoding()` | 两次结果 `is` 同一对象（单例生效） |
| T3.2 | 正例 | `test_encoding_loads_on_first_call` | 首次调用 `_encoding()` | 返回可用 `tiktoken.Encoding`（能 `encode_ordinary("x")`） |
| T3.3 | 边界 | `test_encoding_lru_cache_maxsize_one` | 多次调用 | 始终返回同一实例（不重复加载词表） |

### F4 字节上界短路

| 编号 | 类型 | 用例名 | 输入 | 预期 |
|---|---|---|---|---|
| T4.1 | 正例 | `test_truncate_zero_window_returns_as_is` | 任意消息 + `context_window=0` / `-5` | 原样返回（4 个 ctx=0 组零行为变化） |
| T4.2 | 正例 | `test_truncate_upper_bound_fits` | 短内容 + 大 `context_window` | `_token_upper_bound ≤ limit` → 原样返回 |
| T4.3 | **边界（关键）** | `test_truncate_upper_bound_equal` | 构造消息使 `_token_upper_bound == limit` | 走短路、原样返回（不进编码） |
| T4.4 | **边界（关键）** | `test_truncate_upper_bound_plus_one` | 构造消息使 `_token_upper_bound == limit + 1` | **不**短路、进编码路径（触发二分/截断） |
| T4.5 | 反例 | `test_truncate_oversize_gets_truncated` | 超大内容 + 小 `context_window` | 返回子集（长度 < 原文） |
| T4.6 | 正例 | `test_truncate_keeps_system` | system + 多条超大 user | 输出首条为 system，且长度缩短 |
| T4.7 | **反例（集成期新增）** | `test_upper_bound_covers_empty_messages` | N 条 `{"role": "", "content": ""}`，`limit` 介于 `N*_MESSAGE_OVERHEAD` 与字节上界之间（如 N=1000、`context_window` 取小于 1000 的值） | `estimate_tokens == N * _MESSAGE_OVERHEAD` 且 `_token_upper_bound >= estimate_tokens`；**不得**因字节数为 0 而短路放行（`_MESSAGE_OVERHEAD` 计入上界的回归守卫） |

### F5 硬依赖声明

| 编号 | 类型 | 用例名 | 输入 | 预期 |
|---|---|---|---|---|
| T5.1 | 正例 | `test_tiktoken_importable_in_venv` | `import tiktoken` | 导入成功（依赖已装；环境侧验收） |
| T5.2 | 正例 | `test_pyproject_declares_tiktoken` | 解析 `pyproject.toml` | `dependencies` 含 `tiktoken>=0.14.0`（静态声明验收） |

**用例总数**：**22**（正例 12 / 反例 4 / 边界值 6）。其中 T4.7 为主 agent 集成期修正后新增的回归守卫。

> 注：原文档误写为「24（正例 11 / 反例 6 / 边界值 7）」，验证子 agent 逐条点表核为 22 条，已更正。

---

## 6. 明确不做（不改什么）

| # | 不做的事 | 理由 |
|---|---|---|
| 1 | **不改 4 个 `context_window=0` 分组的行为** | 它们走 `context_window <= 0` 早退，本次仅替换其内部实现，对 96% 流量零行为变化 |
| 2 | 不改 `context_window` 的语义（早退 / reserve / limit 公式不变） | 仅替换「是否超限」的判定手段（字节上界 + 真实分词），决策边界一致 |
| 3 | 不改 `estimate_tokens` / `truncate_to_context_window` / `_extract_text` 的**签名** | 外部调用方 `pipeline/_shared.py:181`、`router.py:18` 导入不变 |
| 4 | **不加内容 hash 缓存** | v1 先 measure first；跳过理由在回报中向用户说明（待实测后再议，不在本任务范围） |
| 5 | 不引入除 `tiktoken` 外的任何新依赖 / 抽象 | 决策阶梯第 1/2/3 级；单例用标准库 `lru_cache` |
| 6 | 不把 `tiktoken` 当可选依赖 / 不静默降级 | F5 已硬依赖；降级 = 精度无声退化，不可接受 |
| 7 | 不动 `docs/efficiency-improvement-plan-2026-09-17.md` | 主 agent 负责；本任务只改 `pyproject.toml` 与 `context.py` |

---

## 7. 风险与前置条件

1. **o200k_base 只是 DeepSeek 的代理词表**：可宣称「更准」不可宣称「精确」。实测真实 agent 载荷
   −17.5%，优于旧启发式的 −51% ~ +34%，但仍非 DeepSeek 真值。属已知近似。
2. **首次使用需联网下载词表**（约几 MB），缓存于 `TIKTOKEN_CACHE_DIR`（默认用户缓存目录）；
   **离线/生产环境需预置**该缓存，否则首次请求会阻塞下载或失败。
3. **tiktoken 是编译扩展（Rust wheel）**：需对应平台 wheel；当前目标 `Python313 / win_amd64`
   有预建 wheel，但跨平台部署（Linux 生产、ARM 等）需确认 wheel 可用性。
4. **backup 组（3.9% 流量）的截断结果会因估算变准而变化**：旧启发式系统性低估中文 → 现在会
   **多截**（更可能触发截断/保留更少消息）。这是**预期收益**（消除真实超窗风险），但属**行为变化**，
   需要回归验证——尤其关注 backup 组上游是否对截断后消息敏感。
5. ~~字节上界短路的退化场景~~ **已在主 agent 集成期修正**：初版 `_byte_size` 只累加字节数，
   漏了每条的 `_MESSAGE_OVERHEAD`，使「上界」在 `role` 与 `content` 同时为空的退化消息上不成立
   （字节数 0 → 误短路放行，而 `estimate_tokens` 实为 N）。修正为 `_token_upper_bound`（逐条计入
   固定开销）后上界严格成立，并由 T4.7 设回归守卫。**这是本次双 Agent 流程抓到的一处真实缺陷。**
6. **不改变发给上游的字节**：`estimate_tokens` 输出仅用于截断决策；截断后写入上游的仍是原消息对象
   的子集（content 字节未改动），本次无序列化/编码变化。
