# botflow 代码审查：LLM-Proxy → LLM 工作流升级

> 审查对象：`E:\src\openbotflow`（botflow v1.1.0）
> 审查目的：熟悉项目代码与架构，评估"将简单 proxy 升级为 LLM 工作流"的改造基础
> 结论先行：**PipelineEngine 骨架 + 3 个内建策略 + DB type/params 迁移已落地，非流式路径已切到 PipelineEngine；流式路径、Admin API type/params 透传、`/admin/strategies`、LangGraph 策略均未落地。代码可运行，但存在 1 个高严重度架构隐患和若干中低严重度问题，需在进入 P3/P5/P6 前修复。**

---

## 1. 项目定位与架构总览

botflow 是一个基于 FastAPI + aiosqlite 的 LLM 中间件网关，当前为 **Phase 1（LLM Proxy）**。核心能力：

- 多 Provider 聚合（OpenAI/Azure/Anthropic/Google/Ollama/vLLM/DeepSeek），`api_format` 字段做 per-model SDK 覆盖（中转站场景）
- 4 种 API 格式：OpenAI Chat Completions / Responses、Anthropic Messages、Google Gemini
- 分组加权路由 + 冷却 + 重试 + fallback group 降级
- 多客户端 Key（sha256 哈希）、全链路审计日志、每日摘要、IP 速率限制、模型同步

### 分层结构

```
请求 → RateLimitMiddleware → AuthMiddleware (Client Key)
   │
   ├─ /v1/* → protocol_adapter(转 internal dict)
   │          ├─ 非流式 → _handle_chat_non_stream → PipelineEngine.route (stream=False)
   │          └─ 流式   → _stream_common → GroupRouter.route (stream=True)   ← 仍走旧路由
   │
   └─ /admin/* → verify_admin_key → admin_api → db._*_raw 方法
```

### 关键目录

| 模块 | 职责 |
|------|------|
| `core.py` | FastAPI 主服务、中间件、端点、流式/非流式 handler、lifespan 后台任务 |
| `router.py` | 旧 `GroupRouter` + `CooldownManager` + 权重/重试工具 + **全局 semaphore/endpoint/provider 缓存** |
| `pipeline/` | 新 `PipelineEngine` + `BaseStrategy` + 3 内建策略 + `_shared.py`（**复制了一份全局缓存**） |
| `protocol_adapter.py` | 4 种 API 格式 ↔ internal dict 转换 |
| `admin_api.py` | `/admin/*` REST 管理接口 |
| `storage/db.py` | aiosqlite 数据层 + 迁移（`type`/`params` 列已加） |

---

## 2. 升级现状（P1–P7 阶段映射）

| 阶段 | 内容 | 状态 |
|------|------|------|
| P1-1 | `model_groups` 加 `type`/`params` + DB CRUD + 迁移 | ✅ 完成（db.py 550/595/1067/1084） |
| P1-2 | `pipeline/` 骨架：`BaseStrategy` + `RouteResult` + `STRATEGY_REGISTRY` + `_shared.py` | ✅ 完成 |
| P1-3 | 3 个内建策略（random_weights / round_robin / sequential） | ✅ 完成 |
| P1-4 | `PipelineEngine` + core.py **非流式**接入 | ✅ 完成（core.py 949 `_handle_chat_non_stream`） |
| P3 | core.py **流式**路径迁移到 PipelineEngine | ❌ 未完成（`_stream_common` 仍走 `GroupRouter`） |
| P4 | Admin 支持 type/params + `/admin/strategies` | ❌ 未完成 |
| P5/P6 | LangGraphStrategy（可选 langgraph 依赖） | ❌ 未完成（pyproject 无 `pipeline` optional-deps） |
| P7 | 清理 `GroupRouter` 标 deprecated | ❌ 未完成 |

**结论**：非流式路径已是"策略可插拔"的工作流雏形，流式与 Admin 仍是旧路由。这正是"升级到 LLM 工作流"要补齐的部分。

---

## 3. P1-4 审查报告遗留问题的落地核对

`P1-4_review.md` 标记 🔴 需修改后重审，列出 3 个必修项。逐项核对当前代码：

| 编号 | 问题 | 当前代码 | 状态 |
|------|------|---------|------|
| KP-1 | `_get_extra_route_params` 改签名破坏流式 | `core.py` 实际**没有改** `_get_extra_route_params`，而是在 `_handle_chat_non_stream` 内单独取 `engine`/`group`（949-971 行） | ✅ 已按方案 A 修复 |
| IP-1 | `NoAvailableModelError` 不触发 fallback | `engine.py` 87 行 except 列表已含 `NoAvailableModelError` | ✅ 已修复 |
| 兼容 | `_routing` 字段丢失 | `_shared.py` `call_llm` 226 行成功路径注入 `result["_routing"]` | ✅ 已修复 |
| IP-3 | 缺流式回归测试 | 流式未迁移，暂不适用 | ➡️ 留到 P3 |

**判定：P1-4 阻断项已全部消化，代码实现比文档更稳。**

---

## 4. 发现的问题（按严重度）

### P0 — 高危

**P0-1 `router.py` 与 `pipeline/_shared.py` 各自维护一套全局缓存，导致并发保护与配置热更新在"双路由并存"下失效**

`_provider_semaphores`、`_endpoint_cache`、`_provider_cache` 在两个模块里各有一份互不相通的副本（`router.py:35,184,188` vs `pipeline/_shared.py:36,37,40`）。

- 后果 A（限流失效）：非流式走 `_shared._provider_semaphores`，流式走 `router._provider_semaphores`。`upstream_semaphore_size` 本意是"同一 provider 跨 group 限流"，现在流式/非流式各自限流，总并发 = 2× 上限，thundering-herd 保护被削弱。
- 后果 B（缓存双写）：`_endpoint_cache` 两份，Admin 改 `group_models` 后 60s 窗口内两路径读到不一致的 endpoint 集合。
- 后果 C（孤儿代码）：`invalidate_endpoint_cache()`（`_shared.py:286`）**从未被任何代码调用**（grep 仅定义处命中）。Admin 增删模型/调权重后，endpoint 缓存最长 60s 不失效——这是个静默 bug。

**建议**：把三份全局状态收敛到单一 owner（推荐保留在 `router.py`，`_shared.py` 改为 import 而非复制），并在 Admin 的 group-model 写操作处统一调用 `invalidate_endpoint_cache(group_id)`。这是进入 P3（流式迁移）前必须做的地基。

### P1 — 中危

**P1-1 Admin group CRUD 不透传 `type`/`params`（P5 未落地，且现状有坑）**

- `admin_api.create_group/update_group` 没有 `type`/`params` 参数；PATCH 走 `update_group_raw`，其 `type`/`params` 是带默认值的 **keyword-only 形参**（`db.py:1084` `type="random_weights", params=None`）。
- 后果：对已有 `round_robin` group 做一次 `PATCH /admin/groups/{id}`（如只改 `is_enabled`），admin_api 不传 `type`/`params` → `update_group_raw` 用默认 `type="random_weights"` + `params='{}'` 覆盖回去，**悄悄把策略类型改回随机权重**。
- 这是 P4/P5 的阻断前置：要么把 `type`/`params` 加入 admin 接口并透传，要么让 `update_group_raw` 支持"未传则保留原值"（None 语义）。

**P1-2 `verify_llm_key`/`verify_admin_key` 的 `HTTPAuthorizationCredentials` 注解未导入**

`auth.py` 只 `from fastapi.security import HTTPBearer`，但两个函数签名用了 `credentials: Optional[HTTPAuthorizationCredentials]`（56/84 行）。`get_type_hints` 复现 `NameError`。

- 现状为何没炸：`from __future__ import annotations` 让注解变字符串；FastAPI 对这个参数按 `credentials` 名字匹配注入。
- 风险：这是个"能跑但语义悬空"的注解——任何对 `typing.get_type_hints(auth.verify_admin_key)` 的反射、第三方 linter、或未来 FastAPI 版本变更都可能踩雷。**一行修复：补 import。**

**P1-3 `AllModelsCooldownError` 与 `NoAvailableModelError` 语义重叠**

策略层（`strategies.py`）全部抛 `NoAvailableModelError`；只有旧 `GroupRouter`（流式路径）抛 `AllModelsCooldownError`。engine.py 两个都捕获（87 行），行为正确，但"全冷却"和"无模型"两种本应区分的情况现在共用一个异常。P3 流式迁移到策略层后，`AllModelsCooldownError` 将彻底无人抛出，可考虑合并或加注释说明。

**P1-4 `x-api-key` 头在文档中被提及但代码从未实现**

README/design 写"Header: `Authorization: Bearer <key>` 或 `x-api-key: <key>`"，但 `AuthMiddleware`（core.py 584-589）只解析 `Authorization`，`grep x-api-key` 在 src 中无任何命中。Anthropic 官方客户端默认发 `x-api-key`，这些客户端会 401。**要么实现 `x-api-key`，要么修正文档。**

### P2 — 低危

**P2-1 `RateLimitMiddleware` 的 IP 兜底键有内存放大风险**

`_get_rate_limit_key`（core.py 497-507）：无 Bearer 且无 `api_key` query 时用 `request.client.host`。攻击者随机化源 IP（NAT/代理下）即可让 `self._requests` dict 无限增长（`max_keys=10000` + 5 分钟清理只是缓解，不是根因）。配合 `AuthMiddleware` 强制要求 token，实际影响有限，但"匿名 IP 兜底"这条路径在 `/health` 之外没有意义，可考虑直接拒绝匿名。

**P2-2 测试套件极慢（629 用例跑了 26+ 分钟仍未结束，被我手动终止）**

`tests/test_pipeline_base.py` 等用 `patch(..._shared.call_llm)`，但部分用例（如 `test_call_llm_*`）走到真实 `exponential_backoff` 的 `asyncio.sleep`。建议：统一 monkeypatch 掉 `exponential_backoff`（已有用例这么做，部分漏了）。这是 100% 覆盖率目标下的测试基建问题，不是功能 bug，但会拖垮 CI。

**P2-3 `dedup` 请求去重缓存对"非幂等"场景有风险**

`chat_completions`（core.py 772-793）对非流式请求做 sha256 去重 + 5 分钟 TTL。`_generate_request_id` 把 `api_key_id` 打进 hash（避免跨租户泄漏，这点做得对），但对相同 payload 的**重试**会直接命中旧结果。若上游是非确定性模型或用户期望"再跑一次"，结果被静默复用。建议在 body 支持显式 `no_cache`/`force` 开关。

---

## 5. 设计亮点（值得保留）

- **策略只做"选择"不做"调用"**（`BaseStrategy` 抽象），`call_llm` 独立函数，单测无需实例化 strategy，mock 边界清晰。
- **`PipelineEngine` 用 `db_factory` 而非 `db` 实例**，避免长生命周期下连接失效——`core.py` `_get_engine` 传 `_get_db` 函数引用，符合设计审查结论。
- **fallback 三重防护**：`_visited` 环检测 + `_fallback_depth>3` 限深 + `ConfigurationError` 不 fallback（engine.py 67-94），跨策略类型降级安全。
- **去重 cache key 含 `api_key_id`**，杜绝跨租户缓存泄漏。
- **DB 迁移模式统一**（`SELECT col ... LIMIT 1` 失败则 `ALTER TABLE`，`db.py:205-227`），旧库无损升级。

---

## 6. 工作流升级路线图建议

基于现状，"升级到 LLM 工作流"的最小推进序列：

1. **先修 P0-1**（收敛全局缓存 + Admin 写后 `invalidate_endpoint_cache`）——流式迁移的地基。
2. **修 P1-2 / P1-4**（一行 import + `x-api-key` 或文档）——消除悬空注解与文档不一致。
3. **P3 流式迁移**：`_stream_common` 从 `GroupRouter` 切到 `PipelineEngine.route(stream=True)`，补流式回归测试（即 P1-4 遗留的 IP-3）。
4. **P4/P5 Admin**：`create/update_group` 透传 `type`/`params` + 新增 `GET /admin/strategies`；同时解决 P1-1 的"PATCH 覆盖策略类型"坑。
5. **P6 工作流核心**：`LangGraphStrategy`，`params` 承载 `entry`/`nodes`/`edges`（design.md 4.4 已给出 schema 与 `ast.parse` 安全求值方案），`pyproject` 加 `[pipeline]` optional-deps。
6. **P7 清理**：`GroupRouter` 标 deprecated，删除 router.py 中被 `_shared.py` 复制的全局状态。

> 按 AGENTS.md 的"决策阶梯"，第 1/2 步是纯删除/收敛，第 3 步是等价迁移（行为不变），风险低；第 5 步才是真正引入"工作流"能力，建议在前 4 步稳定后再动，避免一次性大改。

---

## 7. 决策与落地（2026-09-10 评审）

原 4 个待确认项，产品/研发已拍板：

| 项 | 决策 | 落地 |
|----|------|------|
| 1. `x-api-key` 支持 | **支持** | ✅ 已实现：`core.py` `AuthMiddleware` 解析 `x-api-key` 头（优先于 `Authorization`），Anthropic/Google 原生客户端开箱即用。新增 3 个测试（valid / invalid / precedence）于 `test_core_endpoints.py` |
| 2. 去重开关 | **不用** | ➡️ 保持现状，非流式去重 cache 不动 |
| 3. langgraph 依赖 | **hard 依赖** | ✅ 已改：`pyproject.toml` 把 `langgraph>=0.2.0` 放入 `dependencies`（原设计为 optional），P6 策略不再"未安装则跳过" |
| 4. 测试提速 | **可以** | ➡️ 已知问题，本轮未专门处理（见 P2-2）；CI 上限待定 |

### 本轮已完成的改动

| 文件 | 改动 |
|------|------|
| `src/botflow/core.py` | `AuthMiddleware` 支持 `x-api-key` 头（优先解析），401 提示文案更新 |
| `tests/test_core_endpoints.py` | 新增 `test_auth_middleware_x_api_key_{valid,invalid,precedence}` 3 个用例 |
| `pyproject.toml` | `langgraph>=0.2.0` 升为 hard 依赖 |

### 尚未处理（下一轮）

- **P0-1 双路由全局缓存收敛**（router.py vs pipeline/_shared.py）+ Admin 写后调 `invalidate_endpoint_cache` —— 进 P3 前必须先做
- P1-1 Admin group CRUD 透传 type/params + 修 `update_group_raw` 的"PATCH 覆盖策略类型"坑
- P1-2 `auth.py` 补 `HTTPAuthorizationCredentials` import（一行）
- P3 流式路径迁移到 PipelineEngine（补流式回归测试）
- P6 LangGraphStrategy 实现（`params` 承载图定义）
