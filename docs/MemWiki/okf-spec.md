# Open Knowledge Format (OKF) v0.1 参考

> 来源: [Google Cloud Platform / knowledge-catalog / okf / SPEC.md](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
> 用途: MemWiki 的存储格式规范依据

## 概述

OKF 是一种开放、人类与 Agent 友好的知识表示格式。核心设计：

- **一个目录的 markdown 文件 + YAML frontmatter**
- 无需 Schema Registry、无中心化权威、无强制工具链
- 可读、可解析、可 diff、可跨组织/工具/时间移植

## 知识包（Knowledge Bundle）结构

```
path/to/bundle/
├── index.md                      # 可选。目录索引，渐进式披露
├── log.md                        # 可选。变更历史，时间倒序
├── <concept>.md                  # 根级概念
└── <subdirectory>/
    ├── index.md
    ├── <concept>.md
    └── <subdirectory>/
        └── …
```

### 保留文件名

| 文件名 | 用途 |
|--------|------|
| `index.md` | 目录索引（见 §6） |
| `log.md` | 更新历史（见 §7） |

## 概念文档格式

### Frontmatter（YAML）

```yaml
---
type: <Type name>                  # 必填
title: <Optional display name>
description: <Optional one-line summary>
resource: <Optional canonical URI>
tags: [<tag>, <tag>, …]            # 可选
timestamp: <ISO 8601 datetime>     # 可选
# … 其他自定义字段
---
```

- `type` — **必填**。简短标识概念类型，消费方用于路由/过滤/展示
- `title` — 推荐。人类可读显示名
- `description` — 推荐。一句话摘要
- `tags` — 可选。跨分类标签列表
- `resource` — 可选。底层资产的规范 URI
- `timestamp` — 可选。ISO 8601 最后修改时间

### Body

标准 markdown，推荐使用结构化内容（标题、列表、表格、代码块）。

约定节标题：

| 标题 | 用途 |
|------|------|
| `# Schema` | 资产列/字段的结构化描述 |
| `# Examples` | 实际使用示例 |
| `# Citations` | 外部来源引用 |

### 示例

```markdown
---
type: BigQuery Table
title: Customer Orders
description: One row per completed customer order across all channels.
resource: https://console.cloud.google.com/bigquery?p=acme&d=sales&t=orders
tags: [sales, orders, revenue]
timestamp: 2026-05-28T14:30:00Z
---
```

## 交叉链接

- **绝对路径**: `[customers table](/tables/customers.md)`（推荐）
- **相对路径**: `[neighboring concept](./other.md)`
- 链接表示非类型化的有向关系
- 消费方 **必须容忍断链**（目标不存在 ≠ 格式错误）

## 索引文件（index.md）

无 frontmatter，按标题分组：

```markdown
# Section / Group Heading

* [Title 1](relative-url-1) - short description of item 1
* [Title 2](relative-url-2) - short description of item 2
```

## 日志文件（log.md）

可选，时间倒序：

```markdown
## 2026-05-22
* **Update**: Added new BigQuery table reference for [Customer Metrics](/tables/customer-metrics.md).
* **Creation**: Established the [Dataplex Playbook](/playbooks/dataplex.md).
```

## 合规性

OKF v0.1 合规要求：
1. 每个非保留 .md 文件包含可解析的 YAML frontmatter
2. 每个 frontmatter 包含非空 `type` 字段
3. index.md / log.md 遵循 §6 / §7 格式

消费方 **不能** 因以下原因拒绝 bundle：
- 缺少可选 frontmatter 字段
- 未知 type 值
- 未知额外 frontmatter 字段
- 断链
- 缺少 index.md

## 与 botflow MemWiki 的关系

| OKF 概念 | MemWiki 对应 |
|----------|----------------|
| Knowledge Bundle | `{workspace}/MemWiki/` 目录 |
| Concept | 每个 .md 文件 |
| Concept ID | `concepts/rag.md` 等文件路径 |
| type | frontmatter `type` 字段：concept/source/entity/synthesis |
| index.md | `{workspace}/MemWiki/index.md`（自动生成） |
| log.md | `{workspace}/MemWiki/log.md`（自动生成） |
| 交叉链接 | `[[WikiLink]]` 语法（兼容 Obsidian） |
