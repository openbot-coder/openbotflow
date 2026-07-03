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

- **双协议兼容** - 同时支持 OpenAI 和 Anthropic API 格式
- **分组路由** - 权重随机选择，支持跨 Provider 模型混合调度
- **错误容错** - 自动重试、冷却机制、故障转移
- **MCP 管理接口** - 通过 MCP 协议管理 Provider/Model/Group
- **调用审计** - 完整的调用日志、统计分析、成本追踪
- **异步架构** - 基于 aiosqlite 的全异步数据库操作
- **安全防护** - 时序攻击防护、CORS 控制、速率限制

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
botflow set mcp-key your-mcp-key

# 或通过 MCP 工具动态配置
# create_provider, create_model, create_group 等
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

## MCP 工具

botflow 通过 MCP (Model Context Protocol) 提供完整的 LLM Provider 管理能力。MCP 服务通过 **SSE (Server-Sent Events)** 传输，可被 Claude Desktop、Cursor 等 MCP 客户端调用。

### MCP 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/mcp/sse` | GET | SSE 连接端点，客户端订阅事件流 |
| `/mcp/messages/{session_id}` | POST | 消息端点，客户端发送请求 |

### 认证配置

MCP 接口支持 API Key 认证。通过 CLI 设置 MCP Key：

```bash
botflow set mcp-key your-secret-key
```

调用时通过查询参数传递：

```
GET /mcp/sse?api_key=your-secret-key
```

如果未配置 MCP Key，则不需要认证（仅限开发环境使用）。

### 可用工具列表

#### Provider 管理

| 工具 | 说明 |
|------|------|
| `create_provider` | 创建 LLM Provider |
| `update_provider` | 更新 Provider 配置 |
| `delete_provider` | 删除 Provider |
| `get_provider` | 获取 Provider 详情 |
| `list_providers` | 列出所有 Provider |

#### Model 管理

| 工具 | 说明 |
|------|------|
| `create_model` | 创建模型 |
| `update_model` | 更新模型配置 |
| `delete_model` | 删除模型 |
| `list_models` | 列出所有模型 |

#### Group 管理

| 工具 | 说明 |
|------|------|
| `create_group` | 创建模型分组 |
| `update_group` | 更新分组配置 |
| `delete_group` | 删除分组 |
| `get_group` | 获取分组详情 |
| `list_groups` | 列出所有分组 |
| `add_model_to_group` | 将模型添加到分组（支持权重配置） |

#### 统计查询

| 工具 | 说明 |
|------|------|
| `query_model_stats` | 查询模型统计 |
| `query_group_stats` | 查询分组统计 |
| `query_messages` | 查询调用日志 |
| `query_cost_summary` | 查询成本汇总 |

### 调用示例

#### 1. 创建 Provider

```python
import asyncio
from mcp import ClientSession
from mcp.client.sse import sse_client

async def main():
    # 连接到 botflow MCP SSE 服务（带认证）
    async with sse_client("http://localhost:8080/mcp/sse?api_key=your-secret-key") as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            
            # 创建 OpenAI 兼容的 Provider
            result = await session.call_tool("create_provider", {
                "name": "openai",
                "provider_type": "openai",
                "api_key": "sk-your-api-key",
                "base_url": "https://api.openai.com/v1",
            })
            print(result)

asyncio.run(main())
```

#### 2. 创建模型并添加到分组

```python
# 创建模型
result = await session.call_tool("create_model", {
    "name": "gpt-4",
    "provider_id": 1,  # 刚创建的 Provider ID
    "display_name": "GPT-4",
    "max_retries": 3,
    "cooldown_seconds": 60,
})

# 创建分组
result = await session.call_tool("create_group", {
    "name": "production",
    "description": "生产环境分组",
})

# 将模型添加到分组（权重 2.0）
result = await session.call_tool("add_model_to_group", {
    "group_id": 1,
    "model_id": 1,
    "weight": 2.0,
})
```

#### 3. 查询统计

```python
# 查询模型统计
result = await session.call_tool("query_model_stats", {
    "model_id": 1,
})
# 输出:
# Model [1] gpt-4:
#   Total calls: 150
#   Success: 145
#   Errors: 5
#   Prompt tokens: 45,000
#   Completion tokens: 12,000
#   Total cost: $1.2500

# 查询成本汇总
result = await session.call_tool("query_cost_summary", {
    "days": 30,
})
```

### Claude Desktop 配置

在 `~/.claude/claude_desktop_config.json` 中添加：

**方式一：URL 参数认证**

```json
{
  "mcpServers": {
    "botflow": {
      "type": "sse",
      "url": "http://localhost:8080/mcp/sse?api_key=your-secret-key"
    }
  }
}
```

**方式二：Header 认证**

```json
{
  "mcpServers": {
    "botflow": {
      "type": "sse",
      "url": "http://localhost:8080/mcp/sse",
      "headers": {
        "Authorization": "Bearer your-secret-key"
      }
    }
  }
}
```

**方式三：无认证（仅开发环境）**

```json
{
  "mcpServers": {
    "botflow": {
      "type": "sse",
      "url": "http://localhost:8080/mcp/sse"
    }
  }
}
```

### Cursor / 其他 MCP 客户端配置

对于支持 SSE 的 MCP 客户端，可使用以下格式：

```json
{
  "mcpServers": {
    "botflow": {
      "type": "sse",
      "url": "http://localhost:8080/mcp/sse?api_key=your-secret-key"
    }
  }
}
```

## 安全特性

botflow 内置多项安全防护措施：

- **时序攻击防护**: 密钥比较使用常量时间算法，防止通过响应时间差异推断有效密钥
- **CORS 控制**: 通过环境变量配置允许的来源，生产环境需明确指定可信域名
- **速率限制**: 基于 IP 的速率限制（默认 100 次/分钟），防止暴力破解和 DoS 攻击
- **SQL 注入防护**: 全程使用参数化查询，列名通过白名单验证
- **敏感信息脱敏**: API Key 在日志和 MCP 输出中自动脱敏

详细安全审计报告请参阅 `docs/security_audit/` 目录。

## 项目结构

```
botflow/
├── src/botflow/           # 源码
│   ├── core.py            # FastAPI 主服务
│   ├── router.py          # 路由引擎
│   ├── protocol_adapter.py # 协议适配
│   ├── providers/         # LLM Provider 适配
│   ├── mcp/               # MCP 管理接口
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

## 技术栈

- **Web 框架**: FastAPI
- **数据库**: SQLite (aiosqlite 异步驱动)
- **HTTP 客户端**: httpx
- **配置管理**: pydantic-settings
- **日志**: loguru
- **MCP 协议**: mcp-python-sdk

## License

MIT
