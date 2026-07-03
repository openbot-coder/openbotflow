# botflow AI 中间件平台 - 设计文档

> 版本: 0.1.0
> 最后更新: 2026-07-03

---

## 项目概述

botflow 是一个 **AI 中间件平台**，提供三大核心能力：

1. **LLM Proxy** - 统一 LLM 网关，模型分组 + 权重路由 + 错误容错
2. **LLM-Wiki** - 基于 Memory Agent 的自主维护知识库（MCP 接口）
3. **IM Bridge** - 多平台 IM 统一接入

---

## 整体架构

```
┌─────────────────────────────────────────────────────┐
│                     botflow                          │
│                                                      │
│  ┌──────────────┐  ┌──────────┐  ┌───────────────┐  │
│  │   LLM Proxy   │  │  LLM-Wiki │  │   IM Bridge   │  │
│   │  (Phase 1)    │  │ (Phase 2) │  │  (Phase 3)    │  │
│  └──────┬───────┘  └─────┬────┘  └───────┬───────┘  │
│         │                │                │          │
│         ▼                ▼                ▼          │
│  ┌──────────────────────────────────────────────┐   │
│  │           Core Infrastructure                 │   │
│  │   Config / Logging / Storage / MCP Server     │   │
│  └──────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────┘

---

## Workspace 管理

botflow 通过统一的 **Workspace** 概念管理全部运行时数据，确保 llm-proxy、llm-wiki、im-bridge 三个模块的数据隔离与共享规则清晰。

### 工作空间确定方式

- Workspace 路径通过 **CLI 参数** 指定，不依赖环境变量传递
- 默认路径: `~/.botflow/`
- 可通过 `botflow run --workspace /path/to/workspace` 覆盖
- 所有模块共享同一个 workspace 根目录

### 目录结构

```
{workspace}/                   （默认: ~/.botflow/）
├── .env                        # 环境变量文件（API Key 等敏感信息）
├── data/                       # 运行时持久化数据
│   └── botflow.db              #   统一 SQLite 数据库
│                                #   - providers / models / model_groups
│                                #   - call_logs
│                                #   - wiki_pages（Phase 2）
│                                #   - im_sessions（Phase 3）
├── logs/                       # 日志文件（按模块 + 日期轮转）
│   ├── proxy-2026-07-03.log
│   ├── wiki-2026-07-03.log
│   └── im-2026-07-03.log
└── workspace/                  # 用户自定义工作区（可扩展挂载点）
```

### .env 文件加载

- 启动时自动加载 `{workspace}/.env` 中的环境变量
- 用于注入 API Key 等敏感信息：`OPENAI_API_KEY=sk-xxx`、`ANTHROPIC_API_KEY=sk-xxx`
- 支持 `python-dotenv` 标准格式（`KEY=VALUE`、`# 注释`）
- 运行时不写回 .env 文件，仅读取

#### 配置优先级

| 优先级 | 来源 | 示例 |
|--------|------|------|
| 最高 | CLI 参数 | `--workspace /path --port 8080` |
| 高 | 环境变量 | `BOTFLOW_WORKSPACE=/path` |
| 中 | `.env` 文件 | `OPENAI_API_KEY=sk-xxx` |
| 低 | 代码默认值 | `~/.botflow/` |

> 高优先级覆盖低优先级。API Key 等敏感配置仅在 `.env` 中设置，不以 CLI 参数传递。

### 模块职责边界

| 模块 | 数据存储 | 对外接口 |
|------|----------|----------|
| llm-proxy | `data/botflow.db` | HTTP (OpenAI/Anthropic 兼容) + MCP 管理 + MCP 统计 |
| llm-wiki | `data/botflow.db` | MCP 工具接口 |
| im-bridge | `data/botflow.db` | Webhook + WebSocket |

## CLI 命令

botflow 提供简洁的 CLI 接口，所有运行时管理通过 MCP 工具完成。

### 命令列表

| 命令 | 说明 |
|------|------|
| `botflow run --workspace PATH --host IP --port NUM` | **启动主服务（前台运行）**（FastAPI + MCP）|
| `botflow set llm-key <KEY>` | 设置 LLM 调用的 API Key（存入数据库）|
| `botflow set mcp-key <KEY>` | 设置 MCP 服务鉴权 Key（存入数据库）|

### 命令详述

#### `botflow run` — 启动主服务（前台运行）

启动后前台驻留，同时提供：
- **HTTP 服务**（OpenAI/Anthropic 双协议）— 端口通过 `--port` 指定
- **MCP 服务**（管理 + 统计）— 用于 AI IDE 等 MCP 客户端连接
- MCP 客户端需要提供 `mcp-key` 进行鉴权

```bash
# 示例
botflow run --workspace ~/my-botflow --host 0.0.0.0 --port 8080
```

> 所有子命令（Provider/Group/Model 管理）均通过 **MCP 工具** 实现，CLI 层面保持简洁。

#### `botflow set` — 配置 API Key

- `botflow set llm-key sk-xxx` — 将 LLM 鉴权 Key 存入 `botflow.db` 的配置表
- `botflow set mcp-key mcp-xxx` — 将 MCP 鉴权 Key 存入 `botflow.db` 的配置表
- HTTP 请求需携带 `Authorization: Bearer {llm-key}` 头
- MCP 客户端需携带 `BOTFLOW_MCP_KEY` 进行鉴权

---

## Phase 1: LLM Proxy

### 核心概念

- **模型分组 (Model Group)**: 将一组模型聚合为一个逻辑组，每个模型有权重
- **权重随机选择**: 调用时按权重从分组中随机选取一个模型
- **错误重试 + Fallback**: 调用失败时按策略自动重试或切换到同组其他模型
- **冷却机制 (Cooldown)**: 模型持续失败时自动冷却，避免无效调用
- **SQLite 审计日志**: 每次调用记录完整请求/响应/消耗，保留半年
- **MCP 统计查询**: 对外提供 MCP 接口查询 Token 消耗和调用明细

### 架构设计

```
                                    ┌──────────────────┐
                                    │   Model Group     │
                                    │   Config (DB)     │
                                    └────────┬─────────┘
                                             │
                    ┌────────────────────────┴────────────────────────┐
                    │                                                 │
                    ▼                                                 ▼
    ┌───────────────────────────┐          ┌───────────────────────────┐
    │  OpenAI-compatible        │          │  Anthropic-compatible     │
    │  /v1/chat/completions     │          │  /v1/messages             │
    │  /v1/completions          │          │                           │
    └──────────┬────────────────┘          └──────────┬────────────────┘
               │                                     │
               └────────────────┬────────────────────┘
                                │
                    ┌───────────▼───────────┐
                    │   Protocol Adapter    │
                    │  (统一内部格式)        │
                    └───────────┬───────────┘
                                │
                    ┌───────────▼───────────┐
                    │  Model Group Router   │
                                             │
                                    ┌───────┴────────────┐
                                    │  Weighted Random    │
                                    │  Selection + Retry  │
                                    │  + Fallback + Cool  │
                                    └───────┬────────────┘
                                            │
                    ┌───────────────────────┼───────────────────────┐
                    ▼                       ▼                       ▼
            ┌──────────────┐      ┌──────────────┐       ┌──────────────┐
            │  Provider A   │      │  Provider B   │       │  Provider C   │
            │  (OpenAI)     │      │  (Anthropic)  │       │  (vLLM)       │
            └──────┬───────┘      └──────┬───────┘       └──────┬───────┘
                   │                     │                       │
                   └──────────┬──────────┴───────────┬───────────┘
                              │                      │
                              ▼                      ▼
                     ┌──────────────────────────────────┐
                     │        SQLite Audit Store         │
                     │  (raw request/response + tokens)  │
                     │       保留 6 个月自动清理          │
                     └──────────────────────────────────┘
```

### MCP 统计查询接口

```
┌──────────────┐    MCP Protocol    ┌───────────────────┐
│  MCP Client   │◄──────────────────►│  MCP Stats Server │
│  (Cursor/etc) │                    │  (FastMCP)        │
└──────────────┘                    │  Tools:            │
                                     │  - query_model_stats(model, start, end)
                                     │    → token_usage, call_count, cost
                                     │  - query_group_stats(group, start, end)
                                     │    → group aggregated stats
                                     │  - query_messages(start, end, filters)
                                     │    → message detail list
                                     │  - query_cost_summary(start, end)
                                     │    → cost breakdown by model/group
                                     └───────────────────┘
```

**技术实现**:
- 使用 `FastMCP.sse_app()` 生成 Starlette 应用，挂载到 `/mcp` 路径
- SSE 传输协议，支持长连接和实时事件推送
- 工具在应用启动时通过 `register_manager_tools()` 和 `register_stats_tools()` 注册

### 模块划分

| 模块 | 文件 | 职责 |
|------|------|------|
| 核心服务 | `src/botflow/core.py` | FastAPI 主服务 + MCP 服务编排（OpenAI/Anthropic 双协议） |
| 模型分组路由 | `src/botflow/router.py` | 模型分组 + 权重选择 + Fallback + 冷却 |
| 供应商适配基类 | `src/botflow/providers/base.py` | 统一供应商接口 |
| OpenAI 适配 | `src/botflow/providers/openai_compat.py` | OpenAI / Azure / vLLM / Ollama |
| Anthropic 适配 | `src/botflow/providers/anthropic_provider.py` | Anthropic Claude |
| Google 适配 | `src/botflow/providers/google_provider.py` | Google Gemini |
| 鉴权中间件 | `src/botflow/auth.py` | LLM-Key / MCP-Key 鉴权 |
| 数据库存储 | `src/botflow/storage/db.py` | SQLite 统一数据库操作 |
| 存储模型 | `src/botflow/storage/models.py` | Pydantic 数据模型定义 |
| 存储清理 | `src/botflow/storage/cleanup.py` | 半年数据自动清理 |
| MCP 管理服务 | `src/botflow/mcp/manager.py` | MCP Provider/Group/Model CRUD 管理 |
| MCP 统计服务 | `src/botflow/mcp/stats.py` | MCP 统计查询服务 |
| 前端协议适配 | `src/botflow/protocol_adapter.py` | OpenAI/Anthropic 请求→统一内部格式 |

### 前端协议适配

LLM Proxy 同时支持 **OpenAI 兼容接口** 和 **Anthropic 兼容接口**，前端 SDK 可直接接入：

```
┌──────────┐     OpenAI 格式     ┌──────────────────────────────────────┐
│ OpenAI    │ ──────────────────►│                                      │
│ SDK       │                    │   Protocol Adapter                   │
└──────────┘                    │   ┌──────────────────────────────┐   │
                                 │   │  请求标准化:                   │   │
┌──────────┐     Anthropic 格式  │   │  - model_group 解析            │   │
│ Anthropic │ ──────────────────►│   │  - messages → 统一格式         │   │
│ SDK       │                    │   │  - parameters 规范化           │   │
└──────────┘                    │   └──────────────────────────────┘   │
                                 │            │                        │
                                 │            ▼                        │
                                 │    统一内部请求体                    │
                                 └──────────────────────────────────────┘
```

### API 端点

| 端点 | 协议 | 方法 | 说明 |
|------|------|------|------|
| `POST /v1/chat/completions` | OpenAI | 聊天补全 | 核心代理，按模型分组路由。`stream=true` 时返回 SSE 流式 |
| `POST /v1/completions` | OpenAI | 补全 | 兼容。`stream=true` 时返回 SSE 流式 |
| `POST /v1/embeddings` | OpenAI | 向量 | 兼容 |
| `GET /v1/models` | OpenAI / Anthropic | **可用模型列表** | **返回所有分组及其模型，详见下方** |
| `POST /v1/messages` | Anthropic | 消息 | Anthropic 格式兼容。`stream=true` 时返回 SSE 流式 |
| `POST /v1/messages?stream=true` | Anthropic | 流式消息 | SSE 流式 |

#### /v1/models 接口详述

`GET /v1/models` 同时兼容 OpenAI 和 Anthropic 两种调用方式，返回当前配置中所有模型分组及其模型信息。

**响应格式（OpenAI 兼容）**:

```json
{
  "object": "list",
  "data": [
    {
      "id": "gpt-4o-mini",
      "object": "model",
      "created": 1700000000,
      "owned_by": "openai",
      "group": "fast-group",
      "description": "快速响应模型组",
      "weight": 5
    },
    {
      "id": "claude-3-haiku",
      "object": "model",
      "created": 1700000000,
      "owned_by": "anthropic",
      "group": "fast-group",
      "description": "快速响应模型组",
      "weight": 3
    },
    {
      "id": "gpt-4o",
      "object": "model",
      "created": 1700000000,
      "owned_by": "openai",
      "group": "powerful-group",
      "description": "高能力模型组",
      "weight": 4
    }
  ]
}
```

**响应格式（Anthropic 兼容）**:

```json
{
  "data": [
    {
      "type": "model",
      "id": "gpt-4o-mini@fast-group",
      "display_name": "gpt-4o-mini (fast-group)",
      "created_at": "2026-01-01T00:00:00Z",
      "group": "fast-group"
    }
  ]
}
```

> 两种格式底层使用同一数据源（`data/botflow.db` 中的 providers + models + model_groups 表），只做序列化差异。models 信息**动态从数据库加载**，支持运行时修改即时生效。

### 权重选择算法

采用**加权随机选择（Weighted Random Selection）** 算法：

1. 计算分组内所有启用模型的总权重 `S = sum(weights)`
2. 在 `[0, S)` 范围内生成随机数 `r`
3. 遍历模型列表，累加权重直到 `cumulative >= r`，选中当前模型
4. 权重为 0 的模型不会被选中（参与累积但无实质几率）

```python
# 伪代码
def weighted_random_select(models: list[Model]) -> Model:
    total_weight = sum(m.weight for m in models if m.weight > 0)
    if total_weight == 0:
        raise NoAvailableModelError()
    r = random.uniform(0, total_weight)
    cumulative = 0
    for model in models:
        cumulative += model.weight
        if r < cumulative:
            return model
```

> 选中后跳过冷却中的模型，自动选择下一个权重候选。

#### 边界情况

| 场景 | 处理方式 |
|------|----------|
| 分组内所有模型权重为 0 | 返回 `503 No Available Model`，日志记录 WARNING |
| 分组内所有模型均在冷却中 | 返回 `503 All Models Cooldown`，日志记录冷却模型列表及剩余冷却时间 |
| 单个模型权重为 0 | 不影响其他模型选择，该模型被跳过 |
| 权重为负数 | 视为 0，日志记录 WARNING |

### 调用流程（含错误处理）

```
1. 客户端请求 {model_group: "fast-group"}
2. 按权重从 fast-group 中随机选择一个模型（如 gpt-4o-mini）
3. 检查模型是否在冷却期 → 是则选择下一个
4. 调用 provider
5. 成功 → 记录 SQLite（request/response/tokens/tool_calls）→ 返回
6. 失败，按错误类型处理:
   ├─ 可重试错误（网络超时、429限流、5xx服务器错误）:
   │   a. 记录错误
   │   b. 有重试次数 → 指数退避等待后重试
   │      - 初始等待: 1s
   │      - 倍数: 2x
   │      - 最大等待: 30s
   │      - 公式: wait = min(initial * 2^(attempt-1), max_wait) + jitter
   │   c. 重试用尽 → fallback 到同组下一个未冷却模型
   │
   └─ 不可重试错误（400参数错误、401鉴权失败、404不存在）:
       a. 记录错误
       b. 立即 fallback（不重试）

7. 连续失败达到 cooldown_failure_threshold（默认 3 次）→ 模型进入冷却期
   - 冷却期间跳过该模型的所有选择
   - cooldown_seconds 过后自动恢复
8. 同组所有模型均失败或冷却中 → 返回 503 Service Unavailable
```

#### 错误分类表

| 错误类型 | HTTP 状态码 | 是否重试 | 说明 |
|----------|-------------|----------|------|
| 超时 | timeout | 是 | 网络层超时 |
| 限流 | 429 | 是 | 触发速率限制 |
| 服务端错误 | 500/502/503 | 是 | 上游服务异常 |
| 参数错误 | 400 | 否 | 请求格式错误 |
| 鉴权失败 | 401 | 否 | API Key 无效 |
| 资源不存在 | 404 | 否 | 模型/端点不存在 |
| 上下文过长 | 413 | 否 | 输入超出限制 |

### SQLite 数据模型

`{workspace}/data/botflow.db` 统一存放所有持久化数据，包括供应商配置、模型分组、调用审计日志等。

```sql
-- ==================== 供应商管理 ====================
CREATE TABLE providers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT UNIQUE NOT NULL,  -- 供应商名称: openai / anthropic / google
    provider_type   TEXT NOT NULL,         -- 类型: openai_compat / anthropic / google
    api_key         TEXT NOT NULL,         -- API Key（从 .env 引用或明文）
    base_url        TEXT,                  -- 自定义端点（可选）
    extra_config    TEXT,                  -- 扩展配置（JSON）
    is_enabled      INTEGER DEFAULT 1,    -- 是否启用
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ==================== 模型管理 ====================
CREATE TABLE models (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,         -- 模型名称: gpt-4o / claude-3-opus
    provider_id     INTEGER NOT NULL,      -- 所属供应商
    display_name    TEXT,                  -- 显示名称
    max_retries           INTEGER DEFAULT 3,     -- 最大重试次数
    cooldown_seconds       INTEGER DEFAULT 60,  -- 冷却时间（秒）
    cooldown_failure_threshold INTEGER DEFAULT 3, -- 触发冷却的连续失败次数
    extra_config    TEXT,                  -- 扩展配置（JSON）
    is_enabled      INTEGER DEFAULT 1,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (provider_id) REFERENCES providers(id)
);

-- ==================== 模型分组管理 ====================
CREATE TABLE model_groups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT UNIQUE NOT NULL,  -- 分组名称: fast-group
    description     TEXT,                  -- 分组描述
    is_enabled      INTEGER DEFAULT 1,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ==================== 分组-模型关联（含权重） ====================
CREATE TABLE group_models (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id        INTEGER NOT NULL,      -- 关联分组
    model_id        INTEGER NOT NULL,      -- 关联模型
    weight          INTEGER DEFAULT 1,     -- 权重（越高被选中概率越大）
    is_enabled      INTEGER DEFAULT 1,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (group_id) REFERENCES model_groups(id),
    FOREIGN KEY (model_id) REFERENCES models(id),
    UNIQUE(group_id, model_id)
);

-- ==================== 调用审计日志 ====================
CREATE TABLE call_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id        INTEGER,              -- 使用的模型分组
    model_id        INTEGER,              -- 实际调用的模型
    provider_id     INTEGER,              -- 供应商
    group_name      TEXT,                  -- 冗余: 分组名称（便于查询）
    selected_model  TEXT,                  -- 冗余: 模型名称
    provider_name   TEXT,                  -- 冗余: 供应商名称
    request_body    TEXT,                  -- 原始请求（raw JSON）
    response_body   TEXT,                  -- 原始响应（raw JSON）
    status          TEXT,                  -- success / failed / fallback
    duration_ms     INTEGER,               -- 调用耗时（毫秒）
    prompt_tokens   INTEGER,               -- 输入 tokens
    completion_tokens INTEGER,             -- 输出 tokens
    cache_tokens    INTEGER DEFAULT 0,     -- 缓存命中 tokens（参考LLM返回的 prompt_tokens_details.cached_tokens / cache_read_input_tokens）
    total_tokens    INTEGER,               -- 总 tokens
    tool_calls      TEXT,                  -- Tool call 记录（JSON 数组，记录调用的 tool name、arguments、results）
    cost            REAL,                  -- 估算费用（美元）
    error_message   TEXT,                  -- 错误信息（如有）
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (group_id) REFERENCES model_groups(id),
    FOREIGN KEY (model_id) REFERENCES models(id),
    FOREIGN KEY (provider_id) REFERENCES providers(id)
);

-- 审计日志索引
CREATE INDEX idx_call_logs_created_at ON call_logs(created_at);
CREATE INDEX idx_call_logs_group ON call_logs(group_id, created_at);
CREATE INDEX idx_call_logs_model ON call_logs(model_id, created_at);
CREATE INDEX idx_call_logs_provider ON call_logs(provider_id, created_at);
```

#### 关键设计说明

- **供应商凭证**: `providers.api_key` 直接存储明文值（程序启动时从 `.env` 加载替换占位符），或存储 `${ENV_VAR_NAME}` 格式在运行时解析
- **冗余字段**: `call_logs` 中的 `group_name` / `selected_model` / `provider_name` 为冗余设计，确保审计日志在配置变更后仍可读
- **统一存储**: 所有模块（proxy、wiki、im）共享同一个 `botflow.db`，通过不同表格分区

#### SQLite 性能说明

- **WAL 模式**: 启用 `PRAGMA journal_mode=WAL`，支持读写并发，显著提升写入性能
- **连接管理**: 使用 `aiosqlite` 异步驱动，单连接串行化写入，无连接池开销
- **批量写入**: 高并发场景下 call_logs 采用批量插入 + 异步队列，避免逐条写入的性能开销
- **查询优化**: 统计查询走已建立的索引，大时间范围查询按月分区扫描
- **未来迁移**: 数据层通过 Repository 模式封装（`storage/db.py`），后续可无缝切换至 PostgreSQL 等数据库

### 数据保留策略

- 每 24 小时自动执行一次清理任务
- 删除 `created_at < now() - interval 6 months` 的 `call_logs` 记录
- 清理操作在低峰期执行，删除前记录影响行数到日志

### 日志规范

- **日志级别**: DEBUG / INFO / WARNING / ERROR，通过 `BOTFLOW_LOG_LEVEL` 环境变量配置，默认为 INFO
- **日志格式**: `2026-07-03 10:30:00.123 | botflow.proxy | INFO | 调用完成 model=gpt-4o tokens=150`
- **日志轮转**: 按文件大小（100MB）和时间（每天）轮转，保留 30 天
- **结构化字段**: 每条日志包含 `timestamp`, `module`, `level`, `request_id` 等结构化字段
- **关键监控指标**:
  - 请求总数 / 成功数 / 失败数（按分组 + 模型维度）
  - 平均 / P50 / P99 响应延迟
  - Token 消耗速率（tokens/min）
  - 冷却模型数量

### 并发与限流

- **HTTP 服务并发**: 依赖 Uvicorn 多 worker 模式（`--workers N`），单进程异步事件循环处理数千并发连接
- **SQLite 写入队列**: call_logs 写入通过异步队列串行化，避免 SQLite 并发写入冲突
- **上游 LLM 限流保护**: 可配置全局 RPS（每秒请求数）限制，防止触发供应商 API 限流
- **MCP 连接数**: MCP 服务默认最大 10 个并发客户端连接
- **客户端速率限制**: 基于 IP 的速率限制中间件（默认 100 次/分钟），防止暴力破解和 DoS 攻击

### 安全性

- **传输安全**: 建议在生产环境前置反向代理（Nginx/Caddy）配置 HTTPS/TLS
- **请求鉴权**:
  - HTTP 请求需携带 `Authorization: Bearer {llm-key}` 头
  - MCP 客户端可通过查询参数 (`?api_key=xxx`) 或 HTTP Header (`Authorization: Bearer xxx`) 进行鉴权
- **输入验证**: 使用 Pydantic 模型严格校验所有 API 请求体，拒绝格式非法请求
- **SQL 注入防护**: 全程使用参数化查询 (`?` 占位符)，不拼接 SQL 语句；列名通过白名单验证
- **敏感信息**: API Key 仅存储在 `.env` 文件中，不写入日志；MCP 管理工具输出时自动脱敏
- **时序攻击防护**: 密钥比较使用 `hmac.compare_digest()` 进行常量时间比较，防止通过响应时间差异推断有效密钥
- **CORS 配置**: 通过环境变量 `BOTFLOW_CORS_ORIGINS` 配置允许的来源，生产环境需明确指定可信域名
- **速率限制**: 基于 IP 的内存速率限制器，返回 429 状态码和 `retry_after` 头

#### 安全审计

项目定期进行安全审计，审计报告存放于 `docs/security_audit/` 目录。当前已识别并修复的问题：

| 严重度 | 问题 | 修复状态 |
|--------|------|----------|
| 中危 | 时序攻击漏洞 | ✅ 已修复 |
| 中危 | CORS 配置过于宽松 | ✅ 已修复 |
| 中危 | 缺少速率限制 | ✅ 已修复 |
| 低危 | API 密钥明文存储 | 📋 计划中 |

### MCP 统计查询工具

| 工具 | 参数 | 返回 |
|------|------|------|
| `query_model_stats` | `model_name`, `start_time`, `end_time`, `group_name?` | `{total_calls, success_calls, failed_calls, prompt_tokens, completion_tokens, total_tokens, avg_duration_ms}` |
| `query_group_stats` | `group_name`, `start_time`, `end_time` | `{total_calls, model_stats[...], total_tokens, avg_cost}` |
| `query_messages` | `start_time`, `end_time`, `group_name?`, `model_name?`, `status?`, `page?`, `page_size?` | `{total, page, page_size, items: [{id, group, model, status, tokens, duration, created_at, request_preview, response_preview}]}` |
| `query_cost_summary` | `start_time`, `end_time`, `group_by?` (model/group/day) | `{items: [{name, total_calls, total_tokens, estimated_cost}]}` |

### MCP 管理工具（Provider + 分组管理）

**Provider 管理**:

| 工具 | 参数 | 返回 | 说明 |
|------|------|------|------|
| `create_provider` | `name`, `provider_type`, `api_key`, `base_url?`, `extra_config?` | `{id, name, provider_type}` | 新增 LLM 供应商 |
| `update_provider` | `id`, `api_key?`, `base_url?`, `is_enabled?`, `extra_config?` | `{id, name, updated}` | 更新供应商配置 |
| `delete_provider` | `id` | `{deleted}` | 删除供应商（同时移除关联模型） |
| `list_providers` | - | `{providers: [{id, name, provider_type, model_count, is_enabled}]}` | 列出所有供应商 |
| `get_provider` | `id` | `{id, name, provider_type, base_url, models: [...]}` | 查看供应商详情 |

**模型管理**:

| 工具 | 参数 | 返回 | 说明 |
|------|------|------|------|
| `create_model` | `name`, `provider_id`, `display_name?`, `max_retries?`, `cooldown_seconds?`, `cooldown_failure_threshold?` | `{id, name, provider_id}` | 新增模型 |
| `update_model` | `id`, `name?`, `max_retries?`, `cooldown_seconds?`, `cooldown_failure_threshold?`, `is_enabled?` | `{id, updated}` | 更新模型配置 |
| `delete_model` | `id` | `{deleted}` | 删除模型 |
| `list_models` | `group_name?` | `{models: [{id, name, provider, group, weight, status(active/cooldown)}]}` | 列出所有可用模型 |

**分组管理**:

| 工具 | 参数 | 返回 | 说明 |
|------|------|------|------|
| `create_group` | `name`, `description?` | `{id, name}` | 新增模型分组 |
| `update_group` | `id`, `name?`, `description?`, `is_enabled?` | `{id, updated}` | 更新分组信息 |
| `delete_group` | `id` | `{deleted}` | 删除分组 |
| `list_groups` | - | `{groups: [{id, name, description, model_count, enabled_count}]}` | 列出所有分组 |
| `get_group` | `id` | `{id, name, description, models: [{model_id, name, provider, weight}]}` | 查看分组详情 |

**分组-模型关联**:

| 工具 | 参数 | 返回 | 说明 |
|------|------|------|------|
| `add_model_to_group` | `group_id`, `model_id`, `weight` | `{id, group_id, model_id, weight}` | 添加模型到分组 |
| `remove_model_from_group` | `group_id`, `model_id` | `{deleted}` | 从分组移除模型 |
| `update_model_weight` | `group_id`, `model_id`, `weight` | `{updated}` | 修改模型在分组中的权重 |

#### 管理工具使用流程示例

```
1. 创建 Provider:  create_provider(name="my-openai", provider_type="openai_compat", api_key="${MY_KEY}")
2. 创建 Model:     create_model(name="gpt-4o", provider_id=1, max_retries=3)
3. 创建 Group:     create_group(name="fast-group", description="快速响应组")
4. 添加模型:       add_model_to_group(group_id=1, model_id=1, weight=5)
5. 查询分组:       get_group(id=1)  →  查看分组下所有模型及权重
6. 调整权重:       update_model_weight(group_id=1, model_id=1, weight=3)
7. 上线使用:       客户端直接 POST /v1/chat/completions 指定 model=fast-group
```

---

## 技术栈

- **语言**: Python >=3.13
- **Web 框架**: FastAPI + Uvicorn
- **AI 框架**: LangChain
- **配置**: Pydantic Settings + `{workspace}/.env`（数据库存储模型配置）
- **存储**: SQLite (aiosqlite)
- **协议**: MCP (Model Context Protocol)
- **构建**: uv_build

---

## 文件结构（Phase 1 完成后）

```
d:\src\botflow\
├── pyproject.toml
├── .python-version
├── docs/
│   └── design.md                   # 本设计文档
├── src\botflow\
│   ├── __init__.py                 # CLI 入口
│   ├── cli.py                      # CLI 命令（start / set）
│   ├── config.py                   # 全局配置 + .env 加载
│   ├── workspace.py                # Workspace 路径管理
│   ├── core.py                     # FastAPI 主服务（原 llm_proxy/server.py）
│   ├── router.py                   # 分组路由+权重+冷却（原 llm_proxy/router.py）
│   ├── protocol_adapter.py         # 前端协议适配
│   ├── auth.py                     # LLM-Key / MCP-Key 鉴权
│   ├── common\                     # 通用工具
│   │   ├── __init__.py
│   │   ├── logger.py
│   │   └── exceptions.py
│   ├── providers\                  # 供应商适配
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── openai_compat.py
│   │   ├── anthropic_provider.py
│   │   └── google_provider.py
│   ├── mcp\                        # MCP 服务
│   │   ├── __init__.py
│   │   ├── manager.py              # Provider/Group/Model CRUD 管理
│   │   └── stats.py                # 统计查询
│   └── storage\                    # 数据库
│       ├── __init__.py
│       ├── db.py                   # SQLite 统一数据库操作
│       ├── models.py               # Pydantic 数据模型
│       └── cleanup.py              # 半年数据自动清理
```

---

## Phase 2: LLM-Wiki（规划中）

- 对外提供 MCP 服务
- 工具: `remember`, `learn`, `research`, `query`, `recall`
- 对内: Dream 定时任务（自动巡检 Wiki 库）
- 数据存储: `{workspace}/data/botflow.db`

## Phase 3: IM 对接（规划中）

- 统一 IM Adapter 抽象层
- 国内: 企业微信 / 钉钉 / 飞书
- 海外: Telegram / Discord / Slack
- 数据存储: `{workspace}/data/botflow.db`
