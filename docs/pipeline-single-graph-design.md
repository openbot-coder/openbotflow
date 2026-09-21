# 调用骨架设计：驱动四步 + 策略执行图

> 版本：2.0 | 日期：2026-09-21 | 状态：**待评审**（评审通过后再动代码）
> 基线：`3f82234`（main）。前置：`docs/coverage-2026-09-18.md` 已把覆盖率补到 100%（1024 passed / 4004 stmts / 0 miss）。
> 关联：`docs/design.md §3.5 §4.2`、`docs/pipeline_router_design.md`、`src/botflow/pipeline/langgraph_engine.py`、`src/botflow/pipeline/base.py`
> **v2 变更**：边界按评审决议调整 —— 驱动层（图外）承担四步骨架，`StateGraph` 收敛为**单次「策略执行」**（v1 曾把重试/降级放进图内，已按评审改为图外统一）。

---

## 1. 目标：两层、零重复

| 层 | 位置 | 职责 |
|---|---|---|
| **驱动层** | 图外（`core.py`） | 四步骨架：① 分组 → ② 取策略模板 + 分组参数 → ③ 生成策略 → ④ 循环「策略执行 / 失败降级到 backup 组」 |
| **执行层** | 图内（`StateGraph(RouteState)`） | **单次「策略执行」**：选端点 → 逐端点调用（含端点级重试/退避/冷却） |

一句话概括职责切分：**驱动管「跳哪组、试几次」；图管「这一组怎么把请求打出去」。**

现状的病根是**这套骨架被写了两遍** —— 非流式全在图里（`langgraph_engine.py`），流式全在 `core._stream_common` 里另写了一遍（~180 行）。本设计把骨架收敛到驱动一处，图只保留「一次策略执行」，并顺带修掉三个既有缺陷。

**验收硬指标**：非流式行为逐字段不变；单元覆盖率保持 100%（**不允许用新的 `# UNCOVERED` 标记掩盖缺口**）；4 种协议全部走 mq3 真实回归。

---

## 2. 现状：边界逐行核对

### 2.1 现状图内（`pipeline/langgraph_engine.py`）

| 节点 / 边 | 位置 | 职责 | v2 归属 |
|---|---|---|---|
| `_resolve_group` | `:135-182` | 首轮透传 / fallback 轮从 DB 加载；环检测、深度 > 3、fallback 缺失三类守卫（写入 `fatal_error`） | **移出图** → 驱动步骤 ①④ |
| `_load_and_select` | `:185-244` | `STRATEGY_REGISTRY[group.type]` → `select_endpoints()`；失败时把**异常对象**存入 `state["error"]` | 拆：取模板/建策略 → 驱动 ②③；选端点 → 图内 |
| `_try_call` | `:247-291` | 跨端点顺序尝试，每端点经 `_shared.call_llm` 做重试 + 退避 + 冷却 + 信号量 | 图内保留 |
| `_finalize_error` | `:294-317` | 把 `fatal_error` / `error` 压成 `{"error": {...}}` | 图内保留（供驱动读） |
| `_route_after_load` | `:324-328` | `stream=True` → `END` ← **流式绕开图的唯一开关** | 删除（图不再分流） |
| `_route_after_call` | `:331-349` | `fatal_error` → error；有 result → success；否则 → fallback | **降级边移出图** → 驱动 |
| `_raise_routing_error` | `:111-132` | 出口处按原始异常类型重抛 | 保留（驱动侧复用） |

### 2.2 现状图外（`core.py`）

| 环节 | 位置 | 非流式 | 流式 |
|---|---|---|---|
| 模型名 → group_id | `_get_group_id` `:625-658` | 图外 | 图外 |
| group 载入 | `_get_extra_route_params` `:940-956` | 图外 | 图外 |
| 端点选择 | — | 图内 | 图内 |
| 上游调用 + 每端点重试 | `_handle_chat_non_stream` `:973-981` / `_stream_common` `:1104-1144` | 图内 | **图外（重复实现）** |
| 冷却记录 | `_stream_common` `:1152` / `:1197-1202` | 图内 | **图外（重复实现）** |
| 组级 fallback | `_stream_common` `:1204-1219`（`fallback_attempted`，**只 1 次**） | 图内（最多 3 跳 + 环检测） | **图外（只 1 跳）** |
| 落库 / 计时 / 错误响应 | `:990-1021` / `:1181-1240` | 图外 | 图外 |
| SSE 序列化 / 断开检测 | `_stream_common` `:1154-1181` | — | 图外 |

### 2.3 三个既有缺陷

1. **流式缺组级 fallback**：`_route_after_load` 在 `stream=True` 时无条件 `END`。路由阶段就无可用模型（主组全冷却）时 → 流式 **502**，而非流式会降级到 backup 组并成功。违反 `pipeline_router_design.md:15`（「所有策略共享 cooldown/retry/fallback」）。
2. **流式降级只 1 跳**：`fallback_attempted` 是布尔量，降级一次即放弃；非流式支持最多 3 跳 + 环检测。两者语义不一致。
3. **两遍实现**：流式的重试/冷却/降级是与 `_shared.call_llm` + 图条件边同源的第二份代码 —— 任何一处改了另一处不会跟着改。

> 缺陷 1、2 是「骨架写了两遍」的直接后果；v2 把骨架提到驱动层后，两者**同时消失**（驱动只有一份循环，流式与非流式共用）。
> v1 曾列出的「`state["error"]` 残留」缺陷随边界调整自然消失：图不再跨组多轮执行，每次「策略执行」都是一次全新 `ainvoke`，不存在跨轮 state 合并问题。

---

## 3. 目标架构

### 3.1 驱动层四步（图外，`core.py`）

```python
async def _drive(internal: dict, mode: Literal["call", "stream"]):
    # ① 分组：model 名 → ModelGroup（含未命中兜底首个启用组）
    group = await _resolve_model(internal)          # 步骤一

    visited, depth = {group.id}, 0
    while True:
        # ② 取策略模板 + 分组参数    ③ 生成策略
        strategy = _build_strategy(group)           # STRATEGY_REGISTRY[group.type](group.params or {})

        try:
            # ④ 策略执行 —— 这一层是 StateGraph（见 §3.2）
            return await _engine.run(strategy, group, mode, internal)
        except RecoverableRouteError as exc:
            nxt = group.fallback_group_id
            if not nxt or nxt in visited or depth >= MAX_DEPTH:   # 无 backup / 环 / 超深
                raise
            group = await _engine.load_group(nxt)   # 60s 缓存
            visited.add(nxt)
            depth += 1
```

**「重试次数」的两层含义（必须区分，避免双重重试）**：

| 层级 | 含义 | 上限 | 位置 |
|---|---|---|---|
| 端点级重试 | 同一端点遇 429/5xx/timeout 的重试 + 指数退避 | `ep.max_retries`（DB 列，默认 3） | **图内** `call_llm`（`_shared.py`） |
| 组级降级 | 切到 `fallback_group_id` 重建策略后重试 | 最多 3 跳 + 环检测 | **驱动** while 循环 |

即用户描述里的 `for _ in range(重试次数)` 落在**驱动层**，对应的是「组级降级」这一层；端点级重试仍在图内（`call_llm`），不要在驱动里再套一层。

### 3.2 执行层：`StateGraph` = 单次「策略执行」

```
START
  │
  ▼
select_endpoints       调 strategy.select_endpoints()（缓存 → 冷却过滤 → 排序 → 截断）
  │
  ├─ 无可用端点 / 策略异常 ──► finalize_error ──► END   （写 error，recoverable=True）
  │
  ▼
try_call  |  try_stream     逐端点：call_llm / chat_stream（端点级重试 + 退避 + 冷却）
  │
  ├─ 成功 ──► END（result 非空）
  └─ 全失败 ──► finalize_error ──► END（写 error，recoverable=True）
```

**图里不再有 `resolve_group`、不再有 fallback 边。** 图的出口只有两种：`result` 有值（成功）或 `error` 有值（失败）。驱动读出口状态决定「返回」还是「切 backup 组重来」。

`recoverable` 标志告诉驱动「这次失败能不能换组再试」。逐条判定见 **§3.5**。原则一句话：

> **图内（选端点 / 逐端点调用 / 首 chunk 之前）的任何失败 → 可降级；驱动层的配置错误、以及已推出 chunk 之后的失败 → 不可降级。**

这条原则的设计目标是**逐字保持非流式现状**：现在图里 `select_endpoints` / `try_call` 的任何失败都会走 fallback 边，故它们必须标 `recoverable=True`；而驱动步骤 ②③ 抛的 `ConfigurationError` 现在是**未捕获的节点异常 → 502**，故必须标 `False`。

### 3.3 传输层分离：图不 `yield`

HTTP 的 SSE 生成器无法从图节点内部 `yield`。用 LangGraph 原生的**自定义流**解决（已实测可用，见 §7）：

```python
# 图内：try_stream 节点 —— 只推「provider 原生 chunk」，不碰 SSE
from langgraph.config import get_stream_writer

writer = get_stream_writer()      # 仅在 stream 上下文可用，需 try/except 兜底
...
writer({"chunk": chunk})          # 逐块推出
```

```python
# core 侧：astream(stream_mode=["custom", "values"]) —— 只做协议与落库
async for mode, payload in engine.stream_events(...):
    if mode == "custom":
        chunk = payload["chunk"]
        chunk["model"] = model_name            # 原有行为保持不变
        lines, usage = serialize(chunk)        # serialize 由调用方提供（4 种协议各一套）
        if usage:
            usage_final = usage
        for line in lines:
            yield line
    else:
        final_state = payload                  # 收尾：取 used_model_id / provider_id / error
yield done_signal
```

**职责切分**：

| 层 | 负责 |
|---|---|
| 驱动（`core.py`） | 分组、建策略、组级降级循环、4 种协议入参归一与 SSE 序列化、`HTTPException(404/502)` 映射、`_log_call` 落库与计时 |
| 图（`langgraph_engine.py`） | 选端点、跨端点尝试、端点级重试/退避/冷却、断开中止、错误定型 |

### 3.4 `RouteState` 增补

```python
class RouteState(TypedDict, total=False):
    ...
    mode: Literal["call", "stream"]      # 取代含糊的 stream: bool（保留兼容别名）
    stream_started: bool                 # 首个 chunk 已推出 → 之后失败不可再降级
    stream_stats: dict                   # {"chunks": int, "usage": dict | None}
    recoverable: bool                    # 失败是否允许驱动降级
    # 传输依赖不进 state，走 GraphContext（与 db / cooldown 同路）
```

`GraphContext` 增加一个字段：

```python
@dataclass
class GraphContext:
    db: Database
    cooldown: CooldownManager
    request: Any | None = None           # 仅用于 stream 模式下的 is_disconnected()
```

### 3.5 失败分类与 `recoverable` 判定表（逐条）

**判定规则：白名单制**（2026-09-21 评审决议 —— 由「未预期异常也降级」收紧为**显式白名单**）

只有落在白名单里的失败才 `recoverable=True`；**其余一律 `False`（上抛 + 留痕）**，避免把代码 bug 伪装成上游故障。

**降级白名单（`recoverable=True`）** —— 只有「换一个组可能成功」的失败：

| 白名单项 | 判定依据 |
|---|---|
| `AllModelsCooldownError` | 异常类型 |
| `NoAvailableModelError`（含「no enabled models」/「total weight ≤ 0」） | 异常类型 |
| `ProviderError` 且 `is_retryable_error(e) is True`（429/500/502/503/504/timeout） | 类型 + `router.is_retryable_error` |
| 端点全失败（图内已耗尽候选端点及其 `ep.max_retries`） | **状态**，不是异常类型 |

**黑名单（`recoverable=False`）**：`ConfigurationError`、`StrategyError`、`HTTPException`、`asyncio.CancelledError`，以及**任何未列入白名单的 `Exception`**（DB 故障、类型错误…）。

> ⚠️ 这是**新增的行为变更**（见 §9 R9）：现状是「`select_endpoints` / `try_call` 抛任何异常都降级」，收紧后非流式遇到**未预期异常**将**直接 502 而非降级**。换来的是失败原因不被降级链路掩盖 —— 这也是黑名单失败**必须留痕**（§3.6）的原因。

**三点边界**：

1. 驱动步骤 ②③（取模板 / 建策略）→ 天然黑名单（`ConfigurationError`）。
2. 图内（`select_endpoints` / `try_call` / `try_stream` 且尚未推出任何 chunk）→ 按白名单判。
3. 已推出 chunk 之后（`stream_started=True`），或降级守卫（环 / 深度 / 无 backup）→ 一律 `False`。

**逐条表**

| # | 阶段 | 失败点（代码位置） | 异常 / 状态 | `recoverable` | 现状（非流式） | 理由 |
|---|---|---|---|---|---|---|
| 1 | 驱动 ① | `_get_group_id` 无启用分组 | `HTTPException(404)` | —（循环外） | 404 | 在降级循环之外，直接返回 404 |
| 2 | 驱动 ① | 加载 backup 组（`engine._load_group`） | `ConfigurationError` 组不存在 | ❌ | 图内 `fatal_error`「Fallback group N not found」→ 终止 | 降级目标自身不存在 |
| 3 | 驱动 ② | 取模板 `STRATEGY_REGISTRY.get()` | `ConfigurationError` 未知策略名（`langgraph_engine.py:214`） | ❌ | 节点异常 → 502 | 换组读的是同一份注册表 |
| 4 | 驱动 ② | langgraph 策略硬拒绝 | `ConfigurationError`（`langgraph_engine.py:206`） | ❌（阶段三落地后取消该拒绝） | 节点异常 → 502 | 同上 |
| 5 | 驱动 ③ | 建策略 `strategy_cls(params)` | 参数非法 | ❌ | 不抛（`params` 为自由 dict） | 当前无触发路径 |
| 6 | 图 · select | `strategies.py:34/80/128` | `AllModelsCooldownError` | ✅ | 捕获 → fallback | **缺陷 1 核心场景**：backup 组可能不在冷却 |
| 7 | 图 · select | `strategies.py:30/76/124` | `NoAvailableModelError`「no enabled models」 | ✅ | 捕获 → fallback | backup 组可能仍有启用模型 |
| 8 | 图 · select | `router.py:311/334` | `NoAvailableModelError`「total weight ≤ 0」 | ✅ | 捕获 → fallback | 同上 |
| 9 | 图 · select | 未预期异常（DB 故障、类型错误…） | `Exception` | ❌ | 捕获 → fallback | **收紧为黑名单（R9 行为变更）**：不属「换组可能成功」，上抛 502 并**留痕** |
| 10 | 图 · call | 端点全失败（`call_llm` 返回 `None`） | 无异常，`error="All endpoints in group failed"` | ✅ | → fallback | 上游瞬时故障 |
| 11 | 图 · call | 不可重试错误（4xx 非 429） | 图内 `break` 后归入 #10 | ✅ | → fallback | 端点是「这一组打不出去」而非代码 bug；按**状态**判可降级 |
| 12 | 图 · stream | 首 chunk 之前失败 | 超时 / 空流 / 可重试异常 | ✅ | 流式现状：**不降级** → **改为降级** | 缺陷 1/2 的修复面（R4 行为变更） |
| 13 | 图 · stream | 已推出 chunk 后失败（`stream_started=True`） | 任意 | ❌ | 已推内容无法收回 | R5：只报错不降级 |
| 14 | 驱动 ④ | 降级守卫：环 / 深度 > 3 / 无 backup | — | ❌ | 图内 `fatal_error` → 终止 | 防环与预算控制 |

**实现要点（保 `call_logs.error_type` 不失真）**：驱动在「判定不降级」或「降级预算耗尽」时，必须 `raise` **原始类型化异常**（复用 `langgraph_engine._raise_routing_error`），而不是笼统的 `ProviderError` —— `core._log_call` 记录 `type(e).__name__`，`AllModelsCooldownError`（全冷却，稍后重试即可）与 `NoAvailableModelError`（根本没有模型可路由）在运维上是两回事。

### 3.6 失败留痕（可观测性）

**问题：现状有 4 处失败不留库，「重试后成功」的失败尝试完全查不到。**

| # | 缺口 | 位置 | 后果 |
|---|---|---|---|
| G1 | 逐次尝试失败只 `log.warning`，不落库 | `_shared.py:134-146`（`call_llm`）、`core.py:1130-1144`（流式重试环） | **重试成功时 `call_logs` 只有一条 success**，之前的失败无迹可查 |
| G2 | 最终错误行丢失归属 | `core.py:1009-1010` 写死 `model_id=None, provider_id=None`；流式 `:1227-1228` 在 `used_ep` 未设置时同样为 `None` | 失败行无法定位到具体模型 / 供应商 |
| G3 | 降级前的组失败无痕 | `core.py:1204-1219` 切 backup 组 | 主组失败只在应用日志里，DB 无行 |
| G4 | 空流 / 首 chunk 超时**不留痕**；且有一段不可达死代码 | `core.py:1125-1129`（⚠️ **冷却是记了的** —— `break` 退出的是内层 `for attempt` 循环，控制流随即落到 `:1197` 的 `record_failure`，见 `SG-0_features.md §1.1`）；真正的问题在 `:1146-1147` 的 `if gen is None: break` **不可达** | 失败尝试无迹可查（同 G1 根因）；死代码挂着 `# UNCOVERED` 需删除 |

**目标**：**每一次失败尝试都留下一条可查记录，即使该请求最终成功。**

**设计**

1. **新增表 `call_attempts`**（append-only，一次失败尝试一行）：

```sql
CREATE TABLE IF NOT EXISTS call_attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  TEXT,                       -- 关联 call_logs.request_id
    group_id    INTEGER,
    model_id    INTEGER,
    provider_id INTEGER,
    stage       TEXT NOT NULL,              -- 'select' | 'call' | 'stream'
    endpoint_idx INTEGER,                   -- 端点在该组候选列表中的序号
    attempt_no  INTEGER,                    -- 该端点内的第几次尝试
    error_type  TEXT,
    error_message TEXT,
    duration_ms INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_call_attempts_request ON call_attempts(request_id);
```

2. **图收集、驱动落库**（`call_llm` 不引入 DB 依赖）：
   - `RouteState.attempts: list[dict]` —— 每次失败尝试 append 一条。
   - 驱动在每次「策略执行」返回后**取走** state 里的 attempts，跨 backup 组累加，与最终 `_log_call` 同批写入 `call_attempts`。
   - **成功路径（`status="success"`）与失败路径都写** —— 这正是「重试了也要记录」的落点。

3. **修 G2**：`_try_call` 把最后一次尝试的 `model_id` / `provider_id` 一并写进 state，驱动用它填最终错误行（不再写死 `None`）。

4. **修 G4**：空流 / 首 chunk 超时并入统一失败路径 —— **补 attempt 留痕**，并删除 `core.py:1146-1147` 的不可达死代码。⚠️ **不新增 `cooldown.record_failure`**：冷却现状已在记（见本节 G4 行与 `SG-0_features.md §1.1`），重复添加会变成双重计数，使 `cooldown_failure_threshold` 提前触发。

5. **`call_llm` 暴露失败原因**：现签名在失败时 `return None`，**异常被吞掉**。需改为返回 `(result | None, last_error | None)`（或等价机制），否则白名单判定（§3.5）与留痕都拿不到 `error_type`。

**配套**：`admin_api` 增加只读查询（按 `request_id` / `model_id` / 时间范围），否则这张表只能靠手写 SQL 查。

---

## 4. 阶段一：驱动四步落地 + 图瘦身

这是本设计的核心改动，一次性把「骨架」提到驱动层并统一两条路径。

### 4.1 图的删改

| 动作 | 对象 |
|---|---|
| **删除节点** | `_resolve_group`（含 fallback 组加载与三类守卫） |
| **删除边** | `_route_after_load`（stream 分流）、`try_call → resolve_group` 的 fallback 边 |
| **删除** | 驱动侧 `_stream_common` 的端点重试环（`:1104-1144`）、`fallback_attempted` 一次性降级（`:1204-1219`） |
| **保留并下移** | `_load_and_select` 的「取模板 + 建策略」→ 驱动步骤 ②③；`select_endpoints` 调用 → 图 `select_endpoints` 节点 |
| **新增节点** | `try_stream`（流式调用，经 `writer()` 推 chunk） |

### 4.2 驱动侧归一后的形态

```python
async def _stream_common(internal, serialize, done_signal="data: [DONE]\n\n", request=None):
    """流式：驱动四步 + 图内策略执行，core 只做序列化与落库。"""
    model_name = internal.get("model", "")
    ... 见 §3.1 的 while 与 §3.3 的消费循环 ...
```

`_stream_common` 从 ~180 行降到 ~60 行；非流式 `_handle_chat_non_stream` 与它共用同一段驱动骨架（差异只在 `mode` 与出口如何送出）。

### 4.3 缺陷修复结果

- 缺陷 1（流式缺组级 fallback）：驱动循环对两种 mode 一致 → **流式也能降级到 backup 组**。
- 缺陷 2（流式只 1 跳）：驱动统一用「最多 3 跳 + 环检测」→ 与非流式对齐（**行为变更**，见 §9 R4）。
- 缺陷 3：随边界调整自然消失（每次「策略执行」是全新 `ainvoke`）。

---

## 5. 阶段二：分组解析与缓存入驱动（步骤 ①②）

`_get_group_id`（`core.py:625-658`）+ `_get_extra_route_params`（`:940-956`）合并为一个驱动入口：

```python
async def _resolve_model(internal: dict) -> ModelGroup:
    """model 名 → group_id → ModelGroup，带 60s 缓存。"""
```

- **收益**：消掉每请求 2 次无缓存查库（`db.list_groups(enabled_only=True)` + `db.get_group()`）。分组配置变更频率极低，且 Admin 改动已会主动 `invalidate_*`，缓存安全。
- **必须处理**：「无启用分组」时 `_get_group_id` 抛的是 `HTTPException(404)` —— 这是**驱动层**错误，不得进图被默认映射成 502。保留 404 语义。
- **兼容**：未命中组名时的「兜底第一个启用组 + DEPRECATION 警告」行为保持（`docs/design.md §4.1`）。

---

## 6. 阶段三：`langgraph` 策略作为真子图

现状：`LangGraphStrategy` 名字含 LangGraph 但**零依赖** —— 手写邻接表 + `while` 循环（`MAX_STEPS=50`），且被 `langgraph_engine.py:206-210` 显式拒绝（`ConfigurationError` → 502）。于是 `params` 在服务路径上**完全无效**。

v2 的边界让这一步变得自然：既然图就是「策略执行」，多步工作流策略可以直接作为**子图**挂进来：

```python
g.add_node("workflow", build_workflow_subgraph(group.params).compile())
```

- **收益**：`params` 首次在服务路径上生效；多步工作流自动获得端点级重试、组级降级、落库与限流，不再是一套平行实现。
- **工作量与风险**：三个阶段里最大的一块 —— 需处理子图与主图的 state 键映射（`messages` / `state` / 各节点产出），并满足 100% 覆盖率。建议在阶段一、二稳定并上线后再启动。
- **决议（已确认）**：采用「**重写为真子图**」；舍弃「委托 `LangGraphStrategy.execute()` 的最小改动」方案（只能让 `params` 勉强生效，拿不到重试/冷却/降级/限流，收益过小）。
- **落地顺序**：阶段三依赖阶段一/二的图边界（图 = 策略执行），故排在最后；子图与主图的 state 键映射（`messages` / `state` / 各节点产出）是主要风险点。

---

## 7. 依赖与可行性（已实测）

`uv.lock` 被 `.gitignore:63` 忽略 → **各环境的 langgraph 版本不受约束**。本机实测：

```
python 3.13.14
langgraph==1.2.11   langgraph-checkpoint==4.2.0   langchain-core==1.6.3
langgraph.config.get_stream_writer: OK
graph.compile(): OK
app.astream 形参含 stream_mode；Pregel.astream 源码含 "custom"
```

`pyproject.toml` 原声明 `langgraph>=0.2.0` —— **低于 `get_stream_writer` 的引入版本**。**已按评审决议提升为 `langgraph>=1.0`**（本设计依赖该 API，旧版会在导入期直接失败）。`uv.lock` 被 gitignore 意味着这一下限是唯一的版本约束，各环境部署后仍应核对 `langgraph.__version__`。

`langgraph_engine.py:1-19` 的模块 docstring 记录过一个历史问题：`ainvoke` 在 Windows/Python 3.13 + LangSmith tracing 下曾因线程问题死锁（生产 uvicorn 下无此问题）。`astream` 走入同一套执行器，**必须在 mq3 与生产各实测一次**再合并。

---

## 8. 回归矩阵

### 8.1 驱动层（新增，两种 mode 共用）

单元需覆盖：主组成功、主组全冷却 → backup 组、backup 也全冷却、降级环、深度 > 3、backup 组不存在、`recoverable=False` 时**不降级**直接上抛。

### 8.2 图内（策略执行）

单元需覆盖：首 chunk 超时、首 chunk 空流、首 chunk 抛可重试错误（429/500/503）、首 chunk 抛不可重试错误、mid-stream 失败、客户端断开、同组多端点轮换、无可用端点。

### 8.3 外部回归（客户端 = `api.vxquant.com` / tailnet `100.88.88.2`，经 Tailscale 打 `100.88.88.88:4000`）

**4 种协议各两条（流式 + 非流式）**：

| 协议 | 端点 |
|---|---|
| OpenAI Chat | `POST /v1/chat/completions`（`stream: true` / `false`） |
| OpenAI Legacy | `POST /v1/completions` |
| Anthropic | `POST /v1/messages` |
| OpenAI Responses | `POST /v1/responses` |

外加落库核对：`call_logs` 的 `status` / `model_id` / `provider_id` / `duration_ms` / `prompt_tokens` / `completion_tokens` 与改动前逐条一致（写入是 `buffer=100, flush=5s` 批量缓冲，需等 5s 再查）。

### 8.4 非流式防回归基线

对同一组参数，改动前后响应体**逐字段 diff 为空**。重点：`_routing` 被 `pop` 后的字段集合、`error_type` 落库值、未知组名的兜底行为、`HTTPException(404/502)` 的映射。

> **R9 例外**：未预期异常（黑名单）的响应由「降级后可能成功」变为「502」，这是评审确认的行为变更，不作为回归失败判定。

### 8.5 失败留痕（§3.6）验收

| 场景 | 断言 |
|---|---|
| 首端点失败 → 次端点成功 | `call_logs` 1 行 `status="success"`；`call_attempts` ≥1 行，`error_type` / `model_id` / `provider_id` 齐全（验证 G1） |
| 主组全失败 → backup 组成功 | `call_attempts` 同时含主组与 backup 组的尝试（验证 G3） |
| 全失败 | `call_logs` 错误行的 `model_id` / `provider_id` **非 `None`**（验证 G2） |
| 空流 | 该端点被 `cooldown.record_failure`（验证 G4） |

---

## 9. 风险与回滚

| # | 风险 | 处置 |
|---|---|---|
| R1 | `get_stream_writer()` 仅在 stream 上下文可用；非流式路径调用会抛错 | 节点内 `try/except RuntimeError` 兜底；`try_stream` 只由 stream 分支进入 |
| R2 | langgraph 版本漂移（无 lock 文件） | 抬高 `pyproject.toml` 下限；部署后核对 `langgraph.__version__` |
| R3 | `astream` 在 uvicorn 下的行为未验证 | 合并前在 mq3 + 生产各跑一次真实流式 |
| R4 | 流式降级由 1 跳变最多 3 跳（**行为变更**） | **已确认接受**（与非流式对齐，即缺陷 1 的修复）；`docs/design.md §3.5/§4.2` 已同步改写 |
| R5 | mid-stream 失败不可回退（已推出 chunk 无法收回） | 保持现有语义：`stream_started=True` 后只报错不降级 |
| R6 | 驱动层新增循环，若与图内 `call_llm` 重试叠加会放大调用量 | 显式区分两层重试（见 §3.1），驱动循环**不做**端点级重试 |
| R7 | 4 种协议共用驱动骨架，改造面覆盖全部协议 | 按 §8.3 的 8 条外部回归全跑 |
| R8 | §3.6 新增 `call_attempts` 表（**schema 变更**） | 表为 append-only 且与主链路解耦：旧代码不读它即无影响，回滚时**保留不删** |
| R9 | 降级白名单收紧 → 非流式遇未预期异常由「降级」变「502」（**行为变更**） | 已评审确认（§11 #3）；黑名单失败**必须留痕**（§3.6），避免变成静默失败 |

**回滚**：阶段一 + 二动 `core.py` / `langgraph_engine.py` / `pipeline/*` / `storage/*` 与测试。除 §3.6 新增的 `call_attempts` 表（append-only、与主链路解耦，回滚时保留不删）外无其它 schema 变更；依赖侧仅抬高 `langgraph` 下限。整体 `git revert` 可回到当前行为（行为差异见 R9）。

---

## 10. 实施顺序与验收标准

| 阶段 | 内容 | 验收 |
|---|---|---|
| **一** | 驱动四步落地（含组级降级循环）+ 图瘦身（删 `_resolve_group`/fallback 边，加 `try_stream`）+ 传输层分离 + 降级白名单 + **失败留痕（§3.6）** + 抬高 langgraph 下限 | 单元覆盖率 100%；4 协议 × 流式/非流式外部回归通过；非流式响应逐字段不变（R9 例外）；`call_logs` 落库字段一致；§8.5 四条留痕断言全绿 |
| **二** | 步骤 ①② 的 `_resolve_model` 入驱动 + 60s 缓存 + 保留 404 语义 | 每请求 DB 查询次数下降（打点实测）；未知组名兜底行为不变 |
| **三** | `langgraph` 策略重写为真 `StateGraph` 子图 | `params` 在服务路径首次生效；多步工作流回归；覆盖率 100% |

**统一红线**：任何阶段都**不得**用新增 `# UNCOVERED` 标记来凑 100%。若确实遇到不可达行，需在评审中单独举证（参考 `docs/coverage-2026-09-18.md §7.3` 的复核更正 —— 那里就有一处标记被复核推翻）。

---

## 11. 评审决议（2026-09-21 已确认）

| # | 事项 | 决议 |
|---|---|---|
| 1 | 图的边界 | **驱动四步在图外，`StateGraph` = 单次「策略执行」**（§1、§3） |
| 2 | 流式降级 1 跳 → 3 跳（R4 行为变更） | **接受** —— 与非流式对齐，即缺陷 1 的修复 |
| 3 | `recoverable` 判定 | **收紧为显式白名单**（§3.5；非流式遇未预期异常改 502，即 R9） |
| 4 | `langgraph` 依赖下限 | **已在 `pyproject.toml` 提升为 `>=1.0`**；`pipeline_router_design.md §9` 已同步 |
| 5 | 阶段三范围 | **重写为真 `StateGraph` 子图**（舍弃「委托调用」最小改动方案） |
| 6 | `docs/design.md` 同步 | §3.5 / §4.2 / §4.1 均已改写 |
| 7 | 失败留痕 | **新增 §3.6**：`call_attempts` 表 + 逐尝试留痕（G1）+ 归属修正（G2）+ 降级前组失败留痕（G3）+ 空流/超时补冷却与留痕（G4） |

**任务清单（已开始）**

- `docs/tasks/SG-0_features.md` —— 失败留痕（可独立先行，先于图重构落地，用于验收重构）
- `docs/tasks/SG-1_features.md` —— 阶段一主体：驱动四步 + 图瘦身 + 传输层分离 + 白名单
