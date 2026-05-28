# openbotflow

BotFlow Gateway V2 — 单机版 AI Agent 消息网关

## 特性

- **多协议统一消息模型**：支持 IM（企业微信）、A2A（Agent-to-Agent）、ACP 三类协议
- **统一消息模型**：GatewayMessage 作为所有协议的通用载体
- **存储极简**：SQLite 单文件（botflow.db），WAL 模式
- **Session 冷热分离**：元数据落库，消息存内存
- **Graceful Shutdown**：SIGTERM 时等待 Lane Queue 清空

## 架构

参考 ARCHITECTURE_V2.md

## 快速开始

```bash
pip install -e .
cp config.example.yaml config.yaml
# 编辑 config.yaml 填入实际配置
uvicorn botflow.api.main:app --reload
```

## 项目结构

```
botflow/
├── core/          # 核心：GatewayMessage、Session、Router
├── channels/      # 协议适配器：WeCom、A2A、ACP
├── llm/           # LLM 调用层
├── storage/       # SQLite 存储：Session、Registry
└── api/           # FastAPI 应用和路由
tests/             # 测试套件
scripts/           # 工具脚本
```

## License

MIT
