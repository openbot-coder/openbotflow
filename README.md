# botflow

AI 中间件平台 - LLM Proxy / LLM 网关

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

---

## 简介

botflow 是一个单机版 LLM 网关，在 FastAPI 之上把多个上游 LLM 服务商统一为 OpenAI 兼容接口。核心能力：

1. **LLM Proxy** - 统一 LLM 网关，模型分组、策略路由、冷却容错、多 Provider 调度
2. **调用审计** - 全链路调用日志、统计、成本追踪，每日生成对话摘要
3. **客户端 Key 管理** - 多租户 API Key（sha256 哈希存储），调用日志按 Key 隔离

当前版本：**v3.0.0 — LangGraph 工作流引擎（PipelineEngine + 策略系统）**

> 规划中但**当前代码未实现**：独立的 LLM-Wiki 知识库（仅有每日摘要）、IM Bridge 多平台接入。README 不把它们列为现有能力。

## 核心特性

- **客户端 4 种 API 格式** - OpenAI Chat Completions、OpenAI legacy Completions、OpenAI Responses、Anthropic Messages
- **上游 3 种 wire 格式** - OpenAI 兼容（openai / azure / ollama / vllm / deepseek 共用 SDK）、Anthropic Messages、Google GenAI
- **per-model SDK 覆盖** - `models.api_format` 非空时覆盖 `provider_type` 选择 SDK 类（中转站聚合多厂商模型场景）
- **工作流引擎** - 基于 LangGraph 的 `PipelineEngine` + 可插拔策略系统；分组路由由 `model_groups.type` 驱动
- **4 个内建策略** - `random_weights`（默认）/ `round_robin` / `sequential` / `langgraph`
- **冷却容错** - 连续失败达阈值进入 cooldown，状态持久化到 DB 并支持重启恢复
- **Fallback 降级** - `fallback_group_id` 逐级降级；无可用 fallback、深度超 3 层或检测到环路时报 fatal error
- **Context Window Truncation** - 按组内最小 `context_window` 截断；保留 system 消息 + 二分查找可容纳的最近 N 条（按新近度，非语义排序）
- **Per-model Proxy** - `extra_config["proxy"]` 指定独立 HTTP 代理
- **REST 管理接口** - `/admin` HTTP API 管理 Provider / Model / Group / Key / 统计，含管理 Key 鉴权与内置管理面板
- **Model Sync** - 从上游 `/v1/models` 自动发现新模型，支持定时同步与手动触发
- **速率限制** - 按客户端 Key 的滑动窗口限流（**硬编码 300 次/分钟**，无配置项）
- **异步架构** - 基于 aiosqlite 的全异步数据库操作
- **安全防护** - 常量时间比对、CORS 可控、参数化查询

## 快速开始

### 安装

```bash
git clone https://github.com/openbot-coder/openbotflow.git
cd openbotflow

# 安装运行时 + 开发依赖（dev 是 dependency-group，uv sync 默认包含）
uv sync
```

### 启动服务

```bash
# 直接启动（默认监听 0.0.0.0:8080）
botflow run

# 指定端口
botflow run --port 8080

# 指定 workspace —— 注意 --workspace 是全局参数，必须放在子命令之前
botflow --workspace /path/to/workspace run

# 或使用 uvicorn
uvicorn botflow.core:app --host 0.0.0.0 --port 8080
```

### 配置 Provider

```bash
# 通过 CLI 配置密钥
botflow set llm-key sk-your-api-key
botflow set admin-key your-admin-key

# 或通过 REST 管理接口动态配置（见下文 /admin）
# POST /admin/providers, POST /admin/models, POST /admin/groups ...
```

## 配置项

全部配置项均可通过环境变量（前缀 `BOTFLOW_`）或 workspace 下的 `.env` 文件设置。优先级：CLI 参数 > 环境变量 > `.env` > 默认值。

| 环境变量 | 默认值 | 说明 |
|------|------|--------|
| `BOTFLOW_WORKSPACE` | `~/.botflow` | Workspace 目录（未显式指定时 `workspace.py` 实际回退到当前目录） |
| `BOTFLOW_HOST` | `0.0.0.0` | 监听地址（`botflow run` 会用 CLI 默认值覆盖） |
| `BOTFLOW_PORT` | `8080` | 监听端口 |
| `BOTFLOW_ADMIN_KEY` | （空） | Admin API 保护密钥 |
| `BOTFLOW_LLM_KEY` | （空） | 无客户端 Key 时的回退密钥；运行时另读取非前缀的 `LLM_KEY` |
| `BOTFLOW_API_KEYS` | （空） | 逗号分隔的客户端 Key。**声明的字段，当前代码未读取** |
| `BOTFLOW_LOG_LEVEL` | `INFO` | 日志级别 |
| `BOTFLOW_CORS_ORIGINS` | `*` | CORS 允许来源（逗号分隔；为 `*` 时自动禁用 credentials） |
| `BOTFLOW_STREAM_TIMEOUT` | `30.0` | 流式等待首个 chunk 的超时秒数 |
| `BOTFLOW_UPSTREAM_SEMAPHORE_SIZE` | `0` | 每 Provider 的上游并发上限，`0` = 不限 |
| `BOTFLOW_CALL_LOG_DETAIL_DAYS` | `1` | 调用明细保留天数（过期仅清大字段，保留统计列） |
| `BOTFLOW_RAW_SESSION_RETENTION_DAYS` | `7` | 原始会话压缩包保留天数 |
| `BOTFLOW_CALL_LOGS_RETENTION_DAYS` | `180` | call_logs 整行删除天数 |
| `BOTFLOW_DAILY_SUMMARY_HOUR` | `0` | 每日摘要任务的 UTC 小时（0-23） |
| `BOTFLOW_SUMMARY_GROUP` | （空） | 摘要使用的分组名，空 = 默认分组 |
| `BOTFLOW_MODEL_SYNC_INTERVAL` | `60` | 模型自动同步间隔（分钟，0 = 禁用） |

## API 接口

### OpenAI 兼容

```bash
# Chat Completions
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer $CLIENT_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'

# List Models
curl http://localhost:8080/v1/models
```

### OpenAI Responses API

```bash
curl -X POST http://localhost:8080/v1/responses \
  -H "Authorization: Bearer $CLIENT_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input": "Hello!", "model": "gpt-4o"}'
```

### Anthropic 兼容

```bash
curl -X POST http://localhost:8080/v1/messages \
  -H "Authorization: Bearer $CLIENT_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-20250514",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

格式由**路径**选择：`/v1/chat/completions` 与 `/v1/completions` → OpenAI，`/v1/messages` → Anthropic，`/v1/responses` → Responses。

## REST 管理接口

通过 `/admin` HTTP API 提供 Provider / Model / Group / Key / 统计管理能力，所有接口需用**管理 Key**（`BOTFLOW_ADMIN_KEY`）通过 `Authorization: Bearer <admin-key>` 鉴权。浏览器访问 `/admin/` 可打开内置管理面板。

```bash
botflow set admin-key your-admin-secret
```

### 端点一览

| 端点 | 方法 | 说明 |
|------|------|------|
| `/admin/` | GET | 内置管理面板（HTML） |
| `/admin/providers` | GET / POST | 列出 / 创建 Provider |
| `/admin/providers/{id}` | GET / PATCH / DELETE | Provider 详情 / 更新 / 删除 |
| `/admin/models` | GET / POST | 列出 / 创建模型 |
| `/admin/models/{id}` | GET / PATCH / DELETE | 模型详情 / 更新 / 删除 |
| `/admin/groups` | GET / POST | 列出 / 创建分组 |
| `/admin/groups/{id}` | GET / PATCH / DELETE | 分组详情 / 更新 / 删除 |
| `/admin/groups/{id}/details` | GET | 分组内的模型明细 |
| `/admin/groups/{id}/models` | POST | 将模型加入分组（支持权重） |
| `/admin/groups/{id}/models/{model_id}` | PATCH / DELETE | 调整权重 / 移出分组 |
| `/admin/strategies` | GET | 列出可用路由策略名 |
| `/admin/stats/models` | GET | 模型统计（可按 `api_key_id` 过滤） |
| `/admin/stats/groups` | GET | 分组统计 |
| `/admin/stats/cost` | GET | 成本汇总（`days`、`api_key_id`） |
| `/admin/logs` | GET | 调用日志查询（`api_key_id`、`status` 等过滤） |
| `/admin/summaries/{day}` | GET | 某日摘要 |
| `/admin/apikeys` | GET / POST | 列出 / 创建客户端 Key |
| `/admin/apikeys/{key_id}` | PATCH / DELETE | 启用/禁用 / 删除客户端 Key |

> 注：模型同步**没有** admin 端点，只能通过 `botflow model sync` 或定时任务触发。

### 调用示例

```bash
# 创建 Provider
curl -X POST http://localhost:8080/admin/providers \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"openai","provider_type":"openai","base_url":"https://api.openai.com/v1","api_key":"sk-xxx"}'

# 创建模型并加入分组
curl -X POST http://localhost:8080/admin/models \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -d '{"provider_id":1,"name":"gpt-4"}'
curl -X POST http://localhost:8080/admin/groups/1/models \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -d '{"model_id":1,"weight":2}'

# 创建客户端 API Key（每个 Key 的日志独立隔离）
curl -X POST http://localhost:8080/admin/apikeys \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -d '{"raw_key":"ck-xxxx","label":"team-a"}'
```

### 客户端多 Key

代理入口（`/v1/*`）使用**客户端 API Key** 鉴权，读取顺序为 `x-api-key` → `Authorization: Bearer`。Key 存于 `api_keys` 表（仅存 sha256）。**未注册任何客户端 Key 时**回退到 DB config 中的单个 `llm_key`（兼容旧部署，启动时会把环境变量 `LLM_KEY` 同步进 DB 并注册为 `legacy:llm_key`）。

### 每日摘要

服务内置 asyncio 后台任务，每天在 `daily_summary_hour`（默认 `0`，UTC）运行：

1. 汇总前一天全部调用日志 → 用量/错误统计；
2. 调用 `summary_group`（默认分组）生成对话摘要，存 `daily_summaries`；
3. 原始会话 gzip 压缩存入 `raw_sessions`，按 `raw_session_retention_days`（默认 7）滚动删除；
4. 明细日志大字段在 `call_log_detail_days`（默认 1）天后清空，保留统计列；整行按 `call_logs_retention_days`（默认 180）删除。

也可手动触发：`botflow summary --day YYYY-MM-DD`。

## CLI 速览

```bash
botflow [--workspace DIR] <command>

run      --host 0.0.0.0 --port 8080 [--config FILE]   # 启动服务
stop / restart / status --port 8080 / logs -n 50      # 服务管理
set <key> <value> / get <key> / config                # DB config 读写
cleanup  --days 180                                   # 清理旧 call_logs
summary  [--day YYYY-MM-DD]                           # 触发每日摘要

provider list|get|add|update|delete
model    list|get|add|update|delete|sync [--provider-id ID]
group    list|get|add|update|delete|add-model|remove-model|set-weight
apikey   list|add|update|disable|enable|delete
stats    cost|model|group|recent -n 20
version
```

> 无 `init`、无 `service` 子命令。

## 安全特性

- **时序攻击防护**: 密钥比较使用 `secrets.compare_digest`
- **CORS 控制**: 通过 `BOTFLOW_CORS_ORIGINS` 配置允许来源，为 `*` 时自动关闭 credentials
- **速率限制**: 按客户端 Key 的滑动窗口限流，硬编码 300 次/分钟（跳过 `/health`）
- **SQL 注入防护**: 全程参数化查询
- **敏感信息脱敏**: API Key 仅返回 sha256 前缀，不回显明文
- **Traceback 限长**: 错误堆栈截断

安全审计报告见 `docs/security_audit/`（**2026-07-03 快照**，当时针对 v0.1.0；其中多数问题已修复，provider `api_key` 明文存储一项仍未处理）。

## 项目结构

```
botflow/
├── src/botflow/
│   ├── core.py             # FastAPI 主服务 + 路由/流式/后台任务/限流中间件
│   ├── router.py           # 路由引擎（端点缓存 + CooldownManager）
│   ├── protocol_adapter.py # 协议适配（客户端 4 种格式 ↔ 内部表示）
│   ├── auth.py             # 鉴权（客户端 Key + Admin Key）
│   ├── admin_api.py        # REST 管理接口
│   ├── admin_dashboard.py  # /admin/ 内置面板
│   ├── config.py           # pydantic-settings 配置
│   ├── workspace.py        # Workspace 路径管理
│   ├── cli/
│   │   ├── main.py         # CLI 入口
│   │   └── service.py      # start/stop/status/logs（PID 文件管理）
│   ├── pipeline/           # LangGraph 工作流引擎
│   │   ├── base.py         # BaseStrategy / STRATEGY_REGISTRY
│   │   ├── strategies.py   # random_weights / round_robin / sequential
│   │   ├── langgraph_engine.py  # LangGraphEngine + langgraph 策略
│   │   ├── engine.py       # PipelineEngine（代理到 LangGraphEngine）
│   │   └── _shared.py      # 端点缓存 re-export + call_llm
│   ├── providers/          # base / openai_compat / anthropic / google / deepseek
│   ├── common/             # logger / exceptions / context（截断）/ content_converters
│   └── storage/
│       ├── db.py           # SQLite 数据库层（含迁移）
│       ├── models.py       # Pydantic 数据模型
│       └── daily_summary.py# 每日摘要 + 各类保留期清理
├── tests/                  # 测试
├── docs/                   # 文档
└── pyproject.toml
```

## 开发

```bash
# 运行测试（testpaths=["tests"] 已配置，integration 用例默认排除）
uv run pytest

# 覆盖率报告
uv run pytest --cov=botflow --cov-report=html

# 运行集成测试（需要真实 API 密钥）
uv run pytest -m integration

# Lint / 类型检查（当前基线：ruff 477 错、format 70 文件待重排、mypy 32 错）
uv run ruff check .
uv run ruff format --check .
uv run mypy src/botflow
```

## 文档

- [设计文档](docs/design.md) - 系统设计、数据模型、API 定义
- [AI 助手规范](AGENTS.md) - 开发规范和最佳实践
- [使用指南 Skill](.mimocode/skills/botflow-guide/SKILL.md) - 快速上手 botflow

## 技术栈

- **Web 框架**: FastAPI
- **工作流引擎**: LangGraph（硬依赖）
- **数据库**: SQLite (aiosqlite 异步驱动，WAL 模式)
- **LLM 客户端**: 官方 SDK（openai / anthropic / google-genai）
- **配置管理**: pydantic-settings
- **日志**: loguru

## License

MIT

> 注意：仓库中**当前没有 `LICENSE` 文件**，`pyproject.toml` 也未声明 license 字段。
