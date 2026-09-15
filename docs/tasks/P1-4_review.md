# P1-4 审查报告：PipelineEngine 骨架 + core.py 非流式接入

> 审查者：验证 Agent
> 审查日期：2026-09-09
> 审查对象：`docs/tasks/P1-4_features.md` + `docs/tasks/P1-4_tests.md`
> 设计依据：`docs/pipeline_router_design.md` v2.0 第 6 节

---

## 审查结论：🔴 需修改后重审

存在 **1 个关键问题** 和 **3 个重要问题**，需修改后重审。

---

## 一、设计文档一致性审查

### ✅ 一致的部分

| 功能点 | 设计文档节 | 一致性 | 备注 |
|--------|-----------|--------|------|
| F1: `__init__` + db_factory | 6.3 | ✅ | 完全一致 |
| F2: `_load_group` 60s TTL | 6.3 | ✅ | lazy expiration 策略正确 |
| F3: `_create_strategy` | 6.3 | ✅ | ConfigurationError 错误信息格式正确 |
| F4: `route()` non-streaming | 6.3 | ✅ | fallback 异常类型列表正确 |
| F5: Fallback 循环检测 | 8.3 | ✅ | `_visited: set` 实现正确 |
| F6: 深度限制 + ConfigError | 8.3 | ✅ | 深度限制为 3（4 层），符合设计 |
| F7: `__init__.py` 导出 | 6.2 | ✅ | 导出列表完整 |

---

## 二、🔴 关键问题

### KP-1: `_get_extra_route_params` 改造会破坏 `_stream_common`

**严重程度**：🔴 阻断

**问题描述**：

功能点文档 F8 将 `_get_extra_route_params` 的返回类型从 `(int, GroupRouter, dict)` 改为 `(int, PipelineEngine, ModelGroup, dict)`（3 元组 → 4 元组）。

但 `_stream_common`（core.py:1060）也调用此函数：

```python
# _stream_common 第 1060 行
group_id, router, safe_extra = await _get_extra_route_params(internal, stream=True)
```

如果返回类型改为 4 元组，Python 解包会抛出 `ValueError: too many values to unpack`，**流式请求全部崩溃**。

功能点文档同时声称"流式路径不受影响"，但改造 `_get_extra_route_params` 必然破坏流式路径。两处声明自相矛盾。

**修复建议**（任选其一）：

**方案 A**（推荐，最小改动）：不改造 `_get_extra_route_params`。在 `_handle_chat_non_stream` 中单独获取 engine 和 group：
```python
async def _handle_chat_non_stream(internal, request, format_response):
    group_id, router, safe_extra = await _get_extra_route_params(internal)
    engine = _get_engine()
    group = await engine._load_group(group_id)
    result = await engine.route(group=group, messages=..., stream=False, **safe_extra)
```

**方案 B**：`_get_extra_route_params` 保持返回 `(int, GroupRouter, dict)`，新增独立的 `_get_engine_route_params` 函数供非流式路径使用。

---

## 三、🟡 重要问题

### IP-1: `NoAvailableModelError` 不触发 fallback，与 `AllModelsCooldownError` 存在缺口

**严重程度**：🟡 设计缺陷

**问题描述**：

功能点文档 F4 和测试 R-05 声称 `AllModelsCooldownError` 触发 fallback。但三个内建策略在"所有模型冷却"时实际抛出的是 `NoAvailableModelError`：

```python
# strategies.py — RandomWeightsStrategy.select_endpoints
available = filter_available(endpoints, cooldown, group_id)
if not available:
    raise NoAvailableModelError(f"Group {group_id}: all models on cooldown")
```

`NoAvailableModelError` 不在 fallback 捕获列表 `(AllModelsCooldownError, ProviderError, StrategyError)` 中。同样，"group 无模型配置"时也抛出 `NoAvailableModelError`，也不触发 fallback。

而 `AllModelsCooldownError` 在整个代码库中**从未被任何代码抛出**（仅在 `exceptions.py` 中定义）。

**影响**：所有模型冷却或 group 无模型时，fallback 机制完全失效。

**修复建议**：

两步修复：
1. 将 `NoAvailableModelError` 加入 fallback 捕获列表，或
2. 让策略在"所有模型冷却"时抛出 `AllModelsCooldownError` 而非 `NoAvailableModelError`

推荐方案 1（在 Engine 层面加一个异常即可），或者更彻底地合并这两个语义重叠的异常类。

---

### IP-2: 测试 R-05（`AllModelsCooldownError` 触发 fallback）无法被真实策略触发

**严重程度**：🟡 测试有效性

**问题描述**：

测试 R-05 的描述是 `AllModelsCooldownError 触发 fallback`，但：
- 所有内建策略不会抛出 `AllModelsCooldownError`
- `call_llm()` 也不会抛出
- 该异常仅在 `exceptions.py` 中定义，从未被 raise

此测试只能验证 Engine 层面的 try/except 逻辑，但无法验证真实场景下的 fallback 行为。应替换为使用 `NoAvailableModelError`（修复 IP-1 后）或使用 `ProviderError`（所有 endpoint 失败的场景，已在 R-04 覆盖）。

---

### IP-3: 缺少关键回归测试

**严重程度**：🟡 测试覆盖

**缺失测试**：

| # | 缺失测试 | 原因 | 优先级 |
|---|---------|------|--------|
| 1 | `_stream_common` 仍使用 `GroupRouter`（回归测试） | P1-4 改造 core.py 后，需确保流式路径未受影响 | 高 |
| 2 | `_get_extra_route_params` 返回值变更验证 | F8 改造了返回类型，但无测试验证新返回值 | 高 |
| 3 | `_get_engine()` 构造参数验证（`_get_db` 和 `_cooldown_manager`） | C-03 描述"验证 `_get_db` 作为 factory 传入"，但详细代码仅检查返回类型 | 中 |
| 4 | `StrategyError` 触发 fallback（R-06）的详细测试代码 | 有描述但无详细代码，与 R-04/R-05 不一致 | 中 |
| 5 | `ConfigurationError` 在 fallback 路径中的传播（`_load_group` 找不到 fallback group） | R-13 仅覆盖 `_create_strategy` 抛出场景，未覆盖 fallback 路径 | 低 |

---

## 四、核心逻辑审查

### 4.1 `PipelineEngine.route()` 异常处理流程

```
route()
  │
  ├─ _fallback_depth > 3 → ProviderError("Fallback chain too deep")     ✅ 正确
  │
  ├─ group.id in _visited → ProviderError("Fallback cycle detected")    ✅ 正确
  │
  ├─ _create_strategy(group)  ← 在 try 块外部                              ✅ 正确
  │  └─ 未知 type → ConfigurationError（不被 except 捕获，直接传播）
  │
  ├─ try: strategy.execute()
  │  ├─ 成功 → 返回 dict                                                ✅
  │  └─ AllModelsCooldownError / ProviderError / StrategyError           ✅
  │     ├─ 有 fallback_group_id → 递归 route()                          ✅
  │     └─ 无 fallback → re-raise                                       ✅
  │
  └─ except ConfigurationError → raise（不 fallback）                    ✅
```

**注意**：`_create_strategy()` 在 try 块**外部**调用，其 `ConfigurationError` 不会被任何 except 捕获。这在功能点文档中未明确说明，但行为正确——未知策略类型不应 fallback（尝试 fallback group 可能也是同类型）。

### 4.2 `core.py` 改造逻辑

**`_get_engine()`**：
- ✅ 单例 + 懒初始化
- ✅ `db_factory=_get_db`（传函数引用，非调用结果）
- ✅ `cooldown=_cooldown_manager`（全局实例共享）

**`_handle_chat_non_stream`**：
- ✅ 从 `_get_extra_route_params` 解构新增的 `group`（前提是 KP-1 被修复）
- ✅ `engine.route()` 参数传递正确
- ✅ `stream=False` 显式传递
- ✅ 返回值格式不变，后续 log_call / format_response 逻辑不受影响

### 4.3 `_stream_common` 保持不变

- ✅ P1-4 明确不改动流式路径（设计文档 6.5 Phase 1）
- ⚠️ 但前提是不会被 `_get_extra_route_params` 的改造波及（见 KP-1）

---

## 五、测试覆盖矩阵审查

### 5.1 功能点覆盖度

| 功能点 | 测试用例 | 正例 | 反例 | 边界 | 评价 |
|--------|---------|------|------|------|------|
| F1: `__init__` | E-01~E-03 | 3 | - | - | ✅ 完整 |
| F2: `_load_group` | G-01~G-09 | 5 | 1 | 3 | ✅ 完整（含缓存命中/过期/边界） |
| F3: `_create_strategy` | S-01~S-06 | 4 | 2 | - | ✅ 完整（含 3 种策略 + 参数传递 + 未知 type） |
| F4: `route` non-stream | R-01~R-08 | 6 | 2 | - | ⚠️ R-05 有效性存疑（见 IP-2） |
| F5: 循环检测 | R-09~R-10 | - | 2 | - | ✅ 完整 |
| F6: 深度限制 | R-11~R-12 | - | 1 | 1 | ⚠️ R-12（恰好 3 层）缺详细代码 |
| F7: `__init__.py` | (import) | 1 | - | - | ✅ |
| F8: `_get_engine` | C-01~C-03 | 3 | - | - | ⚠️ C-03 缺构造参数验证 |
| F9: `_handle_chat_non_stream` | C-04~C-07 | 4 | - | - | ⚠️ 依赖 KP-1 修复 |
| F10: `ConfigurationError` | (已存在) | - | - | - | ✅ |

### 5.2 测试数量统计

- 功能点文档声称：26 个测试
- 实际计数：
  - PipelineEngine 单元测试：E(3) + G(9) + S(6) + R(14) = 32 个详细用例
  - core.py 集成测试：C(7) 个详细用例
  - 含 import 验证：1 个
  - **总计：约 40 个用例**

**注意**：文档声称"26 个测试"，但覆盖矩阵中实际列出的用例编号从 E-01 到 R-14、C-01 到 C-07，远超 26 个。覆盖矩阵中的计数（正例 26 + 反例 8 + 边界 4 = 38）与正文列出的用例数不一致。应统一数字。

### 5.3 Mock 策略评价

- ✅ 使用 `Mock(spec=Database)` / `AsyncMock` 正确模拟异步 DB
- ✅ 使用 `patch.object` 替换策略方法，隔离测试
- ✅ 缓存过期通过直接修改时间戳模拟，简洁有效
- ⚠️ 未 Mock `NoAvailableModelError` / `AllModelsCooldownError`，与 IP-1/IP-2 相关

---

## 六、向后兼容审查

### ✅ 兼容的部分

| 场景 | 评价 |
|------|------|
| 非流式请求行为等价 | ✅ `strategy.execute()` 内部逻辑与 `GroupRouter._route_non_stream` 一致 |
| 流式请求不受影响 | ✅ `_stream_common` 保持使用 GroupRouter（前提是 KP-1 修复） |
| `_get_router()` 保留 | ✅ 流式路径和现有测试仍可使用 |
| 返回值格式不变 | ✅ `engine.route()` 返回 dict，`_handle_chat_non_stream` 后续逻辑无需改动 |
| `_routing` 字段 | ⚠️ `GroupRouter.route()` 返回带 `_routing` 的 dict（含 model_id, provider_id），`strategy.execute()` 返回 `call_llm()` 的原始结果。需确认 `call_llm()` 的返回值是否包含 `_routing` |

### ⚠️ 潜在兼容性问题

**`_routing` 字段传递**：

`_handle_chat_non_stream` 从 result 中取出 `_routing`：
```python
routing = result.pop("_routing", {})
```

现有 `GroupRouter.route()` 会在返回值中注入 `_routing`。但 `strategy.execute()` → `call_llm()` 的返回值来自 `provider.chat()`，不一定包含 `_routing`。

如果 `call_llm()` 不注入 `_routing`，则 `routing` 为空 dict，`model_id` 和 `provider_id` 都为 None，call_log 中的 model_id/provider_id 将丢失。

**建议**：在 `BaseStrategy.execute()` 或 `call_llm()` 中确保返回值包含 `_routing`，或在 `PipelineEngine.route()` 中补充路由元数据。

---

## 七、修改建议汇总

| 优先级 | 编号 | 问题 | 建议修改 |
|--------|------|------|---------|
| 🔴 P0 | KP-1 | `_get_extra_route_params` 返回值破坏流式 | 方案 A：不改造 `_get_extra_route_params`，在 `_handle_chat_non_stream` 中单独获取 engine 和 group |
| 🟡 P1 | IP-1 | `NoAvailableModelError` 不触发 fallback | 将 `NoAvailableModelError` 加入 fallback 捕获列表，或让策略抛出 `AllModelsCooldownError` |
| 🟡 P1 | 兼容 | `_routing` 字段可能丢失 | 在 `PipelineEngine.route()` 或 `call_llm()` 中注入 `_routing` |
| 🟡 P1 | IP-3 | 缺少流式回归测试 | 新增 `_stream_common` 回归测试 |
| 🟢 P2 | IP-2 | R-05 测试有效性 | 修复 IP-1 后同步更新测试 |
| 🟢 P2 | IP-3 | 构造参数验证 | 补充 C-03 的详细测试代码 |
| 🟢 P2 | 统计 | 测试数量声称不一致 | 统一文档中的测试数量 |

---

## 八、最终判定

**结论：🔴 需修改后重审**

必须修复：
1. **KP-1**：`_get_extra_route_params` 返回值变更会破坏流式路径（阻断性 bug）
2. **IP-1**：`NoAvailableModelError` 与 `AllModelsCooldownError` 的 fallback 缺口
3. **兼容性**：`_routing` 字段传递可能丢失

建议同步修复：
4. **IP-3**：补充关键回归测试

修复后需重新审查功能点文档和测试文档。
