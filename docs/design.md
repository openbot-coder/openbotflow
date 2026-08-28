# botflow LLM Proxy — 设计文档

> 版本：1.1.0 | 最后更新：2026-08-28

## 1. 概述

botflow 是一个轻量级 LLM API 聚合代理，运行在 FastAPI 之上，将多个上游 LLM 服务商统一为 OpenAI 兼容接口。核心能力包括：

- **多 Provider 聚合**：支持 OpenAI、Azure、Anthropic、Google、Ollama、vLLM、DeepSeek 等
- **加权随机路由**：按权重在 Model Group 内随机选模型，失败自动降级
- **冷却机制**：连续失败达到阈值后进入 cooldown，避免雪崩
- **多 Key 鉴权**：客户端 API Key 系统（sha256 哈希存储，只返回前缀）
- **管理 REST API**：通过 `BOTFLOW_ADMIN_KEY` 保护，替代旧版 MCP 管理工具
- **全链路审计日志**：记录每次调用的请求/响应、token 数、耗时、成本
- **每日摘要**：LLM 生成的对话 wiki 摘要 + gzip 压缩原始会话

### 运行环境

- Python 3.13+
- SQLite + aiosqlite（WAL 模式，单文件 `botflow.db`）
- FastAPI + uvicorn
- 所有时间戳统一使用 UTC（Python 层 + SQLite `datetime('now')`）

---

## 2. 数据模型

### 2.1 Entity 关系

```
providers (1) ──┬── (N) models
                │
                └── (N) model_groups (1) ── (N) group_models ── (N) models
                                                  ↑ weight ↑
                                                (enabled)

api_keys (client keys) ──── call_logs (api_key_id nullable)
daily_summaries
raw_sessions (gzip blobs)
```

### 2.2 Providers 表

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增 |
| name | TEXT UNIQUE | 供应商名称，如 "openai" |
| provider_type | TEXT | SDK 类型：`openai`/`azure`/`anthropic`/`google`/`ollama`/`vllm`/`deepseek` |
| api_key | TEXT | 上游 API Key（明文存储） |
| base_url | TEXT | 上游 Base URL |
| extra_config | TEXT JSON | 扩展配置（如 `{"api_version": "2024-02-01"}`） |
| is_enabled | INTEGER | 是否启用 |
| created_at / updated_at | TEXT | UTC 时间戳 |

### 2.3 Models 表

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增 |
| name | TEXT | 模型名，如 "gpt-4o" |
| provider_id | INTEGER FK | 引用 providers |
| display_name | TEXT | 显示名 |
| api_format | TEXT | **SDK 覆盖**：非空时覆盖 provider_type 选择 SDK 类（如 provider 是 openai 但 api_format 为 deepseek） |
| context_window | INTEGER | 上下文窗口大小（0 = 未知，不做截断） |
| max_retries | INTEGER | 最大重试次数 |
| cooldown_seconds | INTEGER | cooldown 时长（秒） |
| cooldown_failure_threshold | INTEGER | 触发 cooldown 的连续失败次数 |
| extra_config | TEXT JSON | 扩展配置，支持 `proxy` 子字段（如 `{"proxy": "http://127.0.0.1:7890"}`） |
| is_enabled | INTEGER | 是否启用 |
| created_at / updated_at | TEXT | UTC 时间戳 |

### 2.4 Model Groups 表

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增 |
| name | TEXT UNIQUE | 组名，如 "default" |
| description | TEXT | 描述 |
| is_enabled | INTEGER | 是否启用 |
| fallback_group_id | INTEGER FK | 失败时降级到的组 |
| created_at / updated_at | TEXT | UTC 时间戳 |

### 2.5 Group Models 关联表

| 字段 | 类型 | 说明 |
|------|------|------|
| group_id + model_id | COMPOSITE PK | 联合主键 |
| weight | REAL | 权重（用于加权随机选择） |
| is_enabled | INTEGER | 是否启用 |
| created_at | TEXT | UTC 时间戳 |

### 2.6 Call Logs 表

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增 |
| api_key_id | INTEGER FK | 客户端 key（可为 NULL） |
| group_id / model_id / provider_id | INTEGER | 路由信息 |
| request_body / response_body | TEXT | 完整请求/响应 JSON |
| status | TEXT | `success`/`error`/`timeout`/`cooldown`/`cancelled` |
| error_type / error_message | TEXT | 错误详情 |
| traceback | TEXT | 限长堆栈 |
| request_id | TEXT | 关联重试/流式分片 |
| duration_ms | INTEGER | 耗时毫秒 |
| prompt_tokens / completion_tokens / cache_tokens / total_tokens | INTEGER | Token 用量 |
| tool_calls | TEXT JSON | 工具调用记录 |
| cost | REAL | 费用 |
| created_at | TEXT | UTC 时间戳 |

### 2.7 Client API Keys 表

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增 |
| key_hash | TEXT UNIQUE | **sha256 哈希**，原始 key 永不存储 |
| label | TEXT | 备注 |
| is_enabled | INTEGER | 是否启用 |
| created_at | TEXT | UTC 时间戳 |

### 2.8 Daily Summaries 表

| 字段 | 类型 | 说明 |
|------|------|------|
| day | TEXT UNIQUE | YYYY-MM-DD |
| summary_md | TEXT | LLM 生成的 wiki 摘要 |
| stats_json | TEXT | 聚合统计数据 |
| created_at | TEXT | UTC 时间戳 |

### 2.9 Raw Sessions 表

| 字段 | 类型 | 说明 |
|------|------|------|
| day | TEXT UNIQUE | YYYY-MM-DD |
| blob | BLOB | gzip 压缩的 call_logs JSON |
| created_at | TEXT | UTC 时间戳 |

---

## 3. API 格式适配层

### 3.1 四种支持的 API 格式

| 格式 | 对应 SDK/类 | 路由键 |
|------|------------|--------|
| OpenAI Chat Completions | `OpenAICompatProvider` | `openai`, `azure`, `ollama`, `vllm` |
| DeepSeek Chat | `DeepSeekProvider` | `deepseek` |
| Anthropic Messages | `AnthropicProvider` | `anthropic` |
| Google Gemini | `GoogleProvider` | `google` |

### 3.2 `api_format` 字段语义

`models.api_format` 是非空时覆盖 `provider_type` 选择 SDK 类的**每模型覆盖字段**。典型场景：中转站聚合多个厂商模型——一个 provider 连接不同厂商的模型，每个 model 指定自己对应的 SDK 格式。

路由缓存键从 `(provider_id, provider_type)` 改为 `(provider_id, resolved_type, proxy)`，其中 `resolved_type = api_format if api_format else provider_type`。

### 3.3 请求规范

**`POST /v1/chat/completions`**（主要入口）

- 支持 OpenAI Chat Completions 格式
- Header: `Authorization: Bearer <key>` 或 `x-api-key: <key>`
- Body: standard OpenAI chat messages, `model` 字段可选（不选则用 default group）

**`POST /v1/responses`**（OpenAI Responses API）

- 接受 `{input, model, stream?, instructions?, tools?}` 格式
- `instructions` → 转换为 system message
- `tools` → 转换为 function calling format
- 支持流式 SSE 输出

**`GET /v1/models`**（标准端点，无鉴权，但受 admin key 额外保护）

- 返回 group 内所有 enabled 模型的列表（兼容 OpenAI `/v1/models`）

### 3.4 Stream 模式

- `stream: true` 时返回 SSE 流
- 流式路由：先返回候选端点列表，调用方按顺序尝试直到第一个开始 streaming
- 非流式：直接返回完整响应

---

## 4. 路由引擎

### 4.1 路由流程

```
请求
  │
  ▼
Auth (API Key)
  │
  ▼
Resolve Group (model → group, or default)
  │
  ▼
Load Endpoints (cache 1min)
  │
  ▼
Filter Cooldown
  │
  ├─ All cooldown → fallback_group? → 递归
  │
  ▼
Weighted Random Select
  │
  ▼
Context Window Truncation (if set)
  │
  ▼
Attempt Call (with retry + backoff)
  │
  ├─ Success → record_success, return
  │
  └─ Fail → record_failure, cooldown?
              │
              ├─ Retryable (429/5xx/timeout) → exponential backoff → retry
              └─ Exhausted → next model in group
```

### 4.2 Cooldown Manager

- 内存中的 `CooldownState` 字典，key 为 `(group_id, model_id)`
- 连续失败达阈值 → 进入 cooldown，持续 N 秒
- **持久化**：重启时从 DB config 表恢复（key 格式 `cooldown:{gid}:{mid}`）
- 使用 `time.monotonic()` 检测是否过期

### 4.3 权重算法

- `weighted_random_select`：按权重均匀随机选择一个
- `weighted_random_order`：不放回加权随机排序，用于流式 fallback 顺序
- 权重为 0 的模型不参与选择

### 4.4 Context Window Truncation

当 group 内任一模型设置了 `context_window > 0` 时，取最小值对所有消息截断（使用 BM25 相关性排序保留最相关消息）。

---

## 5. 管理 API（Admin REST）

所有 admin 路由以 `/admin` 为前缀，由 `verify_admin_key` 中间件保护（基于 `BOTFLOW_ADMIN_KEY` 环境变量）。

| 方法 | 路径 | 功能 |
|------|------|------|
| POST | `/admin/providers` | 创建 Provider |
| GET | `/admin/providers` | 列出 Providers |
| GET | `/admin/providers/{id}` | 获取 Provider |
| PATCH | `/admin/providers/{id}` | 更新 Provider |
| DELETE | `/admin/providers/{id}` | 删除 Provider |
| POST | `/admin/models` | 创建 Model |
| GET | `/admin/models` | 列出 Models |
| GET | `/admin/models/{id}` | 获取 Model |
| PATCH | `/admin/models/{id}` | 更新 Model |
| DELETE | `/admin/models/{id}` | 删除 Model |
| POST | `/admin/groups` | 创建 Group |
| GET | `/admin/groups` | 列出 Groups |
| GET | `/admin/groups/{id}/details` | 获取 Group + Models |
| PATCH | `/admin/groups/{id}` | 更新 Group |
| DELETE | `/admin/groups/{id}` | 删除 Group |
| POST | `/admin/groups/{id}/models` | 添加 Model 到 Group |
| PATCH | `/admin/groups/{id}/models/{mid}` | 更新权重 |
| DELETE | `/admin/groups/{id}/models/{mid}` | 从 Group 移除 Model |
| POST | `/admin/apikeys` | 创建 Client API Key |
| GET | `/admin/apikeys` | 列出 Client Keys |
| PATCH | `/admin/apikeys/{id}` | 启/禁 Key |
| DELETE | `/admin/apikeys/{id}` | 删除 Key |
| GET | `/admin/stats/models` | 模型调用统计 |
| GET | `/admin/stats/groups` | 分组调用统计 |
| GET | `/admin/stats/cost` | 成本汇总 |
| GET | `/admin/logs` | 查询调用日志 |
| GET | `/admin/summaries/{day}` | 获取每日摘要 |
| POST | `/admin/models/sync` | 从上游同步模型列表 |

---

## 6. CLI 命令

```
botflow init                  # 初始化 workspace（创建 db + 默认配置）
botflow run                   # 启动代理服务（默认 http://0.0.0.0:8000）
botflow provider [add|list|get|update|delete] ...
botflow model [add|list|get|update|delete|sync] ...
botflow group [add|list|get|update|delete] ...
botflow group-model [add|remove|list] ...
botflow apikey [add|list|enable|disable|delete] ...
botflow config [get|set] ...
botflow stats [model|group|cost] ...
botflow logs [tail|query] ...
botflow summary [generate|get] ...
botflow service [start|stop|status|restart|logs]   # systemd 服务管理
```

---

## 7. 配置

通过 `.env` 文件或 `config` 表（优先使用数据库）管理：

| 配置键 | 默认值 | 说明 |
|--------|--------|------|
| `BOTFLOW_ADMIN_KEY` | （必填） | Admin API 保护密钥 |
| `BOTFLOW_PORT` | `8000` | 监听端口 |
| `BOTFLOW_HOST` | `0.0.0.0` | 监听地址 |
| `DEFAULT_GROUP` | `1` | 默认路由分组 ID |
| `LOG_RETENTION_DAYS` | `30` | 调用日志保留天数 |
| `SUMMARY_RETENTION_DAYS` | `30` | 每日摘要保留天数 |
| `RATE_LIMIT_ENABLED` | `false` | 是否启用 IP 级速率限制 |
| `RATE_LIMIT_MAX_REQUESTS` | `60` | 每分钟最大请求数 |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | 速率限制窗口（秒） |
| `MODEL_SYNC_INTERVAL` | `60` | 模型自动同步间隔（分钟，0=禁用） |

---

## 8. 安全

| 措施 | 说明 |
|------|------|
| API Key 哈希 | 客户端 Key 存 sha256，永远不回显明文；创建时返回 `key_hash[:8] + …` |
| Admin Key | `BOTFLOW_ADMIN_KEY` 保护所有 `/admin/*` 路由 |
| WAL 模式 | SQLite WAL 模式，避免锁竞争 |
| 输入校验 | 所有外部输入通过 Pydantic + 手动校验 |
| SQL 注入防护 | 全部使用参数化查询（`?` 占位符） |
| Traceback 限长 | 错误堆栈截断至 2000 字符 |

---

## 9. 目录结构

```
src/botflow/
├── __init__.py
├── cli.py                  # CLI 入口
├── config.py               # 全局配置 + .env 加载
├── workspace.py            # Workspace 路径管理
├── core.py                 # FastAPI 主服务 + 路由注册
├── router.py               # 路由引擎（GroupRouter, CooldownManager）
├── protocol_adapter.py     # 协议适配层（OpenAI/Responses/Anthropic/DeepSeek/Google）
├── auth.py                 # 鉴权中间件（Client Key + Admin Key）
├── admin_api.py            # REST 管理 API
├── daily_summary.py        # 每日摘要定时任务
├── rate_limit.py           # IP 级速率限制
├── common/
│   ├── exceptions.py       # 自定义异常
│   ├── logger.py           # Loguru 日志配置
│   └── context.py          # Context window 截断（BM25 相关性排序）
├── providers/
│   ├── base.py             # BaseProvider 抽象基类
│   ├── openai_compat.py    # OpenAI Chat Completions 适配器
│   ├── anthropic_provider.py
│   ├── google_provider.py
│   └── deepseek_provider.py

└── storage/
    ├── db.py               # SQLite 数据库层
    └── models.py           # Pydantic 数据模型
```

---

## 10. 已知技术债

| 项目 | 说明 |
|------|------|
| 全局单例 | `_db`、`_config`、`_log_writer`、`_active_db` 分散在多模块 |
| *_raw 方法 | db.py 中同时存在带/不带 `_raw` 后缀的方法，部分冗余 |
| `_get_db()` / `_active_db` | 分层略不清晰，可在后续重构中统一 |
