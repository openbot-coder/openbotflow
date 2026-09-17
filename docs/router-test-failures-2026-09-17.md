# 8 个既有失败的根因分析（2026-09-17）

> 目标：查清 main 上 **8 个既有失败**（`c993667` 与 `08973e9` 失败集合完全相同）的根因。
> 结论：**不是测试用例互相污染**，而是 **1 个实现漏改 + 1 个异常类型丢失 + 1 个过时用例**。
> 证据环境：`mq3.vxquant.com`（Ubuntu 24.04 / Python 3.13.15 / pytest 9.1.1），
> 隔离副本 `/tmp/bfcopy`（`git archive c993667` 导出）+ `/tmp/bfi` venv。
> 本机（Windows）无法跑 async 用例，故所有 async 证据均取自 Linux。

---

## 0. 先纠正一个错误结论

`docs/deploy-mq3-2026-09-17.md` §5.1 与 `.workbuddy/memory` 当时写的是：

> 「典型断言不符……疑似**跨用例全局冷却状态污染**导致」

**这个判断是错的。** 本次用三种跑法交叉验证，失败集合**逐字一致**：

| 跑法 | 结果 |
|---|---|
| 8 条用例**单独**跑（`pytest <8 个 node id>`） | **8 failed** |
| 按文件单独跑（`test_pipeline_strategies.py` / `test_router.py` / `test_router_full.py`） | **1 + 4 + 3 failed** |
| 三个文件**合并**跑 | **8 failed**（与单独跑完全相同） |

→ 既然**单独跑也全挂**，就不存在任何「前序用例污染后序用例」的可能。
真正原因是**确定性的代码缺陷**（下节）。`Model 1 entered cooldown for 1000s` 只是
`test_router_full.py` 自己 `record_failure(..., cooldown_seconds=1000)` 的预期日志，不是污染痕迹。

---

## 1. 三类根因

### 根因 A —— `RoundRobinStrategy` 丢了设计规定的计数器 wrap（1 个失败）

`tests/test_pipeline_strategies.py::TestRoundRobinStrategy::test_counter_overflow_protection`

```
E   assert 2000 == 0
tests/test_pipeline_strategies.py:308: assert 2000 == 0
```

**设计文档写得很明确**，`docs/tasks/P1-3_features.md:113`：

```python
self._counters[group_id] = next_idx % (len(available) * 1000)
```

**实现漏了取模**（`src/botflow/pipeline/strategies.py:88`，改前）：

```python
self._counters[group_id] = next_idx          # ← 少了 % (len(available) * 1000)
```

测试把计数器置为 `1999`（`len(available)==2` → 基数 2000），期望 `(1999+1) % 2000 == 0`，
实际是 `2000`。**这是实现漏改，不是测试写错**。

**修复**：恢复设计里的取模（`len(available) * 1000` 是 `len(available)` 的倍数，所以
`(idx % base) % n == idx % n`，轮询序列**不变**，只是计数器不再无界增长）。

---

### 根因 B —— LangGraph 节点把异常压成字符串，**异常类型丢失**（6 个失败）

涉及用例：

- `tests/test_router.py::TestPipelineEngineRouting::{test_no_models_in_group, test_no_available_provider, test_disabled_provider_skipped}`
- `tests/test_router_full.py::{test_route_no_models, test_route_non_stream_all_cooldown_raises, test_route_stream_all_cooldown_no_fallback_raises}`

典型报错：

```
E   botflow.common.exceptions.ProviderError: No fallback group available
src/botflow/pipeline/langgraph_engine.py:469: in route
```

机制（三处叠加）：

1. **节点吞掉异常类型**——`_load_and_select` 的 `except` 分支（改前）：

   ```python
   except Exception as exc:
       # Recoverable — store in error (not fatal) so graph can try fallback
       return {"error": str(exc)}          # ← str(exc)：类型在这里就没了
   ```

   该分支本身是**故意的**（可恢复错误要留给 fallback 图重试），错的是只留了字符串。

2. **非流式统一改抛 `ProviderError`**——`route()` 只看 `result["error"]["message"]`，
   对任何失败都 `raise ProviderError(msg)`；而 `_finalize_error` 又**优先**用
   `fatal_error`（`_resolve_group` 里的字符串 `"No fallback group available"`），
   于是**原始成因连消息都被覆盖**。

3. **流式统一改抛泛化的 `NoAvailableModelError`**——`route_stream()` 只看
   `if not endpoints`，完全无视 `state["error"]`（改前 :514）：

   ```python
   endpoints = result.get("endpoints", [])
   if not endpoints:
       raise NoAvailableModelError(f"Group {group.id} has no available models for streaming")
   ```

   所以「全部模型都在冷却」（`AllModelsCooldownError`，是 `NoAvailableModelError` 的**子类**）
   被降级成父类，`pytest.raises(AllModelsCooldownError)` 自然失败。

**影响面（诚实评估）**：`core.py:1020` 把**所有**路由异常统一成 `HTTPException(502)`，
所以**客户端可见的状态码不变**；但 `core.py:1016` 会把 `type(e).__name__` 写进
`call_logs.error_type` —— 运维**再也分不清**「全组冷却（等一会儿就好）」和
「压根没配模型 / 上游全挂」，排障信息被抹平。属**诊断能力退化**，不是可用性故障。

**修复**：`_load_and_select` 改为存**异常对象**（`{"error": exc}`），并新增
`_raise_routing_error(raw, default_exc)`：能拿回原始异常就**按原类型重抛**，
拿不回来（已被压成字符串，如 `_try_call` 的 `"All endpoints in group failed"`）才退化为
`default_exc`。`route()` 与非流式 `route_stream()` 都改用它。

---

### 根因 C —— `test_route_stream_all_on_cooldown_fallback` 是**过时用例**（1 个失败）

```
E   botflow.common.exceptions.NoAvailableModelError: Group 1 has no available models for streaming
src/botflow/pipeline/langgraph_engine.py:514: in route_stream
```

该用例断言：主组全部冷却时，`route_stream()` **自己**降级到 `fallback_group_id=4` 并返回
`group_id == 4`。

但 **`docs/design.md:197-200` §3.5 明确规定了相反的分工**：

> `stream: true` 时返回 SSE 流。**流式路由只做端点选择**，实际迭代与重试由调用方
> （`core.py`）完成；fallback 分组**只尝试一次**。

`design.md:244` 再次强调「流式路径的 fallback 分组只尝试一次（由 `fallback_attempted` 标志控制）」，
而 `fallback_attempted` 这个标志**就在 `core.py::_stream_common` 里**（`core.py:1092/1207`）。
即：**流式的分组级 fallback 归调用方**，不归图。

`git log -S` 显示该用例是 **`1b9a0c9`（"chore(release): v3.0.0 — 收口提交 LangGraph 工作流重构"）
一次性引入**的，与 LangGraph 重构同批落地 —— 很可能引入时就从未绿过。

**修复**：按设计文档把用例改写为 `test_route_stream_all_on_cooldown_raises_typed_error`，
断言「全组冷却 → 抛**具体类型**的 `AllModelsCooldownError`」（依赖根因 B 的修复）。

---

## 2. 顺带发现的**未修**问题（供决策，不在本次范围）

**流式路径在「路由阶段就无可用模型」时，完全不会尝试 fallback 分组。**

- 非流式：图内 `resolve_group` 会走 fallback → 主组没模型时自动用备用组。
- 流式：`_route_after_load` 只返回 `"done"`（END），**不经过 `resolve_group`**；而调用方
  `core.py` 的 `fallback_attempted` 逻辑**只在 `route_stream()` 成功返回之后**才生效
  （`core.py:1206`，用的是 `route_result.get("fallback_group_id")`）。
- 结果：若主组所有模型都处于冷却，**流式请求直接 502，永不尝试 `backup` 组**；
  同样的请求走非流式则会成功降级。

这与 `docs/pipeline_router_design.md:15`「所有策略共享 cooldown、retry、**fallback**」
的原则不一致，**建议单独开一条任务**评估（要动 `core.py` 流式热路径，影响面比本次修复大）。
本次**未改**。

---

## 3. 修复清单与验证

| 文件 | 改动 |
|---|---|
| `src/botflow/pipeline/strategies.py` | `RoundRobinStrategy` 恢复 `% (len(available) * 1000)` 计数 wrap（根因 A） |
| `src/botflow/pipeline/langgraph_engine.py` | ① `_load_and_select` 存异常对象；② 新增 `_raise_routing_error()`；③ `route()` 两处错误出口改用它；④ `route_stream()` 无端点时优先重抛原始类型；⑤ `RouteState.error` 注释与类型（根因 B） |
| `tests/test_router.py` | `test_route_stream_all_on_cooldown_fallback` → `test_route_stream_all_on_cooldown_raises_typed_error`，按 `design.md §3.5` 断言具体异常类型（根因 C） |

**验证**（Linux，`/tmp/bfcopy`，`PYTHONPATH=src`）：

- 三个 router 相关文件：**0 failed**（改前 8 failed）
- 全量 `pytest tests/ --cov=botflow`：见 `docs/version-3.1.0-2026-09-17.md`
