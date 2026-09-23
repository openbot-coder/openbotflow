# admin_auth 功能点与测试用例：管理员用户名/密码自助开通与登录

> 任务类型：产品化自助鉴权（首启用 ADMIN_KEY 开通，日常登录只用用户名+密码）
> 范围（用户已拍板）：① 复用 `config` KV 表零迁移存储账号与会话；② pbkdf2_hmac 密码哈希（stdlib）；
> ③ 新增 4 个 `/admin/auth/*` 端点；④ `verify_admin_key` 扩展为 admin key / session token 双通道；
> ⑤ SPA 登录页改造（setup / login 双视图）；⑥ `docs/design.md` 端点清单同步。
> 关联：`src/botflow/auth.py:93`（verify_admin_key 现状）、`src/botflow/admin_api.py:17`（admin_router）、
> `src/botflow/storage/db.py:298-323`（config KV / cleanup_config_by_prefix，`execute_write` 在 db.py:281）、
> `src/botflow/static/admin/index.html` 89-102（登录块）、417-544（api()/doLogin/doLogout）、719（onMounted）。
> **版本**：v2（已按 `docs/tasks/admin_auth_review.md` 打回意见整改，见文末「整改记录」）。

---

## 0. 硬约束（违反即打回）

1. **零数据库迁移**：不加表、不改 schema，只用已有 `config` 表的 `get_config/set_config/execute_write/cleanup_config_by_prefix`。
2. **零新增依赖**：密码哈希、token 生成、比较全部 stdlib（`hashlib` / `secrets` / `json` / `time`）。
3. **向后兼容**：脚本/探针继续用 `Bearer <BOTFLOW_ADMIN_KEY>` 调 admin API，行为不变；`_extract_token` 现有语义不回归。
4. **admin key 只存比对，不落库不外泄**：`GET /admin/auth/status` 不返回 token；会话只存 `sha256(token)[:32]`，不存原文。
5. **`verify_admin_key` 签名不变**：被 30+ 条路由 `Depends()` 的依赖函数**禁止**新增带默认值的参数（FastAPI 路由注册时抛 `FastAPIError`，审核实验坐实，P0-1）。
6. **覆盖率 100%**（`PYTHONPATH=src python -m pytest tests/ --cov=botflow -m "not integration"`），不可覆盖行标 `# UNCOVERED: [原因]`；本任务零迁移，**预期 UNCOVERED = 0**。

---

## 1. 功能点清单

### F1 密码哈希与校验（`src/botflow/auth.py`）

- 模块常量 `PBKDF2_ITERATIONS = 600_000`（测试可 monkeypatch 调低，如 1000，避免单测变慢）。
- `hash_password(password: str) -> str`：`hashlib.pbkdf2_hmac('sha256', password.encode(), salt, PBKDF2_ITERATIONS)`；
  salt 用 `secrets.token_bytes(16)`；存档格式 **`pbkdf2_sha256$<iters>$<salt_hex>$<hash_hex>`**。
- `verify_password(password: str, stored: str) -> bool`：解析存档 → 用存档里的 iters/salt 重算 →
  `secrets.compare_digest` 比对 hash_hex；存档格式非法（段数不对 / 非 pbkdf2_sha256 前缀 / hex 解析失败）
  → 返回 `False`（不抛异常）。
- 每次 `hash_password` 生成新 salt → 同一密码两次哈希结果不同（往返验证靠 `verify_password`，不靠字符串相等）。
- **长度校验不在哈希层**（F1 只管算）：≥8 / ≤128 的入口校验在 F6/F7 端点、pbkdf2 调用**之前**完成（P1-6）。

### F2 会话存取辅助（`src/botflow/auth.py`）

- 模块常量 `SESSION_TTL_SECONDS = 7 * 24 * 3600`（7 天）。
- `session_config_key(token: str) -> str`：返回 `"admin_sess:" + hashlib.sha256(token.encode()).hexdigest()[:32]`。
- `create_session(db, username) -> tuple[str, float]`：`token = secrets.token_urlsafe(32)`；
  `exp = time.time() + SESSION_TTL_SECONDS`；`set_config(session_config_key(token), json.dumps({"username": ..., "exp": ...}))`；
  返回 `(token, exp)`。
- `resolve_session(db, token) -> Optional[dict]`：按 key `get_config`；无记录 → `None`；JSON 解析失败（损坏）→ `None`；
  `exp <= time.time()`（过期）→ `None`；否则返回 `{"username", "exp"}`。
- 会话清理复用 `db.cleanup_config_by_prefix("admin_sess:%", SESSION_TTL_SECONDS)`——**前缀必须带 `%`**：
  SQL 是 `DELETE ... WHERE key LIKE ?` 原样匹配，不带通配符**删 0 行**（db.py:319；现有调用方先例
  core.py:407 传 `"dedup:%"`、test_db_full.py:144 传 `"tmp:%"`；P0-2）。按 `updated_at` 删过期行，在 login 成功路径调用。

### F3 admin key 读取共用（`src/botflow/auth.py`）

- `_require_admin_key() -> str`：读 `get_config().admin_key`，为空 → 抛现有 500
  `HTTPException("Server admin key is not configured (BOTFLOW_ADMIN_KEY).")`；否则返回 admin key。
  setup 端点与 `verify_admin_key` 共用（消除重复分支）。

### F4 `verify_admin_key` 扩展（`src/botflow/auth.py:93`，**签名不变**）

- **签名保持 `verify_admin_key(request, authorization, credentials)` 三参数原样不变，不加 `db` 参数**
  （硬约束 5 / P0-1）。该函数被 30+ 条路由 `Depends()`，新增带默认值的参数会让 FastAPI 在**路由注册时**
  抛 `FastAPIError: Invalid args for response field!` → 应用启动即崩、全部 admin 路由无法注册（审核实验证实；
  `verify_llm_key` 的 `db: Database = None` 之所以「能用」，只因它从未被 `Depends()` 使用）。
- **session 分支内部取库**：`db = get_db()`（`auth.py:12` 已 import，无需改 import）——与 admin_api 端点
  `db = get_db()` 直调同机制；`Database.initialize()` 会设置 `_active_db`（db.py:250-251），
  HTTP 层测试 fixture 已依赖此机制。**仅 session 分支调用 `get_db()`**，admin key 分支不碰 db。
- 判定顺序（保持原 401/500 文案不变）：
  1. `_require_admin_key()`：admin key 未配置 → **500**（现有分支保留，最先判）。
  2. 提取 token（`_extract_token` + `credentials` 优先的现有逻辑**原样保留**）。
  3. **token 为空 → 直接 401**（显式短路，先于 ①②；否则 `compare_digest(None, key)` 抛 TypeError → 500、
     `sha256(None.encode())` 抛 AttributeError——等价于现逻辑 `not token or ...` 的短路语义，P1-4；T4.4 守卫）。
  4. ① admin key 比对 → 通过（脚本/探针兼容路径）。**比对前转 bytes**：`compare_digest(token.encode(), admin_key.encode())`——
     `secrets.compare_digest` 对**非 ASCII 的 str** 抛 `TypeError`（审核实验坐实），转 bytes 后无 ASCII 限制、
     非 ASCII token 自然不匹配落入 ②③ → 401 而非 500（定档，P1-10；T4.13 守卫）。
  5. ② `resolve_session(db=get_db(), token)` 命中 → 通过。
  6. ③ 都不中 → 现有 **401 `Invalid admin key.`**（文案不改，避免破坏既有测试/探针）。
- 通过时 `request.state.is_admin = True`（两条通道都设）。

### F5 `GET /admin/auth/status`（`src/botflow/admin_api.py`，**无 verify_admin_key 依赖**）

- 返回 `{"configured": bool, "username": str | None}`：读 `admin_account` config；未建号 →
  `{"configured": false, "username": null}`；已建号 → `{"configured": true, "username": "<存档用户名>"}`。
- username 供登录表单预填；**绝不返回** pwd_hash / admin key。
- **有意不带 `success` 字段**（P2-11）：本端点是 SPA 启动的状态查询，不是操作结果；与其余 admin 端点的
  `{"success": true, ...}` 风格差异是有意为之，T5.4 把 key 集合锁死为恰 `{configured, username}`。

### F6 `POST /admin/auth/setup`（`src/botflow/admin_api.py`，无 verify_admin_key）

- body **平铺 JSON** `{token, username, password}`（P2-15）：新端点无 SPA 兼容包袱，与 PATCH 平铺先例一致
  （admin_api.py:20-27 注释）；doSetup 发的就是这个平铺形状，勿套 `{"req": {...}}`。
- token 校验：`_require_admin_key()`（未配置 → 500）后**转 bytes** `compare_digest(token.encode(), key.encode())`
  （P1-10：非 ASCII token 不再 TypeError→500），不等 → **401，detail 统一 `"Invalid admin key."`**
  （与 verify_admin_key 文案一致、不泄露任何状态，P2-12；T6.2 断言文案）。
- 参数校验：**统一走 Pydantic 模型 → 422**（P2-13，不再写「400/422」二选一）：
  - `username` strip 后非空、strip 后 **≤64** 字符；
  - `password` **≥8 且 ≤128** 字符（P1-6：pbkdf2 成本随输入线性增长，login/setup 免登录，无上限 =
    单请求 CPU 放大器；上限在**算哈希之前**校验）。
- 写入：`set_config("admin_account", json.dumps({"username": <strip 后>, "pwd_hash": hash_password(password)}))`。
- **已建号时同一端点 = 重置**：覆盖 `admin_account`，并 **purge 全部会话**：
  `execute_write("DELETE FROM config WHERE key LIKE 'admin_sess:%'")`（固定字面量，非用户输入，无注入面；
  不能用 `cleanup_config_by_prefix`——它只删过期行，见风险 R3）。
- 返回 `{"success": true}`。

### F7 `POST /admin/auth/login`（`src/botflow/admin_api.py`，无 verify_admin_key）

- body **平铺 JSON** `{username, password}`（同 P2-15 理由）。
- 入参校验（Pydantic → 422，**算哈希之前**）：`password` ≤128（防免登录端点 + 600k 迭代 CPU 放大，P1-6）；
  超长请求不进入 pbkdf2。
- **username strip 定档（P1-8 / R1 结案）**：login 侧对入参 `username.strip()` 后再与存档比对，
  与 F6 setup 的 strip 存档对称——`" alice "` 应能登录成功（T7.11）。
- 未建号 / 用户名不匹配 / 密码不对 → **401 统一文案「用户名或密码错误」**（不区分，防用户名枚举）。
- 成功路径：① `cleanup_config_by_prefix("admin_sess:%", SESSION_TTL_SECONDS)` 清过期会话
  （**必须带 `%`**，P0-2）；② `create_session` → 返回 `{"success": true, "token": ..., "expires_at": <exp float>}`。

### F8 `POST /admin/auth/logout`（`src/botflow/admin_api.py`，无 verify_admin_key）

**定档：甲案——不解析会话、按 key 直删、有 token 即 200**（P0-3，审核推荐并采纳）：

- **任意非空 token**（有效 session / admin key / 伪造 token / 已注销 token）→
  `execute_write("DELETE FROM config WHERE key = ?", (session_config_key(token),))`（多半 0 行）→
  **200 `{"success": true}`**。不先 `resolve_session`，因此响应**不泄露 token 有效性**；
  安全性无虞——不知道原 token 就算不出 `sha256(key)`，删不掉别人的会话。
- 幂等：同一 token 重复 logout 仍 200（删除不存在的行不报错）。
- **完全无凭据**（无 Authorization / 空 token）→ **401**。
- 判定表见风险 R4（已与本节、T8.2、T8.5 统一为甲案）。

### F9 SPA 改造（`src/botflow/static/admin/index.html`，代码另步实现，此处只定验收）

- 见第 3 节前端行为验收点（启动清 legacy key、status 双视图、login/setup 表单、token 存 `bf_admin_token`、
  401 踢出、logout 先调接口、doLogin/doSetup 走原生 fetch）。

### F10 `docs/design.md` 端点清单同步（`docs/design.md` 第 5 节）

- design.md **有** admin API 端点清单章节（`docs/design.md:297-318` 表格）→ 只在表中补 4 行：

  | 方法 | 路径 | 功能 |
  |------|------|------|
  | GET | `/admin/auth/status` | 管理员账号开通状态（免鉴权） |
  | POST | `/admin/auth/setup` | 用 admin key 开通/重置管理员账号 |
  | POST | `/admin/auth/login` | 用户名+密码登录，换取会话 token |
  | POST | `/admin/auth/logout` | 注销当前会话 |

- 同时在该节说明句（`docs/design.md:295`「由 `verify_admin_key` 保护」处）补一句：auth 4 端点除外、
  `verify_admin_key` 现接受 admin key 或会话 token。**只补清单与这一句，不重写章节。**

### 存储布局汇总（写入 `config` 表，零迁移）

| key | value | 写入点 |
|---|---|---|
| `admin_account` | `{"username": str, "pwd_hash": "pbkdf2_sha256$..."}` | F6 setup |
| `admin_sess:<sha256(token)[:32]>` | `{"username": str, "exp": float}` | F2/F7 create_session |

---

## 2. 测试用例清单

> 本任务为**编码子 agent**，不写 `tests/`；下表供验证子 agent 审核后落进：
> - `tests/test_auth.py`——辅助函数 + verify_admin_key 直调（沿用 db fixture + `HTTPAuthorizationCredentials` 构造模式；
>   session 分支需 `monkeypatch.setattr("botflow.auth.get_db", lambda: db)`，见 T4.10/R5）；
> - `tests/test_admin_api.py`——4 端点 HTTP 层（沿用 `TestClient` + `app.dependency_overrides[dbmod.get_db]` 模式；
>   verify_admin_key 内部直调 `get_db()` 依赖 `Database.initialize()` 设置的 `_active_db`，该 fixture 已具备）；
> - **`tests/test_admin_auth_e2e.py`（新文件）**——端到端集成 TI.1–TI.4，标 `@pytest.mark.integration`，
>   TestClient/ASGI 不起真端口。**不放 `tests/test_integration.py`**：那是打 127.0.0.1:4000 的 live 测试
>   （文件级 `pytestmark = integration`），混跑会被无关失败挡住（P1-9）。
>
> 单测中凡涉及 pbkdf2，**必须 monkeypatch `PBKDF2_ITERATIONS` 调低**（如 1000），否则 600k 迭代会让套件明显变慢。

### F1 密码哈希

| 用例 ID | 类型 | 描述 | 期望 |
|---|---|---|---|
| T1.1 | 正例 | `hash_password("correct horse")` 后 `verify_password("correct horse", stored)` | `True` |
| T1.2 | 反例 | 同上存档，校验错误密码 `"wrong password"` | `False` |
| T1.3 | 边界 | 密码恰好 8 个字符（`"12345678"`）哈希往返 | `True` |
| T1.4 | 边界 | 空密码 `""` 哈希往返 | `True`（哈希层不管长度，长度校验在 F6/F7） |
| T1.5 | 正例 | 同一密码连续 `hash_password` 两次 | 两个存档**字符串不同**（salt 随机），且各自 `verify_password` 均 `True` |
| T1.6 | 正例 | 解析存档格式 | 形如 `pbkdf2_sha256$<int>$<hex>$<hex>`，4 段、前缀正确、iters == monkeypatch 后常量 |
| T1.7 | 反例 | `verify_password` 遇非法存档：`"garbage"`（段数不足） | 返回 `False`，**不抛异常** |
| T1.8 | 反例 | 存档前缀错：`"md5$1$ab$cd"` | `False` |
| T1.9 | 反例 | iters 段非整数：`"pbkdf2_sha256$abc$ab$cd"` | `False` |
| T1.10 | 边界 | 存档 hash_hex 非 hex 字符 | `False`（解析失败分支），不抛异常 |
| T1.11 | 反例 | compare_digest 分支：用**等长但不同**的 hash_hex 构造存档后校验 | `False`（走到比对而非提前长度短路） |

### F2 会话辅助

| 用例 ID | 类型 | 描述 | 期望 |
|---|---|---|---|
| T2.1 | 正例 | `session_config_key("tok")` | 等于 `"admin_sess:" + sha256("tok").hexdigest()[:32]`（32 位 hex） |
| T2.2 | 边界 | 两个相近 token（`"tok" / "tok2"`） | key 不同（哈希雪崩） |
| T2.3 | 正例 | `create_session(db, "alice")` | 返回 token 非空、`len(token) >= 40`（token_urlsafe(32)）；`exp ≈ time.time() + SESSION_TTL_SECONDS`（±5s） |
| T2.4 | 正例 | create 后 `resolve_session(db, token)` | 返回 `{"username": "alice", "exp": ...}` |
| T2.5 | 反例 | `resolve_session(db, "never-issued")` | `None` |
| T2.6 | 反例 | 手动 `set_config(session_config_key("t"), "{not json")` 后 resolve | `None`（JSON 损坏不抛异常） |
| T2.7 | 反例 | 写入合法 JSON 但 `exp = time.time() - 1` 后 resolve | `None`（过期） |
| T2.8 | 边界 | `exp = time.time() + 1`（未过期） | 返回会话 dict |
| T2.9 | 边界 | JSON 合法但缺 `exp` 键（`{"username":"x"}`） | `None`（健壮，不 KeyError） |
| T2.10 | 正例 | 写入一条 `updated_at` 很旧的 `admin_sess:` 行 + 一条新的，调 `cleanup_config_by_prefix("admin_sess:%", SESSION_TTL_SECONDS)` | 只删旧行，新行保留。**前缀带 `%` 是 P0-2 守卫：若实现误传 `"admin_sess:"`（无通配符）则删 0 行、本用例必挂** |

### F3 `_require_admin_key`

| 用例 ID | 类型 | 描述 | 期望 |
|---|---|---|---|
| T3.1 | 正例 | `set_config(BotflowSettings(admin_key="k"))` 后调用 | 返回 `"k"` |
| T3.2 | 反例 | `admin_key=""` | 抛 `HTTPException` 500，detail 含 `"not configured"` |

### F4 `verify_admin_key`（直调，模式照 `tests/test_auth.py::TestVerifyAdminKey`；session 分支需 monkeypatch `botflow.auth.get_db`）

| 用例 ID | 类型 | 描述 | 期望 |
|---|---|---|---|
| T4.1 | 正例 | admin key 通道：`Bearer admin-secret` | 通过，`request.state.is_admin is True` |
| T4.2 | 反例 | admin key 未配置（`admin_key=""`）+ 任意 token | **500**（现有分支，最先判定） |
| T4.3 | 反例 | 错误 token、无任何会话 | **401**，detail `"Invalid admin key."`（文案不回归） |
| T4.4 | 反例 | 无 Authorization 头（`authorization=None`、credentials 默认） | **401**——P1-4 短路守卫：空 token 必须在 ①② 之前落 401，**不得**因 `compare_digest(None,...)` TypeError / `sha256(None.encode())` AttributeError 变 500 |
| T4.5 | 正例 | session 通道：先 `create_session`，再 `Bearer <token>` | 通过，`is_admin is True` |
| T4.6 | 边界 | session 通道 + `credentials=HTTPAuthorizationCredentials(...)` 且 `authorization` 为 junk | 以 credentials 为准 → 通过（credentials 优先语义不回归） |
| T4.7 | 反例 | 过期会话 token（exp 已过） | 401 |
| T4.8 | 反例 | 会话 JSON 损坏的 token | 401（不抛 500） |
| T4.9 | 反例 | token 哈希不匹配：自己造一个 `admin_sess:` key 写入合法未过期 JSON，但 presented token 哈希到别的 key | 401（查不到即无效） |
| T4.10 | 正例 | **签名不变**下覆盖 session 分支的 `db = get_db()` 行：直调时不传 db，monkeypatch `botflow.auth.get_db` 返回 fixture db，走 session 通道 | 通过（覆盖内部 `get_db()` 调用行；不 monkeypatch 则 `get_db()` 未初始化抛 RuntimeError） |
| T4.11 | 正例 | admin key 与 session 同时有效（token 恰为 admin key） | 通过、`is_admin is True`。**断言只到「通过」级别**——①②同效通过不可直接观测；若要坐实「① 优先、未走 ②」，可选 monkeypatch `resolve_session` 断言未被调用（P2-16） |
| T4.12 | 反例回归 | `Bearer` 前缀缺失的裸 token（`_extract_token` 容忍语义）且等于 admin key | 通过（`_extract_token` 行为不回归） |
| T4.13 | 反例 | **非 ASCII 错误 token**：`authorization="Bearer 中文token"` | **401 而非 500**（P1-10：比对转 bytes，`compare_digest` 不再对非 ASCII str 抛 TypeError） |

### F5 `GET /admin/auth/status`

| 用例 ID | 类型 | 描述 | 期望 |
|---|---|---|---|
| T5.1 | 正例 | 未建号 | 200，`{"configured": false, "username": null}` |
| T5.2 | 正例 | 已建号（先 setup） | 200，`{"configured": true, "username": "alice"}` |
| T5.3 | 反例 | **不带任何 Authorization 头**访问 | 200（免鉴权，不是 401/503） |
| T5.4 | 反例 | 响应体不含 `pwd_hash` / token 等敏感字段 | key 集合恰为 `{configured, username}`（P2-11：无 `success` 是有意的，本用例锁死） |

### F6 `POST /admin/auth/setup`

| 用例 ID | 类型 | 描述 | 期望 |
|---|---|---|---|
| T6.1 | 正例 | 未建号 + 正确 admin key + 合法 body（**平铺 JSON**） | 200 `{"success": true}`；`admin_account` 落库、`pwd_hash` 以 `pbkdf2_sha256$` 开头 |
| T6.2 | 反例 | `token` 错误 | **401**，detail 恰为 `"Invalid admin key."`（P2-12 文案断言） |
| T6.3 | 反例 | admin key 未配置（`admin_key=""`） | **500** |
| T6.4 | 反例 | `username = "   "`（strip 后为空） | **422**（统一 Pydantic，P2-13），detail 指出 username 不能为空 |
| T6.5 | 反例 | `username = ""` | **422** |
| T6.6 | 反例 | `password = "1234567"`（7 字符） | **422**，detail 指出密码长度 |
| T6.7 | 边界 | `password` 恰 8 字符 | 200 成功 |
| T6.8 | 边界 | `username = "  alice  "` | 200；落库/回显 username 为 `"alice"`（strip 生效） |
| T6.9 | 正例（重置） | 已建号后再 setup（新用户名+新密码，正确 token） | 200；`admin_account` 被覆盖（status 返回新用户名）；**全部 `admin_sess:` 会话被 purge**（此前 login 拿到的 token 再调 admin API → 401） |
| T6.10 | 边界 | body 缺字段（如无 `password`） | 422（FastAPI 校验，不 500） |
| T6.11 | 反例 | 重置时 purge 只删 `admin_sess:`，不动 `admin_account` 与其它 config（如 `llm_key`） | 重置后 `admin_account` 存在、其它 key 不受影响 |
| T6.12 | 反例 | `password` 超长（129+ 字符，如 10KB） | **422**，且**不进入 pbkdf2**（可 monkeypatch `hash_password` 断言未被调用——P1-6 上限守卫） |
| T6.13 | 反例 | body 带**非 ASCII token**（`{"token": "中文token", ...}`） | **401 而非 500**（P1-10：setup 的 token 比对转 bytes） |

### F7 `POST /admin/auth/login`

| 用例 ID | 类型 | 描述 | 期望 |
|---|---|---|---|
| T7.1 | 正例 | 正确用户名+密码（平铺 JSON） | 200 `{"success": true, "token": <str>, "expires_at": <float>}`；`expires_at ≈ now + 7d` |
| T7.2 | 反例 | **未建号**直接 login | **401**，detail 恰为「用户名或密码错误」 |
| T7.3 | 反例 | 已建号但用户名错 | 401，同上文案 |
| T7.4 | 反例 | 用户名对、密码错 | 401，**同 T7.2/T7.3 完全相同文案**（不泄露哪个错） |
| T7.5 | 正例 | 登录成功后 `resolve_session(db, token)` | 能解析出该 username（会话已写入） |
| T7.6 | 正例（会话清理） | 库里预置一条 `updated_at` 很旧的 `admin_sess:` 行 → 登录 | 旧行被 `cleanup_config_by_prefix("admin_sess:%", ...)` 删除（**P0-2 守卫**：前缀无 `%` 则删 0 行、本用例挂），新会话存在 |
| T7.7 | 边界 | 密码恰 8 字符的账号登录 | 200 |
| T7.8 | 反例 | token 字段确认 | 响应 token 不等于明文密码、不等于 admin key（是新生成的 urlsafe 串） |
| T7.9 | 边界 | 空 body / 缺 password | 422 |
| T7.10 | 反例 | login 带超长 password（129+ 字符） | **422**，且**不进入 pbkdf2**（monkeypatch `verify_password`/`hash_password` 断言未被调用——免登录端点 CPU 放大守卫，P1-6） |
| T7.11 | 正例 | 用户名带空格：`" alice "` + 正确密码（存档为 `"alice"`） | **200**（P1-8 strip 定档守卫） |

### F8 `POST /admin/auth/logout`（甲案：有 token 即 200）

| 用例 ID | 类型 | 描述 | 期望 |
|---|---|---|---|
| T8.1 | 正例 | 带有效 session token | 200 `{"success": true}`；该会话行被删（再用同 token 调 admin API → 401） |
| T8.2 | 边界（幂等） | 同一 token 再 logout 一次 | 仍 200 `{"success": true}`（按 key 直删、删除不存在的行不报错） |
| T8.3 | 正例 | 带 admin key | 200 `{"success": true}`（no-op，删不到任何会话——见 R4） |
| T8.4 | 反例 | 完全无 Authorization 头 / 空 token | **401**（甲案唯一的 401） |
| T8.5 | 反例 | 伪造不存在的 token | **200 `{"success": true}`**（甲案：不解析会话，响应与有效 token **完全一致**、不泄露 token 有效性；与 T8.2 同为「按 key 直删、0 行也 200」的同一语义） |
| T8.6 | 反例 | logout 后 `admin_account` 不受影响 | status 仍 `configured=true`，原密码仍可登录 |

### F9 端到端集成（必做；**新文件 `tests/test_admin_auth_e2e.py`**，标 `@pytest.mark.integration`，TestClient/ASGI 不起真端口）

| 用例 ID | 类型 | 描述 | 期望 |
|---|---|---|---|
| TI.1 | 正例 | **setup → login → session 调 admin API → logout → 401**：① `GET /admin/auth/status`（未开通）② `POST /auth/setup`（admin key）③ `POST /auth/login` 拿 token ④ `GET /admin/providers` 带 `Bearer <session token>` ⑤ `POST /auth/logout` ⑥ 再调 ④ | ①configured=false ②200 ③200 有 token ④**200** ⑤200 ⑥**401** |
| TI.2 | 正例 | 向后兼容：同一套已 setup 环境，直接用 `Bearer <admin key>` 调 `GET /admin/providers` | 200（脚本/探针路径不回归） |
| TI.3 | 反例 | 重置链路：setup → login 拿 token → **再次 setup（重置）** → 旧 session token 调 admin API | 401（purge 生效） |
| TI.4 | 反例 | 未 setup 时 status → login | status `configured=false`；login 401「用户名或密码错误」 |

**用例总数**：**74**（单元 70 + 集成 4）。
按类型：正例 24 / 反例 37 / 边界值 13（基线 69 = 正 23 / 反 33 / 边 13，系复审逐条点数；整改补入
T4.13、T6.12、T6.13、T7.10 四条反例 + T7.11 一条正例）。**分档以各表「类型」列为准**，
落地时逐条点表核数，如与本行不符以逐条表为准并更正此行。

---

## 3. 前端行为验收点（`src/botflow/static/admin/index.html`，代码另步实现）

> 代码区参考：模板登录块 89-102 行、script 区 417-544 行（`api()` / `doLogin` / `doLogout`）、
> `onMounted` 在 **719 行**（P2-14：头部第 9 行的行号引用已修正，勿在 417-544 里找 onMounted）。
> SPA 无自动化测试基建，以下为**人工/主 agent 验收清单**，逐条可勾。

| 编号 | 验收点 | 期望 |
|---|---|---|
| A1 | 启动清理 legacy key | onMounted 时 `localStorage.removeItem('bf_admin_key')`，旧版存的明文 admin key 不再被读取 |
| A2 | 启动取状态 | 调 `GET /admin/auth/status`（无 Authorization 头）决定视图 |
| A3 | 未开通视图 | `configured=false` → 显示 **setup 表单**：Token / 用户名 / 密码 三字段 + 提交按钮 |
| A4 | 已开通视图 | `configured=true` → 显示 **login 表单**：用户名（**预填 status 返回的 username**）/ 密码 两字段 |
| A5 | 重置入口 | login 视图有「用 Token 重置」链接 → 切到 setup 表单 |
| A6 | doSetup | 用**原生 fetch**（不走 `api()`）POST `/admin/auth/setup`，body 为**平铺 JSON** `{token, username, password}`（P2-15，与 F6 一致，勿套 `{"req":{...}}`）；成功 → 提示并切到 login 表单（预填新用户名）；失败 → 表单内展示服务端 detail（如密码太短/超长 422） |
| A7 | doLogin | 用**原生 fetch** POST `/admin/auth/login`，body **平铺 JSON** `{username, password}`；成功 → token 存 `localStorage['bf_admin_token']`、`authenticated=true`、进入主界面；失败（401）→ 展示「用户名或密码错误」，**不触发全局 401 踢出逻辑** |
| A8 | `api()` 带会话 token | `Authorization: Bearer <bf_admin_token>`；`adminKey`/token 为空时**不发 Authorization 头** |
| A9 | 401 踢出（已登录态） | 任意业务 API 返回 401 且当前 `authenticated=true` → 清 `bf_admin_token`、回登录页（session 已失效/过期/被重置） |
| A10 | doLogout | 先调 `POST /admin/auth/logout`（带当前 token），再清本地 `bf_admin_token`、回登录页；接口失败也清本地（本地登出不被网络故障卡住）。甲案下带 token 的 logout 恒 200，此兜底仅防网络故障 |
| A11 | 登录页无 admin key 字段 | login 视图**不再**出现「Admin Key」输入框（旧 89-102 行的 token 框只保留在 setup 视图） |
| A12 | 刷新持久化 | 登录成功后刷新页面 → 读 `bf_admin_token` 直接进主界面（配合 A2 status 为 configured 时不再强弹登录，token 有效性由首个 API 的 401 兜底） |

---

## 4. 风险与边界（含发现的设计漏洞）

| # | 风险/漏洞 | 影响 | 建议/定档 |
|---|---|---|---|
| R1 | **login 侧 username strip**（原悬而未决） | 用户带空格登录莫名 401 | **已定档（P1-8）**：login 对入参 `username.strip()` 后再与存档比对，与 F6 存档 strip 对称；T7.11 守卫 |
| R2 | **status 免鉴权暴露 username** | 未登录者可枚举出管理员用户名（配合弱密码可爆破） | 可接受（用户名非秘密、面板本就内网部署）；记录在案。若要收紧需引入 csrf/专用限流，超出本任务范围 |
| R3 | **purge 全部会话与 `cleanup_config_by_prefix` 语义不匹配**：该函数按 `updated_at < now-TTL` 删**过期**行，无法「删全部」；且前缀参数是 `LIKE` 原样匹配、**不带 `%` 连过期行也删 0 行**（P0-2） | setup 重置若误用 cleanup，旧 token 重置后仍有效（安全洞）；login 过期清理若前缀漏 `%` 则永远清不掉 | 重置 purge 用 `execute_write("DELETE FROM config WHERE key LIKE 'admin_sess:%'")`；login 过期清理用 `cleanup_config_by_prefix("admin_sess:%", SESSION_TTL_SECONDS)`（**两处都带 `%`**）。**T6.9/T2.10/T7.6/TI.3 是守卫** |
| R4 | **logout 凭据判定**（原 R4/T8.2/T8.5 三方矛盾） | 「已注销 token 再 logout」与「伪造 token」删库后状态相同，信息论上不可区分，401/200 档位必须统一 | **已定档甲案（P0-3）**：不解析会话、按 key 直删——**任意非空 token（有效/admin key/伪造/已注销）→ 200 `{"success": true}`**；**完全无凭据 → 401**。响应不泄露 token 有效性；不知道原 token 算不出 sha256 key、删不掉他人会话。F8/T8.2/T8.5 已同步 |
| R5 | **新逻辑开始用 db，会打破现有直调测试**（结论不变，原因已按 P0-1 修正）：`tests/test_auth.py::TestVerifyAdminKey` 4 个旧用例不传 db；其中走到 session 分支的（如 `test_invalid` 的错误 token → ② `get_db()`）在未初始化时抛 RuntimeError 而非返回 401 | 旧测试挂 | 预期副作用（测试适配、非功能回归）：验证子 agent 更新旧用例——错误 token 用例 monkeypatch `botflow.auth.get_db`（照 `TestVerifyLLMKey::test_uses_module_get_db_when_db_none` 模式）或确保 fixture `Database.initialize()` 已设 `_active_db`。**注意：签名不变（P0-1），不能给旧测试传 db 参数** |
| R6 | **admin key 未配置时 session 也进 500**（500 分支最先判） | 运维删掉 `BOTFLOW_ADMIN_KEY` 后，即便账号会话有效，全部 admin API 与 setup 都 500 | 设计如此（admin key 是根信任，setup 重置也依赖它），保留；记录为已知行为，测试 T4.2/T6.3 守卫 |
| R7 | **login/setup 弱口令爆破面**（原论断有误，按 P1-7 改为事实） | 弱口令风险仍在 | **事实**：`RateLimitMiddleware` 只跳过 `/health`（core.py:548），**对 `/admin/*` 同样生效**——无 Authorization 的 login/setup 按 `request.client.host` 分桶 300 次/分钟（core.py:522-532,576），带 Bearer 按 token 分桶。此为**粗限流，不足以防弱口令爆破**；本任务不加专用登录限流（YAGNI），风险记录保留，密码 ≥8 + ≤128 + pbkdf2 600k 抬高爆破成本 |
| R8 | **会话无滑动续期**：7 天固定过期，用到第 6.9 天登录会话仍会在第 7 天断 | 体验 | 按设计不做滑动续期（YAGNI）；SPA A9 的 401 踢出已兜底，用户重登即可 |
| R9 | **`exp` 与 `updated_at` 双时钟**：cleanup 按 `updated_at`（SQLite `datetime('now')` UTC），resolve 按 `exp`（`time.time()` epoch） | 时区/时钟源不同可能导致清理早删或晚删 | `set_config` 写入时 `updated_at ≈ 创建时刻`，TTL 相同 → 偏差可忽略；会话不更新（无 touch），`updated_at` 恒等于创建时刻。记录即可，测试 T2.10 覆盖清理行为 |
| R10 | **JSON 损坏的会话行会残留**（resolve 返回 None 但行不删） | 存储垃圾 | login 时 `cleanup_config_by_prefix("admin_sess:%", ...)` 按时间终会清掉；不额外加「损坏即删」逻辑（避免过度工程）。T2.6/T4.8 只要求不炸 |
| R11 | **`compare_digest` 对长度不等的串会快速返回**（仍常数时间于较短者）；对**非 ASCII str 直接抛 TypeError** | 前者：理论侧信道；后者：本应 401 却 500（P1-10） | 侧信道与现状一致不改进；TypeError 已定档修复——**F4 ①与 F6 的 token 比对统一转 bytes**，T4.13/T6.13 守卫 |
| R12 | **pbkdf2 600k 迭代 × 并发/超长 login = CPU 压力**（同步哈希在事件循环里跑；pbkdf2 成本随输入长度线性增长） | 短时 login 风暴或超长密码请求阻塞事件循环 | 已按 P1-6 加**密码 ≤128 上限**（哈希前拦截，T6.12/T7.10 守卫）+ R7 既有 300/min 粗限流；单管理员面板、登录低频，可接受。测试务必 monkeypatch 调低迭代。**若**未来成问题再 `asyncio.to_thread`，本任务不做（YAGNI），记录在案 |

---

## 5. 覆盖率计划

### 预期新增可执行行（源码侧）

| 落点 | 预期行数（量级） |
|---|---|
| `auth.py`：`PBKDF2_ITERATIONS`/`SESSION_TTL` 常量 + `hash_password`/`verify_password` + `session_config_key`/`resolve_session`/`create_session` + `_require_admin_key` | ~60–80 行 |
| `auth.py`：`verify_admin_key` 扩展（**签名不变**，内部 `get_db()` + 空 token 短路 + bytes 比对 + 双通道分支） | 净增 ~10–18 行 |
| `admin_api.py`：4 个 Pydantic body 模型（含长度上限校验）+ 4 端点函数 | ~70–95 行 |
| `index.html`（前端，不计入 Python 覆盖率） | n/a |
| **合计（Python）** | **约 150–195 行** |

### UNCOVERED 声明

- **预期 `# UNCOVERED: [原因]` 标注数 = 0。**
  理由：① 零数据库迁移是设计承诺——不存在「旧库升级分支」；② 全 stdlib 实现，无平台差异分支、
  无可选依赖降级分支；③ 所有错误分支（401/422/500、JSON 损坏、过期、非 ASCII token、空 token 短路）
  均有上表直调或 HTTP 用例覆盖；④ `verify_admin_key` 内部 `get_db()` 行由 T4.10 monkeypatch 覆盖
  （P0-1 修正后的唯一前提）；⑤ 前端 index.html 不在 pytest 覆盖率口径内（覆盖率只算 `botflow` 包）。
- 若实现中出现**确实无法覆盖的行**（例如 FastAPI/Pydantic 框架内部自动生成的行），须在该行标
  `# UNCOVERED: [原因]` 并在 `<任务>_review.md` 里逐条列出；**目标仍是 0 条业务行**。

### 验证命令（AGENTS.md 规定，门槛 100%）

```bash
PYTHONPATH=src python -m pytest tests/ --cov=botflow --cov-report=term -m "not integration"
```

集成用例（TI.1–TI.4）**指名文件**跑、必做不跳过（P1-9）：

```bash
PYTHONPATH=src python -m pytest tests/test_admin_auth_e2e.py
```

> 不用 `pytest tests/ -m integration`：那会把现有 `tests/test_integration.py`（打真实服务
> 127.0.0.1:4000 的 live 测试，文件级 `pytestmark = integration`）一并无差别收集，无实服务时
> TI.1–TI.4 会被无关失败挡住。`@pytest.mark.integration` 标记仍保留，供 `-m` 甄别。

---

## 6. 明确不做（不改什么）

| # | 不做的事 | 理由 |
|---|---|---|
| 1 | 不加表、不改 schema、不写迁移脚本 | 零迁移是硬约束，复用 `config` KV |
| 2 | 不引入 passlib/bcrypt/jwt 等新依赖 | 决策阶梯第 3 级：stdlib `hashlib`/`secrets` 足够 |
| 3 | 不做会话滑动续期、不做多管理员/权限分级 | YAGNI；单管理员 7 天固定 TTL |
| 4 | 不改 `verify_admin_key` 的**签名**与现有 401/500 文案、`_extract_token` 语义 | 签名改动 = 应用起不来（P0-1）；文案/语义改动破坏探针与既有测试（T4.3/T4.12 守卫） |
| 5 | 不给 auth 端点加**专用**登录限流/验证码 | 已有 `RateLimitMiddleware` 300 次/分钟粗限流对 `/admin/*` 生效（R7 已按事实修正），专用限流 YAGNI，另立任务 |
| 6 | 不动 `admin_dashboard.py` / 静态资源挂载逻辑 | 本任务只改 index.html 的登录块与 script 区 |
| 7 | 不重写 `docs/design.md` 其它章节 | 只在第 5 节端点表补 4 行 + 一句保护说明（F10） |
| 8 | 本步不写 `tests/` 下任何代码 | 双 Agent 流程第 1 步只出本文档，交验证子 agent 审核 |

---

## 整改记录（v1 → v2，对照 `docs/tasks/admin_auth_review.md`）

| 报告编号 | 整改内容 | 落到本文档何处 |
|---|---|---|
| **P0-1** | 删除 `db: Database = None` 签名方案：**F4 改为签名三参数原样不变**，session 分支内部 `db = get_db()`（含「30+ 条 Depends 路由注册即崩」的依据说明）；硬约束新增第 5 条锁死此点；T4.10 改写为「覆盖内部 `get_db()` 行的 monkeypatch 模式」；R5 结论保留、原因改为「session 分支直调 get_db 打破旧直调测试、且不能给旧测试传 db 参数」 | 硬约束 5、F4、T4.10、R5、§5 覆盖率表、§6-4 |
| **P0-2** | 清理前缀统一 `"admin_sess:"` → **`"admin_sess:%"`**（LIKE 原样匹配、不带 % 删 0 行；引 core.py:407 `"dedup:%"` 先例）；F6 重置 purge 的 `LIKE 'admin_sess:%'` 字面量本就正确、保留 | F2、F7、T2.10（改为 P0-2 守卫用例）、T7.6（同）、R3、R10 |
| **P0-3** | logout 三处矛盾按**甲案**统一：不解析会话、按 key 直删、**任意非空 token → 200**、无凭据 → 401；F8 重写为判定表、T8.5 期望 401 → **200**（与有效 token 响应一致、不泄露有效性）、R4 改写为甲案定档、T8.2/T8.4/TI.1⑤ 复核一致 | F8、T8.2、T8.5、R4、A10 |
| **P1-4** | F4 判定顺序显式加**「token 为空 → 先 401」短路**（先于 ①②，防 `compare_digest(None)` TypeError / `sha256(None.encode)` AttributeError → 500）；T4.4 改为该短路的守卫并写明期望 | F4 步骤 3、T4.4 |
| **P1-5** | 用例计数更正：原「59」错误 → 基线复审点数 **69**，本次补入 5 条后 **总数 74（单元 70 + 集成 4）**，分档 正 24 / 反 37 / 边 13，保留「以逐条表为准」核数约定 | §2 计数行 |
| **P1-6** | 密码上限定档：**8 ≤ password ≤ 128**、username strip 后 ≤64，setup/login **算哈希之前** Pydantic 校验 → 422；补 **T6.12**（setup 超长密码 422 且不进 pbkdf2）、**T7.10**（login 超长密码 422 且不进 pbkdf2） | F1、F6、F7、T6.12、T7.10、R12 |
| **P1-7** | R7 事实修正：原「现有 admin 路由本无限流」错误 → **`RateLimitMiddleware` 只跳过 `/health`、对 `/admin/*` 生效、300 次/分钟（IP/token 分桶）**；论断改为「粗限流不足以防弱口令爆破，仍不加专用限流（YAGNI）」；§6-5 同步 | R7、§6-5 |
| **P1-8** | login username **strip 定档**（与 setup 对称）；补 **T7.11**（`" alice "` → 200）；R1 从「悬而未决」改为「已定档」 | F7、T7.11、R1 |
| **P1-9** | 集成用例落点改为**新文件 `tests/test_admin_auth_e2e.py`**（标 integration、TestClient 不起端口）；跑测命令改**指名文件**，并注明不跑 `pytest tests/ -m integration` 的原因（现 test_integration.py 是打 127.0.0.1:4000 的 live 测试会污染） | §2 头注、F9 节标题、§5 集成命令 |
| **P1-10** | 非 ASCII token 定档：**F4 ① 与 F6 的 admin key 比对统一转 bytes**（`compare_digest(str.encode(), ...)`），防 TypeError→500；补 **T4.13**（verify_admin_key 非 ASCII → 401）、**T6.13**（setup 非 ASCII token → 401）；R11 补记 TypeError 行为 | F4 步骤 4、F6、T4.13、T6.13、R11 |
| **P2-11** | F5 注明**有意不带 `success` 字段**（状态查询非操作结果），T5.4 锁死 key 集合 | F5、T5.4 |
| **P2-12** | F6 401 detail 定为 **`"Invalid admin key."`**（与 verify_admin_key 一致），T6.2 补文案断言 | F6、T6.2 |
| **P2-13** | F6 参数校验从「400/422 二选一」统一为**Pydantic → 422**，T6.4/T6.5/T6.6 期望收窄为 422 | F6、T6.4–T6.6 |
| **P2-14** | 头部行号引用修正：`onMounted` 在 **719 行**（不在 417-544 区间）；§3 头注同步 | 文档头第 9 行、§3 头注 |
| **P2-15** | F6/F7 注明**平铺 body** 理由（新端点无 SPA 兼容包袱、与 PATCH 平铺先例一致 admin_api.py:20-27）；A6/A7 重申请求体形状 | F6、F7、A6、A7 |
| **P2-16** | T4.11 断言降级为「通过级别」，注明 ①② 同效不可直接观测、坐实分支需可选 monkeypatch `resolve_session` | T4.11 |

> 整改后回传验证子 agent 复审；复审按报告第 5 节约定**只核对 P0/P1 项**，不重复全量核对。
