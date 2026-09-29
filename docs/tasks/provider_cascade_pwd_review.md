# provider_cascade_pwd（供应商停用显示层有效状态 + admin 改密码）— 验证子 agent 审核报告

> 审核对象：编码子 agent 的工作区改动（2 文件 `git diff` + 1 新增文档，未提交）
> 对照文档：`docs/tasks/provider_cascade_pwd_features.md`（v1，契约 F1/F2/F3/F4 + §3.1 P1-1~P1-11、§3.3 P2-1~P2-18、§3.2/§3.4 前端 W 系列）
> 审核结论：**通过（PASS）** —— 实现与契约语义逐条一致，未发现需打回的偏差；验证侧改了实现中 1 处注释用例编号错误（见 §2），无任何语义改动。
> 测试证据：mq3 远端全量 `1315 passed / 0 failed / 23 deselected`，`TOTAL 4533 语句 / 0 missed / 100%`，其中 `admin_api.py 435/0/100%`（见 §5）。

---

## 1. 审核范围与方法

- 逐条核对契约 §0 六条硬约束、§2 F1~F4 全部分支语义、§3.1/§3.3 全部 29 条后端用例的期望行为；
  前端 F2/F4 按契约做静态核对（单文件 SPA 无测试基建，W 系列留给人工验收）。
- 对 team-lead 点名的 3 个疑虑逐一实测核查：helper 是否重复查询拖慢列表、`json.dumps` 是否丢字段、
  400 分支放 verify 之后是否符合 §F3.3（结论见 §3）。
- 本机 Windows 仅做静态检查（跑不了 async/TestClient，已复现挂死）；全量证据取 mq3（§5）。

### 硬约束（§0）逐条核对

| # | 约束 | 结论 |
|---|---|---|
| 1 | D1 只做显示层不写库 / D2 改密码不吊销会话 | ✅ `_models_with_provider_enabled` 只读；`auth_change_password` 只 `set_config("admin_account")`，无任何 `admin_sess` DELETE（P1-8/P2-1/P2-15 固化） |
| 2 | 不动路由层 SQL（`db.py` `AND p.is_enabled = 1`） | ✅ `git diff` 中 `db.py` 零改动；P1-9 回归确认 |
| 3 | 不动 models/providers 表、`provider_enabled` 只进 HTTP 响应 | ✅ 不进 Pydantic `Model`、不入库（P1-8 逐行比对） |
| 4 | 鉴权只用 `Depends(verify_admin_key)` 且不给依赖加参数 | ✅ `admin_api.py:274` `_=Depends(verify_admin_key)`，`auth.py` 零改动 |
| 5 | body 必须是显式 Pydantic 模型 | ✅ `ChangePasswordReq`（`:155`），P2-11 用 OpenAPI `requestBody` + 200 双重固化 |
| 6 | 422 拦在 pbkdf2 之前 | ✅ `min_length=8/max_length=128`，P2-7/P2-8 spy 断言 `hash_password` 零调用 |
| 7 | 编码侧不写测试、不用 `# UNCOVERED`/pragma、中文注释、死代码删净 | ✅ diff 只有 src + 契约文档；全文件无 pragma |
| 8 | 不 commit、不部署 | ✅ `git status` 仍为未提交改动；mq3 只在 `/tmp/bt_casc` 跑测，未碰 `/srv/botflow` |
| 9 | 零新依赖 | ✅ 只用 stdlib（`secrets`/`json`）+ 既有 fastapi/pydantic；前端仍单文件 CDN |

## 2. 审核发现（实现 vs 契约，逐条）

**语义偏差：0 条。改动：1 处（注释编号，非语义）。**

| # | 发现 | 处理 |
|---|---|---|
| 1 | `auth_change_password` 内 compare_digest 注释写「非 ASCII 密码不吃 TypeError（P1-10）」，但 P1-10 在本契约 §3.1 是「enabled_only 语义不变」，对应用例是 §3.3 **P2-17** | **已改实现注释**：`admin_api.py` 该行改为 `（P2-17）`。纯注释，无语义影响 |
| 2 | 契约文档自身编号串台：§0-6 与 §F3「422…（P1-6）」指的其实是 §3.3 的 P2-7/P2-8（§3.1 的 P1-6 是「单模型详情」） | 不改契约、不改实现（实现注释与契约原文一致），此处记录供主 agent 定夺 |
| 3 | 其余逐条核对全部一致：分支顺序 404→401→400→200、三条 detail 文案、`provider_enabled` 三态真值表与字段缺失兜底、`modelStatus` 分支顺序与 badge 样式、`pwdForm` 4 条本地校验顺序与文案、`kickOn401` 默认行为不变 | 无需处理 |

### team-lead 点名的 3 个疑虑核查结论

1. **`_models_with_provider_enabled` 会否重复查询拖慢列表**：不会。每次请求恰好 1 次 `list_providers_raw()`
   （全量供应商一次取回、内存建 `{id: is_enabled}` 映射），模型侧仍是单次 `list_models_raw`——
   不是按模型逐个查（非 N+1）。`get_model` 单模型也只多 1 次轻量 SELECT，契约 §F1 本就要求两端点共用该 helper。
2. **`json.dumps` 是否丢字段**：不会。改密码只重写 `{"username","pwd_hash"}` 两键——契约 §1.1 定义
   `admin_account` 就只有这两键；`username` 用 `str(account.get("username",""))` 读回原值，
   与 `auth_login` 的 `str(account["username"])` 同口径。模型侧不落 JSON，`{**m.model_dump(), "provider_enabled": ...}`
   只增键不删键（P1-1 断言键集 = `set(Model.model_fields) | {"provider_enabled"}`）。
3. **400 分支放 verify 之后是否符合 §F3.3**：符合，且是契约明文要求——先跑完 pbkdf2 判 401（不靠耗时区分），
   再判新旧相同省掉 600k 迭代 + 一次写库；P2-18 用「current 错 + new==current」钉死 **401 优先**，
   P2-6 钉死 400 路径零写库（`admin_account` 逐字节不变）。

### 其他被点名的实现审查项

- 404 分支：`raw` 空 / `json.loads` 抛 `ValueError` / 解析结果非 dict 三条路径全覆盖（P2-3/4/5），损坏行不被修复覆盖。
- `verify_password(..., str(account.get("pwd_hash","")))`：键缺失 → `""` → 段数不足返回 False → 401 不崩（P2-16）。
- 前端 `badge-gray` 样式存在（`index.html:61`）；`loading = reactive({})`（`:656`）可动态挂 `changePwd`；
  `api()` 旁路条件 `opts.kickOn401 !== false`（`:598`）不传标志时与旧行为逐字节一致；
  `toggleProvider`/`toggleModel` 均 `await loadAll()` → W1-5 供应商重新启用后模型页会重拉 `provider_enabled`。

## 3. 测试落点

全部落在 `tests/test_admin_api.py`（沿用既有 `client`/`AUTH` fixture 风格；新增 `_run`/`_detail_locs` 助手与
`fast_pbkdf2` fixture 把 600k 迭代降为 1000，与 `test_admin_auth.py` 同口径）。

| 组 | 类（行号） | 用例（行号） | 条数 | 覆盖 |
|---|---|---|---|---|
| 需求1 | `TestModelsProviderEnabled`（`:574`） | `test_p1_1`~`test_p1_11`（`:601/:611/:619/:627/:635/:649/:661/:669/:685/:701/:713`） | 11 | §3.1 P1-1~P1-11 全量 1:1 |
| 需求2 | `TestChangePassword`（`:726`） | `test_p2_1`~`test_p2_18`（`:767/:786/:798/:804/:812/:820/:829/:842/:853/:860/:870/:879/:886/:891/:899/:910/:917/:927`） | 18 | §3.3 P2-1~P2-18 全量 1:1 |

- 文件内测试总数 48 → **77**（+29），收集通过（本机 `--collect-only` 77 条）。
- 关键构造手法：P1-5 孤儿引用用连接级 `PRAGMA foreign_keys=OFF` 删供应商行后复原（Schema 有 `REFERENCES ... ON DELETE CASCADE`，直接删会级联删模型）；
  P1-8 用 `SELECT * FROM models` 前后逐行（含 `updated_at`）比对；P1-9 用 `list_groups_with_models()` 回归 JOIN 过滤；
  P2-6/P2-4/P2-5/P2-16 直接 `db.set_config` 造状态；P2-7/P2-8 monkeypatch spy 断言 422 时 `hash_password`/`verify_password` 零调用；
  P2-15 断言 `SELECT key FROM config WHERE key LIKE 'admin_sess:%'` 行数改密前后不变。
- **前端 W 系列不写自动化**（契约 §4：单文件 SPA 无测试基建），静态核对结论见 §6。

## 4. 静态检查（本机 Windows，async/TestClient 跑不了已复现挂死）

- `py_compile`：`src/botflow/admin_api.py`、`tests/test_admin_api.py` 通过。
- `pytest tests/test_admin_api.py --collect-only -q`：**77 tests collected**，无导入/收集错误。
- `index.html` 抽内联 `<script>`（30129 字符）→ `node --check`：**JS_SYNTAX_OK**。
- `git diff` 复核：改动仅 `admin_api.py`（+76/-5）与 `index.html`（+58/-4）+ 本契约文档；`db.py`/`auth.py`/`storage/*` 零改动。

## 5. mq3 跑测原始输出摘要（放行证据）

隔离流程：本机 `tar`（**含 README.md**，否则 `pip install -e` 失败）→ `scp qbot@100.88.88.88:/tmp/` →
解包 `/tmp/bt_casc` → `uv venv --python 3.13 /tmp/btc_venv` → `uv pip install -e . pytest pytest-asyncio pytest-cov` →
`pytest tests/ --cov=botflow --cov-report=term-missing -m "not integration"`。
**全程在 `/tmp`，未在部署目录 `/srv/botflow` 跑 pytest。**

```
platform linux -- Python 3.13.15, pytest-9.1.1
collected 1338 items / 23 deselected / 1315 selected
...
src/botflow/admin_api.py                        435      0   100%
...
TOTAL                                          4533      0   100%
========= 1315 passed, 23 deselected, 10 warnings in 127.82s (0:02:07) =========
```

- **1315 passed / 0 failed / 23 deselected**（23 条 integration 口径与基线一致）。
- **TOTAL 4533 语句 / 0 missed / 100%**；`admin_api.py` **435/0/100%**；全仓 36 模块全 100%。
- 基线（admin_panel_review）1286 passed / 4508 语句 → 本次 **+29 用例 / +25 语句，仍是 0 missed**。
- 未新增任何 `# UNCOVERED` / `# pragma: cover`；新分支全部由 §3 用例实证覆盖。
- 警告仅既有的 aiosqlite date adapter DeprecationWarning 10 条（与基线同源，非本轮引入）。

## 6. 前端人工核对项（W 系列）— 静态核对结论（实机点击验收留给主 agent）

| 用例 | 静态结论 |
|---|---|
| W1-1/W1-2/W1-3/W1-8 | `modelStatus`（`index.html:1033-1037`）三态分支与真值表一致；`provider_enabled === false` 严格判假 → 字段缺失显示「启用」不误报 |
| W1-4 | `toggleModel`（`:1071`）仍发 `PATCH models/{id} {is_enabled}`，按钮文案按 `m.is_enabled`（`:255`）；不碰供应商状态 |
| W1-5 | `toggleProvider`（`:991`）改完 `await loadAll()` 重拉列表 → 模型页自动回绿 |
| W1-6 | 按钮文案 `{{m.is_enabled?'禁用':'启用'}}`（`:255`）未改 |
| W1-7 | 分组页零改动（diff 未触及该区块） |
| W2-1 | 「账号」独立 card 在「运行参数」card 之后（`:440-457`），原 9 字段与「保存设置」按钮原样 |
| W2-2~W2-5 | `changePassword`（`:931-935`）4 条本地校验顺序/文案与契约 F4.2 一字不差，命中即 return 不发请求 |
| W2-6 | 成功 toast「密码已修改」+ 三输入框清空（`:945-946`）；改密码本身不触发踢出 |
| W2-7 | `kickOn401:false`（`:943`）→ 401 只 throw 不 `clearSession`，输入保留可重试 |
| W2-8/W2-9/W2-10 | 404/400 是字符串 detail 直出、422 走 `:590` 拼接，复用既有可读化，均不触发踢出 |
| W2-11/W2-13 | 后端 D2 不吊销（P2-1/P2-14/P2-15 固化）；会话不中断 |
| W2-12 | 旁路条件 `opts.kickOn401 !== false`（`:598`）：其它请求不传标志 → 行为与改前逐字节一致 |

## 7. 遗留风险

| # | 风险 | 判定 |
|---|---|---|
| 1 | W 系列（W1-1~W1-8、W2-1~W2-13）无自动化，仅静态核对 | **属实**——单文件 SPA 无测试基建，契约 §4 明示人工验收；建议主 agent 实机至少点 W1-2、W1-5、W2-6、W2-7、W2-12 |
| 2 | 改密码端点无独立限速 | 属实但低危：入口已过 `verify_admin_key`，单次尝试成本被 `max_length=128` 钉住；契约未要求，不扩权实现 |
| 3 | 账号行缺失/损坏返回 404 而非 401 | 契约拍板（调用者已过鉴权、可区分），且与 login/setup「损坏行不当场修数据」一致 |
| 4 | `provider_enabled` 只在两个 GET 端点附带，create/PATCH 响应不含 | 契约 §F1 范围如此；前端列表只消费 `GET /admin/models`，无实际缺口 |
| 5 | 契约文档 §0-6/§F3 的「P1-6」编号串台（实为 P2-7/P2-8） | 文档瑕疵，不影响语义与断言；是否回改契约由主 agent 定 |

---

**最终结论：实现通过审核（0 语义偏差，改 1 处注释编号）；新增 29 条测试 1:1 覆盖 P1-1~P1-11 与 P2-1~P2-18；mq3 全量 1315 passed / 0 failed、TOTAL 4533 语句 100%（missed 0）、admin_api.py 435/0/100%，达成 100% 放行门槛。可交主 agent 集成验收。**
