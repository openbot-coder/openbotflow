---
name: botflow-guide
description: BotFlow 使用指南 - AI 中间件平台快速上手。当用户询问如何使用 botflow、配置 LLM 供应商、设置 MCP 服务、或查看 API 端点时使用。
---

# BotFlow 使用指南

## 项目简介

BotFlow 是单机版 AI Agent 消息网关，支持：
- OpenAI / Anthropic 兼容 API
- LLM 多供应商路由与负载均衡
- MCP 工具服务（SSE 传输）
- SQLite 数据存储与调用日志

## 快速开始

```bash
# 安装
uv tool install botflow

# 启动服务
botflow run --workspace ~/.botflow

# 配置 API Key
botflow set llm-key <YOUR_KEY>
botflow set mcp-key <YOUR_KEY>
```

## API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/chat/completions` | POST | OpenAI 兼容聊天补全 |
| `/v1/messages` | POST | Anthropic 兼容消息 |
| `/v1/models` | GET | 模型列表 |
| `/v1/embeddings` | POST | Embeddings（待实现） |
| `/mcp/sse` | GET | MCP SSE 传输 |
| `/health` | GET | 健康检查 |

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `BOTFLOW_CORS_ORIGINS` | `http://localhost:3000,http://localhost:8080` | CORS 允许来源 |
| `BOTFLOW_LOG_LEVEL` | `INFO` | 日志级别 |

## 开发

```bash
# 安装依赖
uv sync

# 运行测试
pytest

# 启动开发服务器
uvicorn botflow.core:app --reload
```

## 项目结构

```
src/botflow/
├── core.py            # FastAPI 主服务
├── router.py          # 路由引擎
├── cli.py             # CLI 入口
├── config.py          # 配置管理
├── auth.py            # 鉴权中间件
├── providers/         # LLM 供应商适配
├── mcp/               # MCP 服务
└── storage/           # 数据库存储
```

## 详细文档

- 完整架构设计：[docs/design.md](docs/design.md)
- 安全审计报告：[docs/security_audit/](docs/security_audit/)
