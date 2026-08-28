---
name: botflow-guide
description: BotFlow 使用指南 - AI 中间件平台（LLM 网关）快速上手。当用户询问如何使用 botflow、配置 LLM 供应商/模型/分组、管理客户端 API Key、或查看 API 端点时使用。
---

# BotFlow 使用指南

## 项目简介

BotFlow 是单机版 AI Agent 消息网关 / LLM Proxy，支持：
- OpenAI Chat Completions / Responses、Anthropic Messages 兼容 API
- LLM 多供应商路由与负载均衡（含 fallback 分组）
- 客户端多租户 API Key 管理
- SQLite 数据存储、调用日志与每日汇总

## 快速开始

```bash
# 安装依赖（开发）
uv sync

# 启动服务
botflow run --workspace ~/.botflow

# 配置管理 Key / 代理 LLM Key
botflow set admin-key <YOUR_ADMIN_KEY>
botflow set llm-key <YOUR_LLM_KEY>
```

## API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/chat/completions` | POST | OpenAI 兼容聊天补全（流式/非流式） |
| `/v1/completions` | POST | OpenAI legacy 补全 |
| `/v1/messages` | POST | Anthropic Messages 兼容 |
| `/v1/responses` | POST | OpenAI Responses API |
| `/v1/models` | GET | 模型列表 |
| `/health` | GET | 健康检查 |
| `/admin/*` | REST | 管理接口（provider/model/group/apikey/summary） |

所有推理模型响应均透传 `reasoning_content`（OpenAI: `message.reasoning_content`；Anthropic: `thinking` 块；Responses: `reasoning` output item）。

## CLI 速览

```bash
botflow provider add <name> --type openai --api-key <KEY> --base-url <URL>
botflow model add <name> --provider <id> --model <id> --label <label>
botflow model sync [--provider-id <id>]      # 从上游 /v1/models 自动发现
botflow group add <name> --fallback <group_id>
botflow apikey add <KEY> --label <label>     # 客户端调用 Key
botflow apikey update <id> [--label] [--enabled true/false]
botflow set <key> <value> / botflow get <key>
botflow cleanup [--days N]                   # 清理旧 call_logs
botflow summary                              # 触发每日汇总
botflow run / stop / restart / status / logs
```

## 环境变量 / 配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `cors_origins` | `http://localhost:3000,http://localhost:8080` | CORS 允许来源 |
| `log_level` | `INFO` | 日志级别 |
| `call_log_detail_days` | `1` | 调用明细保留天数 |
| `raw_session_retention_days` | `7` | 原始会话压缩包保留天数 |
| `call_logs_retention_days` | `180` | call_logs 整表保留天数 |
| `model_sync_interval` | `60` | 上游模型同步间隔（分钟，0=禁用） |
| `daily_summary_hour` | `16` | 每日维护任务 UTC 小时 |

## 开发

```bash
uv sync
pytest tests -q          # 回归测试（勿从 scripts/ 目录收集）
uvicorn botflow.core:app --reload
```

## 项目结构

```
src/botflow/
├── core.py            # FastAPI 主服务 + 路由/流式/每日维护
├── router.py          # 路由引擎（分组 + 负载均衡 + fallback）
├── protocol_adapter.py# 4 种 API 格式互通（OpenAI/Anthropic/Responses/legacy）
├── cli/main.py        # CLI 入口
├── config.py          # 配置管理（.env）
├── auth.py            # 鉴权中间件（client/admin key）
├── admin_api.py       # 管理 REST 接口
├── providers/         # LLM 供应商适配（openai_compat/deepseek/anthropic/google）
├── storage/           # SQLite 层 + 日志写入 + 每日汇总/清理
└── common/            # 日志、异常、内容转换器
```

## 详细文档

- 完整架构设计：[docs/design.md](docs/design.md)
