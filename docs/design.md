# botflow AI 中间件平台 - 设计文档

> 版本: 0.1.0
> 最后更新: 2026-07-03

---

## 项目概述

botflow 是一个 **AI 中间件平台**，提供三大核心能力：

1. **LLM Proxy** - 统一 LLM 网关，模型分组 + 权重路由 + 错误容错
2. **MemWiki** - 基于文件的自主维护知识库（MCP 工具接口）
3. **IM Bridge** - 多平台 IM 统一接入

---

## 整体架构

```
┌─────────────────────────────────────────────────────┐
│                     botflow                          │
│                                                      │
│  ┌──────────────┐  ┌──────────┐  ┌───────────────┐  │
│  │   LLM Proxy   │  │  MemWiki  │  │   IM Bridge   │  │
│   │  (Phase 1)    │  │ (Phase 2) │  │  (Phase 3)    │  │
│  └──────┬───────┘  └─────┬────┘  └───────┬───────┘  │
│         │                │                │          │
│         ▼                ▼                ▼          │
│  ┌──────────────────────────────────────────────┐   │
│  │           Core Infrastructure                 │   │
│  │   Config / Logging / Storage / MCP Server     │   │
│  └──────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────┘
```

---

## Workspace 管理

botflow 通过统一的 **Workspace** 概念管理全部运行时数据，确保 llm-proxy、mem-wiki、im-bridge 三个模块的数据隔离与共享规则清晰。

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
│                                #   - im_sessions（Phase 3）
├── logs/                        # 日志文件（按模块 + 日期轮转）
│   ├── proxy-2026-07-03.log
│   ├── im-2026-07-03.log
│   └── agent.log                # Memory Agent 运行日志（持续追加）
├── MemWiki/                     # MemWiki OKF 知识包（运行时）
│   ├── index.md                #   目录索引（自动生成）
│   ├── log.md                   #   变更日志（自动生成）
│   ├── sources/                #   learn 摄取的原始材料摘要
│   ├── concepts/               #   精炼知识概念
│   ├── entities/               #   人物/组织/项目
│   └── syntheses/              #   research 保存的分析结果
├── workspace/                   # 用户自定义工作区（可扩展挂载点）
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
| mem-wiki | `{workspace}/MemWiki/` | MCP 工具接口 |
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

### MCP 元工具架构

外部 MCP 客户端只看到 **3 个元工具**，所有内部操作通过 `tool_call` 统一调用：

```
┌──────────────┐    MCP Protocol    ┌───────────────────┐
│  MCP Client   │◄──────────────────►│  MCP Server       │
│  (Cursor/etc) │                    │  (FastMCP)        │
└──────────────┘                    │  对外 3 个工具:    │
                                    │  - tool_search     │  ← BM25 关键词搜索
                                    │  - tool_describe   │  ← 查看工具参数定义
                                    │  - tool_call       │  ← 调用任意内部工具
                                    └────────┬──────────┘
                                             │
                                    ┌────────▼──────────┐
                                    │  ToolRegistry      │
                                    │  (BM25 索引)       │
                                    │                    │
                                    │  内部工具:          │
                                    │  - Provider CRUD   │
                                    │  - Model CRUD      │
                                    │  - Group CRUD      │
                                    │  - Stats 查询      │
                                    └───────────────────┘
```

**技术实现**:
- `ToolRegistry` + `SimpleBM25` 实现零依赖的 BM25 搜索（`registry.py`）
- 工具在应用启动时通过 `register_manager_tools(registry, db)` 和 `register_stats_tools(registry, db)` 注册到内部注册表
- 外部 MCP 客户端只看到 `tool_search` / `tool_describe` / `tool_call` 三个元工具

**调用流程**:
```
1. tool_search("provider")           → 找到 create_provider 等工具
2. tool_describe("create_provider")  → 查看参数定义
3. tool_call("create_provider", {name: "my-openai", ...})  → 执行
```

### 模块划分

| 模块 | 文件 | 职责 |
|------|------|------|
| 核心服务 | `src/botflow/core.py` | FastAPI 主服务 + MCP 服务编排（OpenAI/Anthropic 双协议） |
| 模型分组路由 | `src/botflow/router.py` | 模型分组 + 权重选择 + Fallback + 冷却 |
| 供应商适配基类 | `src/botflow/providers/base.py` | 统一供应商接口 |
| OpenAI 适配 | `src/botflow/providers/openai_compat.py` | OpenAI / Azure / vLLM / Ollama（使用 openai SDK） |
| Anthropic 适配 | `src/botflow/providers/anthropic_provider.py` | Anthropic Claude（使用 anthropic SDK） |
| Google 适配 | `src/botflow/providers/google_provider.py` | Google Gemini（使用 google-genai SDK） |
| 鉴权中间件 | `src/botflow/auth.py` | LLM-Key / MCP-Key 鉴权 |
| 数据库存储 | `src/botflow/storage/db.py` | SQLite 统一数据库操作 |
| 存储模型 | `src/botflow/storage/models.py` | Pydantic 数据模型定义 |
| 存储清理 | `src/botflow/storage/cleanup.py` | 半年数据自动清理 |
| MCP 管理服务 | `src/botflow/mcp/manager.py` | Provider/Group/Model CRUD 管理（注册到 ToolRegistry） |
| MCP 统计服务 | `src/botflow/mcp/stats.py` | 统计查询服务（注册到 ToolRegistry） |
| MCP 工具注册表 | `src/botflow/mcp/registry.py` | ToolRegistry + SimpleBM25（元工具核心） |
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
    name            TEXT UNIQUE NOT NULL,  -- 供应商名称: openai / anthropic / moonshot / dashscope / openai_compat
    provider_type   TEXT NOT NULL,         -- 类型: openai_compat / anthropic / openai / moonshot / dashscope
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
    context_window  INTEGER DEFAULT 0,    -- 上下文窗口大小（tokens）
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
    request_body    TEXT,                  -- 原始请求（raw JSON）
    response_body   TEXT,                  -- 原始响应（raw JSON）
    status          TEXT,                  -- success / failed / fallback
    duration_ms     INTEGER,               -- 调用耗时（毫秒）
    prompt_tokens   INTEGER,               -- 输入 tokens
    completion_tokens INTEGER,             -- 输出 tokens
    cache_tokens    INTEGER DEFAULT 0,     -- 缓存命中 tokens
    total_tokens    INTEGER,               -- 总 tokens
    tool_calls      TEXT,                  -- Tool call 记录（JSON 数组）
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

### MCP 元工具接口

外部 MCP 客户端只暴露 3 个元工具：

| 元工具 | 参数 | 返回 | 说明 |
|--------|------|------|------|
| `tool_search` | `query: str` | `{results: [{name, description, score}]}` | BM25 搜索可用工具 |
| `tool_describe` | `tool_name: str` | `{name, description, parameters}` | 查看工具参数定义 |
| `tool_call` | `tool_name: str, arguments?: dict` | 工具返回值或 `{error}` | 调用任意内部工具 |

**调用流程**:
```
tool_search("provider")           → [{name: "create_provider", ...}, ...]
tool_describe("create_provider")  → {name, description, parameters: {...}}
tool_call("create_provider", {name: "my-openai", ...})  → {"id": 1, ...}
```

### 内部工具列表（通过 tool_call 调用）

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
| `list_models` | - | `{models: [{id, name, provider, is_enabled}]}` | 列出所有可用模型 |

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
| `add_model_to_group` | `group_id`, `model_id`, `weight` | `{id, group_id, model_id, group_name, model_name, weight}` | 添加模型到分组 |
| `remove_model_from_group` | `group_id`, `model_id` | `{deleted}` | 从分组移除模型 |
| `update_model_weight` | `group_id`, `model_id`, `weight` | `{updated}` | 修改模型在分组中的权重 |

**统计查询**:

| 工具 | 参数 | 返回 | 说明 |
|------|------|------|------|
| `query_model_stats` | `model_name` | `{total_calls, success, failed, tokens, avg_duration_ms}` | 模型调用统计 |
| `query_group_stats` | `group_name` | `{total_calls, ...}` | 分组调用统计 |
| `query_call_logs` | `status?`, `limit?`, `offset?` | `{total, offset, items: [...]}` | 调用记录列表 |
| `query_cost_summary` | `days?` (默认 30) | `{items: [...]}` | 费用汇总 |

#### 使用流程示例

```
1. 搜索工具:      tool_search("provider")
2. 查看参数:      tool_describe("create_provider")
3. 创建 Provider: tool_call("create_provider", {name: "my-openai", provider_type: "openai_compat", api_key: "${MY_KEY}"})
4. 创建 Model:    tool_call("create_model", {name: "gpt-4o", provider_id: 1})
5. 创建 Group:    tool_call("create_group", {name: "fast-group", description: "快速响应组"})
6. 添加模型:      tool_call("add_model_to_group", {group_id: 1, model_id: 1, weight: 5})
7. 查看分组:      tool_call("get_group", {id: 1})
8. 上线使用:      客户端直接 POST /v1/chat/completions 指定 model=fast-group
```

---

## 技术栈

- **语言**: Python >=3.13
- **Web 框架**: FastAPI + Uvicorn
- **配置**: Pydantic Settings + `{workspace}/.env`（数据库存储模型配置）
- **存储**: SQLite (aiosqlite) — LLM Proxy phase 1 数据
            MemWiki 使用纯文件系统（markdown 文件，不依赖 SQLite）
- **协议**: MCP (Model Context Protocol)
- **Agent 框架**: LangChain + LangGraph（create_react_agent）
- **构建**: uv_build
- **LLM 客户端**: 官方 SDK（openai / anthropic / google-genai）

---

### 文件结构（Phase 1 完成后）

```
d:\src\botflow\
├── pyproject.toml
├── .python-version
├── .qoder/
│   └── skills/
│       └── botflow-guide/          # 使用指南 Skill
│           └── SKILL.md
├── docs/
│   ├── design.md                   # 本设计文档
│   ├── llm-wiki-agent.md           # llm-wiki-agent 项目分析参考
│   └── MemWiki/
│       └── okf-spec.md             # Open Knowledge Format 规范（Agent 遵循）
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
│   │   ├── registry.py               # ToolRegistry + SimpleBM25（元工具核心）
│   │   ├── server.py                 # FastMCP 工厂（3 个元工具）
│   │   ├── manager.py               # Provider/Group/Model CRUD 管理
│   │   └── stats.py                # 统计查询
│   ├── storage\                    # 数据库
│       ├── __init__.py
│       ├── db.py                   # SQLite 统一数据库操作
│       ├── models.py               # Pydantic 数据模型
│       └── cleanup.py              # 半年数据自动清理
│   └── wiki\                       # [Phase 2] MemWiki 模块（待实现）
│       ├── __init__.py
│       ├── agent.py                # Memory Agent 核心（LangChain create_react_agent）
│       ├── types.py                # BotflowLLM（BaseChatModel 子类）
│       ├── skills.py               # 5 套 system prompt 模板
│       ├── tools_impl.py           # Agent 工具（LangChain @tool + 路径安全）
│       ├── tools.py                # 5 个 MCP Tools（thin wrapper）
│       └── dream.py                # Dream 后台巡检任务
```

---

## Phase 2: MemWiki — 自主维护知识库

### 三层架构

```
MCP 客户端
    │
    ▼
┌───────────────────────────────────────────────┐
│  MCP Tools  (wiki/tools.py)                   │
│  thin wrapper：接收参数 → 选择 system_prompt   │
│  → 调用 Memory Agent                          │
└───────────────┬───────────────────────────────┘
                │
                ▼
┌───────────────────────────────────────────────┐
│  Memory Agent  (wiki/agent.py)                │
│  LangChain create_react_agent（LangGraph）     │
│  集成 botflow LLM Provider + Wiki Tools       │
│                                               │
│  Agent 工具：                                  │
│  read_file / write_file / ripgrep / glob      │
│  / call_llm                                   │
└───────────────┬───────────────────────────────┘
                │
                ▼
┌───────────────────────────────────────────────┐
│  Wiki Skills  (wiki/skills.py)                │
│  system prompt 模板，每个 MCP 方法对应一套     │
│  指导 Agent 如何使用工具完成任务                │
└───────────────────────────────────────────────┘

独立后台任务（非 agent）：
  Dream  (wiki/dream.py)  ← 每 24h 巡检
```

### 设计理念

MemWiki 是一个基于**纯文件系统**的自主维护知识库，采用三层架构：

- **MCP Tools**：thin wrapper，只做参数组装和 system_prompt 选择
- **Memory Agent**：基于 LangChain create_react_agent，使用 fast 模型，通过 ReAct 循环自主完成任务
- **Wiki Skills**：system prompt 模板，定义每个工具的行为逻辑
- **存即 OKF bundle**：每个 markdown 文件本身就是 OKF 合规的知识包
- **纯文件存储**：不依赖 SQLite，所有内容为 markdown 文件 + YAML frontmatter
- **Obsidian 兼容**：可直接在 Obsidian 中打开浏览

### 路径安全

所有文件操作使用 **URI 风格相对路径**，禁止使用 `./` 或 `../`：

```
合法：  concepts/rag.md, sources/paper.md, index.md
非法：  ./concepts/rag.md, ../data/botflow.db, /etc/passwd
```

- Agent 工具内部将 URI 路径拼接为 `{wiki_dir}/{path}`
- 拼接后做 `resolve()` 校验，确保最终路径仍在 `wiki_dir` 内
- 校验失败拒绝执行，返回 `PathTraversalError`

### 知识包结构

```
{workspace}/MemWiki/          ← OKF 合规的知识包
├── index.md                   ← 所有文件的目录索引（自动生成）
├── log.md                     ← 变更日志（自动生成）
├── sources/                   ← learn 摄取的原始材料摘要
├── concepts/                  ← 精炼知识概念
├── entities/                  ← 人物/组织/项目
└── syntheses/                 ← research 保存的分析结果
```

### 模块划分

| 模块 | 文件 | 职责 |
|------|------|------|
| Memory Agent | `src/botflow/wiki/agent.py` | LangChain create_react_agent + LLM Provider 桥接 |
| Wiki Skills | `src/botflow/wiki/skills.py` | 5 套 system prompt 模板 |
| Agent 工具 | `src/botflow/wiki/tools_impl.py` | 文件读写 / ripgrep / glob / LLM（含路径安全） |
| MCP Tools | `src/botflow/wiki/tools.py` | 5 个 thin wrapper（remember/recall/query/learn/research） |
| 后台巡检 | `src/botflow/wiki/dream.py` | 孤立页/断链/过时检测（独立，非 agent） |

### 文件格式（OKF 标准）

每个 .md 文件使用 YAML frontmatter：

```markdown
---
type: concept                    # concept / source / entity / synthesis
title: RAG 检索增强生成
description: 检索增强生成的核心流程与组件
tags: [rag, llm, retrieval]
timestamp: 2026-07-03T10:00:00Z
source_url: https://...
---

## 概述

RAG 是一种结合检索与生成的架构...

## 相关概念

- [[向量数据库]]
- [[Embedding 模型]]
```

`[[WikiLink]]` 语法用于概念间交叉引用（兼容 Obsidian）。

### 目录组织规则

| 类型 | 目录 | 文件名 | 例 |
|------|------|--------|----|
| source | `sources/` | `{slug}.md` | `sources/attention-paper.md` |
| concept | `concepts/` | `{slug}.md` | `concepts/rag.md` |
| entity | `entities/` | `{TitleCase}.md` | `entities/OpenAI.md` |
| synthesis | `syntheses/` | `{slug}.md` | `syntheses/rag-vs-vector.md` |

- 文件名：source/concept/synthesis 用 kebab-case，entity 用 TitleCase
- `index.md` 和 `log.md` 为保留文件名
- `index.md` 格式：按类型分节的链接列表，每项含一句话描述
- `log.md` 格式：`## [YYYY-MM-DD] operation | title`

### MCP 工具（5 个 thin wrapper）

| 工具 | 参数 | 说明 |
|------|------|------|
| `remember` | `title`, `content`, `type?`, `description?`, `tags?`, `source_url?` | 写入精炼知识到 concepts/ |
| `recall` | `path?` / `title?` / `tag?` / `type?` | 按条件钻取详情 |
| `query` | `query`, `limit?`, `type?` | 全文搜索 |
| `learn` | `url?` / `file_path?` / `content?`, `type?`, `tags?` | 摄取原始材料（MarkItDown 转换） |
| `research` | `topic`, `model_group?` | LLM 调研并写入 syntheses/ |

### Memory Agent

基于 LangChain + LangGraph 的 `create_react_agent` 实现，使用 LLM Proxy 的 fast 模型组（如 gpt-4o-mini）。

#### 实现架构

```
src/botflow/wiki/
├── agent.py          # create_react_agent 封装 + LLM Provider 桥接
├── tools_impl.py     # LangChain @tool 装饰器定义的 Agent 工具
├── skills.py         # system prompt 模板（注入 agent prompt）
└── types.py          # BotflowLLM（BaseChatModel 子类）
```

#### BotflowLLM 桥接层

```python
class BotflowLLM(BaseChatModel):
    """将 botflow Provider 系统桥接为 LangChain BaseChatModel"""
    model_group: str  # fast model group name
    router: GroupRouter

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        # 同步调用 router.route() 获取 ChatCompletionResponse
        ...

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        # 异步调用 router.route()
        ...
```

#### Agent 构建

```python
from langgraph.prebuilt import create_react_agent
from botflow.wiki.agent import BotflowLLM
from botflow.wiki.tools_impl import wiki_tools
from botflow.wiki.skills import SKILLS

llm = BotflowLLM(model_group="fast", router=GroupRouter())

agent = create_react_agent(
    model=llm,
    tools=wiki_tools,
    prompt=SKILLS[skill_name],  # 注入 system prompt
    checkpointer=...,           # 可选：MemorySaver 用于对话历史
    max_iterations=10,
)

# 调用
result = agent.invoke({"messages": [HumanMessage(content=user_args)]})
```

#### Agent 工具集（LangChain @tool）

| 工具 | 说明 |
|------|------|
| `read_file(path)` | 读取 MemWiki 文件内容 |
| `write_file(path, content)` | 写入 MemWiki 文件（自动创建父目录） |
| `ripgrep(pattern, path?)` | ripgrep 搜索 wiki 目录 |
| `glob(pattern)` | glob 模式查找文件 |
| `call_llm(messages, model_group?)` | 调用 LLM Proxy（用于 research/learn） |

### Wiki Skills

每个 MCP 方法对应一套 system prompt，注入到 Agent 的 system message 中。

#### 共享前缀（所有 skill 共用）

```
你是 MemWiki 知识管理助手。你负责维护一个基于纯文件系统的知识库。

## 知识库路径
{wiki_dir}

## OKF 格式规范
在写入/读取 wiki 文件时，必须遵循 OKF 规范（见 okf-spec.md）。

## 页面格式
所有 wiki 页面使用 YAML frontmatter：
---
type: source | concept | entity | synthesis
title: "页面标题"
description: 一句话描述
tags: [tag1, tag2]
timestamp: YYYY-MM-DDTHH:MM:SSZ
---

## 目录结构
sources/    — learn 摄取的原始材料摘要（一源一页）
concepts/   — 精炼知识概念
entities/   — 人物/组织/项目
syntheses/  — research 保存的分析结果

## 命名规范
- source/concept/synthesis: kebab-case（如 attention-paper.md）
- entity: TitleCase（如 OpenAI.md）

## 交叉引用
使用 [[PageName]] wiki 链接语法关联页面。

## 路径规则
- 所有文件路径使用 URI 风格的相对路径（如 concepts/rag.md）
- 禁止使用 ./ 或 ../ 等相对路径字符
- 路径只能在 MemWiki 目录内操作

## 可用工具
- read_file(path): 读取文件
- write_file(path, content): 写入文件
- ripgrep(pattern, path?): 全文搜索
- glob(pattern): 模式查找文件
- call_llm(messages, model_group?): 调用 LLM
```

#### remember skill

```
{共享前缀}

你的任务：将用户提供的知识写入 wiki。

## 步骤
1. slug = title 转 kebab-case
2. 组装 YAML frontmatter（type 默认 concept，除非用户指定）
3. write_file("concepts/{slug}.md", frontmatter + body)
4. 更新 index.md：在 Concepts 节追加条目
5. 追加 log.md：`## [YYYY-MM-DD] remember | {title}`
6. 返回：已写入 {path}，标题：{title}
```

#### recall skill

```
{共享前缀}

你的任务：按条件从 wiki 钻取详情。

## 步骤（按参数决定路径）
- 有 path → read_file(path)
- 有 title → ripgrep(title) 找到文件 → read_file
- 有 tag → ripgrep(tag) 找到文件 → read_file
- 有 type → glob("{type}s/*.md") → 逐个 read_file
- 返回：文件完整内容 + frontmatter
```

#### query skill

```
{共享前缀}

你的任务：全文搜索 wiki，返回匹配结果。

## 步骤
1. ripgrep(pattern=query) 在 wiki 目录搜索
2. 对每个匹配文件，read_file 提取 frontmatter（title/description/type）
3. 返回匹配列表（按相关度排序）

## 输出格式
[{path, title, description, type, matched_lines}]
```

#### learn skill

```
{共享前缀}

你的任务：摄取原始材料（URL/文件/文本）到 wiki。

## 步骤
1. 如果是 file_path → read_file 获取内容（非 .md 文件由外层 MarkItDown 转换后传入）
2. slug = 文件名或标题转 kebab-case
3. 组装 YAML frontmatter（type: source，记录 source_url/file_path）
4. write_file("sources/{slug}.md", frontmatter + 摘要)
5. 更新 index.md：在 Sources 节追加条目
6. 追加 log.md：`## [YYYY-MM-DD] learn | {title}`
7. 可选：用 call_llm 从 source 中提取关键概念 → 用 remember 写入 concepts/
8. 返回：已写入 {path}，标题：{title}
```

#### research skill

```
{共享前缀}

你的任务：LLM 驱动的调研，生成分析并写入 wiki。

## 步骤
1. ripgrep(topic) 搜索 wiki 中已有相关内容
2. 将已有内容 + topic 组装为 prompt → call_llm 生成分析
3. slug = topic 转 kebab-case
4. 组装 YAML frontmatter（type: synthesis）
5. write_file("syntheses/{slug}.md", frontmatter + 分析内容）
6. 更新 index.md：在 Syntheses 节追加条目
7. 追加 log.md：`## [YYYY-MM-DD] research | {topic}`
8. 返回：已写入 {path}，标题：{topic}，摘要：{summary}
```

#### index.md 格式

```markdown
# MemWiki Index

## Sources
- [Source Title](sources/slug.md) — 一句话摘要

## Concepts
- [Concept Name](concepts/slug.md) — 一句话描述

## Entities
- [Entity Name](entities/EntityName.md) — 一句话描述

## Syntheses
- [Analysis Title](syntheses/slug.md) — 回答的问题
```

#### log.md 格式

```
## [YYYY-MM-DD] operation | title
```

Operations: `remember`, `recall`, `query`, `learn`, `research`

### Dream 后台任务

独立后台任务（非 agent），在 lifespan 中启动，每 24 小时运行：

```
1. 孤立页面检查：读取所有 .md 文件，收集 [[links]] → 找无入链页面
2. 过时检查：解析 frontmatter timestamp → 90 天未更新标记
3. 断链检查：[[Link]] 指向的文件不存在 → 报告
4. 刷新 index.md：重新扫描目录生成最新索引
5. 追加 log.md：记录本次 dream 运行摘要
```

### 注册到核心服务

```python
from botflow.wiki.agent import MemoryAgent
from botflow.wiki.tools import register_tools
from botflow.wiki.dream import start_dream_task

# lifespan 中：
wiki_dir = workspace / "MemWiki"
wiki_dir.mkdir(parents=True, exist_ok=True)
agent = MemoryAgent(wiki_dir, model_group="fast")
register_tools(mcp_server, agent)
dream_task = start_dream_task(wiki_dir)
# shutdown:
dream_task.cancel()
try:
    await dream_task
except asyncio.CancelledError:
    pass
```

### 依赖

```toml
dependencies = [
    ...
    "markitdown>=0.1.0",  # 非 .md 文件转换
    "ripgrep>=15.0.0",    # MemWiki Agent 全文搜索
    "langchain-core>=0.3",
    "langgraph>=0.2",     # create_react_agent
]
```

> ripgrep 为 Python pip 包，无需单独安装系统 CLI 工具。

### 对比 llm-wiki-agent

| 维度 | llm-wiki-agent | MemWiki |
|------|---------------|------------|
| 形态 | Coding agent skill | MCP server + Memory Agent + Skills |
| 架构 | 单层（agent 直接调工具） | 三层（MCP Tool → LangChain Agent → Skills + Tools） |
| LLM | botflow provider 系统（fast model）→ LangChain BotflowLLM 桥接 |
| 搜索 | index 关键词 + LLM 选页 + LLM 合成 | **ripgrep** + Agent 自主决策 |
| 存储 | 纯文件 | 纯文件 |
| 跨会话 | git 管理 | git 管理 |
| 路径安全 | 无 | URI 风格路径 + sandbox 校验 |

## Phase 3: IM 对接（规划中）

- 统一 IM Adapter 抽象层
- 国内: 企业微信 / 钉钉 / 飞书
- 海外: Telegram / Discord / Slack
- 数据存储: `{workspace}/data/botflow.db`
