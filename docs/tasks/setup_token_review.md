# setup_token 文档审核报告（验证子 agent）

> 审核对象：`docs/tasks/setup_token_features.md`（v1，465 行，spec-writer 产出）
> 审核人：验证子 agent ｜ 结论：**有条件通过**——P0×2（S2 定档改稿、S3 测试隔离补规格）按第四节整改表
> 改稿后**放行进编码，无需重回审核轮**；P1×1、P2×6 由文档作者/编码子 agent 一并吸收。

---

## 0. 核对方法与对账基准

- **全文逐条对照**三条用户拍板决策、401 双轨文案、覆盖率铁律与 §0 硬约束。
- **现场实读**：`src/botflow/auth.py`（全文）、`admin_api.py`（全文）、`core.py:270-350`、`config.py`（全文）、
  `common/logger.py`（全文）、`static/admin/index.html`（auth 相关段 92-98/450-597/829-839）、
  `tests/test_admin_auth.py`（全文 726 行）、`tests/test_admin_auth_e2e.py`（全文 116 行）、
  `tests/test_core_runtime.py`（lifespan 段 264-471）、`pyproject.toml`、`.gitignore`、`docs/design.md:285-324`；
  `tests/` 目录 glob → **无 `conftest.py`**。
- **机械计数**：用例表逐行点数、12 项必须覆盖映射逐行核对（不信作者自报）。
- **实验复核（本机 Windows 实跑）**：`os.fchmod` 在 win32 **存在且调用不抛**，但 fchmod 后
  `stat.S_IMODE` 仍为 `0o666`（权限位钉不死）→ 坐实 R6「双轨断言」**既必要又可执行**（spy 断言实参可行，
  stat 断言在 Windows 必挂——文档已按双轨定档，T1.3/T1.4 均列入，正确）。
- **对账基准（主 agent 已核 3 项事实）**：① `_require_admin_key` 定义 `auth.py:168`、活调用 `auth.py:190`、
  `admin_api.py:17` import + `:153` 调用（实读一致）；② `lifespan` 在 `core.py:284`，`setup_logging` 在
  `core.py:297-300`（先于 `db = _get_db()` @ `:316`——日志行挂其后天然满足）；③ `env_prefix="BOTFLOW_"`
  在 `config.py:65`，`Path(__file__).resolve().parents[2]` 从 `src/botflow/config.py` 出发 = 仓库根
  （`parents[0]`=src/botflow、`[1]`=src、`[2]`=仓库根）。**三项与文档表述全部一致，无矛盾。**

---

## 一、P0 表（7 项必查 + 安全红线）

| 编号 | 核对点 | 结论 |
|---|---|---|
| S1 | **P0 安全**：status/login/logout 及免鉴权出口绝不返回 token 明文/哈希；明文只落文件+启动日志；KV 只存 sha256；`compare_digest` 常数时间；文件 0600 且**无条件 fchmod 写进行为**（非只写风险） | ✅ **通过**。证据：硬约束 1/2（第 18-21 行）+ §1.3（79-83，明确「KV 哈希同样不出口」）；存储布局表（115）`{"hash","created_at"}` 无明文；F3 步骤 3（162-164）sha256-hex + `compare_digest`；F1（132-133）**「fchmod 无条件执行：修 umask 掩码 + 收紧过宽旧权限」是行为级规格**，R2 只是补充论证；T5.1（324）锁 key 集合**且断言响应全文不含明文/哈希**，A4（370）验收复核；login 出口回的是会话 token（既有行为，非 setup token），logout 只回 `{"success":true}`（`admin_api.py:214` 实读） |
| S2 | **P0 可用性**：写文件失败 fail-fast 定档是否成立；与四分支生命周期是否自洽 | ❌ **不符 → 整改 ZG-1**。独立评估见第一节下方；另有文档内部矛盾点名（死锁论 ↔ 分支③） |
| S3 | **P0 测试隔离**：conftest 级 autouse fixture 强制 `BOTFLOW_SETUP_TOKEN_FILE=tmp_path`；R6 双轨断言可执行 | ❌ **不符（隔离规格完全缺失）→ 整改 ZG-2**。§3 测试计划（250-252）只写了「本任务新文件用 tmp_path」；glob 确认 `tests/conftest.py` 不存在；R5 回归面**漏点名 `tests/test_core_runtime.py::TestLifespan` 5 个既有用例**（346/376/399/422/439，均真实驱动 `core.lifespan`）——F2 接线后它们会走启动生成分支②，把 `.setup_token` **写进仓库根、污染工作树**（F7 把它加进 .gitignore 后连 `git status` 都看不见，污染转暗）。R6 可执行性 ✅（见 §0 实验） |
| S4 | **回归面完整性**：R5 对既有 74 用例点名 vs 实际行号/语义；T6.3 语义重写（500→401）合理性 | ✅ **通过**（抽核 11 处，全对）：① `SETUP_401` @ test_admin_auth.py:**48** ✓；② `_setup()` @ **135**（`token=ADMIN_KEY` 默认）✓；③ T6.2 @ **450** ✓；④ T6.3 @ **455**（原断言 500 + `admin_key=""` 前置，实读一致）✓；⑤ T6.13 @ **531**（断言 detail=SETUP_401，随 ① 改常量即联动）✓；⑥ client fixture @ **84**（`set_config(BotflowSettings(admin_key=...))`）✓；⑦ e2e `_setup` @ test_admin_auth_e2e.py:**61**（`token=ADMIN_KEY`）✓；⑧ e2e 注释「开通（admin key）」@ **80** ✓；⑨⑩ T3.1/T3.2 直调 @ **270/274**、import @ **34** ✓；⑪ 预期不变清单（T4.1–T4.13、T6.4/5/6/10/12 的 422 由 Pydantic 先拦、T7/T8/T1/T2、TI.4、test_admin_api/test_auth）逐类核语义均成立。**T6.3 重写定档自洽**：原 500 = `_require_admin_key` 未配置分支（`auth.py:168-176`），切换后 setup 只依赖 KV token 记录、admin key 彻底解耦（T3.8 `admin_key=""` 仍 200），「无 KV 记录 → 401 `Invalid setup token.`」与 F3 步骤 2/「不再存在 500 分支」（170）完全自洽，且与 R4「401 不区分错因、不泄状态」一致 → **维持 401 正确**（附 P2-6 可选增强） |
| S5 | **文案双轨不串门**：setup=`Invalid setup token.` / Bearer·会话=`Invalid admin key.`；status 形状；index.html 不改结论 | ⚠ **部分通过 → P1-1**。双轨 ✅：硬约束 3（22-25）、F3 步骤 4（165）只改 setup；T3.2/T3.3 断言新文案、T5.3 断言 `"Invalid admin key."` 不动（326）、映射表 352 双守；实测 `verify_admin_key` 文案在 `auth.py:199/212`，不动 ✅。status 形状 ✅：不变项 5（93）`{success, configured, username}` 与 `admin_api.py:145` 实现逐字段一致，T5.1 锁死。**index.html「不改」对字段结构成立**（实读 :97-98 Token/用户名/密码三字段、:581 `doSetup` 发 `{token,username,password}`，字段与请求形状确实不用动），**但 3 处文案在切换后失实/误导**：:98 placeholder「输入管理员密钥」引导用户填 `BOTFLOW_ADMIN_KEY`——恰是 R9（394）要消灭的旧行为、且按 T3.3 必 401；:590 注释「token 错 → 401 "Invalid admin key."」、:594 注释「仍要 BOTFLOW_ADMIN_KEY 才能改账号」切换后均为**假注释** → **P1-1**（改 3 处文案不动结构，或文档显式记录为接受的代价，二选一） |
| S6 | **机械计数**：42=37+5；正17/反17/边8；12 项映射 | ✅ **全部复核无误**（逐条点表）：T1=8、T2=11、T3=8、T4=5、T5=5 → 单测 37；TI=5 → 合计 42=37+5 ✓。分档：单测正 3+4+2+3+2=**14**、反 2+4+5+1+3=**15**、边 3+3+1+1=**8**；集成正 3（TI.1/3/4）、反 2（TI.2/5）→ 总计**正 17/反 17/边 8**，与 357-358 行自报**完全一致**（含「以逐条表为准」兜底句）。12 项必须覆盖映射（342-355）逐行核：每项至少 1 个用例、用例 ID 全部真实存在于上表，无悬空引用 |
| S7 | **F8/F9 落点**：配置项字段名/env 名/默认路径推导；`.gitignore`；design.md 端点表 | ✅ **通过**。`setup_token_file: str = ""` + `env_prefix="BOTFLOW_"`（config.py:65）→ env 名 `BOTFLOW_SETUP_TOKEN_FILE` 推导正确；`parents[2]` = 仓库根 = 生产 `/mnt/deploy/botflow` 推导正确（F8，212-219）；`.gitignore` 现 67 行、`/_*.py` 在 64 行**匹配不到** `.setup_token`（通配不含点前缀名），F7 加 `/.setup_token` 正确；F9 对 design.md:295（现状句「由 verify_admin_key 保护」本就不含 auth 4 端点、需补）与 :301（setup 行改写，含「明文见服务器 .setup_token,0600;开通成功即销毁」）**两处点位与实读行号一致、说明写清** |

### S2 独立评估（定档① fail-fast vs 非致命告警+顺序保证）——审核意见，供文档作者改稿

**推荐：放弃 fail-fast，改档为「先写文件成功 → 再写 KV；写文件失败 loguru ERROR 大字告警、不中止启动」。** 四条论据：

1. **文档的死锁论与其自家分支③矛盾（内部交叉表述矛盾，点名）**。F1（139-140）与 R7（392）称：
   「文件写不进去而 KV 已写哈希 = 死锁态（**永远开不了通**）」。但决策 2 分支③（61 行）明文规定：
   「KV **有**记录但**文件丢失** → 换新重新生成」——写文件失败若未残留文件，**下次启动分支③ 自动轮换自愈**，
   根本不是「永远」。真正不可自愈的只有一种形态：**`O_TRUNC` 已截断、`os.write` 才失败 → 磁盘留下空文件
   + KV 有哈希 → 分支④ 判「双在」永不动**。而这一形态 **fail-fast 恰好救不了**：首启抛异常中止，
   重启后分支④ 不再写文件、不再抛，服务「健康」上线，凭证已死且无告警——比不 fail-fast 更隐蔽。
   即：**fail-fast 挡不住它唯一声称要挡的死锁，却要全体用户为它付出风险**。
2. **比例失当（主 agent 论点成立）**：这是 LLM 网关，setup token 只是开通便利功能。fail-fast 把
   「一个便利文件写不进」放大为 lifespan 抛出 → supervisor 起不来 → **全量 502**（F2，152 行已明示
   「lifespan 抛出 → 服务启动失败（预期）」）。未配置态写文件失败即整体停服，对一个代理服务不成比例。
3. **顺序保证才是根治**：把分支②③ 的动作序改为**先写文件（0600+内容校验）成功 → 再写 KV** 后，
   失败路径全部自愈：文件失败 → 无 KV 记录 → 下次启动分支② 重新生成（T1.8 已覆盖孤儿文件方向）；
   文件成功但 KV 写失败 → 文件在、无 KV → 下次分支② 覆盖重生成；分支③ 轮换同理先落新文件再盖哈希。
   「KV 有哈希、明文永失」的状态**在写入序上就不可能形成**，无需靠停服兜底。
4. **暴露面不减**：非致命方案要求**每次启动失败必打一条 loguru ERROR**（含路径与 errno），A1 验收
   （首次部署看日志）+ 运维 `cat` 失败即第一现场；持续性故障（目录只读）在每个重启周期重复告警，
   可发现性足够。DB/KV 写失败属库故障，`set_config` 自然上抛导致启动失败是合理范围，不必专门豁免。

**随之必须同步改稿的四处（见整改表 ZG-1）**：决策 2 分支②③ 动作序（60-61 行）、F1 错误分支（139-140）、
F2 错误分支（152）、R7 定档（392）、**T2.10 期望从「抛异常」改写为「不抛 + loguru error 可捕获 +
KV 未写 + 文件不存在」**（294 行；T2.11 不动）。

---

## 二、P1 表

| 编号 | 问题 | 建议改法 | 归属 |
|---|---|---|---|
| P1-1 | **index.html 3 处文案在切换后失实/主动误导**（S5 遗留）：:98 placeholder「输入管理员密钥」诱导填 `BOTFLOW_ADMIN_KEY`（按 T3.3 必 401，正是 R9 要消灭的混淆）；:590 注释仍写「401 "Invalid admin key."」、:594 注释仍写「仍要 BOTFLOW_ADMIN_KEY 才能改账号」——切换后均为假注释。「index.html 不改」只对**三字段结构**成立 | 二选一并在文档中定死：**(a) 推荐**——允许改 3 处纯文案（placeholder→「输入 .setup_token 内容（见服务器/启动日志）」、两处注释同步新文案/新来源），**字段结构与请求形状仍不动**，不违反不变项 7 的「结构」限定；(b) 维持零改动，但 §1.4 不变项 7 或 R9 须**显式记录**「占位文案与 2 处注释过时为已知代价」 | 文档作者改稿（不动代码） |

## 三、P2 表（编码/写测试阶段顺手吸收，不阻塞放行）

| 编号 | 问题 | 建议 |
|---|---|---|
| P2-1 | F1 签名写 `ensure_setup_token(db) -> None`（134），F2 却 `await ensure_setup_token(db)`（149）——同步/异步声明不一致 | F1 改为 `async def ensure_setup_token(db) -> None` |
| P2-2 | TI.2 用「Bearer/body 双形式」打 setup（335）：setup 只读 body 的 token 字段，**Bearer 形式实际命中的是「空 token 短路 401」**，并非验证 admin key 被拒 | 措辞改准：Bearer 形式明确标注「走空 token 短路（附带守卫）」，「admin key 被拒」以 body 形式（token=ADMIN_KEY）为准；或拆成两条断言说明 |
| P2-3 | 分支①④ 对**已存在文件**做 fchmod 收紧失败时的处置未写（F1 只写了写文件失败与删文件失败两种） | 归入 ZG-1 同一定档：收紧失败按「文件操作失败」处理（非致命 ERROR 或按新档统一），一句话写进 F1 错误分支 |
| P2-4 | §1.5 存储布局（119-120）文字残缺：「32 hex + 换行与否定死」 | 改为通顺表述，如「换行与否已定死：纯 32 hex、无换行」 |
| P2-5 | R5 ②「连带影响…T6 用例」名单不全：`_setup()` 默认 token 还被 **T5.2/T5.4、T7.3–T7.11、T8.6、test_x2 之外的 T7.x 成功用例**调用（T7.3/4/5/6/7/8/10/11、T8.6 实读均走 `_setup`） | 无需逐个改（helper 默认值一改全好、故列入「预期不变」也成立），但 R5 ② 补一句「连带经 helper 修复的还有 T5.2/T5.4/T7.3-T7.11/T8.6」，防验证阶段误判漏改 |
| P2-6 | F3「无 KV 记录 → 401」（160-161）：客户端只看到统一 401，运维无从区分「token 错」与「服务端从未生成」 | 可选：该分支服务端 `loguru error` 一条（不改响应）；配合 ZG-1 非致命档后此日志就是文件写失败链路的第二现场 |

---

## 四、整改记录表（打回项，由文档作者改 features 文档，本审核不回改）

| 编号 | 问题 | 建议改法（改稿落点） | 归属 |
|---|---|---|---|
| **ZG-1** | **S2/P0：写文件失败 fail-fast 定档不成立**——死锁论与分支③ 自相矛盾，且唯一真死锁形态（截断后写失败残留空文件+KV→分支④ 永锁）fail-fast 救不了；比例上不值当拿全量网关 availability 换 | 改档为「**先写文件成功 → 再写 KV；写文件失败 loguru ERROR 告警、不中止启动**」。同步改 5 处：① 决策 2 分支②③ 动作序（60-61 行，文件先于 KV）；② F1 错误分支（139-140，删 fail-fast/死锁论，写明失败路径自愈推理 + 每次启动必打 ERROR）；③ F2 错误分支（152，删「lifespan 抛出→启动失败（预期）」对文件失败的表述）；④ R7（392 行重写定档与影响列）；⑤ **T2.10（294）期望改写**：不抛、loguru error 可捕获、KV 未写、文件未残留半成品（T2.11 不动）。分支①④ 的 fchmod 收紧失败处置随 P2-3 一并写清 | 文档作者改稿，**无需重回审核轮** |
| **ZG-2** | **S3/P0：测试隔离规格缺失**——无 conftest 级 autouse fixture；接线后 `test_core_runtime.py::TestLifespan` 等 5 个既有用例（:346/:376/:399/:422/:439，真实驱动 `core.lifespan`）会把 `.setup_token` 写进仓库根污染工作树，且 F7 加 ignore 后 `git status` 不可见、污染转暗；R5 回归面也漏点名这 5 条 | §3 测试计划补一段硬要求：**新增/落 `tests/conftest.py` 级 autouse fixture，`monkeypatch.setenv("BOTFLOW_SETUP_TOKEN_FILE", str(tmp_path/".setup_token"))`**（或等价隔离：保证任一 `BotflowSettings()` 构造都在该 env 生效之后），使一切触发 lifespan 的既有用例落 tmp_path；同时 **R5 回归面补点名 `tests/test_core_runtime.py::TestLifespan`×5**（预期不变，但依赖该 fixture 才不变）。R6 双轨断言保留原样（已实测可执行） | 文档作者改稿，**无需重回审核轮** |
| P1-1 | index.html 3 处文案失实/诱导（见二） | 见 P1-1，二选一定档 | 文档作者改稿 |
| P2-1~P2-6 | 见第三节 | 编码+写测试阶段顺手吸收 | 编码子 agent / 验证子 agent 跑测时 |

---

## 五、结论：**有条件通过**

- **计数：P0×2（ZG-1 S2 定档、ZG-2 S3 隔离）、P1×1、P2×6。**
- 文档整体质量高：行号级锚点抽核 30+ 处**零偏差**（含主 agent 对账基准 3 项）、机械计数与 12 项映射
  **逐行复核全对**、S1 安全红线写成行为级规格而非风险描述、R5 对既有 74 用例的必改/不变分档经 11 处
  实读抽查全部成立、T6.3 的 500→401 重写与「admin key 彻底退出开通通道」自洽、四分支生命周期与三条
  拍板决策逐条对得上、覆盖率计划（UNCOVERED=0 + 双命令）与 pyproject 实况一致。
- 两项 P0 均为**方向明确、落点局部的改稿**，不涉及方案重构：ZG-1 按第一节 S2 评估改 5 处定档表述 +
  T2.10 期望；ZG-2 补一段 conftest fixture 规格 + R5 补点名 5 条。
- **放行条件：文档作者完成 ZG-1、ZG-2（P1-1 一并定档）后，直接进入编码子 agent，无需回本审核轮复审**
  （复审只核这三处改动的落位）；P2×6 编码/写测试阶段吸收。
- **S2 明确推荐（一句话）**：改档为「先写文件成功、再写 KV；写文件失败仅 ERROR 告警不中止启动」——
  顺序保证使死锁不可形成、fail-fast 挡不住唯一真死锁却让网关陪葬，比例与自愈性均不如非致命方案。
