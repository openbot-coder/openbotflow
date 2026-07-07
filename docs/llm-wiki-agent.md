# llm-wiki-agent 项目分析参考

> 来源: [SamurAIGPT/llm-wiki-agent](https://github.com/SamurAIGPT/llm-wiki-agent)
> 用途: MemWiki 设计参考和对比

## 概述

llm-wiki-agent 是一个 **Coding Agent 技能**（非服务器），通过 `AGENTS.md` / `CLAUDE.md` 指令文件告诉编码 Agent（Claude Code、Codex、Gemini CLI）如何维护一个知识库。

## 核心架构

```
AGENTS.md / CLAUDE.md  ──→ 告诉 Agent 怎么做
     │
     ├── ingest:  Agent 读 source → 调 LLM(JSON输出) → 写文件到 wiki/
     ├── query:   Agent 读 index → 关键词匹配 → LLM 合成答案
     ├── lint:    读所有页 → LLM 语义分析 → 报孤儿/矛盾/缺口
     └── health:  health.py(纯确定性,0 LLM调用)
```

**形态**: 不是 MCP 服务，不是数据库，不是 API。就是一个**指令集** + 可选的 Python 工具脚本。

## 关键技术选型

| 方面 | 选型 |
|------|------|
| 存储 | 纯文件系统：`wiki/` 目录下 markdown 文件 |
| LLM 调用 | `litellm` 库（支持 OpenAI/Anthropic/Gemini 等），通过 `LLM_MODEL` / `LLM_MODEL_FAST` 环境变量切换 |
| 文件转换 | `markitdown`（PDF/Word/PPT/HTML 转 markdown） |
| 知识图谱 | NetworkX + Louvain + vis.js（`graph/graph.html`） |
| 前端协议无 | 直接文件系统操作 |

## 目录结构

```
raw/                     # 不可变的原始文档
wiki/                    # Agent 维护的知识层
├── index.md             # 所有页面的目录目录（每次 ingest 更新）
├── log.md               # 追加变更日志
├── overview.md          # 跨所有来源的综合总结
├── sources/             # 每个源文档一页摘要
├── entities/            # 人物/公司/项目（自动创建）
├── concepts/            # 思想/框架/方法（自动创建）
└── syntheses/           # query 答案保存为 wiki 页
graph/
├── graph.json           # 持久化节点/边数据（SHA256 缓存）
└── graph.html           # vis.js 交互可视化
```

## 页面格式

每个 wiki 页使用 YAML frontmatter：

```yaml
---
title: "Page Title"
type: source | entity | concept | synthesis
tags: []
sources: []       # list of source slugs
last_updated: YYYY-MM-DD
---
```

使用 `[[WikiLink]]` 语法交叉引用。

## Ingest 流程

```
1. 读源文档（非 markdown 自动用 markitdown 转换 .md）
2. 读 wiki/index.md 和 wiki/overview.md 获取当前上下文
3. 调 LLM（一次调用）返回结构化 JSON：
   { title, slug, source_page, index_entry, overview_update,
     entity_pages[], concept_pages[], contradictions[], log_entry }
4. 写所有页面到磁盘
5. 更新 index.md、overview.md、log.md
6. 后置验证：检查断链、索引覆盖
```

## Query 流程（核心参考）

```
query("attention mechanisms 是什么？")
  │
  ├── 阶段1: 页面选择
  │   1. 读 wiki/index.md（全量目录）
  │   2. 关键词匹配标题（CJK 滑动窗口 / Latin 分词）
  │   3. 如有 graph.json → 扩展邻居节点（置信度>=0.7）
  │   4. 如结果<2页 → 调 LLM 从 index 中选页
  │   5. 上限 15 页
  │
  └── 阶段2: LLM 合成
      1. 读选中页完整内容
      2. 调 LLM（claude-3-5-sonnet）合成答案 + [[WikiLink]] 引用
      3. 可选保存为 syntheses/<slug>.md
```

**关键设计**: 分两阶段——阶段1（搜索）确定且低成本，阶段2（合成）调 LLM。当搜索匹配充分时，阶段1可跳过阶段2。

## 健康检查分层

| 维度 | `health` | `lint` |
|------|----------|--------|
| 范围 | 结构完整性 | 内容质量 |
| LLM 调用 | 零 | 需要（语义分析） |
| 成本 | 免费 | 消耗 Token |
| 频率 | 每次会话 | 每 10-15 次 ingest |
| 检查 | 空文件、索引同步、日志覆盖 | 孤儿、断链、矛盾、缺口 |

## 与 botflow MemWiki 对比

| 维度 | llm-wiki-agent | MemWiki |
|------|---------------|------------|
| 形态 | Coding agent skill（指令集） | MCP Server + Memory Agent + Skills（三层架构） |
| 依赖 | litellm + 调用方 Agent | botflow provider 系统（fast model） |
| 存储 | 纯文件（`wiki/`） | 纯文件（`{workspace}/MemWiki/`） |
| 查询方式 | index 关键词 + LLM 选页 + LLM 合成 | **ripgrep** + Agent 自主决策 |
| 多格式支持 | markitdown | markitdown |
| LLM 在流程中 | ingest/query/lint 全链路 | Memory Agent ReAct 循环 + call_llm 工具 |
| 路径安全 | 无 | URI 风格路径 + sandbox 校验 |
| 图谱 | NetworkX + vis.js（内置） | 无（未来可加） |
| 跨会话 | git 管理 | git 管理 |

## 对我们的关键启发

1. **query 不必调 LLM** — 它的 query 调 LLM 是因为没有 grep，只能靠 LLM 理解 index。我们有 ripgrep，Agent 可以自主搜索
2. **index.md 是核心入口** — 每次操作都维护 index，保持“目录即索引”
3. **ingest 全由 LLM 驱动** — 一次 LLM 调用完成全部知识提取 + 矛盾检测，效率高（我们的 learn 由 Agent + call_llm 工具实现类似效果）
4. **health/lint 分层** — 确定性检查和 LLM 语义检查分离，节能降本（我们的 dream 类似 health 层）
