# dashboard_stats 功能点与测试用例：概览页分组调用趋势图 + 六档固定时间范围统计

> 任务类型：管理面板统计增强（概览页新增「按日分组调用趋势图」+ 给趋势图与模型 Top 10 配一条**共用**的六档固定时间选择器）
> 范围（用户两条拍板 + 主 agent 工程定档，全部硬约束，见 §1）：① 趋势图按**模型组（model_groups）分系列**、X 轴=东八日期，次数/tokens 卡内 tab 切换；② 六个时间选项（最近半小时/最近一小时/当天/当周/当月/近90天）由**一条选择器同时驱动**趋势图与 Top 10；③ 新端点 `GET /admin/stats/trend`；④ `GET /admin/stats/models` 增加可选 `range` 参数与 `total_tokens` 列（向后兼容）。
> 关联（现状引用，行号为当前 `0b00321` 实测；db/models 实际带 `storage/` 中缀）：`admin_api.py:30`（prefix `/admin`）、`:522-530/:533-541/:544-552`（stats 三端点）、`storage/db.py:96-117`（call_logs DDL）、`db.py:154-156`（created_at/model_id/group_id 索引）、`db.py:782-801`（create_call_log，`:790` `datetime('now')` 写 UTC）、`db.py:1068-1087`（list_model_stats）、`db.py:1089-1108`（list_group_stats）、`db.py:1045-1066`（get_cost_summary）、`storage/models.py:90-112`（CallLog 字段）、`auth.py:185-220`（verify_admin_key 双通道，401 文案 `:203-206/:216-219`）、`config.py:57,59`（retention）、`storage/daily_summary.py:188-205`（清理任务）、`index.html:8`（ECharts CDN）、`:156-186`（dashboard 段）、`:163-178`（Top10 卡）、`:467-469`（api() Bearer）、`:632`（既有调用）、`:810-818`（switchPage 刷新）、`docs/design.md:314-316`（stats 端点表）、`pyproject.toml:54-57`（integration marker + addopts）、`tests/test_admin_api.py:22-35/:148-171`（fixture + 既有 stats 用例）、`tests/test_db_full.py:86`。
> **版本**：v2（**定稿**：v1 经 spec-reviewer 审核「有条件通过 P0×1/P1×2/P2×6」，9 项整改全部落地——P0 按方案 B（models 零新增键 + 前端 requestId 序号守卫 + trend 保留回显）、P1 计数 6+1 与 D3 AUTH 统一声明、P2 六项逐条修正；实现原语细化 zoneinfo→timezone(+8)（R5 实测证据）。放行进编码。）

---

## 0. 硬约束（违反即打回）

1. **向后兼容红线：`/stats/models` 不带 `range` 时与现实现逐字段一致**——聚合口径（全时间）、
   `ORDER BY total_calls DESC`、`LIMIT` 缺省 20、`api_key_id` 过滤、既有 **6** 键（`model_id`/
   `model_name`/`total_calls`/`success_calls`/`error_calls`/`total_cost`，`db.py:1076-1080` 实读 6 列）
   的键名与取值一字不改；**唯一允许差异 = 行内新增第 7 键 `total_tokens`**（主 agent 定档明示的新增列，
   旧消费方忽略未知键，不构成破坏），**响应顶层键集同样零新增（无 `range` 回显键）**，D4.1 逐字段断言守卫。
2. **非法 `range` → 422，校验方式定死 FastAPI `Literal`**（`pyproject.toml:11` fastapi>=0.115）：
   `Literal["half_hour","hour","today","week","month","d90"]`。不写手写 if 校验、不写 400 分支；
   `_resolve_range` 在 Literal 之后，**不设兜底错误分支**（不可达 = 死代码，直接删）。
3. **缺省两套，都不许动**：`/stats/trend` 缺省 `range="week"`（默认当周，趋势图有 7 根柱可看）；
   `/stats/models` 缺省 `range=None` = 现行为全时间聚合（兼容约束 1）。
4. **时区固定 Asia/Shanghai = UTC+8，不随服务器时区/DST/配置变**；`call_logs.created_at` 是 UTC 空格
   串（`YYYY-MM-DD HH:MM:SS`，`db.py:790` `datetime('now')` 写入）→ 东八边界比较前减 8 小时转 UTC 串；
   **按日分桶用 sqlite `date(created_at, '+8 hours')`**，保证「今天」桶与 range 边界同口径。实现原语
   **定死 `datetime.timezone(timedelta(hours=8))`（stdlib 零依赖）**——原定 zoneinfo 在测试平台
   （Windows/Py3.13）实测抛 `ZoneInfoNotFoundError` 且需第三方 tzdata，违约束 6，语义不变的细化，见 R5。
5. **鉴权零新增面**：`/stats/trend` 只挂 `Depends(verify_admin_key)`（与 `:522/:533/:544` 既有 stats
   端点一致，双通道在 `auth.py:185-220`）；401 detail `"Invalid admin key."` 一字不改；不新增免鉴权出口。
6. **零第三方依赖**：只用 stdlib（时区原语见 R5、`Literal` 来自 `typing`）；不引 pandas/numpy/pytz/
   tzdata；不引新前端框架、不加构建步骤（单文件 Vue3 CDN，ECharts 沿用 `index.html:8` jsdelivr）。
7. **数据源只查 `call_logs`，不依赖 `daily_summaries`**：call_logs 保留 180 天（`config.py:59`
   `call_logs_retention_days=180`，清理 `daily_summary.py:198-205`），daily_summaries 只留 7 天
   （`daily_summary.py:194` 按 `raw_session_retention_days=7` 清）→ 近 90 天窗口必须落在 call_logs。
8. **不动的端点/函数**：`/stats/groups`、`/stats/cost`、`db.list_group_stats`、`db.get_cost_summary`
   一行不改（D4.3/D4.4 回归守卫）。
9. **AGENTS.md 精神 + 项目记忆沉淀（MEMORY.md）硬约束照抄**：编码+测试 **100% 覆盖**（botflow 包
   TOTAL **0 missed**，AGENTS.md）、不可覆盖行标 `# UNCOVERED: [原因]`（AGENTS.md）；**死代码直接删**、
   **中文提交无 BOM**、**外网验收走 mq3**、**绝不在部署目录跑 pytest** 四条出自项目记忆 **MEMORY.md**
   沉淀（非 AGENTS.md 原文，reviewer P2-1 更正），同样照办。
10. **本步只交本文档**：spec-writer 不碰 `src/`、`tests/`（双 Agent 流程第 1 步，交 spec-reviewer 审核）。

---

## 1. 背景与定案

### 1.1 背景与需求（用户原话）

概览页面：① 增加分组调用趋势图（按日汇总），次数和 tokens；② 模型调用统计 (Top 10) 增加固定时间选项：最近半小时、最近一小时、当天、当周、当月、近90天。

现状：概览页（`index.html:156-186`）只有 4 张 stat cards + 一张 Top10 表（`:163-178`，列=模型/调用次数/成功/失败/花费，`:632` `api('/stats/models?limit=10')` 全时间聚合），无时间维度、无图表；ECharts CDN 已在 `:8` 引入但概览页从未使用。

### 1.2 定案（用户拍板两条 + 主 agent 工程定档，均为硬约束）

**用户拍板 1 — 趋势图「分组」= 按模型分组（model_groups）分系列**：每个组一条线/一根柱，X 轴=日期（东八）。**不是**按 provider、不是按 api_key。

**用户拍板 2 — 六个时间选项 = 两个图表共用一条时间选择器**：点选一处、两图齐刷。**不做**各图独立选择器。

**工程定档 A — `range` 预设枚举（query 参数字面值）**：

| 字面值 | 中文标签（前端） | 窗口语义 | since 计算（定档） |
|---|---|---|---|
| `half_hour` | 最近半小时 | 相对滑动窗口 | `now_utc − 30min` |
| `hour` | 最近一小时 | 相对滑动窗口 | `now_utc − 60min` |
| `today` | 当天 | 东八自然日 | 东八今日 `00:00:00` → 减 8h 转 UTC 串 |
| `week` | 当周（缺省） | 东八自然周 | 东八本周一 `00:00:00` → 减 8h 转 UTC 串 |
| `month` | 当月 | 东八自然月 | 东八本月 1 日 `00:00:00` → 减 8h 转 UTC 串 |
| `d90` | 近90天 | 相对滑动窗口 | `now_utc − 90天` |

- `until` **一律 = `now_utc`**（含）；两端恒为 UTC 空格串 `%Y-%m-%d %H:%M:%S`；SQL 比较 `created_at >= ? AND created_at <= ?`（同格式字典序 = 时间序）。
- **两类预设换算差异（必须写清）**：`half_hour/hour/d90` 相对滑动——`datetime.now(timezone.utc)` 直接加减，**不碰东八**；`today/week/month` 东八自然边界——先把 `now` 转 UTC+8 求本地 `00:00:00`/周一/1 日，再**减 8 小时**变回 UTC 串（`created_at` 存 UTC）。分桶仍统一 `date(created_at,'+8 hours')`，`today` 首桶与 since 必然同日。
- 非法值 → **422**（FastAPI `Literal`，§0.2）；缺省两套见 §0.3。

**工程定档 B — 新端点 `GET /admin/stats/trend`**（挂 `admin_router`，prefix `/admin` @ `admin_api.py:30`）：

- 鉴权 `Depends(verify_admin_key)`（§0.5）；参数 `range: Literal[...] = "week"`（缺省 week）。
- 响应（行式，定死）：
  ```json
  {"success": true, "range": "week",
   "trend": [{"day": "2026-09-24", "group_id": 1, "group_name": "default",
              "calls": 12, "tokens": 3456}]}
  ```
  `calls = COUNT(*)`；`tokens = COALESCE(SUM(total_tokens), 0)`；`day` 为东八日期串 `YYYY-MM-DD`。
- **只返回有数据的 (day, group) 行，空窗不补零**（前端 pivot 补，F6）；空数据 → `"trend": []`（200）。
- **INNER JOIN `model_groups`**（口径与 `list_group_stats` @ `db.py:1102` 一致）→ `group_id` 为 NULL 的行天然排除（D1.4）。

**工程定档 C — `GET /admin/stats/models` 升级（向后兼容）**：

- 新增可选 `range: Optional[Literal[...]] = None`（同枚举；None=现行为，§0.1/§0.3）。
- SELECT 增加 `COALESCE(SUM(cl.total_tokens), 0) AS total_tokens` 列（**恒定存在，不按 range 分支**——一条 SQL 保无聊，兼容定义 §0.1）。
- `limit` 缺省 20 不动（前端传 `limit=10`）；`api_key_id` 过滤不动；`list_group_stats` / `/stats/cost` **不动**。
- **响应顶层键集零新增**（仍是 `success/model_stats`，**不回带 `range` 回显**，F5 原样）；两图竞态守卫走**前端请求序号 `requestId`**（R9/F6），不依赖 models 回显；trend 端点的 `range` 回显保留（定档 B）。

**工程定档 D — db 层与纯函数**：

- 新方法 `Database.list_group_trend(since_utc: str, until_utc: str) -> list[dict]`（`storage/db.py`）。
- `Database.list_model_stats` 加 `since_utc/until_utc: Optional[str] = None` 两参（None 时不加时间条件，保 §0.1 兼容）。
- range→窗口 = 独立纯函数 `botflow.admin_api._resolve_range(range: str, now=None) -> tuple[str, str]`：`now` 可注入（测试固定时刻），返回 UTC 空格串 `(since, until)`；无 I/O、无全局态。
- 时区：`datetime.timezone(timedelta(hours=8))` 模块常量（`CN_TZ`），**不引第三方**（R5）。

**工程定档 E — SPA（`index.html`，Vue3 + ECharts，`echarts@5` CDN @ `:8` 已引入）**：

- **时间选择器按钮组**：位置 = stat cards（`:157-162`）与 Top10 卡（`:163-178`）之间；六个固定中文标签（最近半小时/最近一小时/当天/当周/当月/近90天），**默认选中「当周」**；点选同时刷新趋势图 + Top 10（两请求都带新 range）。
- **新卡片「分组调用趋势图」**：紧跟选择器之后、Top10 卡之前；ECharts，X=日期（东八），**卡内两个 tab 切换「次数 / tokens」**（主 agent 定档：避免多组 × 2 指标十余条线过乱）——当前 tab 下每分组一个 series，按日桶 pivot，**缺日在前端补 0**（补法见 F6，坑见 R3）。
- **Top10 表加一列 tokens**：表头顺序 `模型 / 调用次数 / 成功 / 失败 / tokens / 花费`（`index.html:167`）。
- **调用改造**：`:632` → `/stats/models?limit=10&range=<选中>`；新增 `api('/stats/trend?range=<选中>')`；`switchPage('dashboard')`（`:813`）与选择器点击都触发两图刷新。
- **鉴权**：SPA 走既有 `api()`（`:467-469` 自动带 `Authorization: Bearer <session>`），新端点同样只挂 `verify_admin_key`——**不新增鉴权面**（§0.5）。

### 1.3 不变项（显式声明，逐条不动）

| # | 不变项 | 现状锚点 |
|---|---|---|
| 1 | `/stats/groups` 响应与实现 | `admin_api.py:533-541`、`db.py:1089-1108` |
| 2 | `/stats/cost` 响应与实现（按 UTC 日 `DATE(created_at)` 分桶，与本任务东八口径不同——差异记录在 R2，不改） | `admin_api.py:544-552`、`db.py:1045-1066` |
| 3 | `verify_admin_key` 双通道与 401 文案 `"Invalid admin key."` | `auth.py:185-220`（`:203-206/:216-219`） |
| 4 | `/stats/models` 无 `range` 时的聚合口径/排序/limit/api_key_id 过滤 | `admin_api.py:522-530`、`db.py:1068-1087` |
| 5 | 既有 stats 单测原样通过 | `tests/test_admin_api.py:148-171`、`tests/test_db_full.py:86` |
| 6 | SPA 鉴权方式（session Bearer 走 `api()`）与单文件 Vue CDN 形态 | `index.html:467-469`、`:8` |
| 7 | 数据保留策略：call_logs 180 天、daily_summaries/raw_sessions 7 天 | `config.py:57,59`、`daily_summary.py:188-205` |
| 8 | ECharts 继续走 jsdelivr CDN，不本地打包 | `index.html:8` |

### 1.4 规模与性能基线（写作用例与 R1 时对照）

生产 `call_logs` ~6.2 万行；5 groups / 7 providers / 572 models。`call_logs(created_at)` **已有索引**（`db.py:154`，另有 `:155/:156` model_id/group_id）→ 窗口过滤走 range scan，GROUP BY 只在子集聚合。**结论：无需补迁移加索引**（R1 定档依据）。

---

## 2. 功能点清单

### F1 range 解析纯函数（`src/botflow/admin_api.py` 新增）

- **行为**：模块常量 `CN_TZ = timezone(timedelta(hours=8))`（Asia/Shanghai 别名，固定 +8，R5）；
  `RANGE_VALUES = ("half_hour","hour","today","week","month","d90")`（两端点 **`Literal[RANGE_VALUES]`
  单一出处定死**——元组展开 Py3.13 实测可行；校验只走 FastAPI `Literal` 自动 422，不写手写校验分支）；
  `_resolve_range(range: str, now: datetime | None = None) -> tuple[str, str]`：
  - **precondition**：`now` **仅测试注入用**——生产调用一律不传、走 `datetime.now(timezone.utc)`；注入时要求 aware UTC（docstring 写明，不加运行时校验，§0.2）；
  - `now is None` → `datetime.now(timezone.utc)`；
  - `half_hour/hour/d90`：`since = now − 30min/60min/90天`（直接 UTC 加减）；
  - `today`：`local = now.astimezone(CN_TZ)` → `local.replace(hour=0,minute=0,second=0,microsecond=0)` → `astimezone(timezone.utc)`；
  - `week`：同上求本地今日 00:00 → 减 `local.weekday()` 天（周一=0）→ 转 UTC；
  - `month`：同上求本地今日 00:00 → `replace(day=1)` → 转 UTC；
  - `until = now`；两端 `strftime("%Y-%m-%d %H:%M:%S")`（空格格式，无 T、无微秒、无时区后缀）。
- **输入输出**：`(range, now)` → UTC 空格串 `(since, until)`，恒 `since <= until`。
- **错误分支**：**没有**——非法值已被端点层 `Literal` 422 拦截（§0.2），函数内不写 else/raise 兜底
  （不可达 = 死代码，直接删）。测试直调只测六个合法值（D2.x）。
- **涉及文件**：`src/botflow/admin_api.py`。

### F2 db 新方法 `list_group_trend`（`src/botflow/storage/db.py` 新增）

- **行为**：紧邻 `list_group_stats`（`db.py:1089-1108` 之后）新增：
  ```sql
  SELECT date(cl.created_at, '+8 hours') AS day,
         mg.id   AS group_id,
         mg.name AS group_name,
         COUNT(*) AS calls,
         COALESCE(SUM(cl.total_tokens), 0) AS tokens
  FROM call_logs cl
  JOIN model_groups mg ON mg.id = cl.group_id
  WHERE cl.created_at >= ? AND cl.created_at <= ?
  GROUP BY day, mg.id, mg.name
  ORDER BY day ASC, mg.id ASC
  ```
  完整可执行原文如上（别名 `cl`/`mg` 与 `list_group_stats` 惯例一致）；参数 = `(since_utc, until_utc)`。
- **输入输出**：`list[dict]`，键 = `day/group_id/group_name/calls/tokens`；只含有数据的 (day, group) 行；
  `day` 是东八日期串（`+8 hours` 与 §0.4 边界换算同口径）；INNER JOIN → `group_id IS NULL` 行排除。
- **错误分支**：无（查询无匹配 → 空列表，D1.5）。
- **涉及文件**：`src/botflow/storage/db.py`。

### F3 db `list_model_stats` 升级（`src/botflow/storage/db.py:1068-1087`）

- **行为**：签名加 `since_utc: Optional[str] = None, until_utc: Optional[str] = None` 两参；`where` 追加
  条件仅当**两者都非 None**（调用方保证成对传，F1 输出即成对）：`AND cl.created_at >= ? AND
  cl.created_at <= ?`（插在 `api_key_id` 条件之后、`GROUP BY` 之前）；SELECT 恒定加一列：
  ```sql
  SELECT m.id AS model_id, m.name AS model_name,
         COUNT(*) AS total_calls,
         SUM(CASE WHEN cl.status='success' THEN 1 ELSE 0 END) AS success_calls,
         SUM(CASE WHEN cl.status='error'   THEN 1 ELSE 0 END) AS error_calls,
         COALESCE(SUM(cl.cost), 0.0)         AS total_cost,
         COALESCE(SUM(cl.total_tokens), 0)   AS total_tokens
  FROM call_logs cl JOIN models m ON m.id = cl.model_id
  WHERE {where}
  GROUP BY m.id, m.name
  ORDER BY total_calls DESC LIMIT ?
  ```
- **输入输出**：同现 `list[dict]` + 新键 `total_tokens`（NULL 行由 COALESCE 归 0，R4/D1.8）；
  `since/until=None` → 不加时间条件 = 全时间现行为（§0.1）。
- **错误分支**：无新分支（None 判断即全部）。
- **涉及文件**：`src/botflow/storage/db.py`。

### F4 新端点 `GET /admin/stats/trend`（`src/botflow/admin_api.py`）

- **行为**：紧邻 stats 段（`:517-552` 内、`/stats/cost` 之后）新增 `async def get_trend_stats(range:
  Literal[...] = "week", _=Depends(verify_admin_key))` → `_resolve_range(range)` →
  `db.list_group_trend(since, until)` → `{"success": True, "range": range, "trend": rows}`。
- **输入输出**：query `range` 六枚举缺省 `week` → §1.2 定档 B 的 JSON；非法 range → 422（FastAPI 自动 validation detail，不自定义文案）。
- **错误分支**：非法 range 422；未鉴权/假 key 401（`verify_admin_key` 原样）；空数据 200 `trend: []`；**业务层无 500、无 404 分支**——依赖层 500 照旧：admin key 未配置时 `verify_admin_key`→`_require_admin_key` 内部抛 500（`auth.py:175-182`，既有行为，与既有 stats 三点相同，非本端点引入）。
- **涉及文件**：`src/botflow/admin_api.py`。

### F5 端点 `GET /admin/stats/models` 升级（`src/botflow/admin_api.py:522-530`）

- **行为**：签名加 `range: Optional[Literal[...]] = None`；`range is not None` 时 `_resolve_range` 透传
  `since_utc/until_utc`，否则透传 `(None, None)`；响应仍是 `{"success": True, "model_stats": stats}`——**键集不加不减**（`total_tokens` 在行内，D4.1）。
- **输入输出**：无 range = 现行为 + 行内新键 `total_tokens`；带 range = 窗口内聚合 + 新键。
- **错误分支**：非法 range 422（Literal）；其余分支不变。
- **涉及文件**：`src/botflow/admin_api.py`（端点）、`storage/db.py`（F3）。

### F6 SPA 概览页改造（`src/botflow/static/admin/index.html`）

- **行为**（四处）：
  1. **时间选择器按钮组**：插在 stat cards（`:157-162` 结束）与 Top10 卡（`:163` 起）之间；六按钮绑 `rangeSel` ref（初值 `'week'`），选中态复用既有样式；点击 → `rangeSel.value = v` → 并行 `loadStats()` + `loadTrend()`。
  2. **趋势图卡**：选择器后新增卡片「分组调用趋势图」，卡头右侧两 tab（次数/tokens）绑 `trendMetric` ref（初值 `'calls'`）；卡体 `<div ref>` 容器 + ECharts 实例（懒初始化一次，`switchPage`/数据到达 `setOption`）；`if (!window.echarts)` 显示「图表组件加载失败」文本（CDN 挂了不抛 JS 错，R7）。series = 每 `group_name` 一条，X = 期望日轴。
  3. **pivot 补零定档**：`loadTrend()` 拿 `trend[]` 后——
     - **日轴** = `[min(返回的 day) … max(max(返回的 day), 东八今天)]` 逐日连续；东八今天用固定 +8 公式 `new Date(Date.now() + 8*3600*1000).toISOString().slice(0,10)`（浏览器时区无关，与后端同口径，R3/R6）；返回空 → 显示既有 `.empty` 文案；
     - **分组集** = 返回行 `group_name` 去重；每个 (day, group) 缺失 → 补 `0`；
     - `trendMetric` 切换 → 同一份原始 `trend[]` 重新 pivot 后 `setOption`（不重发请求）。
  4. **Top10 改造**：`:167` 表头插 `tokens` 列（模型/调用次数/成功/失败/tokens/花费），行渲染插 `<td>{{s.total_tokens||0}}</td>`；`:632` 改 `api('/stats/models?limit=10&range='+rangeSel.value)`；`loadAll()` 拆出 `loadStats()`/`loadTrend()`（`switchPage` `:813` 与选择器共用）。
- **输入输出**：仅 **trend 端点**回带 `range` 回显（`/stats/models` 响应键集零新增、不回带，F5/§1.2 定档 C）；两图随选择器同步刷新；**竞态守卫 = 前端请求序号**：SPA 维护单调递增 `requestId`，发起时记录，响应回来 `requestId < 最新` 则丢弃——**趋势图与 Top 10 共用同一个 `requestId`**（一次切换同时作废两图过期响应，R9）。
- **错误分支**：单接口失败沿用 `Promise.allSettled` + toast（`:626-644`），一张图挂不拖死另一张；echarts 缺失降级文本；401 沿用既有会话失效逻辑；**响应回来先比对请求序号（`requestId` 非最新则整批丢弃），两图同序号统一作废**（防连点乱序，R9）；trend 的 `range` 回显保留仅供调试/A 验收核对，不参与守卫判定。
- **涉及文件**：`src/botflow/static/admin/index.html`。

### F7 `docs/design.md` 端点表同步（`docs/design.md:314-316`）

- **行为**：`:314` `/admin/stats/models` 功能列补「（可选 range 六档固定时间窗）」；`:314-316` 间新增一行 `| GET | /admin/stats/trend | 分组按日调用趋势（range 六档） |`。**只动这两处，不重写第 5 节。**
- **涉及文件**：`docs/design.md`（文档改动，无输入输出/错误分支）。

### 涉及文件总表

| 文件 | 改动 |
|---|---|
| `src/botflow/admin_api.py` | 新增 `CN_TZ`/`_resolve_range`（F1）、新端点 `/stats/trend`（F4）、`/stats/models` 加 range 参数（F5） |
| `src/botflow/storage/db.py` | 新增 `list_group_trend`（F2）、`list_model_stats` 加两参 + tokens 列（F3） |
| `src/botflow/static/admin/index.html` | 选择器 + 趋势图卡 + pivot 补零 + Top10 tokens 列 + 调用改造（F6） |
| `docs/design.md` | `:314` 补注、新增 trend 行（F7） |
| `tests/test_dashboard_stats.py` | **新增文件**：D1.x–D4.x 单测（32 条，§3） |
| `tests/test_dashboard_stats_e2e.py` | **新增文件**：TI.1–TI.3，文件级 `pytestmark = integration`（§3） |
| `src/botflow/storage/models.py` / `auth.py` / `config.py` / `storage/daily_summary.py` / `/stats/groups` / `/stats/cost` | **零触碰**（§0.8、§1.3） |

---

## 3. 测试用例清单

> 本任务 spec 阶段**不写 `tests/`**；下表供验证子 agent 落进：**单测 D1.x–D4.x → `tests/test_dashboard_stats.py`**（新文件）
> ——db 层直调 `Database`（`tmp_path` 建库 + `initialize()`，照 `tests/test_admin_api.py:22-35` 的 client/AUTH
> fixture 模式挂 `app.dependency_overrides[dbmod.get_db]`）；`_resolve_range` 直调并**注入 `now`** 固定时刻；
> **集成 TI.1–TI.3 → `tests/test_dashboard_stats_e2e.py`**（新文件），**文件级 `pytestmark = integration`**。
>
> **命令硬要求**：`pyproject.toml:57` 的 `addopts = ["-m", "not integration"]` 对指名文件同样生效——集成
> 命令**必须显式带 `-m integration`**，单测走 AGENTS.md 100% 覆盖门槛命令：
>
> ```bash
> PYTHONPATH=src python -m pytest tests/ --cov=botflow -m "not integration"           # 单测（门槛）
> PYTHONPATH=src python -m pytest tests/test_dashboard_stats_e2e.py -m integration    # 集成（必做）
> ```
>
> **seed 惯例（D1/D3/D4 通用）**：`created_at` 由 `db.py:790` `datetime('now')` 写入 → 需指定时刻的用例
> （D1.3/D1.6 等）用裸 `conn.execute("INSERT INTO call_logs (..., created_at) VALUES (..., ?)")` 显式给
> UTC 空格串；其余用 `create_call_log(CallLog(...))` 造数。分组先行：`model_groups` 2 组 + `models` 2 型
> + `group_models` 关联（INNER JOIN 依赖）。

### D1.x db 层（单测，直调 `list_group_trend` / `list_model_stats`）

| 用例 ID | 类型 | 描述 | 期望 |
|---|---|---|---|
| D1.1 | 正例 | 两组 × 两天造数 → `list_group_trend(since, until)` 覆盖全窗 | 行式 dict，键恰 `day/group_id/group_name/calls/tokens`；各 (day,group) 的 calls/tokens 与造数一致；`day` 为东八日期串 |
| D1.2 | 正例 | `total_tokens` 为 NULL 的行混入（列可空） | 该行不炸且计 0：`COALESCE` 生效，`tokens` = 非 NULL 行之和 |
| D1.3 | 边界 | 窗口卡点：行 `created_at == since`、`== until`、窗外前后各一行 | 两端**含**（`>=`/`<=`），窗外两行不出现 |
| D1.4 | 反例 | `group_id = NULL` 的 call_log 行 | **被排除**（INNER JOIN `model_groups`），不出现在任何行 |
| D1.5 | 反例 | 空表 / 窗口内无数据 | 返回 `[]`，不抛 |
| D1.6 | 边界 | **跨 UTC 日界分桶**：行 A `created_at="2026-09-23 16:30:00"`（东八=09-24 00:30）、行 B `="2026-09-24 00:30:00"`（东八=08:30） | 两行 `day` 都是 `"2026-09-24"`（`date(created_at,'+8 hours')` 与东八边界同口径，R3 核心守卫） |
| D1.7 | 正例 | `list_model_stats(since_utc, until_utc)` 窗口过滤 | 只统计窗内行；窗外行的 calls/tokens/cost 均不计入 |
| D1.8 | 正例 | `list_model_stats` 行含 `total_tokens`，且混入 NULL tokens 行 | 新键存在；NULL 归 0 后累加正确（COALESCE） |
| D1.9 | 边界 | `list_model_stats()` 缺省（不传两参） | 与升级前同数据全时间聚合；**行含新键 `total_tokens`**（§0.1 唯一差异） |
| D1.10 | 正例 | 多模型不同调用量 | `ORDER BY total_calls DESC` 排序与 `LIMIT ?` 生效（回归 `db.py:1084` 语义） |
| D1.11 | 边界 | 同 day 多组、多 day | `ORDER BY day ASC, group_id ASC` 稳定序（前端不依赖排序，但断言防漂移） |

### D2.x range 解析（单测，直调 `_resolve_range`，**注入固定 `now`**）

| 用例 ID | 类型 | 描述 | 期望 |
|---|---|---|---|
| D2.1 | 正例 | `now = 2026-09-24 03:00:00 UTC`（东八 11:00 周四），六枚举逐一 | 输出恰为两段 UTC 空格串 `("...","2026-09-24 03:00:00")`；格式无 `T`/微秒/时区后缀 |
| D2.2 | 边界 | **today 跨 UTC 日界**：`now = 2026-09-23 16:30:00 UTC`（东八已是 09-24 00:30） | `since = "2026-09-23 16:00:00"`（东八今日 00:00 减 8h）、`until = now` |
| D2.3 | 边界 | **week 起于周一**：`now = 东八周一 00:30`（UTC 前一日 16:30） | `since = 前一 UTC 日 16:00:00`（即东八周一 00:00 转 UTC） |
| D2.4 | 边界 | **week 止于周日**：`now = 东八周日` | `since = 本周一（东八）00:00 转 UTC`，窗口含整周至 now |
| D2.5 | 边界 | **month 月初与月中**各一：`now = 东八 10-01 00:10` / `now = 东八 10-15 12:00` | 两者 `since` 均 = 东八 10-01 00:00 → UTC `09-30 16:00:00`；`until` = 各自 now（月初窗口不为空、不越上月） |
| D2.6 | 正例 | `half_hour` / `hour`：注入 now 直接加减 | `since = now−30min / now−60min`（UTC 直算，不经东八） |
| D2.7 | 正例 | `d90` | `since = now−90天`（UTC 直算），`until = now` |
| D2.8 | 边界 | 恒等式 | 六者一律 `since <= until` 且 `until == 注入的 now`（秒级截断） |

### D3.x 端点（单测，HTTP 层 `TestClient`）

> **AUTH 统一前置声明：除 D3.4 鉴权反例按描述（无头 / 假 key）外，D3.1–D3.9 全部带 `headers=AUTH`。**
> 依据：FastAPI 求值顺序 = 先跑 `Depends(verify_admin_key)` 子依赖、后校验本层 query 参数——不带 AUTH 时
> 401 先于 422/200 抛出，D3.2/D3.3/D3.5/D3.6/D3.7 的 200/422 期望只在带 AUTH 前提下成立（D3.3/D3.6：
> AUTH 先过鉴权 → 本层 `Literal` 校验产出 422；D3.2 缺省外的合法值：AUTH 先过 → 200）。

| 用例 ID | 类型 | 描述 | 期望 |
|---|---|---|---|
| D3.1 | 正例 | `GET /admin/stats/trend`（缺省，带 AUTH） | 200；键恰 `success/range/trend`；`range == "week"`（缺省 week）；`trend` 元素形状同定档 B |
| D3.2 | 正例 | `?range=today` 造今日数据 | 200；`range` 回显 `"today"`；行落在东八今日桶 |
| D3.3 | 反例 | `?range=fortnight`（枚举外） | **422**（FastAPI Literal 校验，非 400/500） |
| D3.4 | 反例 | 鉴权：无 Authorization → `/stats/trend`；`Bearer wrong-key` → `/stats/models?range=hour` | 均 **401**，detail 恰 `"Invalid admin key."`（文案不漂移） |
| D3.5 | 正例 | `GET /admin/stats/models?range=hour&limit=10` 造窗口内外数据（带 AUTH） | 200；**顶层键集恰 `success/model_stats`（无 `range` 回显键，键集零新增）**；`model_stats` 每行含 `total_tokens`；仅窗内行计入 |
| D3.6 | 反例 | `GET /admin/stats/models?range=bogus` | **422** |
| D3.7 | 边界 | 空库调 `/stats/trend` | 200 + `"trend": []`（不是 404/500） |
| D3.8 | 边界 | `/stats/models` 不带 limit | 缺省 20 生效（`admin_api.py:524` 不变），最多返回 20 行 |
| D3.9 | 正例 | **session token 通道**（`app.dependency_overrides` 走登录签发的会话，或直调 `verify_admin_key` session 分支既有测试模式）调 `/stats/trend` | 200（双通道既有行为对新端点同样生效） |

### D4.x 兼容回归（单测，HTTP 层）

| 用例 ID | 类型 | 描述 | 期望 |
|---|---|---|---|
| D4.1 | 正例 | **无 range 旧调用逐字段对账**：同库先记升级前期望（或用固定造数手算），`GET /admin/stats/models`（无 range） | 200；**顶层键集恰 `success/model_stats`（无 `range` 回显键）**；行内**既有 6 键（`model_id`/`model_name`/`total_calls`/`success_calls`/`error_calls`/`total_cost`）键名/值/顺序 + 排序/`limit`/`api_key_id` 行为逐字段一致**，行内新增第 7 键 `total_tokens`（§0.1 红线，`db.py:1076-1080` 实读 6 列） |
| D4.2 | 正例 | `?api_key_id=1` 过滤（照 `tests/test_admin_api.py:154` 语义） | 过滤仍生效，且不带 range 时行为同旧 |
| D4.3 | 边界 | `/admin/stats/groups`、`/admin/stats/cost?days=30` 响应 | **逐字段与改动前一致**（本任务零触碰，D4.3 必须在同 PR 仍绿） |
| D4.4 | 正例 | 既有用例原样通过：`tests/test_admin_api.py:148-171`（stats 三端点）、`tests/test_db_full.py:86` | 全绿（点名回归面） |

### 集成端到端（`tests/test_dashboard_stats_e2e.py`，文件级 `pytestmark = integration`，命令带 `-m integration`）

| 用例 ID | 类型 | 描述 | 期望 |
|---|---|---|---|
| TI.1 | 正例 | 全栈口径一致：真实 `Database` + lifespan/挂载 admin_router，造一批跨 3 天 × 2 组数据，同窗口先调 `/stats/trend?range=d90` 再调 `/stats/models?range=d90&limit=100` | 两图数字自洽：trend 全行 `calls` 总和 == models 全行 `total_calls` 总和；tokens 同理（INNER JOIN 口径差仅来自 trend JOIN groups / models JOIN models——造数保证 group_id/model_id 均有效时二者相等） |
| TI.2 | 反例 | 鉴权三连：无头 401 / `Bearer 假key` 401 / 正确 admin key 200（对 `/stats/trend`） | 401 文案 `"Invalid admin key."`；200 带正确 `range/trend` 形状 |
| TI.3 | 正例 | 兼容共存：同会话先后 `GET /admin/stats/models`（无 range）与 `?range=today` | 均 200；前者行集合 ⊇ 后者（全时间 ⊇ 今日），前者除 `total_tokens` 外形状同旧 |

### 必须覆盖项 → 用例映射（逐项对账）

| 要求项 | 落到 |
|---|---|
| 趋势图按模型组分系列、按日汇总 calls+tokens | D1.1 / D1.11 / D3.1 / TI.1 |
| 东八分桶与 range 边界同口径（跨 UTC 日界） | D1.6 / D2.2 / D2.3 / D2.5 / D3.2 |
| 六档 range 枚举 + 缺省 week | D2.1–D2.8 / D3.1 / D3.2 |
| 非法 range → 422 | D3.3 / D3.6 |
| 半小时/一小时/90天 相对窗口 | D2.6 / D2.7 / D3.5 |
| tokens 聚合含 NULL 行 COALESCE | D1.2 / D1.8 |
| group_id NULL 排除（INNER JOIN） | D1.4 |
| 空数据返回空列表 | D1.5 / D3.7 |
| `/stats/models` 无 range 逐字段兼容 | D4.1 / D4.2 / TI.3 / D4.4（既有用例） |
| `/stats/groups`、`/stats/cost` 不动 | D4.3 / D4.4 |
| 双通道鉴权、401 文案不漂移 | D3.4 / D3.9 / TI.2 |
| 缺省 limit=20 | D3.8 |
| SPA 六选一两图齐刷 / tokens 列 / tab 切换 | A3–A6（前端无单测，验收覆盖，见 §4） |

**用例总数**：**35**（单测 **32**：D1.1–D1.11=11、D2.1–D2.8=8、D3.1–D3.9=9、D4.1–D4.4=4；集成 **3**：TI.1–TI.3）。
按类型：**正例 17 / 反例 6 / 边界 12**（单测 正 15 / 反 5 / 边 12；集成 正 2 / 反 1 / 边 0）。
**分档以各表「类型」列为准**，落地时逐条点表核数，如与本行不符以逐条表为准并更正此行。

---

## 4. 验收要点（外网视角，人工/主 agent 逐条勾；外网验收走 mq3）

| 编号 | 验收步骤 | 期望 |
|---|---|---|
| A1 | 部署后开概览页（`/admin/` → dashboard） | stat cards 与 Top10 之间出现**一条**时间选择器按钮组，六个中文标签，默认选中「当周」 |
| A2 | 页面初始加载 | 趋势图与 Top10 按 `range=week` 出数（网络面板可见 `GET /admin/stats/trend?range=week` 与 `/admin/stats/models?limit=10&range=week`） |
| A3 | 六个选项**逐一**点击 | 每次点击两图**同时**刷新（两个请求的 `range` 同步变化），数字/柱数随窗口变化 |
| A4 | 看趋势图结构 | X 轴=日期（东八）；**每个有数据的分组各出一个 series**（组数以生产实数为准，不写死；图例=组名）；卡内「次数/tokens」tab 切换后 series 换指标但同窗口 |
| A5 | Top10 表头 | 列 = 模型/调用次数/成功/失败/**tokens**/花费；tokens 随选择器刷新 |
| A6 | 选「近90天」 | 出图（生产 ~6.2 万行、索引在 `db.py:154`，应秒出）；趋势图横轴覆盖近 90 天有数据日 |
| A7 | `curl "…/admin/stats/trend?range=xxx"`（枚举外值） | **422** |
| A8 | `curl "…/admin/stats/models"`（**不带 range**，Bearer admin key） | 200，顶层键集仍为 `success/model_stats`（**无 `range` 回显键**）；`model_stats` 行内旧行为逐字段兼容（除新增第 7 键 `total_tokens`）；老脚本不破 |
| A9 | `curl "…/admin/stats/trend"` 带**假** admin key | **401** `{"detail":"Invalid admin key."}`（新端点无免鉴权面） |
| A10 | 东八「今天」核对：造一条刚发生的调用，选「当天」 | 趋势图今天的柱包含它（分桶与边界同口径，无 8 小时错位） |

---

## 5. 风险与边界

| # | 风险/漏洞 | 影响 | 建议/定档 |
|---|---|---|---|
| R1 | **大窗口聚合性能**（d90 扫 6.2 万行 GROUP BY） | 概览页卡顿 | **已缓解，无需补迁移**：`db.py:154` 已有 `idx_call_logs_created_at`（另有 `:155/:156` model_id/group_id），窗口过滤走 range scan、聚合只在子集上；6.2 万行 SQLite 单次聚合毫秒级。**定档不加新索引、不做缓存**（YAGNI）；A6 实测复核 |
| R2 | **东八趋势图与 `/stats/cost` 的 UTC 日口径不一致**（`db.py:1055` 用 `DATE(created_at)`=UTC 日） | 用户对照两处日线差 8 小时 | 记录在案：cost 端点属不变项（§0.8/§1.3-2）**不改**；本任务统东八。文档与 design.md 注明差异，未来若对齐另开任务 |
| R3 | **东八分桶与 UTC 存储错位**（本任务最大正确性风险） | 「今天」桶整体偏 8 小时、today 窗口漏首批 | 双侧统一：since/until 用 `CN_TZ` 求边界再减 8h（F1）；分桶用 `date(created_at,'+8 hours')`（F2）；D1.6/D2.2/D2.3/D2.5/D3.2/A10 四层守卫。**禁止**用 `DATE(created_at)`（UTC 日）做 trend 分桶 |
| R4 | **`total_tokens` 历史 NULL 行**（DDL DEFAULT 0 但列可空，`db.py:108`） | SUM 出 NULL → JSON `null`，前端 `toFixed`/累加炸 | SQL 层 `COALESCE(SUM(...),0)`（F2/F3 定档原文已含）；前端 `s.total_tokens||0` 兜底（F6-4）；D1.2/D1.8 守卫 |
| R5 | **原定 zoneinfo 在测试平台不可用**（实测：Windows/Py3.13 `ZoneInfo("Asia/Shanghai")` → `ZoneInfoNotFoundError`；`import tzdata` → ModuleNotFoundError；pyproject 依赖无 tzdata） | 用 zoneinfo 则单测全挂，或须引第三方 tzdata 违反 §0.6 | **细化定档：`datetime.timezone(timedelta(hours=8))`**——stdlib、跨平台、零依赖；Asia/Shanghai 1991 年后无夏令时，固定 +8 **就是**「不随服务器 DST/配置变」的语义本身（§0.4）。生产 Linux 与 Windows 测试行为一致。已在文档头部 v1 标注；D2.x 全部用例在 Windows 跑通即为验收 |
| R6 | **前端补零导致跨组日期对齐 bug**（各组缺日不同 → 系列错位、ECharts category 轴不一致） | 图上柱/线对错日期 | pivot 定档（F6-3）：**先建统一日轴**（min(返回 day) … max(max(day), 东八今天)，逐日连续），再按轴填每组值、缺失补 0——所有 series 共用同一 `xAxis` 数组，**禁止**各 series 自带 data 日期；东八今天用固定 `+8h` 的 `toISOString` 公式（浏览器时区无关）；D1.11 + A4 验收 |
| R7 | **ECharts CDN（`index.html:8` jsdelivr）不可达** | 趋势图卡抛 JS 错，可能拖死整页 Vue | 既有风险非本任务引入（§1.3-8 不改 CDN 形态）；F6-2 定档一行守卫 `if (!window.echarts)` → 卡内降级文本，**不抛错、不阻断其余卡**；不引本地打包 |
| R8 | **range 缺省变化对旧前端/脚本的影响** | 旧调用行为漂移 | `/stats/models` 缺省 None=现行为（§0.3，D4.1/TI.3 守）；`/stats/trend` 是全新端点无旧消费者；SPA 自身改为**始终显式传 range**（A2）。不存在「缺省从全时间悄悄变当周」的破坏 |
| R9 | **快速连点选择器 → 两图响应乱序**（旧 range 的慢响应后到，覆盖新数据） | 图与选中态不一致 | 前端低成本定档（方案 B，主 agent 拍板）：**前端请求序号守卫**——SPA 维护单调递增 `requestId`，发起时记录，响应回来 `requestId < 最新` 则丢弃；**趋势图与 Top 10 共用同一个 `requestId`**，一次切换同时作废两图过期响应；`/stats/models` 响应键集零新增（无 `range` 回显可用，也不需要），trend 的 `range` 回显保留仅作调试/A 验收。写进 F6 错误分支，验收 A3 快速连点抽查 |
| R10 | **`_resolve_range` 被绕过 Literal 直调**（内部函数无校验） | 非法值 KeyError | 明确**不设**兜底分支（§0.2：不可达=死代码）；调用点仅两处端点，均在 Literal 之后（grep 守卫：全仓 `_resolve_range(` 调用点 ≤ 3，含定义） |

---

## 6. 覆盖率计划

### 预期新增/改动可执行行（源码侧）

| 落点 | 预期行数（量级） |
|---|---|
| `admin_api.py`：`CN_TZ` + `RANGE_VALUES` + `_resolve_range`（6 分支 + 格式化） | ~25–35 行 |
| `admin_api.py`：`/stats/trend` 端点 + `/stats/models` 加参改行 | ~10–15 行 |
| `storage/db.py`：`list_group_trend` + `list_model_stats` 加参/加列 | ~25–40 行 |
| `index.html`：选择器 + 趋势图卡 + pivot + tokens 列 + 调用改造 | 不计 Python 覆盖率（F6，A1–A6 人工验收） |
| `docs/design.md` | 不计 Python 覆盖率 |
| **合计（Python）** | **约 60–90 行** |

### UNCOVERED 声明

- **预期 `# UNCOVERED: [原因]` 标注数 = 0。** 理由：① `_resolve_range` 六分支全覆盖（D2.1–D2.8 注入 now 逐一走到）且不写不可达兜底（§0.2）；② `list_group_trend` 聚合/JOIN/空集由 D1.1–D1.6 直调覆盖；③ `list_model_stats` None/非 None 两态由 D1.7–D1.10 + D4.1 覆盖；④ 两端点 200/422/401 分支由 D3.x + TI.2 覆盖；⑤ `index.html`/`design.md` 非 Python，不进覆盖率分母。
- 若实现中出现确实无法覆盖的行，标 `# UNCOVERED: [原因]` 并在 `dashboard_stats_review.md` 逐条列出；**目标仍是 0 条业务行**（botflow 包 TOTAL 0 missed）。

### 验证命令（AGENTS.md 规定，门槛 100%）

```bash
PYTHONPATH=src python -m pytest tests/ --cov=botflow -m "not integration"          # 单测（门槛）
PYTHONPATH=src python -m pytest tests/test_dashboard_stats_e2e.py -m integration   # 集成（TI.1–TI.3 必做，带 -m integration）
```

> 不用 `pytest tests/ -m integration`：会把 `tests/test_integration.py`（打真实服务 127.0.0.1:4000 的 live 测试）一并无差别收集，无实服务时被无关失败挡住（沿用 admin_auth / setup_token 结论）；`pyproject.toml:57` addopts 对指名文件同样生效。

---

## 7. 明确不做（不改什么）

| # | 不做的事 | 理由 |
|---|---|---|
| 1 | **不做自定义日期区间选择器**（任意起止日期 DatePicker） | 用户没选；六档固定预设已覆盖需求，自定义区间引出输入校验/时区歧义/慢查询三类新问题 |
| 2 | **不做导出**（CSV/Excel/PNG 下载） | 用户没选；YAGNI |
| 3 | **不做按 provider / api_key 维度的趋势** | 用户拍板分组=模型组；provider/api_key 维度既有 `/stats/groups`、`/stats/models?api_key_id=`、`/logs` 已覆盖统计 |
| 4 | **不动 `/stats/groups` 与 `/stats/cost` 及其 db 方法** | 不变项 §0.8/§1.3；cost 的 UTC 日口径差异记录在 R2 不顺手改（改=波及既有用例与消费方） |
| 5 | **不引新前端框架 / 构建步骤 / 本地打包 ECharts** | 保持单文件 Vue CDN 形态（§1.3-6/8）；决策阶梯第 1 级 |
| 6 | **不改任何协议端点**（OpenAI 兼容面、`/v1/*`） | 本任务纯管理面统计；零触碰 |
| 7 | **不改 `verify_admin_key` 签名/文案、不新增免鉴权出口** | §0.5；P0-1 历史结论继续有效（签名动=路由注册即崩） |
| 8 | **不补零在服务端**（trend 只回有数据行） | 主 agent 定档：响应行式紧凑、空窗语义归前端 pivot（F6-3），服务端补 0 要造日期序列表=多写代码多一个错位源 |
| 9 | **不引第三方时区库（pytz/tzdata/zoneinfo+tzdata）** | §0.6/R5：固定 +8 语义 stdlib 一行即全 |
| 10 | **不加 `call_logs` 新索引、不加聚合缓存/物化表** | R1：`db.py:154` 索引已在，6.2 万行直查足够；缓存=YAGNI |
| 11 | **不回改历史任务文档**（`admin_auth_features.md`/`setup_token_features.md` 等） | 只新增本文档 + design.md 两处同步（F7） |
| 12 | **本 spec 步不写 `src/`、不写 `tests/`、不跑 pytest** | 双 Agent 流程第 1 步只出本文档，交 spec-reviewer 审核 |
