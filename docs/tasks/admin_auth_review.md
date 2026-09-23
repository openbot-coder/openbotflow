# admin_auth 文档审核报告（验证子 agent）

> 审核对象：`docs/tasks/admin_auth_features.md`（编码子 agent 产出，双 Agent 流程第 1 步）
> 审核人：验证子 agent ｜ 结论：**打回**（3 项 P0 必须整改后重审；P1 建议一并吸收）

---

## 1. 审核对象与方法

- **基准**：`AGENTS.md` 的「测试验收标准」「双 Agent 编码协作流程」「规则清单」「不可偷懒的领域」「决策阶梯」逐条对照。
- **现场核对**：逐条读取文档声称的落点并核实——
  `src/botflow/auth.py`、`src/botflow/admin_api.py`、`src/botflow/storage/db.py`、
  `src/botflow/static/admin/index.html`（89-102 / 417-544 / 719）、`src/botflow/core.py`（中间件）、
  `docs/design.md`（293-318）、`tests/test_auth.py`、`tests/test_admin_api.py`、`tests/test_integration.py`、
  `tests/test_db_full.py`、`pyproject.toml`、`README.md` / `docs/deploy-mq3-*.md`（探针兼容证据）。
- **关键论断实验复核**（脚本落 `.workbuddy/tmp/check_dep.py`、`check2.py`，不入库）：
  1. FastAPI 对「被 `Depends()` 使用的依赖函数中 `db: Database = None` 参数」的分类行为（实跑路由注册）；
  2. `secrets.compare_digest` 对非 ASCII 字符串的行为；
  3. `cleanup_config_by_prefix` 的 `LIKE` 前缀不带 `%` 时的真实删除效果（内存 SQLite 复现 db.py:314-323 SQL）。

---

## 2. 文档准确性核对结果（逐声称 → 属实/不符）

| # | 文档声称 | 核对位置 | 结论 |
|---|---|---|---|
| 1 | `auth.py:93` 是 verify_admin_key 现状 | auth.py:93-113 | **属实**（4 分支：500→提取→compare_digest→401；`_extract_token` 27-34 容忍裸 token） |
| 2 | `admin_api.py:17` admin_router | admin_api.py:17 | **属实** |
| 3 | `db.py:298-323` 为 config KV / cleanup | db.py:298-323（get_config 298 / set_config 304 / cleanup 314-323） | **属实**；`execute_write` 实际在 db.py:281，文档未把它绑定到该行号区间，无误导 |
| 4 | R3：`cleanup_config_by_prefix` 按 `updated_at < now-TTL` 删过期行，**无法删全部** | db.py:314-323 + 实验 | **属实**（实验：不带 `%` 删 0 行；带 `%` 只删过期行）。重置必须另用 `execute_write("DELETE ... LIKE 'admin_sess:%'")` 的结论正确 |
| 5 | index.html 89-102 登录块 / 417-544 api()+doLogin/doLogout | 实读 | **属实**（loginKey 423、api() 430、doLogin 500、doLogout 514、legacy key `bf_admin_key` 423/506/515） |
| 6 | index.html「417-544（…/onMounted）」（文档第 9 行） | 实读 | **不符（轻微）**：`onMounted` 在 **719 行**（第 248 行引用正确，第 9 行行号区间错） |
| 7 | design.md:297-318 有 admin 端点清单表；295 有「由 verify_admin_key 保护」说明句 | design.md:293-318 | **属实**（表格 297-316、318 注、295 说明句均在，F10 补 4 行 + 1 句可落地） |
| 8 | 既有测试模式：test_auth db fixture + `HTTPAuthorizationCredentials` 直调；test_admin_api `TestClient` + `dependency_overrides[dbmod.get_db]` | tests/test_auth.py:15-20,141-187；tests/test_admin_api.py:22-35 | **属实**，用例清单可直接沿用 |
| 9 | 跑测命令与 pyproject 一致 | pyproject.toml:52-74；AGENTS.md:121 | **属实**：`asyncio_mode=auto`、`markers=[integration]`、`addopts=-m "not integration"`、`source=["botflow"]` 均一致；CLI 再写 `-m "not integration"` 与 addopts 重复但无害；`-m integration` 会覆盖 addopts（后者生效）机制正确 |
| 10 | R7：「现有 admin 路由本无限流（限流中间件面向 LLM 代理路径）」 | core.py:542-576 | **不符**：`RateLimitMiddleware` 只跳过 `/health`（core.py:548），**对 `/admin/*` 同样生效**；无 Authorization 的 login/setup 按 `request.client.host` 分桶 300 次/分钟（core.py:522-532）。design.md:390 也写明「跳过 /health」 |
| 11 | F4：`verify_admin_key` 照抄 `verify_llm_key` 的 `db: Database = None` 注入模式 | 实验 + 全仓 grep | **严重不符（P0）**：`verify_llm_key` **从未被 `Depends()` 使用**（src 内仅 import，仅测试直调），其签名从未经过 FastAPI 依赖解析；`verify_admin_key` 被 30+ 条路由 `Depends(...)`（admin_api.py:99-514）。实验实跑：FastAPI 在**路由注册时**即抛 `FastAPIError: Invalid args for response field! <class botflow.storage.db.Database>` → **应用启动即崩、全部 admin 路由无法注册** |
| 12 | F2/F7/T2.10/T7.6：`cleanup_config_by_prefix("admin_sess:", ...)` | db.py:319 + 实验 + 现有调用方 | **不符（P0）**：SQL 为 `key LIKE ?`，参数原样传入、**不自动加通配符**。现有调用方均带 `%`：core.py:407 `"dedup:%"`、test_db_full.py:144 `"tmp:%"`。按文档传 `"admin_sess:"` 实测 **删 0 行**（过期会话永远清不掉） |
| 13 | 向后兼容：Bearer admin key 路径零回归 | README.md:179-193、docs/deploy-mq3-2026-09-17.md:180/253、core.py:604-607 | **属实**：真实探针/文档均用 `Authorization: Bearer $ADMIN_KEY`；`AuthMiddleware` 按 `/admin/` 前缀跳过，auth 4 端点天然不被 LLM key 中间件拦截 |
| 14 | R5：签名变化打破既有直调测试 | tests/test_auth.py:141-187 | **属实**：4 个旧用例均不传 db；新逻辑引入 db 使用后，`test_invalid` 等会走到 `get_db()`，未初始化/遗留连接时行为不确定，需按文档建议适配 |
| 15 | 硬约束零迁移零依赖 | pyproject.toml + 文档通篇 | **属实**：只用 stdlib（hashlib/secrets/json/time）+ 既有 config 表，无偷渡依赖/表/脚手架，符合规则清单 1-3 与决策阶梯第 3 级 |
| 16 | 安全基线：全程 compare_digest / 会话只存哈希 / 401 文案不泄露 / pbkdf2 600k+iters 可升级 | F1-F8 逐条 | **基本属实**（见 P1-6、P1-10 的两处补强）；login 三反例统一「用户名或密码错误」✓；status 不返回 pwd_hash/token ✓；存档 `pbkdf2_sha256$iters$salt$hash` 可升级 ✓ |
| 17 | 用例总数 59（单元 55 + 集成 4）；正 22/反 27/边界 10 | 逐条点数 | **不符**：实际 **69（单元 65 + 集成 4）；正例 23 / 反例 33 / 边界 13**。文档第 242 行已预留「以逐条表为准并更正此行」，见 P1-5 |

---

## 3. 问题清单

### P0（必须改，改完重审）

**P0-1 ｜ F4 的 `db: Database = None` 签名方案会让整个应用起不来**
- 位置：features 文档 F4（第 55 行）、T4.10（第 178 行）、R5（第 276 行）。
- 依据：`verify_admin_key` 是 `Depends(verify_admin_key)` 的真实依赖（admin_api.py 30+ 处）；实验 `.workbuddy/tmp/check_dep.py` 实跑证明 FastAPI 对依赖函数里 `db: Database = None` 在**注册路由时**抛 `FastAPIError: Invalid args for response field!`。`verify_llm_key` 的同款签名「能用」只因为它从未被 `Depends()` 使用（grep 全 src 仅 auth.py 定义 + core.py:31 无用 import）——**文档「照抄 verify_llm_key 模式，含注释」的前提不成立**。
- 修改建议（二选一，推荐 a）：
  - **(a) 最简（决策阶梯第 7 级）**：`verify_admin_key` **签名不改**，session 分支内部 `db = get_db()`（auth.py:12 已 import）。admin_api 端点本来就全是 `db = get_db()` 直调（admin_api.py:101 等），与现状一致；`Database.initialize()` 会设置 `_active_db`（db.py:250-251），test_admin_api fixture 已依赖此机制。直调单测按 T4.10 现成模式 `monkeypatch.setattr("botflow.auth.get_db", lambda: db)` 覆盖兜底行。
  - (b) `db: Annotated[Optional[Database], Depends(get_db)] = None`：合法的子依赖写法，`dependency_overrides[dbmod.get_db]` 对其生效；但 `get_db()` 会在 500 判定**之前**执行，且多一层间接，不如 (a) 无聊可靠。
  - 同步改：F4 措辞、T4.10 描述、R5 的原因表述（R5 结论仍成立：新逻辑开始用 db，旧直调测试必须适配）。

**P0-2 ｜ `cleanup_config_by_prefix("admin_sess:", ...)` 缺 `%`，清理永远删 0 行**
- 位置：F2（第 44 行）、F7（第 81 行）、T2.10（第 156 行）、T7.6（第 216 行）。
- 依据：db.py:319 `DELETE ... WHERE key LIKE ?`，参数不拼通配符；现有调用方 core.py:407 传 `"dedup:%"`、test_db_full.py:144 传 `"tmp:%"`；实验实测 `"admin_sess:"` 删 0 行、`"admin_sess:%"` 正确删旧行。
- 修改建议：四处统一改为 `cleanup_config_by_prefix("admin_sess:%", SESSION_TTL_SECONDS)`。否则按文档实现，T2.10/T7.6 必挂（这两条恰好是守卫用例，但规格本身写错了）。F6/R3 里 purge 用的 `LIKE 'admin_sess:%'` 字面量是对的，不动。

**P0-3 ｜ R4 与 F8/T8.2 对「重复 logout」的期望自相矛盾，实现无法同时满足**
- 位置：F8（第 88 行「重复 logout 仍 200」+ T8.2（第 226 行「仍 200」） ↔ R4（第 275 行「重复 logout 已删 token → 401」）。
- 依据：logout 要区分「伪造 token」（T8.5 期望 401）与「刚注销过的 token」（T8.2 期望 200），二者删库后状态相同，**信息论上不可区分**——除非先 `resolve_session`（则已删 token 解析失败 → 401，T8.2 挂）或不解析直接删（则伪造 token 也 → 200，T8.5 挂）。三者（F8/T8.2、T8.5、R4）只能取其二。
- 修改建议（验证子 agent 定档，推荐 **(甲)**）：
  - **(甲) 不解析、按 key 直删、有 token 即 200**：任意非空 token（含伪造、已注销、admin key）→ `execute_write(DELETE by sha256(key))` → 200 `{"success": true}`；完全无凭据 → 401。安全无虞（不知道原 token 就算不出 key，删不掉别人的会话）。改动：**T8.5 期望改 200、R4 判定表改写**，F8/T8.2 保持，幂等语义名副其实，实现最无聊。
  - (乙) 先 `resolve_session` 再删：有效 → 200；已删/伪造/过期 → 401。改动：**T8.2 改 401、F8 删「幂等 200」表述、R4 保持**。代价：失去幂等，前端 A10「接口失败也清本地」仍兜得住。
  - 定档后同步改 F8 三行、T8.2、T8.5、R4、TI.1 ⑤（两案下 TI.1 均不受影响）。

### P1（建议改，编码+写测试阶段顺手吸收）

**P1-4 ｜ F4 判定顺序未写「token 为空 → 先 401」短路**
- 位置：F4 第 56-61 行。字面实现 `① compare_digest(token, admin_key)` 在 `token=None` 时抛 `TypeError`（现逻辑靠 `not token or ...` 短路，auth.py:108）；`resolve_session(db, None)` 也会在 `sha256(None.encode())` 处 `AttributeError` → T4.4 期望的 401 变 500。
- 建议：F4 步骤 2 后显式加「**token 为空 → 直接 401（先于 ①②）**」，T4.4 保留作守卫。

**P1-5 ｜ 用例计数行全错**
- 位置：第 241 行。实际 **69 条（单元 65 + 集成 4）；正例 23 / 反例 33 / 边界 13**（逐条点数：F1=11, F2=10, F3=2, F4=12, F5=4, F6=11, F7=9, F8=6, TI=4）。文档已预留「以逐条表为准更正此行」——按此更正即可。

**P1-6 ｜ 密码无上限：非认证 login 端点 + pbkdf2 600k = 无界 CPU 放大器**
- 位置：F6（第 73 行只定了 ≥8）、F7（无长度校验）。
- 依据：pbkdf2_hmac 成本随输入长度线性增长（HMAC 按 64B 分块 × 600k 轮）；login 免鉴权、R7 又接受了无专用限流，攻击者 POST 一个几 MB 的 `password` 即可单请求烧掉大量 CPU。属「不可偷懒的领域：校验/安全」。
- 建议：F6 定义 `password` 上限（如 **≤256 字符**，`username` 一并 ≤64/128），setup 与 **login 入口在算哈希之前** 校验，超限 400/422；补用例 **T6.12**（setup 超长密码 → 400/422）、**T7.10**（login 超长密码 → 400/422，且不进入 pbkdf2）。

**P1-7 ｜ R7 限流论断事实错误**
- 位置：R7（第 278 行）。`RateLimitMiddleware` 只跳过 `/health`（core.py:548），`/admin/auth/*` 同样受限：带 Bearer 按 token 分桶、login/setup 无头请求按客户端 IP 分桶，300 次/分钟（core.py:522-532, 576）。
- 建议：R7 改为「已有 300/min/IP（或 per-token）粗限流，**不足以防弱口令爆破**，本任务不加专用限流（YAGNI），风险记录保留」。结论（不加限流）不变，事实必须改准。

**P1-8 ｜ R1（login 是否 strip）悬而未决，需验证子 agent 定档**
- 验证意见：**login 侧对 `username.strip()` 后再比对**，与 F6 存档 strip 对称（用户带空格登录不应莫名 401）。F7 补一句，补正例 **T7.11**：`" alice "` + 正确密码 → 200。

**P1-9 ｜ 集成用例落点与跑测命令会被现有 live 测试污染**
- 位置：第 124 行「`tests/test_integration.py` 或新集成文件」、第 317 行 `pytest tests/ -m integration`。
- 依据：现有 `tests/test_integration.py` 是 **打真实服务 127.0.0.1:4000 的 live 测试**（文件头 `pytestmark = pytest.mark.integration`，httpx 直连）。`pytest tests/ -m integration` 会把它一并无差别收集——无实服务时 TI.1-TI.4 会被无关失败挡住，违反「必做不跳过」。
- 建议：TI 用例放**新文件**（如 `tests/test_admin_auth_e2e.py`，TestClient/ASGI 不起端口，标 `@pytest.mark.integration`），文档跑测命令写明**指名文件**：`PYTHONPATH=src python -m pytest tests/test_admin_auth_e2e.py`（marker 仍保留供甄别）。

**P1-10 ｜ 非 ASCII token 使 `compare_digest` 抛 TypeError → 500**
- 位置：F4 步骤 ①、F6 token 校验（第 72 行）。实验实测 `secrets.compare_digest("中文token", "admin-...")` → `TypeError: comparing strings with non-ASCII characters is not supported`。F6 的 token 来自 JSON body，非 ASCII 是常态输入 → 本应 401 却 500。现网 verify_admin_key 对 header 同样有此潜伏问题（低概率），setup 将其放大。
- 建议：比对前转 bytes（`token.encode()` vs `admin_key.encode()`，`compare_digest` 接受 bytes 且无 ASCII 限制）或先 `token.isascii()` 判否 → 401；补用例 **T4.13 / T6.13**：非 ASCII 错误 token → 401（不是 500）。

### P2（可选）

- **P2-11** F5 status 响应无 `success` 字段，与其余 admin 端点（一律 `{"success": True, ...}`）风格不一致；若有意（T5.4 锁死 key 集合），在 F5 注明一句。
- **P2-12** F6 401 的 `detail` 文案未定：建议统一用 `"Invalid admin key."`（与 verify_admin_key 一致、不泄露任何状态），T6.2 补文案断言。
- **P2-13** F6「400/422 二选一」使 T6.4-T6.6 断言偏松：建议统一走 Pydantic 校验 → 422，测试断言收窄（与 T6.10 一致）。
- **P2-14** 文档头部第 9 行称 417-544 含 `onMounted`，实际在 719（第 248 行已写对）——修一下引用，避免编码阶段找错位置。
- **P2-15** F6/F7 用平铺 body 而非 `Body(embed=True)` 惯例，未写理由；A6/A7 也未重申请求体形状。补一句「新端点无 SPA 兼容包袱，与 PATCH 平铺先例一致（admin_api.py:20-27 注释）；doSetup/doLogin 发平铺 JSON」，防前后端字段嵌套对不上。
- **P2-16** T4.11「走 ① 通过」不可直接观测（①②同效通过）：断言最多到「通过」级别；若要坐实分支，需 monkeypatch `resolve_session` 断言未被调用。降级表述即可。

---

## 4. 用例清单覆盖核对

对照任务点名的关键分支，逐项核对（✓ 已覆盖 / ✗ 缺失 / ⚠ 有但需修）：

| 关键分支 | 覆盖 | 说明 |
|---|---|---|
| verify_admin_key 四分支（500 / admin key / session / 401 兜底） | ✓ | T4.2 / T4.1 / T4.5 / T4.3,T4.4 |
| session 过期 / JSON 损坏 / 哈希不匹配 | ✓ | T4.7 / T4.8 / T4.9，单测层 T2.5-T2.9 |
| credentials 优先语义不回归 | ✓ | T4.6 |
| 裸 token `_extract_token` 语义不回归 | ✓ | T4.12 |
| setup 重置 purge 全部旧会话（R3 守卫） | ✓ | T6.9 / T6.11 / TI.3 |
| **purge/清理 SQL 实际可执行性** | ⚠ | **P0-2**：前缀缺 `%`，按文档实现 T2.10/T7.6 必挂 |
| login 未建号 / 用户名错 / 密码错三反例统一文案 | ✓ | T7.2/T7.3/T7.4 |
| login 成功路径 cleanup 过期会话分支 | ✓ | T7.6（依赖 P0-2 修复） |
| status 未开通 `username=null` / 敏感字段不泄露 | ✓ | T5.1 / T5.4 |
| setup username strip 后为空 | ✓ | T6.4（`"   "`）+ T6.8 strip 生效 |
| **password 上限** | ✗ | 规格未定义、无用例 → **P1-6**，补 T6.12/T7.10 |
| JSON 损坏会话 | ✓ | T2.6 / T4.8 |
| logout 幂等 / 无凭据 / 伪造 token | ⚠ | **P0-3**：T8.2(200) 与 T8.5/R4(401) 矛盾，须定档后修表 |
| 集成端到端 setup→login→API→logout→401 | ✓ | TI.1；向后兼容 TI.2；重置链路 TI.3；未开通 TI.4 |
| pbkdf2 单测性能（monkeypatch 迭代数） | ✓ | 第 125 行强制要求，且 T1.6 断言 iters 与 monkeypatch 常量一致 |
| R5 旧直调测试适配 | ✓ | 已识别（两套 F4 实现方案下均成立） |
| **空 token 短路（TypeError→500）** | ✗ | T4.4 意图对但 F4 规格缺步骤 → P1-4 |
| **非 ASCII token** | ✗ | 补 T4.13/T6.13 → P1-10 |
| **login username strip 定档（R1）** | ✗ | 补 T7.11 → P1-8 |

**建议补入清单的编号**：T4.13（非 ASCII token → 401）、T6.12（setup 超长密码 → 400/422）、T6.13（setup 非 ASCII token → 401）、T7.10（login 超长密码 → 400/422）、T7.11（login 用户名带空格 → 200）；T8.2/T8.5 按 P0-3 定档结果修改其一。补后用例总数为 **74**（单元 70 + 集成 4），计数行按 P1-5 一并更正。

**覆盖率计划核对**：UNCOVERED=0 的承诺**现实**——零迁移无升级分支、全 stdlib 无平台分支、错误分支均有直调/HTTP 用例、前端不计入 `source=["botflow"]` 口径；pydantic 校验行由 422 用例覆盖；唯一前提是 P0-1 修正后 `get_db()` 兜底行由 T4.10 monkeypatch 覆盖。验证命令与 pyproject/AGENTS.md 完全一致（P1-9 的集成命令除外）。

---

## 5. 结论：**打回**

文档整体质量高：落点引用基本准确、R3/R5 等风险洞察正确、正反边界三类维度齐全、零迁移零依赖无偷渡、覆盖率计划现实。但存在 **3 个规格级错误，按文档直接编码/写测试会撞墙**，必须整改后重审：

**必须整改项（P0）：**

1. **F4**：删除 `db: Database = None` 签名方案——该函数被 30+ 条路由 `Depends()`，FastAPI 注册路由即抛 `FastAPIError`（实验坐实，check_dep.py）；改为签名不改、session 分支内部 `get_db()`（推荐），同步改 T4.10、R5 表述。
2. **F2/F7/T2.10/T7.6**：清理前缀 `"admin_sess:"` → **`"admin_sess:%"`**（LIKE 无通配符删 0 行，实验坐实，check2.py；现有调用方均带 `%`）。
3. **R4 ↔ F8/T8.2**：logout 对「已删 token 再注销」的 401/200 矛盾必须定档（推荐甲案：有任意 token 即 200、无凭据 401，改 T8.5+R4），三处表述统一。

**重审时一并核验（P1，可在编码+写测试阶段吸收，无需二次文档往返）：**
P1-4 空 token 短路步骤、P1-5 计数更正为 69→补用例后 74、P1-6 密码上限+2 用例、P1-7 R7 限流事实修正、P1-8 login strip 定档+T7.11、P1-9 集成用例独立文件+指名跑测、P1-10 非 ASCII token 防 TypeError+2 用例。

**P2（可选）**：P2-11 ~ P2-16 见第 3 节，编码阶段顺手处理即可。

> 整改完成后文档回传验证子 agent 复审；复审只核对上述 P0/P1 项，不重复全量核对。

---
---

# 复审（v2 定向核对）

> 复审对象：`docs/tasks/admin_auth_features.md` v2（411 行，文末含「整改记录」节）。
> 范围：按上文约定只核对 P0×3 / P1×7 是否落实且无新矛盾；P2×6 仅记录。
> 方法：全文重读 + 3 处抽查实验（`admin_sess:` 全量 grep、用例行数机械计数、pytest addopts 行为实跑）。

## 一、P0×3：全部落实

| 编号 | 核对点 | 结论 |
|---|---|---|
| P0-1 | F4 签名三参数原样不变（第 57-62 行，硬约束 5 锁死）；session 分支内部 `db = get_db()`（第 63-65 行，且注明 auth.py:12 已有 import、**仅** session 分支取库）；T4.10 改写为「直调 + `monkeypatch.setattr("botflow.auth.get_db", ...)` 覆盖该行」（第 218 行）——与 auth.py 现状一致（`get_db` 已导入、`verify_admin_key` 现为三参数、被 30+ 条 `Depends` 引用）；R5 同步改写（第 324 行，含「签名不变，不能给旧测试传 db 参数」），§5 覆盖率第 ④ 条、§6-4 一致 | ✅ 落实，无新矛盾 |
| P0-2 | 全文 grep `admin_sess:` 逐处核对：调用参数处 **F2(47)/F7(111)/T2.10(196)/T7.6(259)/R3(两处)/R10(329) 均为 `"admin_sess:%"`**；F6 purge `LIKE 'admin_sess:%'` 字面量保留(99)；其余出现处（`session_config_key` 拼接 41、存储布局 150、T2.1/T4.9/T6.9/T6.11 的散文描述）本就该无 `%`，未被误改 | ✅ 落实，改齐且无过改 |
| P0-3 | 甲案五处自洽：F8 判定表（114-124，任意非空 token→200、无凭据→401）＝ T8.2 重复注销 200（271）＝ T8.5 伪造 token→**200**（274）＝ R4 定档（323）＝ A10「甲案下带 token 恒 200，兜底仅防网络故障」（310）；TI.1 ⑤200→⑥401 在「直删生效」语义下成立；T8.4 是 logout 唯一 401，与 F6/F7 的 401 语境不冲突 | ✅ 落实，三方矛盾消除，五处完全自洽 |

## 二、P1×7：全部落实（P1-9 有 1 条复审新发现，见第三节）

| 编号 | 核对点 | 结论 |
|---|---|---|
| P1-4 | F4 判定顺序显式插入步骤 3「token 为空 → 先 401」（69-70 行，注明防 `compare_digest(None)` TypeError / `sha256(None.encode)` AttributeError）；T4.4 改为短路守卫并写明不得变 500（212 行） | ✅ |
| P1-5 | 机械计数核验：用例表实际 **74 行、无重复 ID**；分档按「类型」列实算 **正 24（22+会话清理+重置）/ 反 37（36+反例回归）/ 边 13（12+幂等）**，单元 74−集成 4=70——与第 286-289 行「74（单元 70+集成 4）、正 24/反 37/边 13」**完全一致**，新增 5 条标注（T4.13/T6.12/T6.13/T7.10 反、T7.11 正）与基线 69 可对账 | ✅ |
| P1-6 | 8≤password≤128、strip 后 username≤64，F6/F7 均写明「算哈希之前、Pydantic→422」（93-96、106-107）；F1 注明长度校验不在哈希层（36）；T6.12（422 且 monkeypatch 断言不进 pbkdf2）、T7.10 同款守卫（247、263）；R12 同步 | ✅ |
| P1-7 | R7 重写为事实：只跳过 `/health`、对 `/admin/*` 生效、300 次/分钟 IP/token 分桶（326 行，引 core.py:548/522-532/576，与我首审读码一致）；结论改「粗限流不足以防爆破、仍不加专用限流（YAGNI）」；§6-5 同步 | ✅ |
| P1-8 | login 侧 `username.strip()` 定档（F7 108-109）、T7.11 `" alice "`→200（264）、R1 改「已定档」（320） | ✅ |
| P1-9 | 独立文件 `tests/test_admin_auth_e2e.py` + 不混 live 的理由写清（161-163、277、369-371）✅；**但指名跑测命令有新问题 → 见第三节 P1-9′** | ⚠ 文件落实、命令有残留 |
| P1-10 | F4 步骤 4 与 F6 统一 `compare_digest(token.encode(), key.encode())` bytes 比对（71-73、90-91）——语义正确：utf-8 编码单射，等价性与常数时间性质保持，`compare_digest` 对 bytes 无 ASCII 限制（与复审实验「非 ASCII **str** 才抛 TypeError」互补）；非 ASCII token 不匹配自然落入 ②③→401 的推理成立；T4.13/T6.13、R11 均补上 | ✅ |

## 三、复审新发现（1 条，非文档内容错误、源于本报告 v1 P1-9 建议的疏漏）

**P1-9′（跑测阶段必须吸收）｜v2 第 366 行集成命令实测执行 0 条用例**
- 文档命令：`PYTHONPATH=src python -m pytest tests/test_admin_auth_e2e.py`。
- 实测（`.workbuddy/tmp/test_mark_check.py`，仓库根目录跑）：pyproject `addopts = ["-m", "not integration"]` 会被 pytest 前置注入且**不被指名文件覆盖** → 标了 `integration` 的用例被全部过滤：**`1 deselected`，exit=5（0 条执行）**；追加 `-m integration` 后 **`1 passed`**。
- 影响：按文档原样执行，TI.1–TI.4 一条都跑不到，「集成测试必做不跳过」落空（好在 exit=5 会响亮报错，非静默绿灯）。
- 修正（一行）：**`PYTHONPATH=src python -m pytest tests/test_admin_auth_e2e.py -m integration`**（CLI `-m` 覆盖 addopts，指名文件继续隔离 live 测试，两者兼得）。
- 归属：本条源自我 v1 P1-9 给出的「指名文件」命令建议本身漏了 addopts 交互，编码子 agent 照单落实，**不构成对 v2 文档的打回理由**；由验证子 agent 在第 3 步跑测时直接按修正命令执行，无需再走文档轮回。

**非阻塞提示（落地时留意，不算整改项）**：F8「空 token→401」的取 token 路径应复用 `_extract_token`（硬约束 3）——注意其容忍语义下 `"Bearer "`（scheme 后为空）会返回 `"Bearer"` 这个非空串 → 按甲案落 200（与 T4.12 容忍先例一致）；「空 token→401」指 `_extract_token` 返回 `None`（无头/空头）。

## 四、P2×6：全部顺手吸收（仅记录，不作为打回理由）

P2-11（F5 有意无 `success`＋T5.4 锁 key 集合）、P2-12（401 detail 统一 `"Invalid admin key."`＋T6.2 断言）、P2-13（统一 Pydantic→422，T6.4-T6.6 收窄）、P2-14（头部行号 719 修正）、P2-15（平铺 body 理由＋A6/A7 重申形状）、P2-16（T4.11 断言降级说明）——**6/6 均已落实**，且整改记录表与正文落点对得上。

## 五、复审结论：**通过**

P0×3 全部真正落实且五处/七处交叉表述自洽，P1×7 落实 6.5 条（P1-9 的 live 污染问题已解决，仅剩跑测命令一行旗标——且系我方建议疏漏，修正归验证子 agent 落地执行），P2×6 全部吸收；抽查实验（grep、行数计数、addopts 实跑）未发现其它新矛盾。

**放行进入编码+写测试阶段。** 附带必须项（写测试/跑测时执行，不阻塞放行）：
1. 集成命令用 `pytest tests/test_admin_auth_e2e.py -m integration`（P1-9′，一行修正）；
2. 落测试时按 R5 适配 `tests/test_auth.py::TestVerifyAdminKey` 旧用例（monkeypatch `botflow.auth.get_db`），并按第 286 行约定逐条点表核数（本复审已预核：74/70/4、正 24/反 37/边 13 无误）。

## 六、测试执行（2026-09-23，Linux mq3 隔离环境）

**环境**：mq3（qbot@100.88.88.88），Python 3.12.3，独立 venv `/tmp/cov_venv`；源码包 `_cov.tgz`（md5 `9cf330c5b485ccb08f686dc399eb60de`）解包到 `/tmp/botflow_cov` 执行，**未在任何部署目录跑 pytest**。

**主命令与结果（覆盖率门禁）**：

```
PYTHONPATH=src python -m pytest tests/ --cov=botflow --cov-report=term-missing -m "not integration"
→ 1149 passed, 0 failed, 15 deselected in 82.17s
→ TOTAL 4302 statements / 0 missed = 100%（全部模块 100%，UNCOVERED=0 达成）
```

**首轮缺陷与修复（1 failed → 复跑全绿）**：

- 首轮 `1 failed, 1148 passed`：`test_admin_auth.py::TestSessionHelpers::test_t2_10_cleanup_only_old_rows` 抛 `RuntimeError: Cannot run the event loop while another loop is running`。
- 根因：**测试自身实现缺陷**——辅助函数 `_run()` 用 `asyncio.new_event_loop().run_until_complete()`，只在同步测试合法；t2_10 是 `async def`（pytest-asyncio loop 上下文内）→ `_check_running()` 拒绝。同逻辑的同步用例 t7_6 通过，证明 **src 零缺陷**。
- 处置：打回验证子 agent，`_seed_old_session` 改 `async def` + 两处调用点适配（t2_10 内 `await`、t7_6 内 `_run(...)` 包裹），共 3 处、**断言零改动**；全文件 17 处 `_run(` 调用逐个核对所属函数均为同步 def，无第二处。
- 复跑：`1149 passed, 0 failed`，覆盖率 100%。

**集成测试（P1-9′ 修正命令 `pytest tests/ -m integration`，15 条）**：

| 分组 | 结果 | 依据 |
|---|---|---|
| `test_admin_auth_e2e.py` TI.1–TI.4 | **4/4 PASSED** | 新功能全链路（status→setup→login→session API→logout→Bearer 兼容） |
| `test_integration.py::test_health` | PASSED | 打活服务 `/health` 无需鉴权 |
| `test_group_routing.py` ×3 | FAILED | `httpx.ConnectError`：要求 `127.0.0.1:8765` 活服务，本机无 → 环境依赖 |
| `test_integration.py` ×7 | FAILED | 401：用例**不带 Authorization** 打 `127.0.0.1:4000`；实测直 curl 返回 `{"error":"Missing API key. Provide Authorization: Bearer <key> or x-api-key: <key>."}`，来自 `core.py` **既有 API key 中间件（本次改动零触碰）**，`/health` 同机 200 → **非本次回归** |

上述 10 条 FAILED 为既有 live 测试的环境依赖（pyproject `addopts = -m "not integration"` 默认排除，不属覆盖率门禁面）；与本次改动的交集为零——diff 仅涉 `auth.py`（`verify_admin_key` 签名不变）、`admin_api.py`（新增 4 端点）、SPA、文档与测试。

**结论：覆盖率门禁 100% 通过、功能 E2E 全绿，放行部署（#104 → #105 → #106）。**
