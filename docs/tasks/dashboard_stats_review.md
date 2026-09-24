# dashboard_stats 文档审核报告（spec-reviewer）

> 审核对象：`docs/tasks/dashboard_stats_features.md`（v1，498 行，spec-writer 产出）
> 审核人：spec-reviewer ｜ 结论：**有条件通过**——P0×1（range 回显三处矛盾定档改稿）按第四节整改表
> 改稿后**放行进编码，无需重回审核轮**；P1×2、P2×6 由文档作者/编码子 agent 一并吸收。

---

## 0. 核对方法与对账基准

- **逐行对照实际代码**，不采信文档自洽：全部行号引用、函数签名、SQL 原文逐条回源码核对。
- **现场实读**：`admin_api.py`（全文，stats 段 517-552 与 auth 段精读）、`storage/db.py`（DDL 90-183、
  `create_call_log` 782-801、统计段 966-1108、`get_call_logs_for_day` 1321-1328）、`storage/models.py`
  （CallLog 90-112）、`auth.py:168-235`（`_require_admin_key`/`verify_admin_key`）、`core.py`
  （lifespan 284-350、路由挂载 309-314、`purge_old_call_logs` 接线 :378）、`static/admin/index.html`
  （dashboard 段 154-186、`api()` 465-471、`loadAll` 620-650、`switchPage` 805-820）、`config.py:50-70`、
  `storage/daily_summary.py:185-210`、`docs/design.md:310-320`、`tests/test_admin_api.py`（fixture 18-40、
  TestStats 148-171）、`tests/test_db_full.py:80-95`、`pyproject.toml`、`AGENTS.md`、`MEMORY.md` 关键行。
- **机械计数**：35 用例逐条点表复核、13 行必须覆盖映射逐行核对（不信作者自报）。
- **实验复核（本机 Windows/Py3.13.14 实跑）**：
  1. sqlite `date('2026-09-23 16:00:00','+8 hours')` → `'2026-09-24'`；`15:59:59` → `'2023-09-23'`；
     `since == since` 字符串 `>=` 为真 → **东八 00:00 边界与 `+8 hours` 分桶恰同日，无差一天**；
  2. `ZoneInfo("Asia/Shanghai")` → `ZoneInfoNotFoundError`、`import tzdata` → `ModuleNotFoundError`
     → **R5「zoneinfo 在本仓测试平台不可用」属实**，timezone(+8) 细化定档成立；
  3. `typing.Literal[('half_hour','hour')]` → `Literal['half_hour','hour']` → **元组展开可行**（见 P2-5）。
- **grep 复核**：`idx_call_logs_created_at` 索引、`purge_old_call_logs` 接线点、stats 回归面用例、
  E 探针是否断言 stats 键（`grep -ln stats .workbuddy/tmp/_e_probe*.py` 零命中 → 无探针回归面）、
  §0.9 四条措辞在 `AGENTS.md` 中**零命中**（实出 MEMORY.md）。
- 文档头部声明「行号为当前 `0b00321` 实测」——`git log -1` = `0b00321` ✓ 对账基准成立。

---

## 一、P0 表（团队点名 8 项必查 + 矛盾定档）

| 编号 | 核对点 | 结论 |
|---|---|---|
| S1 | **① range→UTC 窗口换算与东八分桶 `date(created_at,'+8 hours')` 一致性（边界会不会差一天）** | ✅ **通过，无差一天**。证据链：`created_at` 由 `db.py:790` `datetime('now')` 写 UTC 空格串，F1 两端 `strftime("%Y-%m-%d %H:%M:%S")` 同格式 → 字典序即时序（实测 `>=` 含端）；`today/week/month` 边界 = 东八 `00:00:00` 减 8h，恰是 `+8 hours` 分桶的日界——**实测 UTC `16:00:00` → 桶 `09-24`（=since 所在东八日）、`15:59:59` → `09-23`（窗外）**，首桶与 since 同日；`half_hour/hour/d90` 滑动窗口本就不是自然日，首桶截半是语义而非 bug；文档 F1/F2/§0.4 双侧同口径声明与 D1.6/D2.2/D2.3/D2.5/A10 四层守卫齐备。附：`until = now` 秒级截断（strftime 无微秒）与 sqlite `datetime('now')` 秒精度对齐，`<=` 含端实测成立 |
| S2 | **② `/stats/models` 向后兼容声明是否逐字段兼容；「逐字节一致」是否要修正** | ⚠ **方向已对但两处硬伤 → P0-1 + P1-1**。(a) 文档**没有**写「逐字节一致」——§0.1 明写「既有字段一字不改，唯一允许的差异是新增键」，这半句实质正确、无需推倒；(b) **但「既有 5 个响应字段」计数错**：括号里列了 `model_id/model_name/total_calls/success_calls/error_calls/total_cost` **6 个**（`db.py:1076-1080` 实读 6 列），「第 6 个键 total_tokens」应为第 7 个，D4.1「既有 5 键」同错（P1-1）；(c) **更重的：`/stats/models` 到底回不回 `range` 回显，文档三处互相矛盾**（F5「响应键集不加不减」 vs F6 输入输出「两个接口都回带 range 回显」 vs R9「两接口都回带 range 正为此」）——R9 竞态守卫依赖 `resp.range`，而 F5 禁止加键，编码 agent 无所适从（P0-1，见第二节独立评估） |
| S3 | **③ 新端点鉴权是否可能泄漏给免鉴权路径** | ✅ **通过**。F4 端点签名显式 `_=Depends(verify_admin_key)`（与 `:526/:537/:548` 既有 stats 三点一致，实读核对）；`admin_router` 无路由级依赖、全靠逐端点挂——文档挂法正确；SPA 在 `core.py:314` 于 REST router **之后**挂载（`:312` 注释「API routes win」实读），`/admin/stats/trend` 先命中 API；不新增免鉴权出口（auth 4 端点集合不变）；401 文案 D3.4/TI.2/A9 三处断言 `"Invalid admin key."`（`auth.py:205/218` 实读双分支同文案）✓。**附 P2-3**：F4「无 500 分支」表述过满——admin key 未配置时 `verify_admin_key`→`_require_admin_key`（`auth.py:175-182` 实读）抛 500，属依赖层既有行为 |
| S4 | **④ 用例清单能否真正覆盖声明的 100%（对照新增代码行数估条数）** | ✅ **通过**。机械计数：**35 = 单测 32（D1=11+D2=8+D3=9+D4=4）+ 集成 3**，分档 正17/反6/边12（单测正 5+3+4+3=15、反 2+0+3+0=5、边 4+5+2+1=12；集成正2/反1）**逐条点表全对**；13 行必须覆盖映射逐行核、用例 ID 全部真实存在。~60-90 新增行 vs 32 单测密度足够；关键分支逐一对上：`_resolve_range` 的 `now is None` 默认行（D2.x 全注入 now）由 **D3.1/D3.2/D3.5 端点路径**覆盖（F4 调 `_resolve_range(range)` 不传 now）✓；`list_model_stats` 的 None/非 None 两态由 D1.7/D1.9/D4.1 ✓；`range is not None` 双分支由 D3.5/D4.1 ✓；422/401 走 FastAPI/依赖层、不占端点可执行行 ✓。UNCOVERED=0 可信。**附 P1-2**：D3.x 的 AUTH 前置未统一声明（见第二节） |
| S5 | **⑤ `call_logs.created_at` 索引查证结论是否属实** | ✅ **属实**。`db.py:154` `CREATE INDEX IF NOT EXISTS idx_call_logs_created_at ON call_logs(created_at)` 实读一字不差；`:155/:156` model_id/group_id 索引同实。R1「窗口过滤走 range scan、无需补迁移加索引」的依据成立（`date(created_at,'+8 hours')` 作用在 SELECT 列、WHERE 是裸列比较，可用索引） |
| S6 | **⑥ 与 AGENTS.md 硬约束（100% 覆盖、死代码、UNCOVERED 标注）的一致性** | ⚠ **实质一致，出处混标 → P2-1**。100% 覆盖（AGENTS.md 测试验收标准 #3 + §121 门槛命令）、`# UNCOVERED: [原因]` 标注、死代码直接删（§0.2「不可达=死代码直接删」与 AGENTS.md 规则 4「优先考虑删除」同向）、集成必做——全部与 AGENTS.md 一致 ✓；验证/集成双命令与 `pyproject.toml:57` addopts 实况一致 ✓。**但 §0.9 写「AGENTS.md 硬约束照抄：…死代码直接删、中文提交无 BOM、外网验收走 mq3、绝不在部署目录跑 pytest」——grep AGENTS.md 四条零命中**，它们实际在 `MEMORY.md:46/48/56/61`（约束本身真实有效，出处标错） |
| S7 | **⑦ SPA 是静态 HTML——文档是否误把前端逻辑纳入 pytest 覆盖口径** | ✅ **未误纳，处理正确**。三处一致声明：§6 覆盖率表 `index.html` 行标「不计 Python 覆盖率（F6，A1–A6 人工验收）」；UNCOVERED 理由⑤「`index.html` 与 `design.md` 非 Python 文件，不进覆盖率分母」；必须覆盖映射表末行「SPA 六选一两图齐刷 / tokens 列 / tab 切换 → A3–A6（**前端无单测**，验收覆盖，见 §4）」。分母口径干净，R6/R7/R9 前端风险全部落到 A1-A6/A3 验收而非测试用例 ✓ |
| S8 | **⑧ 六选项中文标签与 range 枚举映射表是否完整无歧义** | ✅ **完整无歧义**。§1.2 定档 A 表 6 行齐全：`half_hour`=最近半小时 / `hour`=最近一小时 / `today`=当天 / `week`=当周（缺省） / `month`=当月 / `d90`=近90天；F6-1 按钮标签六项与表逐字一致、`rangeSel` 初值 `'week'` 明确英文枚举（A2 网络面板期望 `range=week` 交叉印证）；`Literal[...]` 枚举值 = 表内字面值；缺省双轨（§0.3：trend=`week`、models=`None`）与 D3.1/D4.1 对齐。三处出现（定档 A 表 / F6 按钮 / A1 验收）互相比对零漂移 |

### P0-1 独立评估（`/stats/models` 是否回带 `range` 回显——三处矛盾）——审核意见，供文档作者改稿

**矛盾实录（文档自身三处，逐字）**：

| 位置 | 原文 | 语义 |
|---|---|---|
| F5（:244-245） | 响应仍是 `{"success": True, "model_stats": stats}`——**响应键集不加不减**（total_tokens 在行内，D4.1） | 顶层**不加** `range` |
| F6 输入输出（:270） | **两个接口都回带 range 回显**；两图随选择器同步刷新 | 顶层**要加** `range` |
| R9（:436） | 响应回来先比对 `resp.range === rangeSel.value`，不等则丢弃（**两接口都回带 range 正为此**） | Top10 的乱序守卫**依赖** `resp.range` |

- `/stats/trend` 侧无争议：F4 明写 `return {"success": True, "range": range, "trend": rows}` ✓。
- 矛盾只在 `/stats/models`：**按 F5 落地 → R9 的竞态守卫对 Top10 不可实现（`resp.range` 恒 undefined，比对恒 false/恒真皆错）；按 R9/F6 落地 → 违反 F5「键集不加不减」**。编码 agent 二选一会直接违反另一条硬声明，且 D3.5/D4.1/A8 按哪个写断言不定。
- §0.1（硬约束 1）语境是「聚合口径/排序/LIMIT/api_key_id/**行内既有字段**」，未覆盖顶层键——即 §0.1 **没有**禁止顶层加 `range`，冲突源在 F5 自己加的「响应键集不加不减」句。

**推荐定档（二选一，改稿落点见第四节）**：

- **方案 A（推荐）**：`/stats/models` 顶层增加 `range` 回显（无 range 请求时回 `null` 或省略，定死一种）。
  理由：① R9 是本任务自己引入的竞态风险（六档选择器快速连点），守卫需要回显才有最廉价的实现；②
  §0.1 已有「旧消费方忽略未知键，不构成破坏」的现成论证，顶层加一键与行内加 `total_tokens` 同性质；
  ③ 两接口对称，F6「两个接口都回带」不用改。
  随之改 3 处：F5「响应键集不加不减」句改为「顶层 +`range` 回显键（沿用新增键不破坏论证），
  `model_stats` 行内既有 6 键不变 + `total_tokens`」；§0.1 补一句「顶层新增 `range` 回显不构成破坏」；
  D3.5 补 `range` 回显断言、D4.1 补「无 range 时顶层键集 = 旧 2 键 + range（或无 range 键，按定死形态）」。
- **方案 B**：维持 F5 零新增，删 F6「两个接口都回带」与 R9 括号句，改为「trend 有回显守卫；Top10
  乱序用前端请求序号（或 AbortController）守卫」。代价：R9 半失效、F6 输入输出句必改，前端多一个小机制。

---

## 二、P1 表

| 编号 | 问题 | 建议改法 | 归属 |
|---|---|---|---|
| P1-1 | **§0.1/D4.1 既有字段计数错**：§0.1（:26-30）「既有 **5** 个响应字段」——括号里列的是 **6** 个（`model_id/model_name/total_calls/success_calls/error_calls/total_cost`，`db.py:1076-1080` 实读 6 列）；「唯一允许的差异是新增**第 6 个**键 total_tokens」应为**第 7 个**；D4.1（:370）「既有 **5** 键键名/值/顺序」同错。方向（既有不变+新增列）本对，纯计数硬伤，但 D4.1 是兼容红线守卫用例——照「5 键」落断言会对不上实况（漏锁 1 个既有键） | 三处 5→6、第 6→第 7：§0.1 两句 + D4.1 一句；建议顺手把 6 键**逐名列出**（就用括号里那份），断言写成「6 个既有键逐一对齐 + 行内新增 `total_tokens`」 | 文档作者改稿 |
| P1-2 | **D3.x 的 AUTH 前置未统一声明，422/200 用例可能拿到 401**：D3 表仅 D3.1（「带 AUTH」）/D3.4（鉴权反例）标注了头；D3.2/D3.3/D3.5/D3.6/D3.7/D3.8 **均未写带 AUTH**。FastAPI 求值顺序 = **先跑 `Depends(verify_admin_key)` 子依赖、后校验本层 query 参数** → 不带 AUTH 时：D3.3/D3.6 期望 **422 实得 401**（依赖先抛）、D3.2/D3.5/D3.7 期望 200 实得 401。seed 惯例段只说「照 client/AUTH fixture 模式」（fixture 是 per-request 传 `headers=AUTH`，不是自动注入），歧义真实存在，落地者漏传会误判为实现 bug | D3 表头加一句：「**除 D3.4 反例按描述（无头/假 key）外，D3.1-D3.9 全部带 `headers=AUTH`**」；或逐行补注 | 文档作者改稿 |

---

## 三、P2 表（编码/写测试阶段顺手吸收，不阻塞放行）

| 编号 | 问题 | 建议 |
|---|---|---|
| P2-1 | **§0.9 出处混标**：「死代码直接删、中文提交无 BOM、外网验收走 mq3、绝不在部署目录跑 pytest」四条写「AGENTS.md 硬约束照抄」——`grep AGENTS.md` 四条**零命中**；实出 `MEMORY.md:46`（死代码）、`:48`（BOM）、`:56`（mq3）、`:61`（部署目录 pytest）。约束本身全部真实有效 | 改为「AGENTS.md（100% 覆盖 + UNCOVERED）+ **项目记忆 MEMORY.md**（BOM/mq3/死代码/跑测隔离）硬约束照抄」，或拆两处标源 |
| P2-2 | **行号偏差**：头部引用与 D4.4 写 `tests/test_admin_api.py:152-168`（stats 三端点）——实测 `class TestStats` @ **148**、`test_models` @ 149、`test_cost` 止于 ~170（172 = `class TestLogs`），实际区间 **148-171**；152 落在 test_models 函数体中间、168 漏掉 test_cost 尾断言。（同文件 `:22-35` fixture、`:154` api_key_id 行、`test_db_full.py:86`、`index.html:8/156-186/163-178/167/467-469/632/626-644/813/810-818`、`auth.py:185-214/:203-206`、`db.py` 全部锚点、`design.md:314-316`、`models.py:90-112`、`config.py:57,59`、`daily_summary.py:188-204/:194`、`pyproject.toml:54-57/:11`——**抽核 30+ 处全部精确**，仅此 1 处偏差） | `152-168` → `148-171`（头部与 D4.4 两处） |
| P2-3 | **F4「无 500 分支」表述过满**：admin key 未配置（`BOTFLOW_ADMIN_KEY=""`）时 `verify_admin_key` 第一步 `_require_admin_key`（`auth.py:175-182` 实读）抛 500 `"Server admin key is not configured"`——依赖层 500 照旧存在，属既有行为、非本端点引入 | F4 错误分支改为「**端点自身**无 500/404 分支；admin key 未配置时依赖层 500 为既有行为（与既有 stats 三点相同）」 |
| P2-4 | **`_resolve_range` 注入 `now` 的 precondition 未落文字**：F1 写「注入时要求 aware UTC」但无处落断言——若注入 **aware 非 UTC**（如 UTC+8 aware），`strftime` 会静默输出东八串当 since（窗口错 8h）；注入 naive 则 `astimezone` 按系统本地时区（Windows 测试机 ≠ 生产）。不违反 §0.2「不写运行时兜底」，但测试纪律要钉死 | D2.x 用例说明补一句「注入的 `now` 一律 `datetime(…, tzinfo=timezone.utc)`」；F1 docstring 写 precondition（不加运行时校验） |
| P2-5 | **`Literal[RANGE_VALUES]` 的二选一 hedge 可定死**：F1 写「不可行则两处字面量并列，实现时二选一」——**本机 Py3.13 实测 `typing.Literal[元组变量]` 正常展开**为 `Literal['half_hour',…]`，可行 | 直接定死 `RANGE_VALUES` 单出处方案，删「二选一」句（避免落地选了并列方案后 `RANGE_VALUES` 成死代码，反踩 §0.9） |
| P2-6 | **A4「5 组 5 系列」与 §1.4「5 groups」生产数字存疑**：`MEMORY.md:228` 模型组记录为 fast/free/smart/backup **4 组**（fast=1/free=2/smart=3/backup=4），未见第 5 组 | A4 改为「每模型组一个系列（图例=组名，生产现约 4-5 组）」，验收别把组数钉死成 5；§1.4 生产基线数字标注数据日期 |

---

## 四、整改记录表（打回项，由文档作者改 features 文档，本审核不回改）

| 编号 | 问题 | 建议改法（改稿落点） | 归属 |
|---|---|---|---|
| **ZG-1（P0）** | **S2c/P0：`/stats/models` 是否回带 `range` 回显，F5/F6/R9 三处互相矛盾**——按 F5 落地则 R9 竞态守卫对 Top10 不可实现，按 R9/F6 落地则违 F5「响应键集不加不减」，D3.5/D4.1/A8 断言形态不定 | 按第一节 P0-1 独立评估**二选一定档**：**方案 A（推荐）** `/stats/models` 顶层加 `range` 回显——改 F5「响应键集不加不减」句（:244-245）、§0.1 补顶层键说明（:26-30）、D3.5 补回显断言、D4.1 补顶层键集断言，F6/R9 原句保留；**方案 B** 保 F5 零新增——删 F6「两个接口都回带」（:270）与 R9 括号句（:436），R9 改「trend 回显守卫 + Top10 前端请求序号守卫」。两方案均须在 §1.2 定档 C 同步一句 | 文档作者改稿，**无需重回审核轮** |
| **P1-1** | S2b：§0.1/D4.1 既有字段计数 5→6、第 6→第 7 | 见二节 P1-1（三处改数字，建议 6 键逐名列出） | 文档作者改稿 |
| **P1-2** | S4：D3.x AUTH 前置未声明，422/200 用例可能误得 401 | 见二节 P1-2（D3 表头一句统一声明） | 文档作者改稿 |
| P2-1~P2-6 | 见第三节 | 编码+写测试阶段顺手吸收 | 编码子 agent / 验证子 agent 跑测时 |

---

## 五、结论：**有条件通过**

- **计数：P0×1（ZG-1 range 回显矛盾）、P1×2、P2×6。**
- 文档整体质量高：行号级锚点抽核 **30+ 处仅 1 处偏差**（P2-2 的 152-168，其余——含 index.html 六处、
  db.py 全部 SQL 锚点、auth/design/config/models/pyproject——**逐字精确**）；35 用例机械计数与 13 行
  映射**逐行复核全对**；东八换算/分桶一致性经本机 sqlite **实测无差一天**（S1）；zoneinfo 不可用经
  **实测坐实**（R5 成立）；`idx_call_logs_created_at` 索引**实读属实**（S5）；鉴权无新增面（S3）、
  覆盖口径未把 SPA 误纳入 pytest（S7）、六档枚举↔中文映射三处零漂移（S8）、用例密度足够支撑
  UNCOVERED=0（S4）。
- 唯一阻断项是**文档内部契约矛盾**（P0-1：F5 vs F6/R9 的 `range` 回显），方向明确、落点局部（改 2-4 处
  文句 + 1-2 条断言），不涉及方案重构；两个 P1 均为改数字/补一句的机械修正。
- **放行条件：文档作者完成 ZG-1（range 回显定档）、P1-1、P1-2 后，直接进入编码子 agent，无需回本审核轮
  复审**（复审只核这三处的落位）；P2×6 编码/写测试阶段吸收。
