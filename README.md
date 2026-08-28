# botflow

AI 中间件平台 - LLM Proxy, LLM-Wiki, IM Bridge

[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

---

## 简介

botflow 是一个轻量级 AI 中间件平台，提供三大核心能力：

1. **LLM Proxy** - 统一 LLM 网关，支持模型分组、权重路由、错误容错、多 Provider 调度
2. **LLM-Wiki** - 基于 Memory Agent 的自主维护知识库（Phase 2）
3. **IM Bridge** - 多平台 IM 统一接入（Phase 3）

当前版本：**Phase 1 - LLM Proxy**

## 核心特性

- **四种 API 格式** - OpenAI Chat Completions / Responses、Anthropic Messages、Google Gemini、DeepSeek 全兼容
- **per-model SDK 覆盖** - `api_format` 字段实现单 Provider 聚合多厂商模型（中转站场景）
- **分组路由** - 权重随机选择，支持跨 Provider 模型混合调度，失败自动降级到 fallback group
- **错误容错** - 自动重试、冷却机制、故障转移
- **Context Window Truncation** - 按最小上下文窗口截断超长消息（BM25 相关性排序）
- **Per-model Proxy** - `extra_config["proxy"]` 指定独立 HTTP 代理
- **REST 管理接口** - 通过 `/admin` HTTP API 管理 Provider/Model/Group，含管理 Key 鉴权
- **多 API Key** - 支持多个客户端 Key（sha256 哈希存储），调用日志按 Key 隔离
- **调用审计** - 完整的调用日志、统计分析、成本追踪；每日生成 LLM Wiki 摘要
- **Model Sync** - 从上游 `/v1/models` 自动发现并添加新模型，定时同步
- **速率限制** - IP 级速率限制，防止暴力破解和 DoS 攻击
- **异步架构** - 基于 aiosqlite 的全异步数据库操作
- **安全防护** - 时序攻击防护、CORS 控制、SQL 注入防护

## 快速开始

### 安装

```bash
# 克隆项目
git clone https://github.com/your-org/botflow.git
cd botflow

# 安装依赖
uv sync

# 安装开发依赖
uv sync --extra dev
```

### 启动服务

```bash
# 直接启动
botflow run

# 指定 workspace 和端口
botflow run --workspace /path/to/workspace --port 8080

# 或使用 uvicorn
uvicorn botflow.core:app --host 0.0.0.0 --port 8080
```

### 配置 Provider

```bash
# 通过 CLI 配置
botflow set llm-key sk-your-api-key
botflow set admin-key your-admin-key

# 或通过 REST 管理接口动态配置（见下文 /admin）
# POST /admin/providers, POST /admin/models, POST /admin/groups ...
```

### 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `BOTFLOW_CORS_ORIGINS` | CORS 允许的来源（逗号分隔） | `http://localhost:3000,http://localhost:8080` |
| `BOTFLOW_LOG_LEVEL` | 日志级别 | `INFO` |

## API 接口

### OpenAI 兼容

```bash
# Chat Completions
curl -X POST http://localhost:8080/v1/chat/completions \
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
# Responses（支持流式）
curl -X POST http://localhost:8080/v1/responses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <client-key>" \
  -d '{
    "input": "Hello!",
    "model": "gpt-4o"
  }'
```

### Anthropic 兼容

```bash
# Messages
curl -X POST http://localhost:8080/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-20250514",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

## REST 管理接口

botflow 通过 `/admin` HTTP API 提供完整的 Provider/Model/Group/统计管理能力，所有接口需用 **管理 Key**（`BOTFLOW_ADMIN_KEY`）通过 `Authorization: Bearer <admin-key>` 鉴权。

### 配置管理 Key

```bash
botflow set admin-key your-admin-secret
```

### 端点一览

| 端点 | 方法 | 说明 |
|------|------|------|
| `/admin/providers` | GET / POST | 列出 / 创建 Provider |
| `/admin/providers/{id}` | GET / PATCH / DELETE | Provider 详情 / 更新 / 删除 |
| `/admin/models` | GET / POST | 列出 / 创建模型 |
| `/admin/models/{id}` | GET / PATCH / DELETE | 模型详情 / 更新 / 删除 |
| `/admin/groups` | GET / POST | 列出 / 创建分组 |
| `/admin/groups/{id}` | GET / PATCH / DELETE | 分组详情 / 更新 / 删除 |
| `/admin/groups/{id}/models` | POST | 将模型加入分组（支持权重/冷却） |
| `/admin/groups/{id}/models/{mid}` | DELETE / PATCH | 移出分组 / 调整权重 |
| `/admin/groups/{id}/details` | GET | 分组内的模型明细 |
| `/admin/stats/models` | GET | 模型统计（可按 `api_key_id` 过滤） |
| `/admin/stats/groups` | GET | 分组统计 |
| `/admin/stats/cost` | GET | 成本汇总（`days`、`api_key_id`） |
| `/admin/logs` | GET | 调用日志查询（`api_key_id`、`status` 等过滤） |
| `/admin/summaries/{day}` | GET | 某日 LLM Wiki 摘要 |
| `/admin/apikeys` | GET / POST | 列出 / 创建客户端 Key |
| `/admin/apikeys/{id}` | PATCH / DELETE | 启用/禁用 / 删除客户端 Key |

### 调用示例

```bash
# 创建 Provider
curl -X POST http://localhost:8080/admin/providers \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"openai","type":"openai","base_url":"https://api.openai.com/v1","api_key":"sk-xxx"}'

# 创建模型并加入分组
curl -X POST http://localhost:8080/admin/models \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -d '{"provider_id":1,"name":"gpt-4","type":"openai"}'
curl -X POST http://localhost:8080/admin/groups/1/models \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -d '{"model_id":1,"weight":2}'

# 创建客户端 API Key（每个 Key 的日志独立隔离）
curl -X POST http://localhost:8080/admin/apikeys \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -d '{"raw_key":"ck-xxxx","label":"team-a"}'
```

### 客户端多 Key

LLM 代理本身（`/v1/*`）使用**客户端 API Key** 鉴权（`Authorization: Bearer <client-key>`）。多个客户端 Key 存于数据库 `api_keys` 表，调用日志按 `api_key_id` 隔离。未配置任何 Key 时回退到单 `BOTFLOW_LLM_KEY`（兼容旧部署）。

### 每日摘要

服务内置 asyncio 后台任务，每天在 `daily_summary_hour`（默认 0 点）运行：
1. 汇总前一天全部调用日志 → 用量/错误统计；
2. 调用指定 `summary_group`（默认 `default`）生成 LLM Wiki 摘要，存 `daily_summaries`；
3. 原始会话 gzip 压缩存入 `raw_sessions`，滚动保留 `raw_session_retention_days`（默认 7）天；
4. 明细日志大字段在 `call_log_detail_days`（默认 1）天后清空，保留统计列。

也可手动触发：`botflow summary --day YYYY-MM-DD`。

## 安全特性

botflow 内置多项安全防护措施：

- **时序攻击防护**: 密钥比较使用常量时间算法，防止通过响应时间差异推断有效密钥
- **CORS 控制**: 通过环境变量配置允许的来源，生产环境需明确指定可信域名
- **速率限制**: 基于 IP 的速率限制（默认 100 次/分钟），防止暴力破解和 DoS 攻击
- **SQL 注入防护**: 全程使用参数化查询，列名通过白名单验证
- **敏感信息脱敏**: API Key 在日志和 `/admin` 输出中自动脱敏（仅返回 hash 前缀）

详细安全审计报告请参阅 `docs/security_audit/` 目录。

## 项目结构

```
botflow/
├── src/botflow/           # 源码
│   ├── core.py            # FastAPI 主服务 + 路由/流式/每日维护
│   ├── router.py          # 路由引擎
│   ├── protocol_adapter.py# 协议适配（4 种 API 格式）
│   ├── auth.py            # 鉴权中间件
│   ├── admin_api.py       # REST 管理接口
│   ├── daily_summary.py   # 每日摘要定时任务
│   ├── rate_limit.py      # IP 级速率限制
│   ├── providers/         # LLM Provider 适配
│   └── storage/           # 数据库层 (aiosqlite)
├── tests/                 # 测试
├── docs/                  # 文档
│   ├── design.md          # 设计文档
│   └── security_audit/    # 安全审计报告
└── pyproject.toml         # 项目配置
```

## 开发

```bash
# 运行测试
uv run pytest

# 运行测试并生成覆盖率报告
uv run pytest --cov=botflow --cov-report=html

# 运行集成测试（需要真实 API 密钥）
uv run pytest -m integration
```

## 文档

- [设计文档](docs/design.md) - 完整的系统设计、数据模型、API 定义
- [AI 助手规范](AGENTS.md) - 开发规范和最佳实践
- [使用指南 Skill](.qoder/skills/botflow-guide/SKILL.md) - 快速上手 botflow

## 技术栈

- **Web 框架**: FastAPI
- **数据库**: SQLite (aiosqlite 异步驱动)
- **LLM 客户端**: 官方 SDK（openai / anthropic / google-genai）
- **配置管理**: pydantic-settings
- **日志**: loguru

## License

MIT
