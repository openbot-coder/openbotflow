# BotFlow Gateway V2 — 架构设计文档

> **版本**: 0.9 (Draft)  
> **日期**: 2026-05-27  
> **核心理念**: 协议即 Channel — IM、ACP、A2A 统一消息模型  
> **架构风格**: TCP/IP 五层分层架构 — 每一层职责清晰，下层为上层提供服务  
> **部署约束**: **单机版（Single Instance）**。不面向多实例水平扩展场景。  
> **关联组件**: LiteLLM Proxy 是独立部署的外部依赖（Docker Compose），不归属 BotFlow 项目范围。  
> **v0.9 核心变更**: 落地千问最终审查——A2A SSE FD 限制说明、优雅关闭逻辑、GroupRegistry Phase 1 SQLite 化、docker-compose 部署说明  
> **v0.8 核心变更**: 落地千问单机版审查意见——存储统一 SQLite（含 ACP Registry）、广播流式处理、Session 冷热分离、GroupRegistry 内存方案确认、Q 表状态刷新  
> **v0.7 核心变更**: 技术栈决策落地 — Python + FastAPI + PostgreSQL；A2A/ACP Channel 实现方案；ACP Registry 表设计  
> **v0.6 核心变更**: 修复 3 个关键矛盾 + 4 个高风险缺陷 + 3 个设计原则违反——Router 不修改 delivery_mode、Session 拆分持久化/运行时、ACK 仅 Unicast、DeliveryResultStatus 术语统一、Broadcast 抽象化、DIP 合规

---

## 1. 问题背景

### 1.1 现有 BotFlow (V1)

V1 是一个消息转发网关，支持微信/企微/Telegram 等 IM 协议，通过规则引擎实现消息路由和转发。

**局限**:
- 仅支持 IM 协议，无法接入 Agent 协议 (ACP/A2A)
- 无会话状态管理，无法支持多轮对话
- 无并发控制，LLM 并发时易触发 OOM
- Channel 耦合度高，新增协议需大量重复代码

### 1.2 V2 目标

将 BotFlow 从 "IM 消息转发网关" 升级为 "统一 Agent 网关":
- **多协议统一消息模型**：所有协议（IM、A2A、ACP）共用 GatewayMessage
- **会话状态管理**：Session 支持多轮对话、带 Lane Queue 投递保证
- **LLM 集成**：LiteLLM Proxy 统一调用
- **Channel 可插拔**：每种协议独立 Channel Adapter
- **单机版**：SQLite 单文件部署，极简运维
