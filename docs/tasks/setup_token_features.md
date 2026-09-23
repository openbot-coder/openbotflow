# setup_token 功能点与测试用例：自动生成 setup token 与开通通道切换

> 任务类型：鉴权重构（setup 凭据从「用户手输 BOTFLOW_ADMIN_KEY」改为「系统自动生成的独立一次性凭证」）
> 范围（用户已拍板的三条决策 + 工程细节，全部硬约束，见 §1）：① 双写出处（`.setup_token` 文件 0600 + 启动日志）；
> ② 生命周期「生成一次、用到开通为止」（FastAPI 启动钩子四分支）；③ `POST /admin/auth/setup` 彻底切换到
> setup token，BOTFLOW_ADMIN_KEY 退出 setup 流程但 Bearer 通道原样保留。
> 关联（现状引用）：`src/botflow/admin_api.py:17`（`_require_admin_key` import）、`admin_api.py:148-165`（auth_setup）、
> `admin_api.py:133-145`（auth_status）、`src/botflow/auth.py:168`（`_require_admin_key` 定义）、`auth.py:190`
> （verify_admin_key 内调用）、`src/botflow/core.py:284-340`（lifespan 启动钩子挂点，`db = _get_db()` 在 :316）、
> `src/botflow/config.py:24`（admin_key）、`common/logger.py:53`（loguru `get_logger`）、`.gitignore:64`（`/_*.py`）、
> `docs/design.md:295,301`（端点表）、`pyproject.toml:55-57`（integration marker + addopts）。
> **版本**：v2（ZG-1/ZG-2/P1-1 整改完成，见文末整改记录；结论：放行进编码）。

---

## 0. 硬约束（违反即打回）

1. **P0 安全红线：任何 HTTP 出口绝不返回 setup token**。`GET /admin/auth/status` 及一切免鉴权接口的响应体
   （含序列化全文）不得出现 token 明文或其 KV 记录——公网免鉴权接口返回开通凭证 = 任何人可抢占账号。
2. **明文只存在两处，均在服务器侧**：`.setup_token` 文件（0600）+ 启动日志一行。**DB（config KV）只存
   sha256 哈希 + 生成时间，明文不进库**。
3. **BOTFLOW_ADMIN_KEY 退出 setup 流程，但它的 Bearer 通道不动**：脚本带
   `Authorization: Bearer <BOTFLOW_ADMIN_KEY>` 调 `/admin/*` 行为不变；`verify_admin_key` 的
   401 文案 `"Invalid admin key."` 一字不改；只有 **setup 端点**的 401 detail 改为
   `"Invalid setup token."`。
4. **零迁移、零新增依赖**：继续用 `config` KV（`get_config/set_config`），文件操作只用 stdlib
   （`os`/`secrets`/`hashlib`）；日志用现成 loguru（`botflow.common.logger.get_logger`）。
5. **不变项（§1.4 逐条列示）一个都不动**：用户名密码登录、PBKDF2-SHA256 600k、会话 7 天存 config KV、
   `verify_admin_key` 双通道、`status` 免鉴权形状——全部原样。
6. **覆盖率 100%**（`PYTHONPATH=src python -m pytest tests/ --cov=botflow -m "not integration"`），
   不可覆盖行标 `# UNCOVERED: [原因]`；本任务**预期 UNCOVERED = 0**。

---

## 1. 背景与定案

### 1.1 背景

现有 admin 登录体系刚上线（commit 0612f8f，生产已部署）：首设表单要求用户输入「token」=
`BOTFLOW_ADMIN_KEY`（一次性开通 → 用户名密码登录，会话 7 天）。产品负责人推翻该设计，定案：**setup token
必须是系统自动生成的独立凭证，与 BOTFLOW_ADMIN_KEY 是两个不同的东西**。

### 1.2 定案（三条决策 + 工程细节，均为硬约束）

**决策 1 — 双写出处**：自动生成的 setup token 写两处，都只在服务器侧：

- 明文文件 `<项目根>/.setup_token`（生产即 `/mnt/deploy/botflow/.setup_token`），权限 **0600**：
  `os.open(path, O_WRONLY|O_CREAT|O_TRUNC, 0o600)` 创建；**创建后必须 `os.fchmod(fd, 0o600)`**——
  `O_CREAT` 的 mode 会被进程 umask 掩码（umask 022 时落成 0644），只有显式 chmod 才钉死 0600；
  若文件已存在但权限过宽，同样在这一步收紧（内容截断重写或仅收紧，见决策 2 各分支）。
- 启动日志打一行（loguru，`botflow.common.logger.get_logger` 现成）：含明文 token，供运维 `cat` 不便时
  从日志取。
- **`/admin/auth/status` 等一切 HTTP 出口绝不返回 token（P0 红线，见硬约束 1，写入 R1）。**

**决策 2 — 生命周期「生成一次用到开通为止」**：挂在 FastAPI 启动钩子（`core.py` lifespan）上：

| 分支 | 条件 | 动作 |
|---|---|---|
| ① 幂等清理 | `admin_account`（config KV 账号记录）**已配置** | 删 token KV 记录（若存在）+ 删 `.setup_token` 文件（若存在）；不生成 |
| ② 首次生成 | 未配置 + KV **无** token 记录 | **动作序：先写文件、再写 KV、失败非致命**（ZG-1 定档）——生成新 token（`secrets.token_hex(16)` = 32 hex 字符）→ ① `os.open(O_WRONLY\|O_CREAT\|O_TRUNC, 0o600)` → 无条件 `os.fchmod(fd, 0o600)` → write 32 hex → fsync/close **成功** → ② 才写 KV（sha256 + 生成时间，明文不进 DB）→ ③ 打日志。**写文件任何一步失败：仅 `log.error`（含路径与 errno），不抛、不中止启动、不写 KV**——下次启动重新生成自愈；**写 KV 失败（文件已成功）：同样只告警**——下次启动无 KV 记录 → 重新生成覆盖文件，自愈 |
| ③ 轮换 | 未配置 + KV **有**记录但**文件校验不过**（文件缺失 **或内容非 32 位 hex**：0 字节/截断/污染） | 明文不可还原 → **换新重新生成**（覆盖旧哈希与文件）；写成功后打日志 `rotated: file missing` / `rotated: file invalid`；写文件/KV 失败同分支②非致命告警 |
| ④ 不动 | 未配置 + KV 有记录且**文件校验通过**（存在且内容恰 32 hex） | **不动**（重启不轮换）；仅按决策 1 把权限收紧到 0600（收紧权限不是轮换，内容与哈希均不变；收紧失败只告警）。**校验不过 → 落分支③轮换**——关掉「KV 在、文件坏 → 永远开不了通」的唯一真死锁 |

- setup **成功**后 → 删 KV 记录 + 删文件（现有的 purge 全部旧会话逻辑**保留**）。

**决策 3 — 开通通道彻底切换**：

- `POST /admin/auth/setup` 的 token 校验改为：**对照 KV 中哈希**（sha256 hex，高熵随机 token 够用）+
  `secrets.compare_digest` 常数时间比对；
- **BOTFLOW_ADMIN_KEY 不再能开通**（退出 setup 流程），但它原有的 Bearer 通道**原样保留**；
- 空/缺失 token 短路 401 的既有守卫保留；401 detail 文案从 `"Invalid admin key."` 改为
  **`"Invalid setup token."`**（**仅 setup 端点**；Bearer 通道的 401 文案不动）；
- login/logout/status 行为不变；
- **`_require_admin_key()` 调用点现状（grep 结论，见 F5）**：setup 不再用它之后**并未成为死代码**——
  `verify_admin_key`（`auth.py:190`）仍是它的调用方，**保留函数**；只删 `admin_api.py:17` 那行
  随之失效的 import（AGENTS.md「优先考虑删除而非添加」适用于这行无用 import）；
- `.gitignore` 增加 `/.setup_token`（现有 `/_*.py` 模式覆盖不到它）。

### 1.3 P0 红线（写进 R1）

`GET /admin/auth/status` 等**免鉴权** HTTP 接口一旦返回 setup token（明文或可反推材料），
公网任何人都能抢先开通/重置管理员账号 = 直接接管面板。**任何端点、任何字段、任何错误响应都不得吐出
token；KV 哈希同样不出口**（哈希虽不可逆，但属「开通凭据的比对材料」，一并封死）。

### 1.4 不变项（显式声明，逐条不动）

| # | 不变项 | 现状锚点 |
|---|---|---|
| 1 | 用户名密码登录 `POST /admin/auth/login` | `admin_api.py:168-193` |
| 2 | PBKDF2-SHA256 600k（`PBKDF2_ITERATIONS`、存档格式、长度校验 8–128） | `auth.py:43,50-74` |
| 3 | 会话 7 天（`SESSION_TTL_SECONDS`）存 config KV、`admin_sess:` 前缀清理 | `auth.py:47,133-165` |
| 4 | `verify_admin_key` 双通道（session token / Bearer admin key），含 500/401 文案与空 token 短路 | `auth.py:179-214` |
| 5 | status 免鉴权形状 `{success, configured, username}`（只减不增字段） | `admin_api.py:133-145` |
| 6 | logout 甲案（带任意非空 token 即 200） | `admin_api.py:196-214` |
| 7 | SPA 首设表单**结构**（Token/用户名/密码三字段结构照旧）——但 `index.html` **改 3 处文案**（表单字段结构不改，SPA 纳入编码范围，见 F6/P1-1） | `admin_auth_features.md` A3/A6 |
| 8 | setup 成功后 purge 全部旧会话（`LIKE 'admin_sess:%'` 字面量） | `admin_api.py:164` |
| 9 | core.py 启动时「admin key 未配置」告警（Bearer 根仍需配置） | `core.py:337-339` |

### 1.5 `_require_admin_key` 调用点现状（grep 全仓结论）

| 位置 | 性质 | 处置 |
|---|---|---|
| `src/botflow/auth.py:168` | 定义 | **保留** |
| `src/botflow/auth.py:190` | `verify_admin_key` 第一步（admin key 未配置 → 500），**活调用方** | 保留 → **不是死代码** |
| `src/botflow/admin_api.py:17` | import；`:153` 在 `auth_setup` 内调用 | setup 切换后 import 成未用 → **删 import 行**（函数本体不动） |
| `tests/test_admin_auth.py:34` import、`:270` T3.1、`:274` T3.2 | 直调测试 | 保留有效（函数不删，用例不改） |
| `docs/tasks/admin_auth_features.md:53,67,90,198,341` | 文档引用 | 不回改历史任务文档 |

**结论：`_require_admin_key` 非死代码，按 AGENTS.md 删除规范只清 `admin_api.py` 的失效 import。**

### 存储布局汇总（写入 `config` 表，零迁移）

| key | value | 写入点 | 删除点 |
|---|---|---|---|
| `admin_setup_token`（新，模块常量 `SETUP_TOKEN_KEY`） | `{"hash": <sha256 hex>, "created_at": <float epoch>}`——**无明文** | F1 分支②③ | F4 setup 成功；F2 分支① |
| `admin_account` | `{"username", "pwd_hash"}`（现状不动） | F3 setup | — |
| `admin_sess:<hash>` | 会话（现状不动） | 现状 | setup purge、login 清理 |

文件：`<项目根>/.setup_token` = token 明文（32 hex + 换行与否定死：**纯 32 hex、无换行**，
`cat`/`strip()` 取用一致）。

---

## 2. 功能点清单

### F1 setup token 生成与双写原语（`src/botflow/auth.py` 新增）

- **行为**：
  - `SETUP_TOKEN_KEY = "admin_setup_token"` 模块常量；
  - `generate_setup_token() -> str`：`secrets.token_hex(16)`（32 hex 字符）；
  - `write_setup_token_file(path: Path, token: str) -> None`：
    `os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)` → `os.fchmod(fd, 0o600)`
    （**fchmod 无条件执行**：修 umask 掩码 + 收紧过宽旧权限，R2）→ `os.write` → `os.close`；
  - `ensure_setup_token(db) -> None`：实现决策 2 的四分支编排（读 `admin_account`、读/写 KV、
    调文件原语、打日志）。
- **输入输出**：输入 = config KV（`admin_account`、`admin_setup_token`）+ 文件系统状态；
  输出 = KV 记录 `{"hash","created_at"}`（仅哈希）+ 0600 文件（明文）+ 一行日志（含明文）。
- **错误分支**（定档 **「先写文件、再写 KV、失败非致命」**，ZG-1 推翻原 fail-fast；理由见 R7——这是
  LLM 网关，开通便利功能不配让全量网关陪葬；顺序保证下「KV 有哈希、明文永失」在代码路径上**不可形成**
  （KV 记录 ⟹ 文件已完整写入））：
  - **写文件失败（open/fchmod/write/fsync/close 任一步）→ 仅 `log.error` 告警（含路径与 errno），不抛、
    不中止启动、不写 KV**：下次启动分支②/③ 重新生成自愈；测试 T2.10 守卫。
  - **写 KV 失败（文件已成功）→ 同样只告警**：下次启动无 KV 记录 → 分支②重新生成覆盖文件，自愈。
  - 分支①删 KV/删文件：**删文件失败只 loguru error、不抛**（清理非致命，下次启动分支①重试）；
    删不存在的 KV/文件天然幂等（`get_config` 空、`FileNotFoundError` 吞掉）；测试 T2.11 守卫。
  - 日志由 `get_logger("<module>")` 出，明文进日志是**有意设计**（暴露面论证见 R3）。
- **涉及文件**：`src/botflow/auth.py`。

### F2 启动钩子接线（`src/botflow/core.py` lifespan）

- **行为**：在 `lifespan` 内、`db = _get_db()`（`core.py:316`）之后、路由挂载前后的任意位置——
  推荐紧随 llm_key 同步块——调用 `await ensure_setup_token(db)`，一行接线；日志复用现有 `log`
  或 auth 模块 logger。
- **输入输出**：无新入参；依赖 lifespan 已就绪的 `db` 与 config。
- **错误分支**：**不再有 fail-fast**——F1 的写文件/写 KV/删文件失败全部降级为 `log.error` 告警，
  `ensure_setup_token` 不抛 → lifespan 不中断、**服务照常启动**（自愈依赖下次启动的分支②/③重新生成）。
- **涉及文件**：`src/botflow/core.py`（lifespan，`core.py:284-340` 区间内一行）。

### F3 setup 端点校验切换（`src/botflow/admin_api.py` `auth_setup`，`admin_api.py:148-165`）

- **行为**：
  1. **空/缺失 token 短路**：`if not req.token:` → 401（`AuthReq.token` 缺省 `""` 的语义保留，
     缺字段 = 空串 = 同一短路，不 422、不 500）；
  2. 读 KV `SETUP_TOKEN_KEY`：**无记录 → 401**（原「admin key 未配置 → 500」分支**废除**，
     admin key 与 setup 彻底解耦，R5/T6.3 改写依据）；
  3. 比对：`secrets.compare_digest(hashlib.sha256(req.token.encode("utf-8")).hexdigest().encode(),
     stored_hash.encode())`——**先哈希再比 hex**，输入任意字节都可哈希，天然免疫非 ASCII
     `compare_digest` TypeError（原 T6.13 的 401 语义不变、实现依据更新）；
  4. 不等 → **401 detail 恰为 `"Invalid setup token."`**（只此端点改文案）；
  5. 相等 → 走既有成功路径（Pydantic 422 校验、`admin_account` 写入、purge 会话，**全部不变**）。
- **输入输出**：body 仍为平铺 `{token, username, password}`；成功 `{"success": true}`；
  失败 401 `{"detail": "Invalid setup token."}`。
- **错误分支**：空 token → 401 短路；无 KV → 401；哈希不等 → 401；非 ASCII token → 401（非 500）；
  参数非法 → 422（不变）；**不再存在 500 分支**。
- **涉及文件**：`src/botflow/admin_api.py`。

### F4 setup 成功双删（`src/botflow/admin_api.py` `auth_setup` 成功路径末尾）

- **行为**：setup 返回 200 前，删 KV `SETUP_TOKEN_KEY` 记录 + 删 `.setup_token` 文件（若在）；
  既有 purge 会话（`admin_api.py:164`）保留。**凭证用毕即焚：开通后 setup token 不再可用**
  （重置账号需重启服务走分支②重新生成——见 §7 明确不做）。
- **输入输出**：成功 200 后的状态 = KV 无 token 记录、文件不存在、`admin_account` 已写、
  `admin_sess:` 全清。
- **错误分支**：文件本就不存在 → `FileNotFoundError` 吞掉（幂等，T4.4）；删 KV 幂等；
  **失败路径（401）绝不清理**（token 输错一次就把凭证删了 = 自锁，T4.3 守卫）。
- **涉及文件**：`src/botflow/admin_api.py`。

### F5 `_require_admin_key` 处置（grep 结论落功能点）

- **行为**：函数**保留**（调用方 `verify_admin_key` @ `auth.py:190` 在，非死代码，见 §1.5）；
  删除 `admin_api.py:17` 的 `from botflow.auth import ... _require_admin_key ...` 中这一项
  （其余 import 项不动）——setup 不再用它后该 import 成未用，按 AGENTS.md「优先考虑删除」清掉。
- **输入输出**：无行为变化（`verify_admin_key` 的 500 分支与文案原样）。
- **错误分支**：无新分支。
- **涉及文件**：`src/botflow/admin_api.py`（import 行）；`src/botflow/auth.py` **不改**。

### F6 不变项回归面（声明 + 守卫用例；index.html 结构不改、**改 3 处文案**）

- **行为**：§1.4 九条不变项逐条由 T5.x / TI.3 回归守卫；其中 **Bearer 通道**（脚本带
  `Authorization: Bearer <BOTFLOW_ADMIN_KEY>` 调 `/admin/*`）必须在 setup 切换后仍 200。
- **index.html 改 3 处**（原「不改」结论修正，P1-1）：表单字段结构不改（三字段照旧），但
  `:98` placeholder「输入管理员密钥」→「输入开通 Token」（**诱导填 admin key 的行为必须消灭**）；
  `:590`/`:594` 两处与实现不符的注释改真。改动量几行，SPA 由此纳入编码范围。
- **输入输出**：同现状（除上述 3 处文案）。
- **错误分支**：`verify_admin_key` 的 401 detail 仍是 `"Invalid admin key."`（**与 setup 新文案
  并存，双文案各守其门**，T5.3）。
- **涉及文件**：`src/botflow/static/admin/index.html`（**仅 3 处文案，结构不改**）；守卫用例见 §2 表 T5.x、TI.3。

### F7 `.gitignore` 追加（仓库卫生）

- **行为**：新增一行 `/.setup_token`（根目录锚定）。现有 `/_*.py`、`*.log` 等模式均匹配不到该文件名；
  不加则 SSH 部署目录 `git status` 持续脏、且存在误提交明文凭据的风险。
- **输入输出**：`git check-ignore .setup_token` → 命中；`git status` 干净。
- **错误分支**：无。
- **涉及文件**：`.gitignore`（现 67 行，追加 1 行 + 注释）。

### F8 文件路径配置项（`src/botflow/config.py` 如需配置项）

- **行为**：`BotflowSettings.setup_token_file: str = ""`（env 前缀 `BOTFLOW_` 自动支持
  `BOTFLOW_SETUP_TOKEN_FILE`）。空 → 默认 `<项目根>/.setup_token`，项目根 = 
  `Path(__file__).resolve().parents[2]`（`src/botflow/config.py` → 项目根；生产
  `/mnt/deploy/botflow` 即项目根 checkout，直接命中）。非空 → 按绝对路径用（兜底非常规安装形态）。
- **输入输出**：路径字符串 → `Path`。
- **错误分支**：配置为相对路径 → 以项目根为基准解析（或直接拒绝要求绝对路径，实现时二选一并写注释）；
  父目录不存在/不可写 → 归 F1「写文件失败仅 `log.error` 告警、非致命」。
- **涉及文件**：`src/botflow/config.py`（加 1 字段，仿 `admin_key` @ `config.py:24`）。

### F9 `docs/design.md` 端点表与保护说明同步（`docs/design.md:295,301`）

- **行为**：
  1. `:301` 行功能列改为：`用启动时自动生成的 setup token 开通 / 重置管理账号（明文见服务器 .setup_token，0600；开通成功即销毁）`；
  2. `:295` 说明句补一句：**auth 4 端点（status/setup/login/logout）不受 `verify_admin_key` 保护**；
     setup 凭据为启动时生成的一次性 setup token，`BOTFLOW_ADMIN_KEY` 只用于 Bearer 通道与会话签发期的
     管理面准入（`verify_admin_key` 双通道之一）。
  3. **只补这两处，不重写第 5 节。**
- **输入输出**：文档。
- **错误分支**：无。
- **涉及文件**：`docs/design.md`。

### 涉及文件总表

| 文件 | 改动 |
|---|---|
| `src/botflow/auth.py` | 新增 `SETUP_TOKEN_KEY` / `generate_setup_token` / `write_setup_token_file` / `ensure_setup_token`（F1）；`_require_admin_key` 不动 |
| `src/botflow/core.py` | lifespan 一行接线 `ensure_setup_token(db)`（F2） |
| `src/botflow/admin_api.py` | `auth_setup` 校验切换 + 成功双删（F3/F4）；删 `_require_admin_key` import（F5） |
| `src/botflow/config.py` | 加 `setup_token_file` 字段（F8） |
| `.gitignore` | 加 `/.setup_token`（F7） |
| `docs/design.md` | `:295` 补一句、`:301` 改一行（F9） |
| `src/botflow/static/admin/index.html` | **结构不改、改 3 处文案**：`:98` placeholder、`:590`/`:594` 注释（F6，P1-1） |
| `tests/conftest.py` | **新增文件（当前不存在）**：autouse fixture 强制 `BOTFLOW_SETUP_TOKEN_FILE` → `tmp_path`，测试隔离（ZG-2，§3 硬规格） |

---

## 3. 测试用例清单

> 本任务为**编码子 agent**，不写 `tests/`；下表供验证子 agent 落进：
> - 单测 T1.x–T5.x → `tests/test_setup_token.py`（新文件）——文件系统分支用 `tmp_path` 作项目根
>   （monkeypatch `setup_token_file` 指向 tmp_path），KV 用既有 db fixture，HTTP 层沿用
>   `TestClient + app.dependency_overrides[dbmod.get_db]` 模式；
> - 集成 TI.1–TI.5 → `tests/test_setup_token_e2e.py`（新文件），**文件级 `pytestmark = integration`**。
>
> **命令硬要求**：`pyproject.toml:57` 的 `addopts = ["-m", "not integration"]` 会把 integration 用例从
> 默认跑里摘掉、**指名文件也逃不掉**，集成命令**必须显式带 `-m integration`**：
>
> ```bash
> PYTHONPATH=src python -m pytest tests/test_setup_token_e2e.py -m integration
> ```
>
> 单测命令（含 `-m "not integration"`，AGENTS.md 门槛命令）：
>
> ```bash
> PYTHONPATH=src python -m pytest tests/ --cov=botflow -m "not integration"
> ```
>
> **硬性隔离规格（ZG-2，新增文件）**：`tests/conftest.py`（**当前不存在，新建**）加 **autouse
> fixture**：所有测试强制 `BOTFLOW_SETUP_TOKEN_FILE` 指向 `tmp_path`（或 monkeypatch settings），
> 保证任何触发 lifespan 的测试都不往仓库根写 `.setup_token`。该文件已列入 §2 涉及文件总表。

### F1 生成与文件原语（单测，`tmp_path` + 直调）

| 用例 ID | 类型 | 描述 | 期望 |
|---|---|---|---|
| T1.1 | 正例 | 未配置全空（无 `admin_account`、无 KV、无文件）→ `ensure_setup_token` | KV 记录存在（`hash`+`created_at`）、文件存在、内容恰 32 hex |
| T1.2 | 反例 | **哈希不落明文**：遍历 KV `admin_setup_token` 的 value | 不含生成的明文 token；`sha256(文件明文) == value["hash"]` |
| T1.3 | 边界 | **文件 0600**：Unix 断言 `stat.S_IMODE == 0o600`；Windows（本仓测试平台）monkeypatch/spy `os.fchmod` 断言实参 `0o600`（平台差异见 R6） | 权限恰 0600 |
| T1.4 | 边界 | 预置 0644 旧文件 + 双在分支再 ensure | mode 收紧到 0600，**内容不变**（收紧≠轮换） |
| T1.5 | 正例 | 双写一致性：文件明文 → sha256 → 与 KV hash `compare_digest` | 通过（双写可互证） |
| T1.6 | 边界 | token 形状 | `len == 32` 且全 hex（`secrets.token_hex(16)`）；文件**无换行**（`read() == token`） |
| T1.7 | 正例 | **启动日志明文**：`loguru logger.add(list.append)` 捕获生成分支日志 | 恰有一行含明文 token |
| T1.8 | 反例 | 覆盖孤儿文件：无 KV + 磁盘残留旧文件 → 走分支② | `O_TRUNC` 换新，文件内容 ≠ 旧内容，KV 为新哈希 |

### F2 生命周期四分支 + 接线（单测）

| 用例 ID | 类型 | 描述 | 期望 |
|---|---|---|---|
| T2.1 | 正例 | **启动生成**（分支②）：未配置 + 无 KV + 无文件 | 同 T1.1 结果 |
| T2.2 | 反例 | **文件丢失/内容损坏走轮换**（分支③④）：预置 KV（旧哈希），删文件；或文件内容非 32 hex（0 字节/截断/污染） | 一律视为丢失 → 重新生成：新文件内容 ≠ 旧明文、KV hash 被覆盖、日志含 `rotated:`（`file missing` / `file invalid`） |
| T2.3 | 边界 | **双在不轮换**（分支④）：预置 KV + 文件且内容为合法 32 hex | 二次 ensure 后 hash 与文件内容**均不变**（仅 mode 收紧，见 T1.4） |
| T2.4 | 正例 | **已配置态幂等清理**（分支①）：`admin_account` 存在 + KV + 文件 | KV 记录删、文件删、`admin_account` 不动 |
| T2.5 | 边界 | 已配置 + 全空 | 不生成、文件**不被创建**、无异常（幂等） |
| T2.6 | 反例 | 已配置 + 孤儿文件（无 KV） | 文件删（清理优先于生成） |
| T2.7 | 边界 | 已配置 + KV 有 + 文件缺 | 只删 KV，**不重建文件** |
| T2.8 | 正例 | 连续两次 ensure（生成后立即重启模拟） | 第二次落分支④，token 不变（生成→不轮换 闭环） |
| T2.9 | 正例 | **lifespan 接线**：照 `tests/test_core_runtime.py` 的 lifespan 模式驱动 `core.lifespan`，spy `ensure_setup_token` | 被 await 且传入当前 db（core.py 接线行不落 UNCOVERED） |
| T2.10 | 反例 | **写文件失败非致命**（ZG-1）：monkeypatch `os.write`/`os.fchmod` 抛 `OSError`（或指到不可写路径） | `ensure_setup_token` **不抛**、`log.error` 可捕获（含路径与 errno）、**KV 未写入**、lifespan/启动继续 |
| T2.11 | 反例 | 删文件失败：已配置态、分支①**清理（轮换掉旧文件）路径**，`os.unlink` monkeypatch 抛 `PermissionError` | **不抛**、loguru error 可捕获、KV 照删（归因：分支①清理非致命，不值阻断启动） |

### F3 setup 校验切换（HTTP 层）

| 用例 ID | 类型 | 描述 | 期望 |
|---|---|---|---|
| T3.1 | 正例 | 预置 KV+文件，body token = 文件明文 + 合法账号密码 | 200 `{"success": true}`，`admin_account` 落库 |
| T3.2 | 反例 | 错误 token（等长随机 hex） | **401，detail 恰 `"Invalid setup token."`**（文案断言） |
| T3.3 | 反例 | **admin key 开通被拒**：body token = `BOTFLOW_ADMIN_KEY`（非 setup token） | **401 `"Invalid setup token."`**（决策 3 核心守卫） |
| T3.4 | 反例 | **空 token 短路**：`"token": ""` | 401 `"Invalid setup token."`，不进哈希比对（可 spy `hashlib.sha256` 断言 setup 路径未调用） |
| T3.5 | 边界 | body **缺 token 字段**（`AuthReq` 缺省 `""`） | 401（不是 422、不是 500——缺省空串与显式空串同短路） |
| T3.6 | 反例 | **无 KV 记录**（未走启动钩子的极端，替代原 T6.3「500」语义） | **401 `"Invalid setup token."`，不是 500**（admin key 未配置也不再 500） |
| T3.7 | 反例 | 非 ASCII token：`{"token": "中文token", ...}` | **401 而非 500**（先 sha256 后比 hex，天然无 TypeError） |
| T3.8 | 正例 | `admin_key=""`（未配置）+ 正确 setup token | 200——**setup 与 BOTFLOW_ADMIN_KEY 彻底解耦** |

### F4 setup 成功双删（HTTP + 文件，`tmp_path` 作项目根）

| 用例 ID | 类型 | 描述 | 期望 |
|---|---|---|---|
| T4.1 | 正例 | **setup 成功双删**：T3.1 断言 200 后立即查 | KV `SETUP_TOKEN_KEY` 记录**已删**、`.setup_token` 文件**已删** |
| T4.2 | 正例 | 已建号再 setup（重置）→ 200 | 双删仍执行 + 旧会话 purge（沿用原 T6.9 断言） |
| T4.3 | 反例 | token 错 → 401 | KV 记录与文件**原样保留**（失败路径绝不清理，防自锁） |
| T4.4 | 边界 | 双删幂等：文件手工删后再 setup（200 路径） | 仍 200（`FileNotFoundError` 吞掉） |
| T4.5 | 正例 | 串联：setup 成功 → 再跑一次 `ensure_setup_token` | 落分支①（账号已配置 → 幂等清理），不生成新 token |

### F5/F6 不变项与 Bearer 回归（HTTP 层）

| 用例 ID | 类型 | 描述 | 期望 |
|---|---|---|---|
| T5.1 | 反例 | **status 永不含 token（P0 红线）**：免鉴权 GET status，断言 key 集合 + **响应全文文本** | key 集合恰 `{success, configured, username}`；`"admin_setup_token" not in r.text`、KV hash 值 not in r.text、明文 token not in r.text |
| T5.2 | 正例 | **Bearer 兼容回归**：`Authorization: Bearer <BOTFLOW_ADMIN_KEY>` 调 `GET /admin/providers` | 200（切换后脚本通道不回归） |
| T5.3 | 反例 | 双文案各守其门：伪造 session token 调受保护端点 | `verify_admin_key` 的 401 detail 仍 **`"Invalid admin key."`**（不随 setup 文案漂移） |
| T5.4 | 正例 | login/logout 语义不变快速回归：setup 后 login → 200 拿 token；logout 200 | 同现状（`_require_admin_key` 仍被 `verify_admin_key` 调用，T3.1/T4.2 原用例继续生效） |
| T5.5 | 反例 | `admin_api` 不再引用 `_require_admin_key`：`import botflow.admin_api as m; assert not hasattr(m, "_require_admin_key")` | 属性不存在（import 清理锁死；函数本体在 `auth` 模块仍可导出直调） |

### 集成端到端（`tests/test_setup_token_e2e.py`，文件级 `pytestmark = integration`，命令必须带 `-m integration`）

| 用例 ID | 类型 | 描述 | 期望 |
|---|---|---|---|
| TI.1 | 正例 | **完整开通链**（外网视角）：启动 ensure → 读 `.setup_token` 拿 token → status（未开通）→ setup（该 token）→ **再查文件/KV 已双删** → login → session 调 API 200 → logout → 再调 401 | 各步状态全通过 |
| TI.2 | 反例 | **admin key 开通被拒**：未开通环境用 `Bearer`/body 双形式尝试 `token = BOTFLOW_ADMIN_KEY` | setup 401 `"Invalid setup token."`、`admin_account` 未创建、status 仍 `configured=false` |
| TI.3 | 正例 | **Bearer 兼容回归**：setup 完成后 `Authorization: Bearer <BOTFLOW_ADMIN_KEY>` 调 `GET /admin/providers` | 200（原 TI.2 语义保留） |
| TI.4 | 正例 | **重启不轮换**：ensure → 记录 token → 再次 ensure（模拟重启，双在分支）→ 用**原 token** setup | 200（旧 token 未失效） |
| TI.5 | 反例 | 重置链路（原 TI.3 保留、token 换 setup token）：setup → login → 再 setup → 旧 session 调 API | 401（purge 生效） |

### 必须覆盖项 → 用例映射（逐项对账）

| 要求项 | 落到 |
|---|---|
| 启动生成 | T1.1 / T2.1 / TI.1 |
| 文件 0600 | T1.3 / T1.4 |
| 文件丢失/内容损坏走轮换 | T2.2 |
| 双在不轮换 | T2.3 / T2.8 / TI.4 |
| setup 成功双删 | T4.1 / T4.2 / TI.1 |
| 已配置态幂等清理 | T2.4 / T2.5 / T2.6 / T2.7 / T4.5 |
| admin key 开通被拒 401 | T3.3 / TI.2 |
| 空 token 短路 | T3.4 / T3.5 |
| 文案断言 | T3.2 / T3.3（`Invalid setup token.`）、T5.3（`Invalid admin key.` 不动） |
| status 永不含 token | T5.1 |
| 哈希不落明文 | T1.2 |
| Bearer 兼容回归 | T5.2 / TI.3 |

**用例总数**：**42**（单测 **37**：T1.1–T1.8=8、T2.1–T2.11=11、T3.1–T3.8=8、T4.1–T4.5=5、T5.1–T5.5=5；集成 **5**：TI.1–TI.5）。
按类型：**正例 17 / 反例 17 / 边界 8**（单测 正 14 / 反 15 / 边 8；集成 正 3 / 反 2 / 边 0）。
**分档以各表「类型」列为准**，落地时逐条点表核数，如与本行不符以逐条表为准并更正此行。

---

## 4. 验收要点（外网视角，人工/主 agent 逐条勾）

| 编号 | 验收步骤 | 期望 |
|---|---|---|
| A1 | 部署后看启动日志 | 有一行含 32 hex 的 setup token 明文日志（未开通态首次启动） |
| A2 | SSH `ls -l /mnt/deploy/botflow/.setup_token` | 权限 `-rw-------`（0600）、属主为部署用户 |
| A3 | SSH `cat /mnt/deploy/botflow/.setup_token` | 拿到 32 hex token（明文可取，仅本机 root/部署用户） |
| A4 | 外网 `curl /admin/auth/status`（免鉴权） | `{success, configured, username}`，**全文无 token/哈希**（P0 红线抽查） |
| A5 | 浏览器开 `/admin/` → 首设表单，**Token 填 A3 取的值** + 用户名 + 密码 → 提交 | 开通成功，切到登录视图 |
| A6 | 用 **BOTFLOW_ADMIN_KEY** 当 Token 再填一次首设表单（或重置入口） | **401 `Invalid setup token.`**（admin key 不再能开通） |
| A7 | 开通成功后回 SSH `ls -l /mnt/deploy/botflow/.setup_token` | 文件已删（用毕即焚）；status `configured=true` |
| A8 | A5 之后用 A5 的用户名密码登录 | 登录成功进主界面；7 天会话照旧 |
| A9 | 脚本带 `Authorization: Bearer <BOTFLOW_ADMIN_KEY>` 调 `GET /admin/providers` | 200（Bearer 通道兼容） |
| A10 | 未开通态重启服务一次 | `.setup_token` 内容与上次一致（重启不轮换，分支④） |
| A11 | 仓库根 `git status` | `.setup_token` 不出现在待提交列表（`.gitignore` 生效） |
| A12 | 故意删 `.setup_token` 后重启（未开通态） | 日志出现 `rotated: file missing`，新 token 与旧文件不同（拿旧 token 开通 → 401，走 R4 预案） |

---

## 5. 风险与边界

| # | 风险/漏洞 | 影响 | 建议/定档 |
|---|---|---|---|
| R1 | **status（或任何免鉴权出口）泄 token = P0 红线** | 公网任何人拿到开通凭证 → 抢占/重置管理员账号 | 绝不返回；T5.1 除锁 key 集合外**再断言响应全文不含明文与 KV 哈希**；code review 把「setup token 出现在任何 response/HTTPException detail」列为一票否决 |
| R2 | **文件权限两个坑**：① `O_CREAT` mode 被 umask 掩码（022 → 落成 0644，group/other 可读）；② 旧文件权限过宽时 `O_CREAT` 不会改已存在文件的 mode | 凭证对同机其他用户可读 | **无条件 `os.fchmod(fd, 0o600)`**（创建与复用路径都做，F1）；T1.3/T1.4 守卫；部署验收 A2 复核 |
| R3 | **日志明文的暴露面论证**（决策 1 有意双写） | 日志文件可被能读 `logs/` 的人拿到 token | 论证：① token 是**短生命周期**凭据——开通即双删，日志里的旧 token 事后作废，暴露窗口 = 「生成→开通」；② 能读部署机日志的人本来就能 `cat .setup_token`（同一信任域），日志**没有**扩大主机内暴露面；③ 唯一增量风险 = 日志被汇聚/上传到低信任系统——部署须保证日志不外发（loguru 30 天本地轮转，现状如此）。记录在案，不做脱敏（做了运维就拿不到 token，与决策 1 相悖） |
| R4 | **文件丢失轮换导致用户拿旧 token 401**（分支③的代价） | 运维存的旧 token 在下次重启后作废，开通 401 且**无提示**（401 文案不区分错因） | 决策 2 明确接受；轮换必打日志 `rotated: file missing`（A12 验收）；预案 = 重新 `cat` 文件或看新日志行。**不加**「区分错因的 401 文案」（泄露状态，违 F3 统一文案原则） |
| R5 | **与既有 74 用例的回归面——逐个点名**（`tests/test_admin_auth.py` + `tests/test_admin_auth_e2e.py`） | 切换后下述用例断言/前置会挂 | **必改**：① `SETUP_401` 常量（test_admin_auth.py:48）`"Invalid admin key."` → `"Invalid setup token."`；② `_setup()` helper（:135，`token=ADMIN_KEY` 默认）改为预置/读取 setup token 明文——**连带影响所有走成功路径的 T6 用例：T6.1、T6.7、T6.8、T6.9、T6.11**；③ **T6.2**（:450 区间）detail 文案断言改新文案；④ **T6.3**（:455 区间）语义重写——原「admin key 未配置 → 500」废除，改为「无 setup token KV 记录 → 401 `Invalid setup token.`」，**500 断言删除**；⑤ **T6.13**（:531）状态码期望 401 不变，但实现依据从「bytes 比对」改「sha256 后比 hex」，若断言了 detail 需同步改文案；⑥ client fixture（:84）需追加预置 KV+文件（`tmp_path`），否则 T6 成功路径全 401；⑦ e2e `_setup()`（test_admin_auth_e2e.py:61，`token=ADMIN_KEY`）→ **TI.1/TI.2/TI.3 前置改用 setup token**（TI.2 的 Bearer 断言本身不变，它就是兼容回归）；⑧ e2e :80 注释「开通（admin key）」措辞同步。**预期不变（复核即可）**：T3.1/T3.2（`_require_admin_key` 保留）、**T4.1–T4.13 全部**（`verify_admin_key` 与 `Invalid admin key.` 文案不动）、T5.1–T5.5、T6.4/T6.5/T6.6/T6.10/T6.12（422 在 Pydantic 层先于 token 校验拦截，与 token 值无关）、T7.1–T7.11、T8.1–T8.6、T1.x/T2.x 全部、TI.4、`test_admin_api.py`/`test_auth.py` 全部（Bearer 通道）；**另补点名（ZG-2）**：`tests/test_core_runtime.py::TestLifespan` 5 条（:346/:376/:399/:422/:439，**真实驱动 lifespan**）——接线后它们会执行启动生成逻辑，归入「**预期不变但受 conftest 隔离保护**」清单（§3 硬规格保证其不往仓库根写 `.setup_token`），各加一条说明逐条核对 |
| R6 | **测试平台是 Windows，`os.fchmod`/`stat` 权限位不可靠**（win32 只保证只读位） | 断言 0600 的用例在 CI/本机假挂或假过 | T1.3/T1.4 定档双轨：`sys.platform == "win32"` → spy `os.fchmod` 断言实参 `0o600`；POSIX → 真实 `stat.S_IMODE` 断言。T2.11 的只读目录同理用 `os.unlink` monkeypatch |
| R7 | **写文件/写 KV 失败的定档**（原「KV 已写、文件失败 = 死锁态」论述经 ZG-1 整段重写） | 这是 LLM 网关，开通便利功能**不配让全量网关陪葬** | 定档撤销 fail-fast，改 **「先写文件、再写 KV、失败非致命」**：动作序 = `os.open(O_WRONLY\|O_CREAT\|O_TRUNC, 0o600)` → 无条件 `fchmod(0o600)` → write → fsync/close **成功** → 才写 KV → 打日志。顺序保证下 **「KV 有哈希、明文永失」在代码路径上不可形成（KV 记录 ⟹ 文件已完整写入）**，原死锁命题不成立。写文件任一步失败仅 `log.error`（含路径与 errno），不抛、不中止启动、不写 KV，下次启动分支②/③重新生成自愈；写 KV 失败（文件已成功）同样只告警，下次启动无 KV → 重新生成覆盖文件自愈。**同时关掉唯一真死锁**：分支④双在态增加文件内容校验——缺失**或内容非 32 位 hex**（0 字节/截断/污染）→ 一律视为丢失走轮换，打 `rotated:` 日志。T2.10 守卫写失败非致命；分支①删失败只记 error 不抛（清理非致命）；T2.11 守卫 |
| R8 | **并行启动竞态**：两进程同时跑 ensure（部署脚本 systemctl 旧进程未退） | 双写交错 → 一方哈希、一方文件不同值 → 拿文件 token 开通 401 | 单实例部署（现状 uvicorn 单进程），概率极低且 R4 预案（重启重取）自愈；**不加文件锁**（YAGNI），记录在案 |
| R9 | **双根并存的职责边界**：BOTFLOW_ADMIN_KEY（Bearer 根，长期）与 setup token（引导根，一次性） | 概念混淆——运维拿 admin key 去填首设表单（正是本次要消灭的旧行为） | 文档双处钉死：design.md F9 说明句 + A6 验收专门测「admin key 开通被拒」；401 文案 `Invalid setup token.` 本身就在提示填错了东西 |
| R10 | **`.setup_token` 随部署目录被备份/镜像带出**（与 `data/`、`.env` 同信任域） | 快照泄露期间的未使用 token | 与 `.env` 等密钥文件同等对待（备份策略不在本任务范围）；开通即焚已把窗口压到最短；`.gitignore`（F7）防入 Git。记录在案 |
| R11 | **孤儿文件**（KV 记录丢、文件在）未配置态 | 走分支②覆盖旧文件 → 持旧 token 者 401 | 与 R4 同族同预案（KV 是权威、明文只是投递）；T1.8/T2.6 覆盖两个方向 |
| R12 | **status 暴露 username**（既有 admin_auth R2，非本任务引入） | 可枚举管理员用户名 | 结论维持原判（用户名非秘密、面板内网部署），记录不重复处置 |

---

## 6. 覆盖率计划

### 预期新增/改动可执行行（源码侧）

| 落点 | 预期行数（量级） |
|---|---|
| `auth.py`：`SETUP_TOKEN_KEY` + `generate_setup_token` + `write_setup_token_file`（含 fchmod/异常）+ `ensure_setup_token`（四分支 + 两错误分支 + 日志） | ~45–65 行 |
| `core.py`：lifespan 一行接线 | 1 行 |
| `admin_api.py`：setup 校验段重写（短路 / 取 KV / sha256+compare_digest / 401 新文案）+ 成功双删 + 删 import | 净增 ~10–20 行 |
| `config.py`：`setup_token_file` 字段 | 1 行 |
| `.gitignore` / `docs/design.md` | 不计入 Python 覆盖率 |
| **合计（Python）** | **约 60–90 行** |

### UNCOVERED 声明

- **预期 `# UNCOVERED: [原因]` 标注数 = 0。**
  理由：① 全 stdlib 文件/哈希操作，无平台分支必须豁免——Windows 权限位差异由 T1.3/T1.4 的
  双轨断言**覆盖两条实现路径**（`fchmod` 调用行被 spy 断言走到），不产生豁免行；
  ② 四分支 + 写文件/写 KV 失败只告警（非致命，T2.10）+ 分支④内容校验轮换（T2.2）+ 删失败吞掉，
  全部有 T1.x/T2.x 直调用例；③ lifespan 接线行由 T2.9
  （照 `tests/test_core_runtime.py` 既有 lifespan 模式）覆盖；④ setup 端点新旧分支由 T3.x/T4.x
  HTTP 用例覆盖；⑤ `.gitignore`/`design.md` 非 Python。
- 若实现中出现确实无法覆盖的行，标 `# UNCOVERED: [原因]` 并在 `<任务>_review.md` 逐条列出；
  **目标仍是 0 条业务行**。

### 验证命令（AGENTS.md 规定，门槛 100%）

```bash
PYTHONPATH=src python -m pytest tests/ --cov=botflow -m "not integration"
```

集成用例（TI.1–TI.5）指名文件跑、必做不跳过，**必须显式带 `-m integration`**（`pyproject.toml:57`
`addopts = ["-m", "not integration"]` 对指名文件同样生效）：

```bash
PYTHONPATH=src python -m pytest tests/test_setup_token_e2e.py -m integration
```

> 不用 `pytest tests/ -m integration`：那会把 `tests/test_integration.py`（打真实服务 127.0.0.1:4000
> 的 live 测试）一并无差别收集，无实服务时会被无关失败挡住（沿用 admin_auth P1-9 结论）。

---

## 7. 明确不做（不改什么）

| # | 不做的事 | 理由 |
|---|---|---|
| 1 | **不做 TTL / 定期轮换 setup token**（唯一轮换触发 = 文件丢失的分支③） | 用户没选；生命周期拍板为「生成一次用到开通为止」，定时轮换徒增「拿旧 token 401」摩擦 |
| 2 | **不做 CLI 查看命令**（如 `botflow setup-token`） | 用户没选；SSH `cat .setup_token` / 看启动日志两个出处已够，加 CLI = 第三出口（多一个泄 token 面） |
| 3 | 不做多枚 setup token / 一次性计数 / 防爆破专用限流 | 单管理员面板、token 128 bit 随机（`token_hex(16)`）不可爆破；既有 `RateLimitMiddleware` 粗限流照旧 |
| 4 | 不把 token 放进 status/任何 API 响应、不做「前端预填 token」 | P0 红线（R1） |
| 5 | 不加密 `.setup_token`、不引入 vault/KMS/新依赖 | 决策阶梯第 3 级：0600 + 主机信任域已够；加密只是把钥匙换个地方 |
| 6 | 不改 `verify_admin_key` 的**签名**、401/500 文案、`_extract_token` 语义 | admin_auth P0-1 结论继续有效（签名动 = 路由注册即崩）；文案动 = 破既有测试与探针 |
| 7 | 不动 login/logout/status 行为与响应形状 | 不变项 §1.4 |
| 8 | 不改 SPA `index.html` 的**表单字段结构**（三字段照旧）；**但改 3 处文案** | 结构不动（不变项 7）；`:98` placeholder「输入管理员密钥」→「输入开通 Token」（消灭诱导填 admin key），`:590`/`:594` 两处失实注释改真（P1-1，见 F6） |
| 9 | 不给 setup 成功后残留的旧日志 token 做「日志脱敏/打码」 | R3 论证：与 `.setup_token` 同信任域，脱敏则运维失去决策 1 的取用入口 |
| 10 | 不回改 `docs/tasks/admin_auth_features.md`（历史任务文档）与 commit 0612f8f 已交付内容 | 只新增本任务文档 + design.md 两处同步（F9） |
| 11 | 本步不写 `tests/`、不跑 pytest、不改 `src/` | 双 Agent 流程第 1 步只出本文档，交验证子 agent 审核 |

---

## 整改记录

| 报告编号 | 整改内容 | 落到本文档何处 |
|---|---|---|
| ZG-1（P0） | 错误分支推翻 fail-fast，定档「先写文件→无条件 fchmod→fsync/close 成功→才写 KV→打日志；任一步失败仅 log.error（含路径与 errno），不抛不中止不写 KV，下次启动自愈」；分支④双在态加文件内容校验（缺失或非 32 hex → 视为丢失走轮换打 `rotated:` 日志）；理由「便利功能不配让全量网关陪葬、KV 记录⟹文件已完整写入」写入 R7 | 决策 2 分支②③④（§1.2 表，三行动作与判据重写）、F1 错误分支、F2 错误分支、F8 错误分支引用、R7 整段重写、T2.2/T2.10/T2.11 期望、必覆盖映射行「文件丢失/内容损坏走轮换」、§6 UNCOVERED ② 条目 |
| ZG-2（P0） | 补测试隔离硬规格：新建 `tests/conftest.py` 加 autouse fixture 强制 `BOTFLOW_SETUP_TOKEN_FILE` → `tmp_path`；涉及文件总表收录该新增文件；R5 补点名 `tests/test_core_runtime.py::TestLifespan` 5 条（:346/:376/:399/:422/:439），归入「预期不变但受 conftest 隔离保护」清单 | §3 测试计划硬性规格段、§2 涉及文件总表（`tests/conftest.py` 行）、R5 末尾补点名 |
| P1-1（P1） | index.html「不改」修正为「结构不改、改 3 处文案」：`:98` placeholder→「输入开通 Token」、`:590`/`:594` 注释改真；SPA 纳入编码范围 | §1.4 不变项 7、F6（标题/新增 index.html 改 3 处条目/涉及文件）、涉及文件总表 index.html 行、§7 #8 |
| P2×6 | 不在本轮处理，编码/写测试阶段吸收 | **转编码阶段** |

**结论：有条件通过 → 整改完成，放行进编码。**（用例总数仍 42，正/反/边分类未变，汇总行无需重算。）
