# botflow LLM Proxy — 设计文档

> 版本：3.1.0 | 最后更新：2026-09-17
>
> 本文档已按当前代码（v3.1.0）校对。凡与代码不符处，以代码为准。

## 1. 概述

botflow 是一个单机版 LLM API 聚合代理，运行在 FastAPI 之上，把多个上游 LLM 服务商统一为 OpenAI 兼容接口。核心能力：

- **多 Provider 聚合**：OpenAI 兼容（openai/azure/ollama/vllm/deepseek）、Anthropic、Google
- **策略驱动路由**：分组通过 `model_groups.type` 选择策略（`random_weights` / `round_robin` / `sequential` / `langgraph`）
- **冷却机制**：连续失败达阈值后进入 cooldown，状态持久化并支持重启恢复
- **多 Key 鉴权**：客户端 API Key 系统（sha256 哈希存储，只返回前缀）
- **管理 REST API**：由 `BOTFLOW_ADMIN_KEY` 保护
- **全链路审计日志**：记录每次调用的请求/响应、token 数、耗时、成本
- **每日摘要**：LLM 生成的对话摘要 + gzip 压缩原始会话

### 运行环境

- Python 3.12+（`pyproject.toml` 声明 `requires-python = ">=3.12"`）
- SQLite + aiosqlite（WAL 模式，单文件 `botflow.db`）
- FastAPI + uvicorn
- LangGraph（**硬依赖**，见 `pyproject.toml`）
- 所有时间戳统一使用 UTC（Python 层 + SQLite `datetime('now')`）

---

## 2. 数据模型

### 2.1 Entity 关系

```
providers (1) ──┬── (N) models ── (N) group_models ── (N) model_groups
                │                        ↑ weight ↑        │ type / params
                │                                        └── fallback_group_id (自引用，非 FK)
                └──────────────────────────────────────────► call_logs (provider_id)
api_keys ────► call_logs (api_key_id)
daily_summaries        config (键值配置表)
raw_sessions (gzip blobs)
```

共 **9 张表**：`providers`、`models`、`model_groups`、`group_models`、`call_logs`、`config`、`api_keys`、`daily_summaries`、`raw_sessions`。

### 2.2 Providers 表

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增 |
| name | TEXT NOT NULL UNIQUE | 供应商名称 |
| provider_type | TEXT NOT NULL | SDK 类型：`openai`/`azure`/`ollama`/`vllm`/`deepseek`/`anthropic`/`google` |
| api_key | TEXT NOT NULL '' | 上游 API Key（**明文存储**，见 §10） |
| base_url | TEXT NOT NULL '' | 上游 Base URL |
| extra_config | TEXT NOT NULL '{}' | 扩展配置 JSON |
| is_enabled | INTEGER NOT NULL 1 | 是否启用 |
| created_at / updated_at | TEXT NOT NULL | UTC 时间戳 |

### 2.3 Models 表

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增 |
| name | TEXT NOT NULL | 模型名 |
| provider_id | INTEGER NOT NULL | **FK → providers(id) ON DELETE CASCADE** |
| display_name | TEXT NOT NULL '' | 显示名 |
| api_format | TEXT NOT NULL '' | **SDK 覆盖**：非空时覆盖 provider_type 选择 SDK 类 |
| max_retries | INTEGER (默认 3) | 单端点最大重试次数 |
| cooldown_seconds | INTEGER (默认 60) | cooldown 时长（秒） |
| cooldown_failure_threshold | INTEGER (默认 3) | 触发 cooldown 的连续失败次数 |
| extra_config | TEXT NOT NULL '{}' | 扩展配置，支持 `proxy` |
| is_enabled | INTEGER NOT NULL 1 | 是否启用 |
| context_window | INTEGER (默认 0) | 上下文窗口，0 = 未知不截断 |
| created_at / updated_at | TEXT NOT NULL | UTC 时间戳 |

约束：`UNIQUE(name, provider_id)`

### 2.4 Model Groups 表

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增 |
| name | TEXT NOT NULL UNIQUE | 组名 |
| description | TEXT NOT NULL '' | 描述 |
| **type** | TEXT NOT NULL 'random_weights' | **路由策略名**，见 §4.3 |
| **params** | TEXT NOT NULL '{}' | **策略参数 JSON** |
| is_enabled | INTEGER NOT NULL 1 | 是否启用 |
| fallback_group_id | INTEGER（可空） | **无 FK 约束**，失败时降级到的组 |
| created_at / updated_at | TEXT NOT NULL | UTC 时间戳 |

### 2.5 Group Models 表

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增 |
| group_id | INTEGER NOT NULL | **FK → model_groups(id) ON DELETE CASCADE** |
| model_id | INTEGER NOT NULL | **FK → models(id) ON DELETE CASCADE** |
| weight | REAL NOT NULL 1.0 | 权重 |
| is_enabled | INTEGER NOT NULL 1 | 是否启用 |
| created_at | TEXT NOT NULL | UTC 时间戳 |

约束：`UNIQUE(group_id, model_id)`

### 2.6 Call Logs 表

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增 |
| api_key_id | INTEGER | 客户端 key（可空），**无 FK** |
| group_id / model_id / provider_id | INTEGER | 路由信息，**均无 FK** |
| request_body / response_body | TEXT | 完整请求/响应 JSON（按保留期清空） |
| status | TEXT NOT NULL '' | `success`/`error` 等 |
| error_type / error_message | TEXT | 错误详情 |
| traceback | TEXT | 限长堆栈 |
| request_id | TEXT | 关联重试/流式分片 |
| duration_ms | INTEGER | 耗时毫秒 |
| prompt_tokens / completion_tokens / cache_tokens / total_tokens | INTEGER 默认 0 | Token 用量 |
| tool_calls | TEXT | 工具调用记录 |
| cost | REAL 默认 0.0 | 费用 |
| created_at | TEXT NOT NULL | UTC 时间戳 |

索引：`created_at`、`model_id`、`group_id`、`api_key_id`、`status`

### 2.7 Config 表（键值配置）

| 字段 | 类型 | 说明 |
|------|------|------|
| key | TEXT PK | 配置键 |
| value | TEXT NOT NULL | 配置值 |
| updated_at | TEXT NOT NULL | UTC 时间戳 |

运行时读写的键：`llm_key`（回退密钥）、`cooldown:{group_id}:{model_id}`（冷却状态）、`dedup:*`（请求去重）。

### 2.8 Client API Keys 表

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增 |
| key_hash | TEXT NOT NULL UNIQUE | **sha256 哈希**，原始 key 永不存储 |
| label | TEXT NOT NULL '' | 备注 |
| is_enabled | INTEGER NOT NULL 1 | 是否启用 |
| created_at | TEXT NOT NULL | UTC 时间戳 |

### 2.9 Daily Summaries / Raw Sessions 表

| 表 | 字段 |
|------|------|
| `daily_summaries` | id PK · day TEXT NOT NULL UNIQUE · summary_md TEXT · stats_json TEXT '{}' · created_at |
| `raw_sessions` | id PK · day TEXT NOT NULL UNIQUE · blob BLOB NOT NULL · created_at |

### 2.10 迁移

表结构由 `CREATE TABLE IF NOT EXISTS` 建立，历史增量列通过 `ALTER TABLE` 补齐（已具备幂等检查）：

| 表 | 增补列 |
|---|---|
| models | `context_window`、`api_format` |
| call_logs | `api_key_id`、`error_type`、`traceback`、`request_id` |
| model_groups | `type`、`params` |

---

## 3. API 格式适配层

### 3.1 客户端入口格式（4 种）

| 客户端端点 | 内部格式 |
|------|------|
| `POST /v1/chat/completions` | OpenAI Chat Completions |
| `POST /v1/completions` | OpenAI legacy（`prompt` 兼容） |
| `POST /v1/responses` | OpenAI Responses API |
| `POST /v1/messages` | Anthropic Messages |

格式由**路径**决定，与请求体内容无关。

### 3.2 上游 wire 格式（3 种）

| wire 格式 | Provider 类 | provider_type / api_format 取值 |
|------|------|------|
| OpenAI 兼容（`AsyncOpenAI`） | `OpenAICompatProvider` | `openai` `azure` `ollama` `vllm` `deepseek` |
| Anthropic Messages | `AnthropicProvider` | `anthropic` |
| Google GenAI `generateContent` | `GoogleProvider` | `google` |

注意：`deepseek` 也走 OpenAI 兼容 wire 格式，只是由 `DeepSeekProvider` 做参数差异处理。**7 个 type token → 4 个 Provider 类 → 3 种上游 wire 格式。**

### 3.3 `api_format` 字段语义

`models.api_format` 非空时覆盖 `provider_type` 选择 SDK 类的**每模型覆盖字段**。典型场景：中转站聚合多个厂商模型——一个 provider 连接不同厂商的模型，每个 model 指定自己对应的 SDK 格式。

路由缓存键为 `(provider_id, resolved_type, proxy)`，其中 `resolved_type = api_format if api_format else provider_type`。

### 3.4 鉴权

- `Authorization: Bearer <key>` 或 `x-api-key: <key>`（`/v1/*`）
- 读取顺序：**先 `x-api-key`，再 `Authorization: Bearer`，最后裸 `Authorization`**
- `/admin/*` 只读 `Authorization` 头，用 `secrets.compare_digest` 与 `admin_key` 比对
- `/health` 无鉴权；限流中间件也跳过 `/health`

### 3.5 Stream 模式

`stream: true` 时返回 SSE 流。**流式与非流式共用同一条调用骨架**：

- **驱动层（图外，`core.py`）** 负责分组、生成策略、以及组级降级循环（最多 3 跳 + 环检测）—— 流式与非流式语义一致。
- **执行层（图内，`StateGraph`）** 负责单次「策略执行」：选端点 → 逐端点调用。流式下该节点经 `langgraph.config.get_stream_writer()` 推送 provider chunk，由驱动层完成 SSE 序列化与落库。
- 已推出首个 chunk 之后失败（`stream_started=True`）**不再降级**，只报错 —— 已下发内容无法收回。

> **本节描述目标态**（`docs/pipeline-single-graph-design.md` v2）。现状是「流式路由只做端点选择、`core._stream_common` 自行重试、fallback 分组只尝试一次」，该实现将在阶段一被替换。

---

## 4. 路由引擎

### 4.1 路由流程

```
请求
  │
  ▼
AuthMiddleware（客户端 Key 校验）
  │
  ▼
驱动层（图外，core.py）—— 四步骨架
  │
  ├─ ① 分组：model → group（或默认组）
  ├─ ② 取模板 + 参数：STRATEGY_REGISTRY[group.type] + group.params
  ├─ ③ 生成策略：strategy_cls(params)
  └─ ④ 循环「策略执行 / 失败降级到 backup 组」（最多 3 跳 + 环检测）
         │
         ▼
      StateGraph（图内）—— 单次「策略执行」
         │
         ├─ 端点缓存（TTL，单一事实源在 router.py，_shared.py 仅 re-export）
         ├─ 冷却过滤
         ├─ 策略选择（group.type 决定）
         ├─ 每端点重试（ep.max_retries；retryable = 429/500/502/503/504/timeout）
         └─ Context Window Truncation（若组内最小 context_window > 0）
         │
         ▼
      上游调用
```

> **本节描述目标态**（`docs/pipeline-single-graph-design.md` v2）。现状的 `PipelineEngine.route / route_stream` 与「图内 `resolve_group → load_and_select → try_call → 条件边`」将在阶段一被替换。

### 4.2 Fallback 语义（重要）

**降级由驱动层统一负责**（图外，流式与非流式同一套），失败不保证一定有 fallback。终止条件：

- `fallback_group_id` 为空，或指向的组不存在
- 降级深度 > 3
- 检测到 fallback 环路（`visited_groups`）

**哪些失败允许降级**由图在出口写入的 `recoverable` 标志决定：图内的失败（选端点、逐端点调用、首 chunk 之前）→ 可降级；驱动层的配置错误（未知策略名、`langgraph` 策略被拒）、以及已推出 chunk 之后的失败 → 不可降级。逐条判定见 `docs/pipeline-single-graph-design.md §3.5`。

> **本节描述目标态**（`docs/pipeline-single-graph-design.md` v2）。现状是非流式由图的 `_route_after_call` / `_resolve_group` 承担最多 3 跳、流式由 `core._stream_common` 的 `fallback_attempted` 只降 1 跳 —— 两者将在阶段一合并到驱动层。

### 4.3 策略系统

`STRATEGY_REGISTRY` 中注册的策略（`GET /admin/strategies` 返回的即为这些名字）：

| 策略名 | 类 | 说明 |
|------|------|------|
| `random_weights` | RandomWeightsStrategy | **默认**，按权重随机 |
| `round_robin` | RoundRobinStrategy | 轮询 |
| `sequential` | SequentialStrategy | 按顺序 |
| `langgraph` | LangGraphStrategy | 由 `params` 定义自定义图 |

选择方式：分组的 `type` 字段。策略参数放在分组的 `params` JSON 中。

`langgraph` 策略的 `params` 使用 `nodes` / `edges` / `entry` / `final`，节点形如 `{prompt, group_id}`，边为 `[from, to]` 或 `[from, to, condition]`（条件为子串匹配）。

> `langgraph` 是**硬依赖**，不再有"未安装则静默跳过"的分支。

### 4.4 Cooldown Manager

- 内存中的状态字典，key 为 `(group_id, model_id)` 元组
- 连续失败达 `cooldown_failure_threshold` → 进入 cooldown，持续 `cooldown_seconds`
- **时间源**：内存判断用 `time.monotonic()`；**持久化边界用 `time.time()`**（墙钟），重启恢复时做墙钟→单调时钟转换并跳过已过期项
- **持久化**：DB `config` 表，键 `cooldown:{group_id}:{model_id}`，值为 `{"failures", "cooldown_until"}`
- 后台任务每 5 分钟落盘一次；启动时恢复

### 4.5 Context Window Truncation

当组内任一模型设置了 `context_window > 0` 时，取**最小值**作为截断上限，然后：

1. 估算 token 数（英文约 4 字符/token，CJK 约 2 字符/token）
2. 固定保留全部 `system` 消息
3. **二分查找**能容纳的最近 N 条历史消息
4. 兜底：仅 system / 仅最后一条

> 排序依据是**新近度（recency）**，**不使用 BM25 或任何语义相关性排序**。

---

## 5. 管理 API（Admin REST）

所有 admin 路由以 `/admin` 为前缀，由 `verify_admin_key` 保护（基于 `BOTFLOW_ADMIN_KEY`）。浏览器访问 `/admin/` 得到内置管理面板（HTML）。auth 4 端点（status/setup/login/logout）不受 `verify_admin_key` 保护：setup 凭据为启动时生成的一次性 setup token（明文见服务器 `.setup_token`，0600），`BOTFLOW_ADMIN_KEY` 只用于 Bearer 通道与会话签发期的管理面准入（`verify_admin_key` 双通道之一）。

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/admin/` | 内置管理面板 |
| GET | `/admin/auth/status` | 管理员账号开通状态（免鉴权） |
| POST | `/admin/auth/setup` | 用启动时自动生成的 setup token 开通 / 重置管理账号（明文见服务器 .setup_token，0600；开通成功即销毁） |
| POST | `/admin/auth/login` | 账号密码登录，换取会话 token |
| POST | `/admin/auth/logout` | 注销会话（带任意非空 token 即 200） |
| POST / GET | `/admin/providers` | 创建 / 列出 Provider |
| GET / PATCH / DELETE | `/admin/providers/{id}` | 详情 / 更新 / 删除 |
| POST / GET | `/admin/models` | 创建 / 列出 Model |
| GET / PATCH / DELETE | `/admin/models/{id}` | 详情 / 更新 / 删除 |
| POST / GET | `/admin/groups` | 创建 / 列出 Group |
| GET / PATCH / DELETE | `/admin/groups/{id}` | 详情 / 更新 / 删除 |
| GET | `/admin/groups/{id}/details` | 分组 + 成员模型明细 |
| POST | `/admin/groups/{id}/models` | 添加 Model 到 Group |
| PATCH / DELETE | `/admin/groups/{id}/models/{model_id}` | 调整权重 / 移出 |
| GET | `/admin/strategies` | 列出可用策略名 |
| GET | `/admin/stats/models` | 模型调用统计 |
| GET | `/admin/stats/groups` | 分组调用统计 |
| GET | `/admin/stats/cost` | 成本汇总 |
| GET | `/admin/logs` | 查询调用日志 |
| GET | `/admin/summaries/{day}` | 获取每日摘要 |
| POST / GET | `/admin/apikeys` | 创建 / 列出客户端 Key |
| PATCH / DELETE | `/admin/apikeys/{key_id}` | 启/禁 / 删除 |

> **不存在** `POST /admin/models/sync`。模型同步仅通过 CLI `botflow model sync` 或定时任务触发。

---

## 6. CLI 命令

`--workspace` 是**全局参数，必须放在子命令之前**（`botflow --workspace DIR run`）；放在子命令后会报 `unrecognized arguments`。

```
botflow [--workspace DIR] <command>

run        --host 0.0.0.0 --port 8080 [--config FILE]
stop
restart    --host --port [--config]
status     --port 8080
logs       -n/--lines 50
set <key> <value>  |  get <key>  |  config
cleanup    --days 180
summary    [--day YYYY-MM-DD]

provider   list [--enabled] | get <id> | add <name> --type --api-key --base-url
           | update <id> [--name --api-key --base-url --enabled] | delete <id>
model      list [--enabled] | get <id> | add <name> --provider-id [...]
           | update <id> [...] | delete <id> | sync [--provider-id ID]
group      list [--enabled] | get <id> | add <name> [--description]
           | update <id> [--name --description --enabled --fallback] | delete <id>
           | add-model <id> <model_id> [--weight 1.0] | remove-model <id> <model_id>
           | set-weight <id> <model_id> <weight>
apikey     list | add <key> [--label] | update <id> [--label --enabled true|false]
           | disable <id> | enable <id> | delete <id>
stats      cost [--days 30] | model <id> | group <id> | recent [-n 20]
version
```

> **不存在** `init` 子命令，也**不存在** `service` 子命令。

---

## 7. 配置

通过环境变量（前缀 `BOTFLOW_`）或 workspace 下的 `.env` 文件设置。优先级：CLI 参数 > 环境变量 > `.env` > 默认值。

| 键 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `workspace` | str | `~/.botflow` | Workspace 目录 |
| `host` | str | `0.0.0.0` | 监听地址 |
| `port` | int | `8080` | 监听端口 |
| `llm_key` | str | `""` | 无客户端 Key 时的回退密钥 |
| `admin_key` | str | `""` | Admin API 保护密钥 |
| `api_keys` | str | `""` | 逗号分隔客户端 Key。**声明但当前代码未读取** |
| `log_level` | str | `INFO` | 日志级别 |
| `cors_origins` | str | `*` | CORS 允许来源，为 `*` 时自动禁用 credentials |
| `stream_timeout` | float | `30.0` | 流式等待首个 chunk 的超时秒数 |
| `upstream_semaphore_size` | int | `0` | 每 Provider 上游并发上限，0 = 不限 |
| `call_log_detail_days` | int | `1` | 调用明细保留天数 |
| `raw_session_retention_days` | int | `7` | 原始会话保留天数 |
| `call_logs_retention_days` | int | `180` | call_logs 整行删除天数 |
| `daily_summary_hour` | int | `0` | 每日摘要任务 UTC 小时 |
| `summary_group` | str | `""` | 摘要使用的分组，空 = 默认分组 |
| `model_sync_interval` | int | `60` | 模型同步间隔（分钟，0 = 禁用） |

另有非前缀的 `LLM_KEY` 环境变量：启动时会同步进 DB `config` 表并注册为 `legacy:llm_key`。

---

## 8. 安全

| 措施 | 说明 |
|------|------|
| API Key 哈希 | 客户端 Key 存 sha256，永远不回显明文 |
| Admin Key | `BOTFLOW_ADMIN_KEY` 保护所有 `/admin/*` 路由，比对用 `secrets.compare_digest` |
| CORS | 由 `BOTFLOW_CORS_ORIGINS` 控制；为 `*` 时自动关闭 credentials |
| 速率限制 | `RateLimitMiddleware` 滑动窗口，**硬编码 300 次/分钟**，key 为 Bearer token 或 `?api_key`，跳过 `/health`。**无配置项** |
| WAL 模式 | SQLite WAL，避免锁竞争 |
| 输入校验 | 外部输入通过 Pydantic Body + 手动校验 |
| SQL 注入防护 | 全部参数化查询 |
| Traceback 限长 | 错误堆栈截断后入库 |

---

## 9. 目录结构

```
src/botflow/
├── __init__.py
├── core.py                 # FastAPI 主服务 + 路由注册 + 流式 + 后台任务 + 限流中间件
├── router.py               # 路由基础设施（端点缓存、CooldownManager、weighted_* 工具）
├── protocol_adapter.py     # 协议适配层（客户端 4 种格式 ↔ 内部表示）
├── auth.py                 # 鉴权（客户端 Key + Admin Key）
├── admin_api.py            # REST 管理 API
├── admin_dashboard.py      # /admin/ 内置面板
├── config.py               # pydantic-settings 配置
├── workspace.py            # Workspace 路径管理
├── cli/
│   ├── main.py             # CLI 入口
│   └── service.py          # start/stop/status/logs + PID 文件
├── pipeline/
│   ├── __init__.py         # 导出 STRATEGY_REGISTRY 等
│   ├── base.py             # BaseStrategy / RouteResult / register_strategy
│   ├── strategies.py       # random_weights / round_robin / sequential
│   ├── langgraph_engine.py # LangGraphEngine + StateGraph 节点 + langgraph 策略
│   ├── langgraph_strategy.py
│   ├── engine.py           # PipelineEngine（代理到 LangGraphEngine）
│   └── _shared.py          # call_llm + 缓存 re-export
├── providers/
│   ├── base.py
│   ├── openai_compat.py
│   ├── anthropic_provider.py
│   ├── google_provider.py
│   └── deepseek_provider.py
├── common/
│   ├── exceptions.py
│   ├── logger.py
│   ├── context.py          # token 估算 + 上下文截断
│   └── content_converters.py
└── storage/
    ├── db.py               # SQLite 数据库层（含 ALTER 迁移）
    ├── models.py           # Pydantic 数据模型
    └── daily_summary.py    # 每日摘要 + 保留期清理
```

> 没有 `rate_limit.py`（限流在 `core.py`），没有 `mcp/`（MCP 模块已在早期版本移除）。

---

## 10. 已知问题与技术债

### 10.1 功能性缺陷

| 严重度 | 位置 | 问题 |
|---|---|---|
| ~~高~~ **已修复** | `cli/service.py` | `restart` 先停止服务，再用 `python -m botflow run --workspace ...` 启动。但 ① `src/botflow/` **没有 `__main__.py`**，`python -m botflow` 直接报错；② `--workspace` 被放在子命令 `run` **之后**，argparse 报 `unrecognized arguments`。**结果是服务被停掉且无法自动恢复，函数却仍写入 PID 并返回 `ok: True`**。**已修复**（详见 `docs/tasks/fix-restart_features.md`）：新建 `__main__.py`、`--workspace` 前移到子命令之前、子进程 stderr 追加落 `logs/botflow.err.log`、新增 `startup_grace=2.0` 有界存活校验（秒死不再报成功）、删除基于消息文本的 stop 守卫（无 PID 文件时不再提前放弃）。另：无 PID 文件时 `restart` 的行为由「不干活 + 退出码 1」变为「直接启动服务」 |
| 中 | `config.py` | `api_keys` 字段声明后**从未被读取**，属死配置 |
| 低 | `workspace.py` | 未传 `--workspace` 时实际回退到**当前目录**，与 help 文本宣称的 `~/.botflow` 及 `config.py` 默认值不一致 |
| 低 | `cli/main.py` | `run` 的 argparse 默认值非 `None`，导致 `BOTFLOW_HOST` / `BOTFLOW_PORT` 环境变量总被 CLI 默认值覆盖 |

### 10.2 类型与结构债

| 项目 | 说明 |
|------|------|
| `db.py` 返回类型标注错误 | `create_provider_raw` / `create_model_raw` / `create_group_raw` 标注返回 `Provider`/`Model`/`ModelGroup`，实际返回 `lastrowid`（int）；`create_api_key` 标注 `-> ApiKey` 但返回 `Optional[ApiKey]`。调用方按 int 使用，故为**标注错误而非运行错误**（mypy 32 错的一部分） |
| `db.py` 模块 docstring | 只列了 6 张表，遗漏 `api_keys`、`daily_summaries`、`raw_sessions` |
| 未使用的导入 | `db.py` 导入 `GroupModel` 但未使用 |
| `call_logs` 缺 FK | 关联列均为裸 INTEGER，无外键约束（可能是有意的写入性能取舍） |
| 全局单例 | `_db`、`_config`、`_log_writer`、`_active_db` 分散在多模块 |
| `*_raw` 方法冗余 | `db.py` 中带/不带 `_raw` 后缀的方法并存 |
| Lint/类型基线 | ruff 477 错、ruff format 70/98 文件待重排、mypy 32 错；CI 中这三项以 `continue-on-error` 仅提示 |

### 10.3 仓库卫生

| 项目 | 说明 |
|------|------|
| 缺 `LICENSE` | README 宣称 MIT，但仓库根目录**没有 `LICENSE` 文件**，`pyproject.toml` 也未声明 license 字段 |
| `build-system` 版本钉陈旧 | `uv-build>=0.11.1,<0.12.0` 不含当前 uv（0.12.11），每次构建均告警 |
| 文档时效 | `docs/` 下多份文档为历史快照（详见各文档头部说明） |
