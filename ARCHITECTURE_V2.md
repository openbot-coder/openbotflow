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

| 能力 | V1 | V2 |
|------|----|----|
| IM 消息转发 | ✅ | ✅ |
| Agent 协议接入 | ❌ | ✅ (A2A + ACP) |
| 多轮对话 | ❌ | ✅ (Session) |
| 并发控制 | ❌ | ✅ (Session 内嵌 Lane Queue) |
| LLM 执行 | ❌ | ✅ (Agent Executor，Adapter 层) |
| 投递模式 | ❌ (仅点播) | ✅ (点播 + 组播 + 广播) |
| 订阅组管理 | ❌ | ✅ (GroupRegistry) |

---

## 2. 技术栈决策

### 2.1 栈概览

| 层级 | 选型 | 决策说明 |
|------|------|---------|
| **语言** | Python 主线，FastAPI | IO 密集型（WebSocket、长连接），异步原生，性能足够 |
| **Rust** | 仅限 tool execution（如需） | 性能瓶颈路径按需引入，不全局铺开 |
| **HTTP Server** | FastAPI（统一） | IM WebSocket、A2A SSE、ACP REST 全部复用同一进程 |
| **持久化** | **SQLite（WAL 模式）** | 全量存储（Session + ACP Registry）统一到单文件，零运维 |
| **Session Store** | SQLite（WAL + check_same_thread=False） | 避免 SQLite 单线程写入成为瓶颈 |
| **LLM 网关** | LiteLLM Proxy（外部依赖） | 独立 Docker Compose 部署，不在本项目范围 |
| **A2A 实现** | 手写 JSON-RPC（FastAPI） | `python-a2a` SDK 仅 0.1.x，生态薄；手写 50 行以内搞定 |
| **ACP 认证** | JWT（HS256 / RS256） | `python-jose` + `passlib` |
| **A2A/ACP 关系** | 两者均为 Channel 插件 | 正交：A2A 管「说话格式」，ACP 管「拓扑发现」 |

### 2.2 存储设计（单机单文件）

> ⚠️ **LiteLLM Proxy 是外部依赖**，独立部署在 Docker Compose 中，不归属 BotFlow 项目。BotFlow 只调用其 API，不共享数据库。

BotFlow 全部数据落单一 SQLite 文件（`botflow.db`）：

```
botflow.db（单文件，极简部署）
├── sessions 表        — Session 元数据（状态、序列号、配置）
├── messages 表        — 消息历史（滑动窗口，冷热分离设计见 5.2.3）
├── acp_agents 表     — ACP Agent 注册表
├── acp_heartbeats 表 — 心跳记录
└── acp_routing_log   — 路由日志（可选，调试用）
```

**WAL 模式配置**（写入吞吐量优化）：

```python
import sqlite3

conn = sqlite3.connect("botflow.db", check_same_thread=False)
conn.execute("PRAGMA journal_mode=WAL")   # 提升并发读写
conn.execute("PRAGMA synchronous=NORMAL")  # 平衡安全与性能
conn.execute("PRAGMA busy_timeout=5000")   # 5s 锁等待（避免单线程写入卡住读取）
```

> 📌 **关于 LiteLLM + SQLite**：LiteLLM 的 Spend Tracking 可配置 SQLite，但 BotFlow 存储与其完全独立。LiteLLM 自有 PostgreSQL/SQLite，不与 BotFlow 混用。

### 2.3 前后端分离决策

BotFlow 走 **BFF（Backend for Frontend）模式**：

- FastAPI 本身是 API 网关 + WebSocket Server
- 前端（如有）通过 REST / WebSocket 调用 BotFlow API
- 不强拆成独立前端项目，减少部署复杂度

### 2.4 A2A vs ACP 分工

```
A2A（说话方式）
  - 管「消息格式」：JSON-RPC 2.0、Task 状态机、SSE 流
  - 管「能力描述」：AgentCard 静态端点
  - 生态：Google ADK 配套，跨厂商 agent 互操作标准

ACP（拓扑发现）
  - 管「找谁通信」：AID Registry、注册/发现/心跳
  - 管「认证」：JWT AID Header
  - 生态：内部自建，按需迭代

两者正交，BotFlow 同时支持，作为不同 Channel 插件共存。
```

---

## 3. TCP/IP 五层架构映射

BotFlow Gateway 采用 TCP/IP 五层模型设计，每一层对标一个真实网络协议层：

```
┌──────────────────────────────────────────────────────────────┐
│ Layer 5: Application (对标 HTTP/FTP)                        │
│ ─────────────────────────────────────────────────────────── │
│ 应用层协议语义（Gateway Core 不含此层）                        │
│  - LLM Executor（Adapter 层，调用 LiteLLM Proxy）           │
│  - A2A Task 生命周期（create/send/cancel）                  │
│  - ACP Agent 编排（A2A/ACP Channel 调用远程 Agent）         │
│  - IM 业务逻辑（AI 回复、规则匹配）                          │
│ 对应: A2A Task, ACP Message, IM 业务逻辑                   │
├──────────────────────────────────────────────────────────────┤
│ Layer 4: Transport (对标 TCP)                               │
│ ─────────────────────────────────────────────────────────── │
│ Session (内嵌 Lane Queue，Unicast 专属):                     │
│  - 序列号 + ACK (去重/排序)                                │
│  - 重传机制 (超时重发/DLQ)                                  │
│  - Lane Queue (FIFO 串行消费)                               │
│  - 状态机 (NEW→ACTIVE→IDLE→CLOSED)                        │
│  - 消息历史 (max_history)                                  │
│ ─────────────────────────────────────────────────────────── │
│ 投递调度 (Unicast / Multicast / Broadcast):                  │
│  - Multicast: 查 GroupRegistry → 投递订阅者                │
│  - Broadcast: 枚举 Channel 全部 identity → 逐个投递       │
│  - DeliveryReceipt: 记录每个接收者送达状态                  │
├──────────────────────────────────────────────────────────────┤
│ Layer 3: Network (对标 IP)                                  │
│ ─────────────────────────────────────────────────────────── │
│ GatewayMessage:                                              │
│  - source + destination (端点: channel_id + identity_id)   │
│  - delivery_mode (unicast/multicast/broadcast)             │
│  - message_type (request/response/event/ack)               │
│  - chat_type (private/group)                               │
│  - subscription_group (组播订阅组 ID)                      │
│ 对应: 消息路由，不关心业务逻辑                             │
├──────────────────────────────────────────────────────────────┤
│ Layer 2: Data Link (对标 Ethernet)                          │
│ ─────────────────────────────────────────────────────────── │
│ Channel:                                                    │
│  - 协议编解码 (原生 msg ↔ GatewayMessage)                  │
│  - 对接后端执行器 (LLM/Agent/... )                          │
│  - WeChat / WeCom / Telegram / A2A / ACP Channel           │
├──────────────────────────────────────────────────────────────┤
│ Layer 1: Physical (对标 PHY)                               │
│ ─────────────────────────────────────────────────────────── │
│ 实际的网络传输:                                             │
│  - WebSocket (WeCom)                                       │
│  - HTTP Polling (WeChat Work)                              │
│  - Telegram Bot API                                        │
│ 对应: 比特流传输                                            │
└──────────────────────────────────────────────────────────────┘
```

**设计原则**：
- **上行** (inbound): Physical → Data Link → Network → Transport → Application
- **下行** (outbound): Application → Transport → Network → Data Link → Physical
- 每层只和相邻层通信，依赖注入解耦

---

## 4. 架构全景

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          BotFlow Gateway V2                                  │
│                                                                             │
│ ┌─────────────────────────────────────────────────────────────────────────┐ │
│ │                    Adapter Layer (Layer 2 + Layer 5 执行器)              │ │
│ │ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────┐ │ │
│ │ │  WeChat  │ │  WeCom   │ │ Telegram │ │  A2A     │ │   ACP        │ │ │
│ │ │ Adapter  │ │ Adapter  │ │ Adapter  │ │ Channel  │ │   Channel    │ │ │
│ │ │ 协议转换  │ │ 协议转换  │ │ 协议转换  │ │ 协议转换  │ │   协议转换    │ │ │
│ │ │+LLM 执行 │ │+LLM 执行 │ │+LLM 执行 │ │+Agent 执 │ │  +Agent 执行 │ │ │
│ │ └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘ └──────┬───────┘ │ │
│ └──────┼────────────┼────────────┼────────────┼──────────────┼─────────┘ │
│        │            │            │            │              │           │
│        │        GatewayMessage (Layer 3)      delivery_mode:             │
│        │ source + destination + subscription_group                       │
│        │            │            │            │              │           │
│ ┌──────┴────────────┴────────────┴────────────┴──────────────┴─────────┐ │
│ │                            Gateway Core                               │ │
│ │                                                                       │ │
│ │ ┌─────────────────┐ ┌─────────────────────┐ ┌─────────────────────┐ │ │
│ │ │ 点播 (Unicast)  │ │ 组播 (Multicast)     │ │ 广播 (Broadcast)   │ │ │
│ │ │                 │ │                     │ │                     │ │ │
│ │ │ Session + Lane  │ │ GroupRegistry        │ │ enum Channel       │ │ │
│ │ │ Queue           │ │ subscribe/unsubscribe│ │ identities → send  │ │ │
│ │ │ ACK/重传/状态机  │ │ get_subscribers()   │ │ 逐个投递           │ │ │
│ │ │ 消息历史         │ │ DeliveryReceipt     │ │ DeliveryReceipt    │ │ │
│ │ └─────────────────┘ └─────────────────────┘ └─────────────────────┘ │ │
│ │                                                                       │ │
│ │ GroupRegistry API: POST /groups/{id}/subscribe                        │ │
│ └───────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 5. 核心数据模型

### 5.1 GatewayMessage

统一消息模型，对标 **IP 协议**：每个消息都有 source（源头）和 destination（终点）。

**设计参考 IP 协议**：source → destination，不关心"谁发起"，只关心"发给谁"。

```python
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class MessageType(str, Enum):
    """消息类型，对应 HTTP 语义"""
    REQUEST = "request"       # 请求消息（用户发给 Agent）
    RESPONSE = "response"     # 响应消息（Agent 回复用户）
    EVENT = "event"           # 事件消息（系统通知、状态变更）
    ACK = "ack"              # 确认消息（Session 层 ACK）


class ChatType(str, Enum):
    """聊天类型"""
    PRIVATE = "private"
    GROUP = "group"


class DeliveryMode(str, Enum):
    """投递模式（Gateway Core 三层投递调度）"""
    UNICAST = "unicast"      # 点播：source → 一个 destination（Session 串行）
    MULTICAST = "multicast"  # 组播：source → 订阅组的全部订阅者（单向，无 Session）
    BROADCAST = "broadcast"  # 广播：source → 该 Channel 所有 identity（单向，无 Session）


class ChannelEndpoint(BaseModel):
    """Channel 端点：source 或 destination
    
    - channel_id: 标识具体的 Channel 实例（如 "wechat-bot-001"）
    - identity_id: 通信对端的身份标识（可代表用户/Agent/程序）
    - 同一个 identity_id 通过不同 channel 发消息 → 不同 Session
    - 同一个 channel 收到不同 identity_id → 不同 Session
    """
    protocol: str                   # wechat/wecom/telegram/acp/a2a
    channel_id: str                 # Channel 实例 ID
    identity_id: str                # 身份 ID（用户/Agent/程序）
    identity_name: str = ""         # 显示名
    identity_type: str = "user"     # user/agent/system/program


class GatewayMessage(BaseModel):
    """统一消息模型（Layer 3: Network）

    设计理念：对标 IP 协议
    - source: 消息从哪来 (channel_id + identity_id)
    - destination: 消息到哪去 (channel_id + identity_id)
    - message_type: 消息用途（请求/响应/事件/确认）
    """

    # ═══ 标识 ═══
    id: str = Field(default_factory=lambda: __import__("uuid").uuid4().hex)
    version: int = 1

    # ═══ 路由 (对标 IP 地址) ═══
    source: ChannelEndpoint = Field(description="消息来源端点")
    destination: ChannelEndpoint = Field(description="消息目标端点")
    delivery_mode: DeliveryMode = DeliveryMode.UNICAST
    original_delivery_mode: Optional[DeliveryMode] = Field(
        default=None,
        description="原始投递模式。帮助下游 Adapter 识别消息的原始投递来源。"
        " - Multicast/Broadcast 复制时：记录原始 multicast/broadcast 值（Router 不修改 delivery_mode）"
        " - 一般消息（1:1 路由，delivery_mode=unicast）：为 None"
    )
    subscription_group: Optional[str] = Field(
        default=None,
        description="订阅组 ID（delivery_mode=multicast 时使用）"
    )
    message_type: MessageType = MessageType.REQUEST

    # ═══ 群聊支持 ═══
    chat_type: ChatType = ChatType.PRIVATE
    group_id: Optional[str] = Field(default=None, description="群聊 ID（群聊时必填）")
    is_mentioned: bool = Field(default=False, description="是否被 @ 提及（群聊场景）")

    # ═══ 内容 ═══
    content: str = Field(default="", description="文本内容")
    content_type: str = Field(default="text", description="text/image/file/structured/task")
    content_data: Optional[dict] = Field(default=None, description="结构化内容数据")

    # ═══ 协议特定数据 ═══
    protocol_data: dict = Field(default_factory=dict, description="A2A:task_id, ACP:aid_header, IM:msg_id")

    # ═══ 元数据 ═══
    timestamp: datetime = Field(default_factory=datetime.now)
    metadata: dict = Field(default_factory=dict)

    @property
    def session_id(self) -> str:
        """会话 ID = channel_id:identity_id（仅 Unicast 使用）"""
        return f"{self.source.channel_id}:{self.source.identity_id}"

    @property
    def lane_key(self) -> str:
        """Lane Queue 分区键 = session_id（仅 Unicast，Session 自带队列）"""
        return self.session_id
```

**设计决策**:

| 特性 | 决策 | 原因 |
|------|------|------|
| 方向 | `source` + `destination` 替代 `direction` | 对标 IP：每条消息都有明确的从/到 |
| 消息类型 | `MessageType` 枚举 | 区分请求/响应/事件/ACK，Session 层据此决定是否需要 ACK |
| 投递模式 | `delivery_mode` 枚举 + `subscription_group` | 点播(Unicast)/组播(Multicast)/广播(Broadcast)；组播走订阅组，广播全局投递 |
| 原始模式 | `original_delivery_mode` 可选字段 | Router 禁止修改 delivery_mode；此字段记录组播/广播源模式，供下游识别"本消息来源是组播/广播" |
| 端点标识 | `channel_id` + `identity_id` 替代 `sender_id` | 区分 channel 实例和身份，支持一个用户多 channel、一个 channel 多用户 |
| 关联 ID | 无需 `correlation_id` | Gateway Core 只负责消息路由，不做请求-响应配对；桥接追踪用 `protocol_data` 按需传递 |
| 群聊 | `chat_type` + `group_id` + `is_mentioned` | IM 场景群聊 vs 私聊分离 |
| 结构化内容 | `content_type` + `content_data` | 对标 HTTP content-type，支持 A2A Task、ACP Message 等 |
| Pydantic | `BaseModel` 替代 `dataclass` | 自动验证、序列化、JSON Schema 生成 |

### 5.1.5 SQLite 单文件设计（单机版）

BotFlow 全量数据落单一 `botflow.db`（WAL 模式）：

```sql
-- SQLite Schema（全部表同属 botflow.db）
-- WAL 模式 + busy_timeout 保证单机并发安全
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;   -- 5s 锁等待，避免单写者阻塞读取
```

| 表名 | 用途 | 备注 |
|------|------|------|
| `sessions` | Session 元数据 | 状态、序列号、配置（不含消息体） |
| `messages` | 消息历史 | 仅热数据，TTL 7 天后归档/删除 |
| `acp_agents` | ACP Agent 注册表 | 单机版直接用 SQLite |
| `acp_heartbeats` | 心跳记录 | 同上 |
| `acp_routing_log` | 路由日志 | 可选，调试时启用 |
| `groups` | GroupRegistry 订阅组 | Phase 1 实现，重启后订阅关系不丢失 |

> 📌 **LiteLLM Proxy 完全独立**：LiteLLM 自有 PostgreSQL/SQLite，不与 BotFlow 共用文件或数据库实例。

---

### 5.2 Session

会话状态，对标 **TCP 协议**：连接管理、状态机、可靠传输（ACK + 重传）。

**设计参考 TCP 协议**：
- **连接**：NEW → ACTIVE → IDLE → CLOSED
- **可靠传输**：序列号 + ACK + 超时重传
- **流量控制**：滑动窗口（背压）

```python
from enum import Enum
from datetime import datetime, timedelta
from typing import Optional, Dict
from pydantic import BaseModel, Field


class SessionState(str, Enum):
    """会话状态机"""
    NEW = "new"               # 新建，尚未激活
    ACTIVE = "active"         # 活跃，正在处理消息
    IDLE = "idle"             # 空闲，等待新消息
    CLOSED = "closed"         # 已关闭


_TRANSITIONS: dict[SessionState, set[SessionState]] = {
    SessionState.NEW: {SessionState.ACTIVE},
    SessionState.ACTIVE: {SessionState.IDLE, SessionState.CLOSED},
    SessionState.IDLE: {SessionState.ACTIVE, SessionState.CLOSED},
    SessionState.CLOSED: set(),  # 终态，不可再转换
}


class StateTransitionError(ValueError):
    """非法的状态转换"""


class SentMessage(BaseModel):
    """已发送但未 ACK 的消息"""
    message: GatewayMessage
    sequence_num: int
    sent_at: datetime
    retry_count: int = 0
    ack_received: bool = False


class Session(BaseModel):
    """会话状态（Layer 4: Transport，仅 Unicast）—— 可持久化部分

    设计理念：对标 TCP
    - 序列号 + ACK：去重、排序、可靠传输（仅 Unicast 需要）
    - 重传机制：超时重发，超过阈值移入 DLQ
    - 状态机：NEW → ACTIVE → IDLE → CLOSED
    - 消息历史：滑动窗口，保留最近 N 条
    - ⚠️ 仅 Unicast 模式创建 Session；Multicast/Broadcast 单向发送，不记录 Session

    可持久化：本类所有字段均为基础类型（str/int/dict/list），可安全存入 SQLite/Redis。
    运行时对象（asyncio.Queue/Task）移至 SessionRuntime。
    """

    # ═══ 标识 ═══
    session_id: str = Field(description="⚠️ 唯一业务主键。channel_id:identity_id，天然全局唯一，不允许重复。")

    # id 字段已废弃（v0.6），不再使用 UUID 作为内部标识，统一以 session_id 为准
    # （保留字段占位以兼容旧序列化数据，反序列化时自动忽略）
    id: Optional[str] = Field(default=None, exclude=True)

    # ═══ 状态机 ═══
    state: SessionState = SessionState.NEW
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    # ═══ 序列号 + ACK (对标 TCP，仅 Unicast) ═══
    next_sequence: int = 0           # 下一个发送序列号
    last_ack: int = -1               # 最后确认的序列号
    pending_messages: Dict[int, SentMessage] = Field(default_factory=dict)

    # ═══ 重传配置 ═══
    max_retries: int = 3
    retry_timeout_seconds: int = 30

    # ═══ 消息历史 (滑动窗口) ═══
    max_history: int = 50
    messages: list[dict] = Field(default_factory=list)  # 存 dict 避免循环引用

    # ═══ 上下文 ═══
    context: dict = Field(default_factory=dict)  # LLM 对话上下文

    # ═══ Lane Queue 配置（持久化配置，运行时由 SessionRuntime 创建队列）═══
    max_queue_size: int = 100                     # 背压上限
    max_processing_time: float = 60.0             # 单条消息最大处理时间（秒），超时视为慢消费者
    drop_policy: str = "reject"                   # 队列满时策略：reject / drop_oldest / drop_newest

    # ═══ 方法 ═══

    def _transition_to(self, target: SessionState) -> None:
        """状态机转换（带校验）"""
        if target not in _TRANSITIONS.get(self.state, set()):
            raise StateTransitionError(
                f"Illegal state transition: {self.state.value} → {target.value}"
            )
        self.state = target
        self.updated_at = datetime.now()

    def activate(self):
        """激活会话"""
        self._transition_to(SessionState.ACTIVE)

    def idle(self):
        """设为空闲（仅 ACTIVE 可转换）"""
        self._transition_to(SessionState.IDLE)

    def close(self):
        """关闭会话（终态）"""
        self._transition_to(SessionState.CLOSED)

    def append_message(self, msg: GatewayMessage):
        """添加消息到历史（滑动窗口）"""
        self.messages.append(msg)
        if len(self.messages) > self.max_history:
            self.messages = self.messages[-self.max_history:]
        self.updated_at = datetime.now()

    def get_context_messages(self, limit: int = 20) -> list[dict]:
        """获取最近 N 条消息作为 LLM 上下文"""
        recent = self.messages[-limit:]
        return [{"role": m.source.identity_type, "content": m.content} for m in recent]

    def send_message(self, msg: GatewayMessage) -> SentMessage:
        """发送消息（待 ACK）—— 仅 Unicast 模式启用 ACK

        Raises:
            ValueError: 如果 msg.delivery_mode 不是 UNICAST
        """
        if msg.delivery_mode != DeliveryMode.UNICAST:
            raise ValueError(
                f"Session.send_message() 仅支持 UNICAST 模式，"
                f"当前 delivery_mode={msg.delivery_mode}. "
                f"Multicast/Broadcast 不创建 Session，不启用 ACK/重传"
            )

        seq = self.next_sequence
        self.next_sequence += 1

        sent = SentMessage(
            message=msg,
            sequence_num=seq,
            sent_at=datetime.now(),
            retry_count=0,
            ack_received=False
        )
        self.pending_messages[seq] = sent
        self.append_message(msg)
        self.activate()
        return sent

    def receive_message(self, msg: GatewayMessage, seq: int) -> bool:
        """接收消息（去重 + 排序）

        Returns:
            True: 新消息，已处理
            False: 重复消息，忽略
        """
        if seq <= self.last_ack:
            # 重复消息（已 ACK 过）
            return False

        self.append_message(msg)
        self.ack(seq)
        self.activate()
        return True

    def ack(self, seq: int):
        """确认收到消息"""
        if seq > self.last_ack:
            self.last_ack = seq
            # 清理已 ACK 的消息
            self.pending_messages = {
                k: v for k, v in self.pending_messages.items()
                if k > self.last_ack
            }

    def check_pending_for_retry(self) -> list[SentMessage]:
        """检查待 ACK 消息，返回需要重发的"""
        now = datetime.now()
        timeout = timedelta(seconds=self.retry_timeout_seconds)
        need_retry = []

        for seq, sent in self.pending_messages.items():
            if not sent.ack_received and (now - sent.sent_at) > timeout:
                if sent.retry_count < self.max_retries:
                    sent.retry_count += 1
                    sent.sent_at = now
                    need_retry.append(sent)
                else:
                    # 超过最大重试次数，标记失败（移入 DLQ）
                    # TODO: 移到 Dead Letter Queue
                    pass

        return need_retry


class SessionRuntime:
    """会话运行时对象（内存态，不可序列化）

    包含 asyncio 对象（Queue/Task），随进程启动/停止创建/销毁。
    与 Session 一对一绑定：Session 持久化状态，SessionRuntime 管理运行时队列和任务。
    """

    def __init__(self, session: Session):
        self.session = session
        self.queue: Optional[asyncio.Queue] = None
        self.active_task: Optional[asyncio.Task] = None
        self._retry_timer: Optional[asyncio.Task] = None

    async def enqueue(self, msg: GatewayMessage) -> bool:
        """入队（背压拒绝 + 慢消费者保护）

        Returns:
            True: 成功入队
            False: 队列已满或处理超时，消息被拒绝
        """
        if self.queue is None:
            self.queue = asyncio.Queue(maxsize=self.session.max_queue_size)

        # 慢消费者保护：检查当前处理任务是否超时
        if self.active_task and not self.active_task.done():
            # 如果任务运行时间超过 max_processing_time，视为慢消费者
            # TODO: 记录慢消费者事件，触发告警
            pass

        try:
            self.queue.put_nowait(msg)
            return True
        except asyncio.QueueFull:
            # 根据 drop_policy 处理
            if self.session.drop_policy == "drop_oldest":
                # 丢弃最旧消息，尝试重新入队
                try:
                    self.queue.get_nowait()  # 丢弃最旧
                    self.queue.put_nowait(msg)
                    return True
                except asyncio.QueueEmpty:
                    return False
            elif self.session.drop_policy == "drop_newest":
                # 直接丢弃新消息
                return False
            else:  # "reject"
                return False

    async def process_queue(self, router: "Router"):
        """消费队列：串行处理同一 session 的消息

        ⚠️ 核心数据路径——Phase 1.5 必须完整实现，当前仅作结构占位。
        实现要点：
        - 超时保护：asyncio.wait_for(router.route(msg), timeout=session.max_processing_time)
        - ACK 处理：router.route 返回后，清理 pending_messages 中已确认项
        - 异常重试：连续失败超过阈值，移入 DLQ 并关闭 Session
        - 退出条件：队列空 + session 状态变为 IDLE/CLOSED 时停止循环
        """
        while True:
            msg = await self.queue.get()
            try:
                self.session.activate()
                raise NotImplementedError(
                    "process_queue 核心路径尚未实现。"
                    "Phase 1.5 必须完成：超时保护、ACK 处理、异常重试、退出条件。"
                )
            finally:
                self.queue.task_done()

    def start_retry_timer(self):
        """启动重传计时器"""
        # TODO: asyncio.sleep 后调用 session.check_pending_for_retry()
        pass

    def cancel_retry_timer(self):
        """取消重传计时器"""
        if self._retry_timer and not self._retry_timer.done():
            self._retry_timer.cancel()
            self._retry_timer = None

    # ═══ 冷热分离：消息不落 SQLite ══════════════════════════════════════
    # 热数据（消息）存内存 SessionRuntime，SQLite 只存 Session 元数据。
    # 这样避免每次写入都操作大字段 JSON，SQLite 写入性能大幅提升。
    #
    # 如果需要消息持久化（如审计需求），在 Session.close() 时：
    #   1. 将 queue 中剩余消息 + 已处理消息（最多 MAX_HISTORY 条）写入 messages 表
    #   2. 或异步写入，不阻塞 close 流程
    _message_history: list[GatewayMessage] = []

    def push_message(self, msg: GatewayMessage) -> None:
        """热数据：追加到内存历史（上限 MAX_HISTORY）"""
        self._message_history.append(msg)
        if len(self._message_history) > self.session.max_history:
            self._message_history.pop(0)

    def get_history(self, limit: int = 50) -> list[GatewayMessage]:
        """读取最近 N 条消息"""
        return self._message_history[-limit:]

    async def flush_to_db(self, store: "SQLiteSessionStore") -> None:
        """可选：持久化历史消息到 SQLite（审计用）"""
        # messages 表存储在 SQLite，但仅作为冷备，不影响热路径性能
        for msg in self._message_history:
            await store.save_message(msg)
```

**设计决策**:

| 特性 | 决策 | 原因 |
|------|------|------|
| 状态机 | NEW → ACTIVE → IDLE → CLOSED | 对标 TCP 连接状态，方便调试和监控 |
| 序列号 | 单调递增 int | 简单、高效，方便去重和排序 |
| ACK 机制 | 收到消息立即 ACK | 快速确认，减少重传 |
| 重传 | 超时重发，最多 3 次 | 平衡可靠性和延迟 |
| DLQ | 超过重试次数移入死信队列 | 防止消息丢失，方便事后分析 |
| 滑动窗口 | max_history=50 | 控制内存占用，保留足够上下文 |
| SessionRuntime | 运行时对象（asyncio.Queue/Task）与 Session 分离 | Session 可持久化，Runtime 随进程重启重建 |
| Lane Queue | SessionRuntime 管理队列，Session 存配置参数 | 分离持久化与运行时，避免序列化失败 |
| 背压 | max_queue_size=100 + drop_policy | 队列满时按策略（reject / drop_oldest / drop_newest）处理 |
| 慢消费者保护 | max_processing_time=60s | 处理超时将触发告警或队列清空 |
| ACK 范围 | 仅 Unicast | send_message 前置校验：非 UNICAST 模式直接拒接 |
| 冷热分离 | messages 不落 SQLite | 消息存内存 SessionRuntime；SQLite 只持久化 Session 元数据 |
| 优雅关闭 | Graceful Shutdown | SIGTERM → 等待 Lane Queue 清空 → 写入未处理消息到 SQLite → 退出 |
| Pydantic | `BaseModel` | 自动验证、序列化、JSON Schema |

---

### 5.3 Lane Queue（SessionRuntime 管理运行时队列）

Lane Queue 不再是一个独立模块，而是 **SessionRuntime 管理的一个 FIFO 队列**，与 Session 一对一绑定。

**架构**：Session（可持久化）存储配置参数（max_queue_size、drop_policy），SessionRuntime 管理运行时队列（asyncio.Queue）和消费任务（asyncio.Task）。

```
入站消息
   │
   ▼
┌──────────────────────┐
│  SessionDispatcher   │  ← 按 session_id 找到/创建 Session + SessionRuntime
│   (channel_id:identity_id)
└──────────┬───────────┘
           │
      ┌────┼────┬─────────┐
      ▼    ▼    ▼         ▼
   ┌────┐┌────┐┌────┐┌──────┐
   │ S1 ││ S2 ││ S3 ││ S4  │  ← Session（持久化状态）
   │    ││    ││    ││     │
   │ SR1││ SR2││ SR3││ SR4 │  ← SessionRuntime（内存队列）
   │QUEUE││QUEUE││QUEUE││QUEUE│
   └─┬──┘└─┬──┘└─┬──┘└──┬──┘
     │     │     │      │
     ▼     ▼     ▼      ▼
  串行消费  串行消费  串行消费  串行消费
   (S1)    (S2)    (S3)    (S4)
```

#### 为什么如此设计

| 之前（独立 Lane Queue） | v0.5（内嵌于 Session） | v0.6（SessionRuntime 分离） |
|------------------------|-----------------------|---------------------------|
| Lane Dispatcher 单独管理 | Session 自带队列 | Session（持久化）+ SessionRuntime（内存） |
| Lane 需要独立的 LRU 淘汰 | Session 的 idle 态即回收信号 | 同上 |
| Lane 和 Session 用同一个 key | 一个 key，一个对象 | 一个 key，两个对象（持久态+运行态） |
| 双份生命周期管理 | 一套生命周期，但不可序列化 | 可序列化 Session + 可重建 Runtime |
| — | queue/Task 混入 Pydantic | ⚠️ 修复：消除序列化失败风险 |

#### 关键参数

```python
# Session（持久化）中存储队列配置
class Session(BaseModel):
    # ...
    
    # ═══ Lane Queue 配置（持久化） ═══
    max_queue_size: int = 100                     # 背压上限
    max_processing_time: float = 60.0             # 单条消息最大处理时间（秒）
    drop_policy: str = "reject"                   # 队列满时：reject / drop_oldest / drop_newest
```

```python
# SessionRuntime（内存）中运行时队列和任务
class SessionRuntime:
    """运行时对象，不可序列化，随进程创建/销毁"""

    def __init__(self, session: Session):
        self.session = session
        self.queue: Optional[asyncio.Queue] = None
        self.active_task: Optional[asyncio.Task] = None
        self._retry_timer: Optional[asyncio.Task] = None

    async def enqueue(self, msg: GatewayMessage) -> bool:
        """入队（背压 + drop_policy）"""
        # 见上文 SessionRuntime 完整实现
        ...

    async def process_queue(self, router: "Router"):
        """串行消费队列"""
        # 见上文 SessionRuntime 完整实现
        ...
```

#### 调度器：SessionDispatcher

```python
from collections import defaultdict
import asyncio


class SessionDispatcher:
    """按 session_id 分发消息到对应的 SessionRuntime 队列"""

    def __init__(self, max_sessions: int = 1000, session_ttl: int = 300):
        self.max_sessions = max_sessions
        self.session_ttl = session_ttl  # 空闲超时
        self._store: dict[str, Session] = {}        # session_id → Session（持久化实体）
        self._runtimes: dict[str, SessionRuntime] = {}  # session_id → SessionRuntime（运行时）
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)  # per-sid 锁

    async def dispatch(self, msg: GatewayMessage) -> bool:
        """分发到对应 SessionRuntime 队列"""
        sid = msg.session_id  # "channel_id:identity_id"
        session = await self._get_or_create_session(sid)
        runtime = await self._get_or_create_runtime(session)
        success = await runtime.enqueue(msg)  # 入队（背压 + drop_policy）
        if success and not runtime.active_task:
            runtime.active_task = asyncio.create_task(runtime.process_queue())
        return success

    async def _get_or_create_session(self, sid: str) -> Session:
        """获取或创建 Session（per-sid 锁保护读-改-写）"""
        async with self._locks[sid]:
            if sid not in self._store:
                if len(self._store) >= self.max_sessions:
                    await self._evict_idle()
                session = Session(session_id=sid)
                self._store[sid] = session
                # TODO: 持久化到 SessionStore (SQLite/Redis)
            return self._store[sid]

    async def _get_or_create_runtime(self, session: Session) -> SessionRuntime:
        """获取或创建运行时（per-sid 锁保护）"""
        sid = session.session_id
        async with self._locks[sid]:
            if sid not in self._runtimes:
                runtime = SessionRuntime(session)
                self._runtimes[sid] = runtime
            return self._runtimes[sid]

    async def _evict_idle(self):
        """淘汰最旧空闲 Session（LRU 策略）"""
        # TODO: 遍历 _store，关闭 state=IDLE 的 Session，移除对应 Runtime
        pass
```

---

## 6. Channel Plugin System

### 6.1 接口定义

```python
class ChannelPlugin(Protocol):
    """Channel 插件接口"""
    
    @property
    def name(self) -> str: ...
    @property
    def protocol(self) -> str: ...  # im/acp/a2a/custom
    
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def health_check(self) -> bool: ...
    
    async def decode(self, raw: Any) -> GatewayMessage: ...
    async def encode(self, msg: GatewayMessage) -> Any: ...
    async def send(self, msg: GatewayMessage) -> bool: ...
    
    def webhook_routes(self) -> list[WebhookRoute]: ...
```

### 6.2 IM Channel (微信/企微/Telegram)

IM 通道的共性:
- 通过 Webhook 接收消息
- 需要 access_token 管理
- 支持文本/图片/文件等媒体类型

```
  Webhook POST
     │
     ▼
┌─────────────┐
│  decode()   │ ──▶ GatewayMessage
└──────┬──────┘
        │
        ▼
   SessionDispatcher (按 session_id 分发)
```

### 6.3 A2A Channel (Google ADK)

#### 6.3.1 特性

| 特性 | 说明 |
|------|------|
| 能力发现 | AgentCard (`/.well-known/agent.json`) |
| 消息格式 | JSON-RPC 2.0 `{"method": "tasks/send", "params": {...}}` |
| Task 生命周期 | submitted → working → completed → canceled → failed |
| 流式推送 | SSE (Server-Sent Events) |
| 技术栈 | FastAPI + Starlette StreamingResponse，手写 JSON-RPC 编解码 |

#### 6.3.2 时序

```
外部 Agent             A2AChannel           Gateway Core        A2AChannel
    │                     │                     │                  │
    │ POST /a2a/tasks/send│                     │                  │
    │────────────────────▶│                     │                  │
    │                     │ decode(req)         │                  │
    │                     │  → GatewayMessage   │                  │
    │                     │────────────────────▶│                  │
    │                     │                     │ process(gm)     │
    │                     │                     │ router.route()  │
    │                     │                     │ session.send()  │
    │                     │                     │◀─────────────────│
    │                     │◀────────────────────│                  │
    │                     │ encode_result(r)   │                  │
    │ ◀────────────────────│                     │                  │
    │ JSON-RPC Response   │                     │                  │
    │                     │                     │                  │
    │ GET /a2a/tasks/{id}/subscribe             │                  │
    │────────────────────▶│                     │                  │
    │  SSE (Task 状态流)  │                     │                  │
    │◀════════════════════│                     │                  │
```

#### 6.3.3 实现代码

```python
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

router = APIRouter(prefix="/a2a")

# ── AgentCard 发现端点（静态） ──────────────────────────────────
AGENT_CARD = {
    "name": "botflow-agent",
    "description": "BotFlow Gateway — Unified Agent Gateway",
    "url": "http://botflow:8000",
    "capabilities": {"streaming": True, "pushNotifications": False},
    "skills": [{"id": "a2a-route", "name": "A2A Task Routing"}],
    "authentication": {"schemes": ["Bearer"]},
}

@router.get("/.well-known/agent.json")
async def agent_card() -> JSONResponse:
    return JSONResponse(AGENT_CARD)

# ── Task 生命周期 ──────────────────────────────────────────────
@router.post("/tasks/send")
async def tasks_send(request: Request) -> JSONResponse:
    """接收外部 Agent 的 JSON-RPC 请求"""
    body = await request.json()

    # 1. decode: 外部协议 → GatewayMessage
    gm = A2AChannel.decode(body)
    if gm is None:
        return JSONResponse({"jsonrpc": "2.0", "error": {"code": -32600}}, status_code=400)

    # 2. 走核心引擎
    result = await gateway.process(gm)

    # 3. encode: GatewayMessage → JSON-RPC Response
    return JSONResponse(A2AChannel.encode_result(result))

@router.post("/tasks/cancel")
async def tasks_cancel(request: Request) -> JSONResponse:
    """取消一个进行中的 Task"""
    body = await request.json()
    task_id = body.get("params", {}).get("id")
    # 通知核心引擎取消 Task
    result = await gateway.cancel_task(task_id)
    return JSONResponse(A2AChannel.encode_result(result))

@router.get("/tasks/{task_id}/subscribe")
async def tasks_subscribe(task_id: str):
    """SSE 流式推送 Task 状态更新"""
    async def event_generator():
        async for status in gateway.subscribe_task(task_id):
            yield f"data: {status.model_dump_json()}\n\n"
    return StreamingResponse(event_generator(), media_type="text/event-stream")

# ── Channel 编解码 ────────────────────────────────────────────
class A2AChannel:
    """A2A 协议编解码（50 行以内）"""

    @staticmethod
    def decode(body: dict) -> GatewayMessage | None:
        """JSON-RPC → GatewayMessage"""
        if body.get("method") != "tasks/send":
            return None
        params = body.get("params", {})
        task = params.get("task", {})
        return GatewayMessage(
            source=ChannelEndpoint(
                protocol="a2a",
                channel_id="a2a-001",
                identity_id=task.get("source", "agent-external"),
            ),
            destination=ChannelEndpoint(
                protocol="a2a",
                channel_id="a2a-001",
                identity_id="botflow",
            ),
            delivery_mode=DeliveryMode.UNICAST,
            message_type=MessageType.REQUEST,
            content=task.get("message", ""),
            protocol_data={"task_id": task.get("id")},
        )

    @staticmethod
    def encode_result(result: EngineResult) -> dict:
        """Gateway Message → JSON-RPC Response"""
        return {
            "jsonrpc": "2.0",
            "id": result.correlation_id,
            "result": {
                "state": result.state.value,  # completed / failed
                "artifacts": [{"parts": [{"text": result.content}]}],
            },
        }

    @staticmethod
    def encode_error(code: int, message: str, task_id: str | None = None) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": task_id,
            "error": {"code": code, "message": message},
        }
```

### 6.4 ACP Channel (AgentUnion)

#### 6.4.1 概念

ACP 用 **AID（Agent ID）** 作为全局唯一标识。每个 Agent 注册到 Registry 后，其他 Agent 通过 AID 发现并投递消息。

```
┌──────────────┐     ACP Channel     ┌──────────────┐
│  Agent A     │────────────────────▶│  Agent B     │
│  AID: stock  │                     │  AID: report │
└──────┬───────┘                     └──────┬───────┘
       │                                    │
       │ 注册/发现                           │ 注册/发现
       ▼                                    ▼
       ┌────────────────────────────────────────┐
       │           ACP Registry                  │
       │  PostgreSQL: acp_agents 表              │
       └────────────────────────────────────────┘
```

#### 6.4.2 特性

| 特性 | 说明 |
|------|------|
| 身份 | AID 全局唯一，如 `stock-query-agent` |
| 消息格式 | `header`（源 AID + 目标 AID + 时间戳）+ `payload`（业务数据） |
| 认证 | JWT Token，Bearer Header 携带 |
| 注册 | FastAPI `POST /acp/agents/register` |
| 发现 | FastAPI `GET /acp/agents` 支持能力过滤 |
| 健康 | `POST /acp/agents/{aid}/heartbeat` 心跳维护 |

#### 6.4.3 PostgreSQL 表结构

```sql
-- ── 1. Agent 注册表 ──────────────────────────────────────────
CREATE TABLE acp_agents (
    aid          VARCHAR(32)  PRIMARY KEY,        -- 唯一标识符
    name         VARCHAR(128) NOT NULL,           -- 人类可读名称
    url          VARCHAR(512) NOT NULL,           -- 服务地址
    description  TEXT,                             -- 能力描述
    capabilities JSONB     DEFAULT '{}',           -- {"streaming": true}
    skills       JSONB     DEFAULT '[]',           -- [{"id": "stock", "name": "股票查询"}]
    auth_scheme  VARCHAR(32) DEFAULT 'Bearer',    -- Bearer/JWT/APIKey
    auth_key     TEXT,                            -- RS256 公钥 或 HS256 secret
    status       VARCHAR(16) DEFAULT 'active',    -- active/inactive/unreachable
    heartbeat_at TIMESTAMPTZ,                     -- 最近心跳
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    updated_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_acp_status   ON acp_agents(status);
CREATE INDEX idx_acp_capabs   ON acp_agents USING GIN (capabilities);

-- ── 2. 心跳记录（可选，健康监控） ────────────────────────────
CREATE TABLE acp_agent_heartbeats (
    id         BIGSERIAL PRIMARY KEY,
    aid        VARCHAR(32) REFERENCES acp_agents(aid) ON DELETE CASCADE,
    latency_ms INTEGER,
    checked_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_heartbeats_aid ON acp_agent_heartbeats(aid, checked_at DESC);

-- ── 3. 路由日志（可选，调试用） ──────────────────────────────
CREATE TABLE acp_routing_log (
    id          BIGSERIAL PRIMARY KEY,
    trace_id    UUID      DEFAULT gen_random_uuid(),
    source_aid  VARCHAR(32),
    dest_aid    VARCHAR(32),
    method      VARCHAR(64),
    status_code SMALLINT,
    latency_ms  INTEGER,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_routing_trace ON acp_routing_log(trace_id);
```

#### 6.4.4 Pydantic 模型

```python
from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional

class AgentCapabilities(BaseModel):
    streaming: bool = False
    push_notifications: bool = False
    input_modes: list[str] = ["text"]
    output_modes: list[str] = ["text"]

class AgentSkill(BaseModel):
    id: str
    name: str
    description: Optional[str] = None

class ACPAgent(BaseModel):
    aid: str
    name: str
    url: str
    description: Optional[str] = None
    capabilities: AgentCapabilities = AgentCapabilities()
    skills: list[AgentSkill] = []
    auth_scheme: str = "Bearer"
    status: str = "active"
    heartbeat_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class ACPRegisterRequest(BaseModel):
    aid: str
    name: str
    url: str
    description: Optional[str] = None
    capabilities: dict = {}
    skills: list[dict] = []
    auth_scheme: str = "Bearer"
    auth_key: str  # 注册时传入公钥/密钥

class ACPRoutingResponse(BaseModel):
    trace_id: str
    dest_aid: str
    dest_url: str
    latency_ms: Optional[int] = None
    status_code: int
```

#### 6.4.5 Channel 实现

```python
from fastapi import APIRouter, Depends, Header, HTTPException
from jose import jwt, JWTError
from sqlalchemy.orm import Session
import httpx

router = APIRouter(prefix="/acp")

# ── 核心：消息发送 ──────────────────────────────────────────────
@router.post("/messages/send")
async def messages_send(
    request: dict,
    x_acp_aid: str = Header(...),           # 源 AID
    authorization: str = Header(...),
    db: Session = Depends(get_db),
):
    # 1. JWT 验证
    token = authorization.removeprefix("Bearer ")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(401, "Invalid token")

    # 2. 查目标 Agent
    dest_aid = request.get("header", {}).get("to")
    agent = db.query(ACPAgent).filter_by(aid=dest_aid, status="active").first()
    if not agent:
        raise HTTPException(404, f"Agent {dest_aid} not found")

    # 3. 解码 → GatewayMessage → 核心引擎
    gm = ACPChannel.decode(x_acp_aid, request)
    result = await gateway.process(gm)

    # 4. 编码返回
    return ACPChannel.encode_result(result)

# ── 注册 ───────────────────────────────────────────────────────
@router.post("/agents/register")
async def agents_register(body: ACPRegisterRequest, db: Session = Depends(get_db)):
    agent = ACPAgent(**body.model_dump(exclude={"auth_key"}))
    db.add(agent)
    db.commit()
    return {"aid": agent.aid, "status": "registered"}

@router.delete("/agents/{aid}")
async def agents_unregister(aid: str, db: Session = Depends(get_db)):
    db.query(ACPAgent).filter_by(aid=aid).delete()
    db.commit()
    return {"aid": aid, "status": "unregistered"}

# ── 发现 ───────────────────────────────────────────────────────
@router.get("/agents")
async def agents_list(
    capabilities: Optional[str] = None,
    db: Session = Depends(get_db),
):
    q = db.query(ACPAgent).filter_by(status="active")
    if capabilities:
        q = q.filter(ACPAgent.capabilities.contains({capabilities: True}))
    return [a.model_dump() for a in q.all()]

@router.get("/agents/{aid}")
async def agents_get(aid: str, db: Session = Depends(get_db)):
    agent = db.query(ACPAgent).filter_by(aid=aid, status="active").first()
    if not agent:
        raise HTTPException(404, "Agent not found")
    return agent.model_dump()

# ── 心跳 ───────────────────────────────────────────────────────
@router.post("/agents/{aid}/heartbeat")
async def agents_heartbeat(aid: str, db: Session = Depends(get_db)):
    db.query(ACPAgent).filter_by(aid=aid).update({"heartbeat_at": datetime.utcnow()})
    db.commit()
    return {"aid": aid, "heartbeat_at": datetime.utcnow()}

# ── 编解码 ─────────────────────────────────────────────────────
class ACPChannel:
    @staticmethod
    def decode(source_aid: str, body: dict) -> GatewayMessage:
        header = body.get("header", {})
        payload = body.get("payload", {})
        return GatewayMessage(
            source=ChannelEndpoint(protocol="acp", channel_id="acp-001", identity_id=source_aid),
            destination=ChannelEndpoint(protocol="acp", channel_id="acp-001", identity_id=header.get("to", "")),
            delivery_mode=DeliveryMode.UNICAST,
            message_type=MessageType.REQUEST,
            content=payload.get("text", ""),
            protocol_data={"aid": source_aid, "seq": header.get("seq")},
        )

    @staticmethod
    def encode_result(result: EngineResult) -> dict:
        return {
            "code": 0,
            "data": result.content,
            "trace_id": result.trace_id,
        }
```

#### 6.4.6 ACP Registry 关系图

```
┌─────────────────┐     ┌──────────────────────┐
│   acp_agents     │     │   acp_routing_log    │
├─────────────────┤     ├──────────────────────┤
│ aid (PK)        │────▶│ trace_id             │
│ name            │     │ source_aid (FK)      │
│ url             │     │ dest_aid   (FK)      │
│ capabilities    │     │ latency_ms           │
│ skills[]        │     └──────────────────────┘
│ auth_scheme     │     ┌──────────────────────┐
│ auth_key        │     │ acp_agent_heartbeats │
│ status          │────▶│ aid (FK)             │
│ heartbeat_at    │     │ latency_ms           │
│ created_at      │     │ checked_at           │
│ updated_at      │     └──────────────────────┘
└─────────────────┘
```

---

### 6.5 Channel Registry — 插件热插拔

Channel 注册中心负责所有 Channel 插件的注册、发现和生命周期管理，支持运行时热插拔。

#### 6.5.1 接口定义

```python
from typing import Optional
from pydantic import BaseModel

class ChannelMetadata(BaseModel):
    """Channel 元信息（用于发现和监控）"""
    name: str
    protocol: str  # im/acp/a2a/custom
    status: str  # active/inactive/error
    registered_at: datetime
    last_heartbeat: datetime


class ChannelRegistry:
    """Channel 注册中心 — 依赖注入使用，不做全局单例约束
    
    ⚠️ 全局可变单例是反模式。应通过依赖注入传入 GatewayCore：
    
    ```python
    # ✅ 依赖注入
    registry = ChannelRegistry()
    gateway = GatewayCore(registry=registry)
    ```
    """

    def __init__(self):
        self._plugins: dict[str, ChannelPlugin] = {}
        self._metadata: dict[str, ChannelMetadata] = {}

    # ── 注册/注销 ──

    def register(self, plugin: ChannelPlugin) -> None:
        """注册 Channel 插件（热插拔）"""
        name = plugin.name
        self._plugins[name] = plugin
        self._metadata[name] = ChannelMetadata(
            name=name,
            protocol=plugin.protocol,
            status="active",
            registered_at=datetime.now(),
            last_heartbeat=datetime.now()
        )

    def unregister(self, name: str) -> None:
        """注销 Channel（停止后移除）"""
        if name in self._plugins:
            self._plugins[name].stop()
            del self._plugins[name]
        self._metadata.pop(name, None)

    # ── 查询 ──

    def get(self, name: str) -> Optional[ChannelPlugin]:
        """按名称获取 Channel"""
        return self._plugins.get(name)

    def get_by_protocol(self, protocol: str) -> list[ChannelPlugin]:
        """获取指定协议的所有 Channel"""
        return [p for p in self._plugins.values() if p.protocol == protocol]

    def list_all(self) -> list[ChannelMetadata]:
        """列出所有已注册的 Channel"""
        return list(self._metadata.values())

    # ── 健康检查 ──

    async def health_check(self, name: str) -> bool:
        """对单个 Channel 执行健康检查"""
        plugin = self._plugins.get(name)
        if not plugin:
            return False
        ok = await plugin.health_check()
        if name in self._metadata:
            self._metadata[name].status = "active" if ok else "error"
            self._metadata[name].last_heartbeat = datetime.now()
        return ok

    async def health_check_all(self) -> dict[str, bool]:
        """对所有 Channel 执行健康检查"""
        return {name: await self.health_check(name) for name in self._plugins}
```

#### 6.5.2 热插拔流程

```
启动时注册:
  Gateway 初始化 → config.channels → 遍历配置 → registry.register(ChannelPlugin)

运行时热插拔:
  POST /admin/channels/register
    → 创建 Channel 实例 → registry.register(plugin)
    → Channel.start()

运行时卸载:
  POST /admin/channels/unregister/{name}
    → registry.unregister(name)
    → stop() + 从内存移除

启动扫描（discovery）:
  src/channels/ 目录扫描
    → 自动发现所有 ChannelPlugin 子类
    → 按协议分类注册
```

#### 6.5.3 启动初始化示例

```python
async def bootstrap_channels(config: GatewayConfigV2, registry: ChannelRegistry):
    # 1. IM Channels
    if config.weixin_bots:
        from .channels.im.wechat import WeChatChannel
        for cfg in config.weixin_bots:
            registry.register(WeChatChannel(cfg))

    if config.wecom_bots:
        from .channels.im.wecom import WeComChannel
        for cfg in config.wecom_bots:
            registry.register(WeComChannel(cfg))

    if config.telegram_bots:
        from .channels.im.telegram import TelegramChannel
        for cfg in config.telegram_bots:
            registry.register(TelegramChannel(cfg))

    # 2. Agent Protocol Channels
    if config.a2a.enabled:
        from .channels.agent.a2a import A2AChannel
        registry.register(A2AChannel(config.a2a))

    if config.acp.enabled:
        from .channels.agent.acp import ACPChannel
        registry.register(ACPChannel(config.acp))

    # 3. 启动所有 Channel
    for plugin in registry._plugins.values():
        await plugin.start()
```

#### 6.5.4 设计决策

| 特性 | 决策 | 原因 |
|------|------|------|
| 单例 | `ChannelRegistry` 全局唯一 | Gateway 只有一个注册中心 |
| 热插拔 | `register/unregister` 运行时可用 | 支持动态增减 Channel |
| 健康检查 | 集中式 `health_check_all()` | 方便监控大盘 |
| 自动发现 | 目录扫描 + 子类发现 | 新增 Channel 只需放文件，无需改配置 |
| 存储分离 | 元数据 `ChannelMetadata` 与插件对象分离 | 元数据可序列化存库，插件对象不持久化 |

---

## 7. Router — 三层投递调度

Gateway Core 的 Router 不负责业务意图判断，只负责按 **投递模式** 将消息送达目标端点。

### 7.1 投递流程

```
  GatewayMessage (delivery_mode: UNICAST / MULTICAST / BROADCAST)
       │
       ▼
┌──────────────────────────────────────────────────────────────────┐
│                        Router                                     │
│                                                                   │
│  ┌─────────────┐                                                  │
│  │ 1️⃣ 点播调度  │ ──── 查 Session（无则新建）                      │
│  │ (Unicast)    │ │   → 入 Lane Queue → 串行消费                   │
│  │              │ │   → ACK/重传/状态机管理                         │
│  └──────┬──────┘ │                                                  │
│         │         │                                                  │
│         ▼         │                                                  │
│  ┌─────────────┐  │                                                  │
│  │ 2️⃣ 组播调度  │ ──── 查 GroupRegistry.get_subscribers(group_id)  │
│  │ (Multicast)  │ │   → 遍历每个订阅者作为独立 destination          │
│  │              │ │   → 调用 Adapter 逐一下发（无 ACK，无 Session） │
│  │              │ │   → 记录 DeliveryReceipt（送达/失败）            │
│  └──────┬──────┘ │                                                  │
│         │         │                                                  │
│         ▼         │                                                  │
│  ┌─────────────┐  │                                                  │
│  │ 3️⃣ 广播调度  │ ──── 枚举 Channel 全部已知 identity               │
│  │ (Broadcast)  │ │   → 调用 Adapter 逐一下发（无 ACK，无 Session） │
│  │              │ │   → 记录 DeliveryReceipt（送达/失败）            │
│  └─────────────┘  │                                                  │
└──────────────────────────────────────────────────────────────────────┘
```

**核心原则**：
- Router 只做"投递"——查 GroupRegistry、查 Channel 身份表、调用 Adapter 下发
- 无 Session / ACK / 重传：Multicast 和 Broadcast 是**单向发送**，不关心对端是否回复
- DeliveryReceipt 只记录"是否投递成功"，不跟踪后期的多轮对话

### 7.2 GroupRegistry — 订阅组管理

```python
from dataclasses import dataclass, field
from typing import Dict, Set


@dataclass
class GroupSubscription:
    """订阅组"""
    group_id: str                           # 订阅组 ID（如 "price-alert"）
    description: str = ""                   # 描述
    subscribers: Dict[str, Set[str]] = field(default_factory=dict)
    # {channel_id: {identity_id, ...}}      # 每个 Channel 下的订阅者集合


class GroupRegistry:
    """组播订阅注册中心（✅ 单机版：内存热缓存 + SQLite 持久化）

    - 动态订阅/退订：subscribers 自行控制是否加入某个组
    - 跨 Channel：同一组可包含不同 Channel 的身份
    - GatewayMessage.subscription_group 匹配 group_id

    **v0.9 决策**：Phase 1 完成 SQLite 持久化，重启后订阅组不丢失。
    热路径走内存 Dict，慢路径（启动加载/写入）走 botflow.db groups 表。"""

    def __init__(self, db_path: str = "botflow.db"):
        self._groups: Dict[str, GroupSubscription] = {}
        self._db_path = db_path
        self._load_from_db()   # 启动时从 SQLite 恢复

    def _load_from_db(self) -> None:
        """从 botflow.db groups 表加载全部订阅组到内存"""
        ...

    def _persist_subscription(
        self, group_id: str, channel_id: str, identity_id: str, op: str
    ) -> None:
        """写入/删除 SQLite（幂等，慢路径）"""
        # op: "subscribe" | "unsubscribe"
        ...

    # ── 同步接口（内存热路径，极快） ───────────────────────────────
    def create_group(self, group_id: str, description: str = "") -> None:
        """创建新订阅组"""
        ...

    def subscribe(self, group_id: str, channel_id: str, identity_id: str) -> None:
        """订阅组（幂等）"""
        # 1. 内存 Dict 写
        # 2. 异步写 SQLite（不阻塞响应）
        ...

    def unsubscribe(self, group_id: str, channel_id: str, identity_id: str) -> bool:
        """退订"""
        ...

    def get_subscribers(self, group_id: str) -> Dict[str, Set[str]]:
        """获取全部订阅者 {channel_id: {identity_id, ...}}"""
        ...

    def list_groups(self) -> Dict[str, GroupSubscription]:
        """列出全部组"""
        ...

    def delete_group(self, group_id: str) -> None:
        """删除组"""
        ...

    async def flush_all(self) -> None:
        """进程退出前：将内存全部写回 SQLite"""
        ...
```

> 📌 **SQLite groups 表**：Phase 1 实现，与 sessions/messages/acp_agents 共用 botflow.db。

### 7.3 DeliveryReceipt — 投递回执

```python
from datetime import datetime
from typing import Optional, Dict, Set
from pydantic import BaseModel, Field


class DeliveryResultStatus(str, Enum):
    """投递结果状态 —— 区别于 Session ACK（TCP 级确认）
    
    术语规范：
    - Transport ACK（Session）：TCP 级确认，仅 Unicast 使用，表示"对端确认接收到消息"
    - Delivery Result（Receipt）：应用级投递报告，表示"系统已尝试发送到目标 Channel"
    """
    ALL_SUCCEEDED = "all_succeeded"    # 全部投递成功
    PARTIAL = "partial"                # 部分成功（组播/广播中仅部分投递成功）
    ALL_FAILED = "all_failed"          # 全部投递失败
    UNKNOWN = "unknown"                # 投递状态不明（如异步投递未返回）


class DeliveryReceipt(BaseModel):
    """投递回执（Multicast / Broadcast 使用）
    
    记录一条消息向多个目标的投递情况（应用级 Delivery Result）。
    不涉及多轮对话——仅记录"是否成功发出"。
    
    与 Session ACK 的区分：
    - Session ACK：点对点 TCP 级确认，"对端确认接收到"
    - DeliveryReceipt：应用级报告，"系统已尝试发送"
    """
    message_id: str                             # 对应 GatewayMessage.id
    delivery_mode: DeliveryMode                 # multicast / broadcast
    target_count: int = 0                       # 应投递目标数
    delivered_count: int = 0                    # 成功送达数
    failed_count: int = 0                       # 失败数
    status: DeliveryResultStatus = DeliveryResultStatus.UNKNOWN
    details: Dict[str, str] = Field(default_factory=dict)
    # {channel_id:identity_id: "ok"|"error_msg"}
    timestamp: datetime = Field(default_factory=datetime.now)
```

### 7.4 三层投递 Router 实现

> ⚠️ **关键约束：Router 不修改 delivery_mode**
> 
> 核心原则：Router 只做投递调度，不改变消息的投递模式语义。
> - Multicast/Broadcast 消息在整个投递链中保持 `delivery_mode=MULTICAST/BROADCAST`
> - 下游 Channel Adapter 通过 `delivery_mode` 判断是否需要创建 Session
> - `original_delivery_mode` 字段记录原始投递模式，供下游适配器识别"本消息来源是组播/广播"

```python
import asyncio
from typing import TYPE_CHECKING

# ═══════════════════════════════════════════════════════
# 接口层：符合 DIP（依赖倒置原则）
# Router 仅依赖抽象接口，不依赖具体实现
# ═══════════════════════════════════════════════════════

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .session import Session


class SessionProvider(ABC):
    """Session 提供者抽象（符合 DIP）"""

    @abstractmethod
    async def get_or_create(self, session_id: str) -> "Session":
        """获取或创建 Session"""
        ...

    @abstractmethod
    async def get_runtime(self, session: "Session") -> "SessionRuntime":
        """获取 Session 的运行时对象"""
        ...


class SubscriptionResolver(ABC):
    """订阅组解析器抽象（符合 DIP）"""

    @abstractmethod
    def get_subscribers(self, group_id: str) -> dict[str, set[str]]:
        """获取订阅者 {channel_id: {identity_id, ...}}"""
        ...


class BroadcastIdentitiesProvider(ABC):
    """广播身份枚举器抽象（符合 DIP）"""

    @abstractmethod
    def get_broadcastable_identities(self, channel_id: str) -> list[str]:
        """获取可广播的身份列表（不同 Channel 实现不同）"""
        ...


# ═══ DIP 测试辅助 ═══

class MockSessionRuntime:
    """SessionRuntime 的 Mock 实现（用于单元测试）

    替换 SessionProvider.get_runtime() 的返回类型，
    使测试不依赖真正的 asyncio.Queue 和 Task。
    """

    def __init__(self, session: "Session"):
        self.session = session
        self.enqueued: list[GatewayMessage] = []

    async def enqueue(self, msg: GatewayMessage) -> bool:
        self.enqueued.append(msg)
        return True


class MockSubscriptionResolver:
    """SubscriptionResolver 的 Mock 实现"""

    def __init__(self):
        self._subscribers: dict[str, dict[str, set[str]]] = {}

    def add_subscriber(self, group: str, channel: str, identity: str):
        self._subscribers.setdefault(group, {}).setdefault(channel, set()).add(identity)

    def get_subscribers(self, group_id: str) -> dict[str, set[str]]:
        return self._subscribers.get(group_id, {})


class MockBroadcastProvider:
    """BroadcastIdentitiesProvider 的 Mock 实现"""

    def __init__(self):
        self._identities: list[str] = []

    def set_identities(self, identities: list[str]):
        self._identities = identities

    def get_broadcastable_identities(self, channel_id: str) -> list[str]:
        return self._identities


# ═══ DIP 抽象接口（核心） ═══

# 接口已在上面定义（SessionProvider / SubscriptionResolver / BroadcastIdentitiesProvider）
# Mock 实现在测试中通过 duck typing 满足接口约束


# ═══════════════════════════════════════════════════════
# Router：三层投递调度器（Gateway Core）
# ═══════════════════════════════════════════════════════

import asyncio
from enum import Enum


class DispatchResult(str, Enum):
    """_dispatch_to_channel 返回值语义

    区分"发送成功"和"消息已送达用户端"——后者需要对方主动 ACK。
    """
    SENT = "sent"       # 已提交到底层 Channel Adapter（异步下发）
    DELIVERED = "delivered"  # 已送达（Channel Adapter 同步确认）
    FAILED = "failed"   # 发送失败
    UNKNOWN = "unknown" # 状态不明


class Router:
    """三层投递调度器（Gateway Core）
    
    职责明确——只做"如何投递"，不做"做什么处理"。
    
    依赖接口（符合 DIP）：
    - SessionProvider：Session 创建和运行时管理
    - SubscriptionResolver：组播订阅组解析
    - BroadcastIdentitiesProvider：广播身份枚举
    """

    def __init__(
        self,
        session_provider: SessionProvider,
        subscription_resolver: SubscriptionResolver,
        broadcast_provider: BroadcastIdentitiesProvider,
    ):
        self._session_provider = session_provider
        self._subscription_resolver = subscription_resolver
        self._broadcast_provider = broadcast_provider
        self._broadcast_semaphore = asyncio.Semaphore(50)  # 控制广播并发上限

    async def route(self, msg: GatewayMessage) -> DeliveryReceipt:
        match msg.delivery_mode:
            case DeliveryMode.UNICAST:
                return await self._handle_unicast(msg)
            case DeliveryMode.MULTICAST:
                return await self._handle_multicast(msg)
            case DeliveryMode.BROADCAST:
                return await self._handle_broadcast(msg)

    async def _handle_unicast(self, msg: GatewayMessage) -> DeliveryReceipt:
        """点播调度
        
        1. 查/建 Session（session_id = channel_id:identity_id）
        2. 入 Lane Queue（SessionRuntime 管理，FIFO 串行消费）
        3. ACK / 重传 / 状态机管理
        4. 投递给 Channel Adapter 处理
        5. 回复携带 session_id，保持多轮对话上下文
        
        仅 Unicast 创建 Session —— Multicast/Broadcast 不创建 Session。
        """
        session = await self._session_provider.get_or_create(msg.session_id)
        runtime = await self._session_provider.get_runtime(session)
        success = await runtime.enqueue(msg)
        if success and not runtime.active_task:
            runtime.active_task = asyncio.create_task(
                runtime.process_queue(self)
            )
        # TODO: 等待处理结果并返回 DeliveryReceipt（Unicast 返回 Session ACK）
        return DeliveryReceipt(
            message_id=msg.id,
            delivery_mode=DeliveryMode.UNICAST,
            target_count=1,
            delivered_count=1 if success else 0,
            status=(DeliveryResultStatus.ALL_SUCCEEDED if success
                    else DeliveryResultStatus.ALL_FAILED),
            details={msg.session_id: "ok" if success else "queue_full"},
        )

    async def _handle_multicast(self, msg: GatewayMessage) -> DeliveryReceipt:
        """组播调度
        
        1. 查 SubscriptionResolver 获取订阅者列表
        2. asyncio.gather 并发下发（Semaphore 控制并发上限）
        3. delivery_mode 保持 MULTICAST，下游不创建 Session
        4. 记录 DeliveryReceipt
        """
        if not msg.subscription_group:
            raise ValueError("Multicast requires subscription_group")

        subscribers = self._subscription_resolver.get_subscribers(msg.subscription_group)

        # 构建所有投递任务
        async def dispatch_one(channel_id: str, identity_id: str) -> tuple[str, str]:
            target_msg = msg.copy(deep=True)
            target_msg.destination = ChannelEndpoint(
                protocol=msg.destination.protocol,
                channel_id=channel_id,
                identity_id=identity_id,
            )
            target_msg.original_delivery_mode = (
                target_msg.original_delivery_mode or msg.delivery_mode
            )
            async with self._broadcast_semaphore:
                try:
                    result = await self._dispatch_to_channel(target_msg)
                    return f"{channel_id}:{identity_id}", result.value
                except Exception as e:
                    return f"{channel_id}:{identity_id}", f"error:{e}"

        tasks = [
            dispatch_one(channel_id, identity_id)
            for channel_id, identity_ids in subscribers.items()
            for identity_id in identity_ids
        ]
        total = len(tasks)
        results = await asyncio.gather(*tasks)

        details = dict(results)
        delivered = sum(1 for v in details.values() if v == "sent" or v == "delivered")
        failed = total - delivered

        return DeliveryReceipt(
            message_id=msg.id,
            delivery_mode=DeliveryMode.MULTICAST,
            target_count=total,
            delivered_count=delivered,
            failed_count=failed,
            status=(
                DeliveryResultStatus.ALL_SUCCEEDED if failed == 0
                else DeliveryResultStatus.PARTIAL if delivered > 0
                else DeliveryResultStatus.ALL_FAILED
            ),
            details=details,
        )

    async def _handle_broadcast(self, msg: GatewayMessage) -> DeliveryReceipt:
        """广播调度（流式分批处理，防止内存溢出）

        1. 枚举 Channel 的全部可广播 identity（调用 get_broadcastable_identities）
        2. **流式分批下发**：每批 BATCH_SIZE=100，用 asyncio.gather 并发发
        3. delivery_mode 保持 BROADCAST，下游不创建 Session
        4. 记录 DeliveryReceipt

        > ⚠️ **千问审查高风险修复**：旧版一次性创建全部 Task，大批量广播（>1000 用户）
        > 会触发 Event Loop 卡顿或 OOM。新版分批处理，每批完成后才拉下一批。
        """
        identities = self._broadcast_provider.get_broadcastable_identities(
            msg.destination.channel_id
        )

        # ── 流式分批处理（防内存溢出） ───────────────────────────────
        BATCH_SIZE = 100
        total = len(identities)
        delivered = 0
        failed = 0
        details: dict[str, str] = {}

        async def dispatch_one(identity: str) -> tuple[str, str]:
            target_msg = msg.copy(deep=True)
            target_msg.destination = ChannelEndpoint(
                protocol=msg.destination.protocol,
                channel_id=msg.destination.channel_id,
                identity_id=identity,
            )
            target_msg.original_delivery_mode = (
                target_msg.original_delivery_mode or msg.delivery_mode
            )
            async with self._broadcast_semaphore:
                try:
                    result = await self._dispatch_to_channel(target_msg)
                    return f"{msg.destination.channel_id}:{identity}", result.value
                except Exception as e:
                    return f"{msg.destination.channel_id}:{identity}", f"error:{e}"

        # 分批执行：每次只创建 BATCH_SIZE 个 Task，不压爆内存
        for i in range(0, total, BATCH_SIZE):
            batch = identities[i : i + BATCH_SIZE]
            tasks = [dispatch_one(identity) for identity in batch]
            batch_results = await asyncio.gather(*tasks)
            for identity, status in dict(batch_results).items():
                details[identity] = status
                if status in ("sent", "delivered"):
                    delivered += 1
                else:
                    failed += 1

        return DeliveryReceipt(
            message_id=msg.id,
            delivery_mode=DeliveryMode.BROADCAST,
            target_count=total,
            delivered_count=delivered,
            failed_count=failed,
            status=(
                DeliveryResultStatus.ALL_SUCCEEDED if failed == 0
                else DeliveryResultStatus.PARTIAL if delivered > 0
                else DeliveryResultStatus.ALL_FAILED
            ),
            details=details,
        )

    async def _dispatch_to_channel(self, target_msg: GatewayMessage) -> DispatchResult:
        """投递消息到 Channel Adapter（具体 Channel 由 destination.channel_id 决定）
        
        DispatchResult 枚举值：
        - SENT：已提交到底层 Channel Adapter（异步下发）
        - DELIVERED：已送达（Channel Adapter 同步确认）
        - FAILED：发送失败
        - UNKNOWN：状态不明
        """
        # TODO: 通过 ChannelRegistry 获取对应 Channel Adapter 并调用其 send()
        # 当前占位，返回 SENT
        return DispatchResult.SENT

    # ═══ DIP 抽象接口：测试辅助 ═══
```
```

### 7.5 三层投递 vs 旧 Router 对比

| 特性 | 旧 Router（v0.3） | 新 Router（v0.6） |
|------|-------------------|--------------------|
| 核心职责 | 规则匹配 → 意图识别 → 桥接 | 点播 → 组播 → 广播投递 |
| 意图分类 | Gateway Core 内置 | 移出到 Adapter 层或外部 Agent |
| 规则引擎 | Gateway Core 内置 | 移出到 Adapter 层 |
| 跨协议桥接 | Gateway Core 内置（Bridge Engine） | 由 Adapter 层 Channel 自行决定 |
| LLM 兜底 | Gateway Core 默认执行 | 由 Adapter 层自己调用 |
| 组播支持 | ❌ 无 | ✅ 通过 SubscriptionResolver + GroupRegistry |
| 广播支持 | ❌ 无 | ✅ 通过 BroadcastIdentitiesProvider |
| 投递回执 | ❌ 无 | ✅ DeliveryReceipt 记录目标投递情况 |
| DIP 合规 | ❌ 直接依赖具体类 | ✅ 依赖 SessionProvider/SubscriptionResolver/BroadcastIdentitiesProvider |
| delivery_mode 完整性 | ❌ Router 修改为 UNICAST | ✅ Router 不修改，保留原始模式 |
| 单实例限制 | — | ✅ GroupRegistry 文档明确（Phase 5 持久化） |
| Session 支持 | 所有消息都入 Session | 仅 Unicast 入 Session |

## 8. Agent Layer

### 8.1 LLM Executor

> Gateway Core 不含 LLM Executor。LLM 调用由 **Adapter 层** 各自实现。
> 每个 IM Adapter（WeChat/WeCom/Telegram）内置 LLM Executor，直接调用 LiteLLM Proxy。
> Agent Channel（A2A/ACP）直接转发到远程 Agent，无需 LLM 参与。

```python
class LLMExecutor:
    """
    位于 Adapter 层，非 Gateway Core 组件。
    每个 IM Adapter 持有自己的 LLMExecutor 实例。
    """
    async def execute(
        self,
        context: list[dict],  # LLM 对话历史
        message: GatewayMessage,
    ) -> str:
        """调用 LLM 并返回回复（由 Adapter 自行包装）"""
        ...
```

### 8.2 Agent Outbound

Agent 可以主动调用外部 Agent (通过 A2A/ACP):

```python
class AgentOutbound:
    async def call_agent(
        self,
        agent_url: str,
        task: str,
        protocol: str = "a2a",
    ) -> str:
        """调用远程 Agent"""
        # 1. 发现 Agent 能力 (AgentCard)
        # 2. 发送 Task
        # 3. 等待结果
        ...
```

---

## 9. Configuration

### 9.1 配置模型

```python
class A2AConfig(BaseModel):
    enabled: bool = True
    agent_name: str = "botflow-agent"
    public_url: str = "http://localhost:8000"
    skills: list[A2ASkill] = []

class ACPConfig(BaseModel):
    enabled: bool = True
    aid: str = ""
    ap_url: str = ""
    auth_token: str = ""

class SessionConfig(BaseModel):
    enabled: bool = True
    db_path: str = "sessions.db"
    context_window: int = 20
    session_ttl: int = 3600  # 空闲超时（秒）
    max_queue_size: int = 100            # Lane Queue 背压上限
    max_processing_time: float = 60.0    # 单条消息最大处理时间（秒，慢消费者保护）
    drop_policy: str = "reject"          # 队列满时策略：reject / drop_oldest / drop_newest


class RouterConfig(BaseModel):
    enabled: bool = True
    multicast_enabled: bool = True
    broadcast_enabled: bool = True
    multicast_retry: int = 2
    broadcast_retry: int = 1


class GatewayConfigV2(BaseModel):
    name: str = "botflow"
    version: str = "2.0.0"
    
    # Channel 配置
    weixin_bots: list[WeixinConfig] = []
    wecom_bots: list[WecomConfig] = []
    telegram_bots: list[TelegramConfig] = []
    
    # Agent Protocol
    a2a: A2AConfig = A2AConfig()
    acp: ACPConfig = ACPConfig()
    
    # Core
    session: SessionConfig = SessionConfig()
    router: RouterConfig = RouterConfig()
    api: ApiConfig = ApiConfig()
    storage: StorageConfig = StorageConfig()
    
    # LLM
    llm_provider: str = "litellm"
    llm_model: str = "gpt-4"
    llm_api_key: str = ""
    llm_base_url: str = ""
```

### 9.2 配置示例

```json
{
  "name": "my-gateway",
  "version": "2.0.0",
  
  "weixin_bots": [
    {
      "enabled": true,
      "app_id": "${WECHAT_APP_ID}",
      "app_secret": "${WECHAT_APP_SECRET}"
    }
  ],
  
  "a2a": {
    "enabled": true,
    "agent_name": "my-agent",
    "public_url": "https://gateway.example.com",
    "skills": [
      {
        "id": "translate",
        "name": "翻译",
        "description": "多语言翻译"
      }
    ]
  },
  
  "session": {
    "enabled": true,
    "db_path": "sessions.db",
    "context_window": 20,
    "session_ttl": 3600,
    "max_queue_size": 100
  },
  
  "llm_provider": "litellm",
  "llm_model": "gpt-4",
  "llm_base_url": "http://localhost:4000"
}
```

---

### 9.3 SessionStore — 会话持久化接口

SessionStore 是 Session 的持久化抽象层，负责 Session 的创建、读取、更新和删除（CRUD）。

#### 9.3.1 接口定义

```python
from typing import Optional
from pydantic import BaseModel


class SessionStore(Protocol):
    """Session 持久化存储接口"""

    async def get_or_create(self, session_id: str) -> Session:
        """获取或创建 Session

        如果 session_id 已存在，返回已有 Session；
        如果不存在，创建新 Session 并持久化。
        """
        ...

    async def get(self, session_id: str) -> Optional[Session]:
        """获取 Session（不自动创建）"""
        ...

    async def save(self, session: Session) -> None:
        """保存 Session（创建或更新）"""
        ...

    async def delete(self, session_id: str) -> None:
        """删除 Session"""
        ...

    async def list_active(self, limit: int = 100) -> list[Session]:
        """列出活跃 Session（用于监控和清理）"""
        ...

    async def list_idle(self, idle_minutes: int = 30) -> list[Session]:
        """列出空闲超过 N 分钟的 Session（用于回收）"""
        ...

    async def cleanup_expired(self, ttl_seconds: int = 3600) -> int:
        """清理过期 Session，返回清理数量"""
        ...


class SQLiteSessionStore(SessionStore):
    """SQLite 实现（单机部署首选）

    ⚠️ 继承 SessionStore Protocol，不继承 BaseModel。
    运行时属性 _connection 不在 Pydantic 序列化范围内，避免 Connection 不可序列化问题。
    """

    def __init__(self, db_path: str = "sessions.db"):
        self.db_path = db_path
        self._connection = None

    async def get_or_create(self, session_id: str) -> Session:
        session = await self.get(session_id)
        if session is None:
            session = Session(session_id=session_id)
            await self.save(session)
        return session

    async def save(self, session: Session) -> None:
        # 序列化 Session 为 JSON（messages 只存轻量引用）
        data = session.model_dump_json()
        # INSERT OR REPLACE INTO sessions (key, data, updated_at)
        ...

    async def get(self, session_id: str) -> Optional[Session]:
        # SELECT data FROM sessions WHERE key = ?
        # 反序列化为 Session 对象
        ...

    async def cleanup_expired(self, ttl_seconds: int = 3600) -> int:
        # DELETE FROM sessions WHERE updated_at < datetime('now', '-{ttl}s')
        ...


class RedisSessionStore(SessionStore):
    """Redis 实现（分布式部署可选）

    ⚠️ 继承 SessionStore Protocol，不继承 BaseModel。
    """

    def __init__(self, redis_url: str = "redis://localhost:6379", key_prefix: str = "botflow:session:"):
        self.redis_url = redis_url
        self.key_prefix = key_prefix
        self._client = None

    async def get_or_create(self, session_id: str) -> Session:
        ...

    async def save(self, session: Session) -> None:
        # SETEX {key_prefix}{session_id} {ttl} {json_data}
        ...
```

#### 9.3.2 序列化策略

| 字段 | 存储方式 | 原因 |
|------|---------|------|
| `id`, `session_id`, `state` | 完整存储 | 核心元数据 |
| `next_sequence`, `last_ack` | 完整存储 | 序列号状态 |
| `pending_messages` | 完整存储 | 重传依赖 |
| `messages` | 只存 `list[dict]`（轻量引用） | 减少存储体积，避免循环引用 |
| `context` | 完整存储 | LLM 对话上下文 |
| `retry_task` | **不存储** | asyncio.Task 不可序列化 |

#### 9.3.3 设计决策

| 特性 | 决策 | 原因 |
|------|------|------|
| 接口抽象 | `SessionStore` Protocol | 支持 SQLite/Redis/内存多种实现 |
| 默认实现 | SQLite | 单机部署，零依赖 |
| 序列化 | Pydantic `model_dump_json()` | 自动处理 BaseModel 嵌套 |
| 消息存储 | `messages` 存 `list[dict]` | 避免 `list[GatewayMessage]` 循环引用 |
| 清理策略 | 定时清理 + 启动扫描 | 防止僵尸 Session 堆积 |
| 重传任务 | 不持久化，启动后重建 | asyncio.Task 不可序列化 |

---

## 10. 数据流示例

### 10.1 点播：IM 私聊 → Agent（Unicast + Session）

```
1. 用户微信发消息 "你好"
2. Webhook POST /webhook/wechat
3. WeChatChannel.decode(xml) → GatewayMessage(
       source=ChannelEndpoint(protocol="wechat", channel_id="bot-A", identity_id="user123"),
       destination=ChannelEndpoint(protocol="botflow", channel_id="bot-A", identity_id="bot"),
       delivery_mode=DeliveryMode.UNICAST,
       message_type=MessageType.REQUEST
   )
4. Router.route(msg)
   └─ delivery_mode=UNICAST → 查/建 Session("bot-A:user123")
5. Session.get_or_create("bot-A:user123") → enqueue(msg) 入 Lane Queue
6. Session.process_queue() → 串行消费 → receive_message(msg, seq=5) 去重 + ACK
7. WeChatAdapter 调用 LLMExecutor → 返回回复
8. 回复 → Session.send_message(reply) → 待 ACK 队列
9. WeChatChannel.encode(reply) → HTTP POST 微信客服接口
10. 收到 ACK → Session.ack(seq) → 清理 pending
```

### 10.2 组播：订阅组通知（Multicast，单向，不记 Session）

```
1. 后台任务/Scheduler 检测到价格警报
2. 生成 GatewayMessage(
       source=ChannelEndpoint(protocol="internal", channel_id="price-alert", identity_id="system"),
       destination=ChannelEndpoint(protocol="internal", channel_id="price-alert", identity_id="*"),
       delivery_mode=DeliveryMode.MULTICAST,
       subscription_group="price-alert-subscribers",
       message_type=MessageType.EVENT
   )
3. Router.route(msg)
   └─ delivery_mode=MULTICAST
      → GroupRegistry.get_subscribers("price-alert-subscribers")
        → {wecom: {user-001, user-002}, telegram: {user-005}}
4. 遍历每个订阅者，逐个调用对应 Channel Adapter 发送（无 ACK，无 Session）
5. 记录 DeliveryReceipt（delivered=3, failed=0）
```

### 10.3 广播：Channel 全量推送（Broadcast，单向，不记 Session）

```
1. 系统公告触发
2. 生成 GatewayMessage(
       source=ChannelEndpoint(protocol="internal", channel_id="system", identity_id="admin"),
       destination=ChannelEndpoint(protocol="wecom", channel_id="bot-01", identity_id="*"),
       delivery_mode=DeliveryMode.BROADCAST,
       message_type=MessageType.EVENT
   )
3. Router.route(msg)
   └─ delivery_mode=BROADCAST
      → 枚举 wecom-bot-01 全部已知 identity（全部关注用户）
4. 逐个调用 WeCom Adapter 发送（无 ACK，无 Session）
5. 记录 DeliveryReceipt
```

### 10.4 A2A Agent → Agent（Unicast + Session）

```
1. 外部 Agent POST /a2a/tasks/send
   {"method": "tasks/send", "params": {"task": {"id": "task-123", ...}}}
2. A2AChannel.decode(request) → GatewayMessage(
       source=ChannelEndpoint(protocol="a2a", channel_id="a2a-001", identity_id="agent-external"),
       destination=ChannelEndpoint(protocol="a2a", channel_id="a2a-001", identity_id="botflow"),
       delivery_mode=DeliveryMode.UNICAST,
       message_type=MessageType.REQUEST,
       protocol_data={"task_id": "task-123"}
   )
3. Router.route(msg)
   └─ delivery_mode=UNICAST → Session("a2a-001:agent-external")
4. Session.get_or_create() → enqueue(msg) → process_queue() → receive_message()
5. A2AChannel.encode(result) → 更新 Task 状态（由 A2AChannel 处理协议转换）
6. 外部 Agent 收到 Task 完成通知
```

### 10.5 Session 重传流程

```
出站消息发送后进入 pending_messages 队列：

msg_sent = session.send_message(reply)
  → pending_messages[seq=5] = SentMessage(retry_count=0)

重传检查（每 30 秒）：
  for sent in pending_messages:
      if time_elapsed > 30s and sent.retry_count < 3:
          resend()  # 重发
          sent.retry_count += 1
      elif sent.retry_count >= 3:
          dlq.push(sent)  # 移入死信队列

收到 ACK：
  session.ack(seq=5)  → pending_messages 清理
```

---

## 11. 目录结构

```
botflow/src/botflow/
├── core/                      # 核心模型（协议层）
│   ├── message.py            # GatewayMessage + ChannelEndpoint + MessageType + ChatType
│   ├── session.py             # Session + SessionState + SentMessage
│   ├── gateway.py             # GatewayConfigV2 + 所有子配置 (A2A/ACP/Session/Router...)
│   └── registry.py            # ChannelRegistry 接口 + ChannelMetadata
│
├── channels/                  # Channel 插件（热插拔）
│   ├── base.py                # ChannelPlugin 接口
│   ├── registry.py            # ChannelRegistry 实现（热插拔管理）
│   ├── im/                    # IM Channel
│   │   ├── wechat.py
│   │   ├── wecom.py
│   │   └── telegram.py
│   └── agent/                 # Agent Protocol Channel
│       ├── a2a.py             # A2A Channel
│       └── acp.py             # ACP Channel
│
├── engine/                    # 核心引擎（仅负责投递调度）
│   ├── router.py              # Router：三层投递调度（Unicast/Multicast/Broadcast）
│   ├── group_registry.py      # GroupRegistry：组播订阅组管理
│   └── delivery_receipt.py    # DeliveryReceipt：投递回执数据模型
│
├── agent/                     # Agent 执行层（Adapter 层负责调用）
│   ├── outbound.py            # Agent Outbound（调外部 Agent，通过 A2A/ACP Channel）
│
├── storage/                   # 存储层
│   ├── session_store.py       # SessionStore 接口 + SQLite/Redis 实现
│   └── db.py                  # 数据库管理（连接池、迁移）
│
├── config/                    # 配置
│   └── settings.py            # 配置加载（环境变量 + YAML）
│
└── api/                       # HTTP API
    ├── server.py              # FastAPI 主入口
    ├── admin.py               # 管理接口（Channel 热插拔、健康检查）
    └── models.py              # API 请求/响应模型
```

**核心原则：**
- `core/` = 协议抽象（GatewayMessage、Session、ChannelEndpoint 等），不含 I/O 依赖
- `storage/` = I/O 操作，不含业务逻辑
- `engine/` = 投递调度逻辑，依赖 core + storage；不含 LLM 调用（由 Adapter 层负责）

---

## 12. 实现路线图

### Phase 1: 核心抽象 (Week 1) ✅ 设计完成
- [x] GatewayMessage 统一消息模型（source/destination, delivery_mode, message_type）
- [x] Session 会话状态（TCP: ACK, 重传, DLQ, 状态机, Lane Queue，仅 Unicast）
- [x] ChannelPlugin 接口 + ChannelRegistry 注册中心
- [x] Router 三层投递调度（Unicast/Multicast/Broadcast）
- [x] GroupRegistry 订阅组管理
- [x] DeliveryReceipt 投递回执
- [x] SessionStore 接口 + SQLite/Redis 实现

### Phase 1.5: Session + 存储实现 (Week 1–2)
- [ ] `SessionStore` SQLite 实现（基础 CRUD + 迁移）
- [ ] `Session.receive_message()` 去重 + ACK 发送
- [ ] `Session.check_pending_for_retry()` 超时重发 + DLQ
- [ ] `Session.enqueue()` + `Session.process_queue()` Lane Queue 实现
- [ ] `Router.route()` 三层投递调度
- [ ] **GroupRegistry SQLite 持久化**（Phase 1 内完成，重启后订阅组不丢失）
- [ ] **Graceful Shutdown**：SIGTERM 处理，等待 Lane Queue 清空后退出；崩溃重启时 SessionStore 恢复未处理消息
- [ ] 测试：Session 重传场景覆盖

### Phase 2: SessionDispatcher + Channel Registry (Week 2)
- [ ] `SessionDispatcher` 会话级隔离（async FIFO + 背压）
- [ ] `Session` 空闲超时回收（session_ttl 机制）
- [ ] `ChannelRegistry` 热插拔 + 健康检查
- [ ] 测试：多 Channel 并发消息隔离

### Phase 3: A2A Channel (Week 3)
- [ ] A2A Channel 插件
- [ ] AgentCard 生成/发现
- [ ] Task 生命周期管理
- [ ] SSE 流式支持
- [ ] **SSE 连接数限制说明**：单机单进程 FD 上限约 800（SSE 长连接默认占 1 FD/连接），>500 并发时需调整系统 `ulimit -n 4096`；Phase 3 压测验证 >500 并发 SSE
- [ ] 压测：100 并发 SSE 连接验证

### Phase 4: ACP Channel (Week 4)
- [ ] ACP Channel 插件
- [ ] AID 发现机制
- [ ] 消息编解码
- [ ] JWT / Bearer Token 认证

### Phase 5: GroupRegistry + 组播 API (Week 5)
- [ ] REST API：POST/DELETE /groups/{id}/subscribe
- [ ] 订阅组生命周期管理（TTL、自动清理）
- [ ] 测试：组播通知投递验证

### Phase 6: LLM Executor（Adapter 层）(Week 6)
> LLM Executor 位于 Adapter 层，非 Gateway Core 组件。
- [ ] LiteLLM Proxy 集成（Adapter 层）
- [ ] Prompt 模板（WeChat/WeCom/Telegram 各一套）
- [ ] 会话上下文管理
- [ ] 模型降级策略

### Phase 7: 集成测试 (Week 7)
- [ ] IM → Agent 完整流程（点播）
- [ ] 组播通知投递验证
- [ ] A2A Agent 互操作
- [ ] 监控/可观测性（OpenTelemetry）
## 13. 已解决问题

| # | 问题 | 决策 | 依据 |
|---|------|------|------|
| Q1 | Session 存储选型 | **SQLite（WAL 模式）** | 单文件，零运维；单机版无需 Redis |
| Q2 | LLM 调用方式 | **LiteLLM Proxy** | 统一管理多模型，团队已有部署 |
| Q3 | Session 循环依赖 | `messages` 改为 `list[dict]` | 不存 GatewayMessage 对象，避免循环 |
| Q4 | 目录结构混乱 | core/ = 协议，storage/ = I/O，engine/ = 投递调度 | 职责边界清晰 |
| Q5 | 缺少 Registry | ChannelRegistry 全量接口已设计 | 支持热插拔 + 健康检查 |
| Q6 | Router 核心职责不清 | 拆分为三层投递调度（Unicast/Multicast/Broadcast） | 只做"如何投递"，不做"做什么处理" |
| Q7 | IntentEngine / BridgeEngine 归属 | 移出 Gateway Core → Adapter 层自行实现 | Gateway Core 保持协议无关，减少强依赖 |
| Q8 | Session 记不记"组播/广播"消息 | Session 仅限 Unicast；Multicast/Broadcast 是单向投递 | 组播/广播不关心多轮对话，无需 Session 上下文 |
| Q9 | 组播/广播投递记录 | 新增 DeliveryReceipt → 仅记录"是否成功发出" | 不跟踪多轮对话，区分于 Session ACK |
| **Q10** | **Router 修改 delivery_mode**（矛盾1） | **Router 禁止修改 delivery_mode，新增 `original_delivery_mode`** | 保持组播/广播模式语义，下游据此判断不创建 Session |
| **Q11** | **ACK 语义双重标准**（矛盾2） | **Session.send_message 增加 Unicast 前置校验** | 只有 Unicast 需要 ACK/重传，组播/广播不进入 pending_messages |
| **Q12** | **DeliveryReceipt 术语混用**（矛盾3） | **重命名为 `DeliveryResultStatus` + 文档明确区分 Session ACK vs Delivery Result** | 统一术语，消除歧义 |
| **Q13** | **Session 混入不可序列化对象**（缺陷1） | **拆分为 Session（可持久化）+ SessionRuntime（内存）** | asyncio.Queue/Task 无法序列化，必须分离 |
| **Q14** | **Lane Queue 慢消费者保护缺失**（缺陷2） | **补充 `max_processing_time` + `drop_policy` 字段** | 防止单条消息处理卡死导致队列阻塞 |
| **Q15** | **GroupRegistry 内存设计**（缺陷3） | **单机版确认：内存 Dict 无问题** | 单实例无数据不一致，无需 Redis；Phase 5 仅多实例扩展时考虑 |
| **Q16** | **Broadcast 依赖全量身份**（缺陷4） | **改用 `get_broadcastable_identities()` 抽象方法** | 微信/企微无法枚举全量用户，由 Channel 自行实现 |
| **Q17** | **Router 违反 DIP**（原则违反） | **新增 SessionProvider / SubscriptionResolver / BroadcastIdentitiesProvider 接口** | Router 依赖抽象而非具体类 |
| **Q18** | **Session 违反 SRP**（原则违反） | **Session 只管持久状态；SessionRuntime 管队列和任务** | Session 职责收窄为状态管理 |

---

## 14. 待讨论问题

| # | 问题 | 现状 | 建议 |
|---|------|------|------|
| Q6 | A2A 流式连接管理 | SSE 连接池/超时未设计 | Phase 3 实现时决策 |
| Q7 | ACP 注册中心 | 自建 vs AgentUnion | 初期自建简单版 |
| Q8 | 认证方案最终决策 | A2A Bearer / ACP JWT | Phase 4 实现时决策 |
| Q9 | OpenTelemetry 集成 | 暂未设计 | Phase 7 后考虑 |
| Q10 | Session 回收策略 | session_ttl 机制已设计 | Phase 2 实现时确认 TTL |
| Q11 | 组播/广播投递失败处理 | DeliveryReceipt 记录失败，但无自动重试 | 按 RouterConfig 重试次数重发 |
| Q12 | GroupRegistry 持久化 | 当前设计为内存存储 | Phase 5 实现时决策（SQLite/Redis） |
| **Q13** | **SessionRuntime 重建策略** | 进程重启后 SessionRuntime 需重建 | Phase 2 实现时确认：从 SessionStore 恢复 Session 后自动创建 Runtime |
| **Q14** | **BroadcastIdentitiesProvider 实现** | 各 Channel 需自行实现 `get_broadcastable_identities()` | Phase 2 实现时确认：WeCom/WeChat 等 Channel 的广播能力 |
| **Q15** | **慢消费者告警机制** | `max_processing_time` 已定义，但告警未设计 | Phase 2 实现时确认：日志/指标/告警策略 |

---

## 15. 设计变更日志

### v0.9 (2026-05-27) — 千问最终审查

| # | 变更 | 原因 |
|---|------|------|
| 1 | **A2A SSE FD 限制说明**：Phase 3 增加「单机单进程 FD 上限约 800，>500 并发需 ulimit -n 4096」 | 细节微调 |
| 2 | **GroupRegistry Phase 1 SQLite 化**：内存热缓存 + botflow.db groups 表持久化，重启不丢失 | 细节微调，用户体验更好 |
| 3 | **Graceful Shutdown 逻辑写入文档**：SIGTERM → 等待 Lane Queue 清空 → 写入未处理消息 → 退出 | 细节微调 |
| 4 | **Phase 1.5 新增项**：GroupRegistry SQLite + Graceful Shutdown | 落地审查建议 |
| 5 | **Phase 5 清理**：GroupRegistry 已移至 Phase 1，保留 REST API 部分 | 消除冗余 |
| 6 | **botflow.db 表增加 `groups` 表**：groups 表写入 5.1.5 SQLite 设计表格 | 文档一致性 |

### v0.8 (2026-05-27) — 千问单机版审查

| # | 变更 | 原因 |
|---|------|------|
| 1 | **存储统一 SQLite**：PostgreSQL → SQLite 全量表，botflow.db 单文件 | 简化单机部署，零运维 |
| 2 | **LiteLLM Proxy 明确为外部依赖**：不共享 DB，仅通过 API 调用 | 避免架构耦合 |
| 3 | **Broadcast 流式分批处理**：`asyncio.gather` 一次性全量 → 分批 BATCH_SIZE=100 | 千问高风险：大批量广播压爆 Event Loop/OOM |
| 4 | **Session 冷热分离**：`messages` 不落 SQLite，存内存 `_message_history`；SQLite 只存元数据 | 千问中风险：写放大导致 SQLite 性能下降 |
| 5 | **ACP Registry 移入 SQLite**：PostgreSQL 表结构 → SQLite 同库表 | 单机单文件，极简部署 |
| 6 | **GroupRegistry 内存方案确认**：标注无需 Redis，单实例无数据不一致 | 千问确认：内存 Dict 单机版完全正确 |
| 7 | **WAL 模式 + busy_timeout**：PRAGMA 配置写入文档 | 单机 SQLite 并发安全 |
| 8 | **已解决问题表格更新**：Q1 / Q15 结论落地 | 千问审查结论同步 |

### v0.7 (2026-05-27) — 技术栈落地

| # | 变更 | 原因 |
|---|------|------|
| 1 | **技术栈决策文档化**：Python + FastAPI + SQLite；BFF 模式；A2A 手写 JSON-RPC | 架构决策收敛，避免后续返工 |
| 2 | **A2A Channel 全面实现**：AgentCard 发现端点 / Task 生命周期 / SSE 流式 / JSON-RPC 编解码 | 提供完整实现代码 |
| 3 | **ACP Channel 全新设计**：AID Registry / JWT 认证 / 注册注销发现心跳 / SQLite 表结构 | 完整 Channel 方案 |
| 4 | **PostgreSQL 共享原则**：独立 DB，连接池隔离 | 避免 LiteLLM Schema 冲突 |

### v0.6 (2026-05-26) — 审查修复版

| # | 变更 | 原因 |
|---|------|------|
| 1 | **Router 禁止修改 delivery_mode**：新增 `original_delivery_mode` 字段 | 矛盾1：Router 强行将组播/广播改为 UNICAST，破坏 Session 边界 |
| 2 | **Session.send_message 增加 Unicast 前置校验**：非 UNICAST 模式直接抛出 ValueError | 矛盾2：组播/广播不应进入 pending_messages + 重传逻辑 |
| 3 | **DeliveryResultStatus 重命名**：原 `DeliveryStatus` 改为 `DeliveryResultStatus`，区分 Session ACK vs Delivery Result | 矛盾3：ACK 语义双重标准 |
| 4 | **Session 拆分为 Session + SessionRuntime**：asyncio.Queue/Task 移入 SessionRuntime | 缺陷1：Session 混入不可序列化对象，SQLite/Redis 存储必然失败 |
| 5 | **Lane Queue 补充慢消费者保护**：新增 `max_processing_time` + `drop_policy` 字段 | 缺陷2：缺失慢消费者保护 |
| 6 | **GroupRegistry 文档明确单实例限制**：标注 Phase 5 持久化 | 缺陷3：纯内存 Dict，多实例部署时订阅状态不一致 |
| 7 | **Broadcast 改用 `get_broadcastable_identities()` 抽象方法** | 缺陷4：微信/企微无法枚举全量用户 |
| 8 | **Router 新增接口层**：SessionProvider / SubscriptionResolver / BroadcastIdentitiesProvider | 原则违反：Router 直接依赖具体类，违反 DIP |
| 9 | **SessionConfig 补充新字段**：`max_processing_time` + `drop_policy` | 配置与实现对齐 |
| 10 | **已解决问题表格更新**：Q10-Q18 新增，v0.6 修复内容全量记录 | 审查报告全量处理 |

### v0.5 (2026-05-26)

| # | 变更 | 原因 |
|---|------|------|
| 1 | **Router 重写**：Rule Engine / Intent Engine / Bridge Engine → 三层投递调度（Unicast/Multicast/Broadcast） | 核心职责不清，Gateway Core 只做"投递"不做"处理" |
| 2 | **新增 `delivery_mode`**：`GatewayMessage` 增加 `DeliveryMode` 枚举字段 | 区分点播/组播/广播三种投递模式 |
| 3 | **新增 `subscription_group`**：`GatewayMessage` 增加组播订阅组 ID 字段 | 组播投递时查找 GroupRegistry |
| 4 | **新增 `GroupRegistry`**：组播订阅组注册中心（跨 Channel 订阅） | 支持多 Channel 的订阅组管理 |
| 5 | **新增 `DeliveryReceipt`**：投递回执（Multicast/Broadcast 使用） | 记录每个接收者的送达状态 |
| 6 | **Session 仅 Unicast**：组播/广播不记 Session，无 ACK/重传 | 业务逻辑（多轮对话）才需要 Session |
| 7 | **Intent Engine / Bridge Engine 移出 Core**：→ Adapter 层自行实现 | 减少 Gateway Core 的强依赖，保持协议无关 |
| 8 | **新增 `RouterConfig`**：三层投递调度配置（组播/广播开关、重试次数） | 可按需启用/禁用组播和广播 |
| 9 | **目录结构更新**：`engine/` 改为仅含投递调度（router.py/group_registry.py/delivery_receipt.py） | 与新设计对齐 |

### v0.4 (2026-05-26)

| # | 变更 | 原因 |
|---|------|------|
| 1 | **删除 `correlation_id`** | Gateway Core 只做路由，不做请求-响应配对 |
| 2 | **`sender_id` → `channel_id` + `identity_id`** | 区分 Channel 实例和用户身份 |
| 3 | **Session / Lane Queue 公式改为 `channel_id:identity_id`** | 一对一同构，Session 和 Lane Key 统一 |
| 4 | **Lane Queue 并入 Session** | 一对一关系，无需独立 Lane 生命周期 |
| 5 | **Layer 5 修正**：LLM Executor 归 Adapter 层 | Gateway Core 不含 LLM 调用 |
| 6 | **`SenderInfo` class 删除** | 功能由 `ChannelEndpoint` 覆盖 |

### 更早版本

| 问题 | 状态 | 解决方案 |
|------|------|---------|
| Session 循环依赖 | ✅ 已修复 | `messages` 改为 `list[dict]` |
| messages 应为 dict 而非 object | ✅ 已修复 | SessionStore 序列化策略明确 |
| 流程图反向 | ✅ 已修复 | A2A 数据流已正确 |
| ACK/retry/DLQ 设计缺失 | ✅ 已修复 | Session 重传流程 9.5 节 |
| 缺少 A2A/ACP 配置字段 | ✅ 已修复 | 配置模型已包含 |
| SessionStore 接口缺失 | ✅ 已修复 | 8.3 节 |
| ChannelRegistry 缺失 | ✅ 已修复 | 5.5 节 |

