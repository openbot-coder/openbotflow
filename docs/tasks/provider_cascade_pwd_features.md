# provider_cascade_pwd 功能点与测试用例：供应商停用显示层有效状态 + admin 改密码

> 任务类型：管理面板增强（前端单文件 Vue3 SPA + REST admin API，零迁移、零新依赖）
> 范围（2 条已拍板决策，全部硬约束，见 §0）：① 模型在 admin 模型页按「显示层有效状态」展示（供应商停用 → 其下模型显示「已禁用（随供应商）」，**不写库**）；② admin 在设置页修改自己的密码（**不吊销任何会话**）。
> 关联（行号为改动前工作区实测）：`admin_api.py:421-429`（`list_models`）、`:432-438`（`get_model`）、`:172/:216/:244`（setup/login/logout）、`:133-145`（`AuthReq`）、`auth.py:56-80`（`hash_password`/`verify_password`）、`:185-220`（`verify_admin_key`）、`storage/db.py:1197`（`list_models_raw`）、`:1154`（`get_provider_raw`）、`:1160`（`list_providers_raw`）、`static/admin/index.html:252-255`（模型页状态列/开关）、`:558-584`（`api()` 含 401 踢出）、`:400-439`（设置页）、`:872-906`（settings 状态与读写）、`:1167-1186`（setup return 导出）。
> **版本**：v1（交验证子 agent 审核）。

---

## 0. 硬约束（违反即打回）

1. **两条拍板决策不可更改**（见 §1.2）：供应商停用的级联只做**显示层**（后端附加字段 + 前端文案），**绝不修改 models/providers 表数据**；改密码**不删除、不重签任何会话**。
2. **不动路由层 SQL**：`db.py` group 加载 SQL 的 `AND p.is_enabled = 1`（约 :672/:743）一行不改——功能上停用即生效，本次只解决「面板上看不出来」。
3. **不动 models/providers 表数据**：不加列、不加表、不加索引；`provider_enabled` 只存在于 HTTP 响应里（`model_dump()` 结果上附加），不进 Pydantic `Model` 模型、不入库。
4. **改密码端点鉴权只用 `Depends(verify_admin_key)`**，不给该依赖加参数（P0-1：给依赖加参数会让全部 admin 路由注册即抛 `FastAPIError`，`auth.py:190-194`）。
5. **新端点 body 必须是显式 Pydantic 模型**（`ChangePasswordReq`）：裸标量参数会被 FastAPI 分类为 query 参数，JSON body 被静默忽略（见 `admin_api.py:73-77` 既有注释）。
6. **422 必须拦在 pbkdf2 之前**：`new_password` 约束与 `AuthReq.password` 完全一致（`min_length=8, max_length=128`，P1-6），超长密码进不到哈希计算。
7. **不写完整测试**（验证子 agent 负责）；不写 `# UNCOVERED` / `# pragma: cover`；中文注释，风格与周边一致；死代码直接删。
8. **不 commit、不部署**。
9. **零新依赖**：只用 stdlib + 既有 fastapi/pydantic；前端仍是单文件 Vue3 CDN，不引入构建步骤。

---

## 1. 背景与定案

### 1.1 背景

- **需求1**：供应商停用后，路由层已把它的模型排除在分组加载之外（`db.py` 的 `AND p.is_enabled = 1`），功能上确实已生效；但 `GET /admin/models` 只回模型自身的 `is_enabled`，前端模型页把停用供应商下的模型仍显示为「启用」——管理员看不出这些模型实际不可用。
- **需求2**：admin 账号只存在于 config KV `admin_account`（`{"username","pwd_hash"}`），目前唯一改密码的途径是 `POST /auth/setup`（需要 setup token，且会**全量吊销会话**）。面板里没有自助改密码入口。

### 1.2 定案（2 条拍板决策，硬约束）

| # | 决策 | 落点 |
|---|------|------|
| D1 | **显示层有效状态，不写库** | 后端在 `GET /admin/models`、`GET /admin/models/{id}` 的每个 model 上附带 `provider_enabled: bool`；前端模型页状态列按「有效状态」三态展示。models/providers 表零改动，模型自身开关仍编辑 `models.is_enabled`。 |
| D2 | **改密码不吊销任何会话** | 新端点只更新 `admin_account.pwd_hash`（新 salt 重新 hash），不触碰 `admin_sess:*`。新旧密码相同 → 400 拒绝（省一次无意义的 600k 迭代写库）。 |

### 1.3 摸底事实（直接采信，均已在当前工作区复核）

- 路由层已排除停用供应商：`db.py` group 加载 SQL 带 `AND p.is_enabled = 1`（约 :672/:743）——**不动**。
- `list_models`（`admin_api.py:421-429`）与 `get_model`（`:432-438`）目前直接 `m.model_dump()` 返回，无供应商状态信息。
- `db.list_providers_raw()`（`db.py:1160`）返回全部 `Provider`（含 `is_enabled`）——可用于一次建 `id → is_enabled` 映射，避免 N+1 查询。
- `Provider` 行不存在时（理论上模型有孤儿 `provider_id`），映射查不到 → 按 `False` 处理，保证不崩。
- `AuthReq.password` 约束为 `Field(min_length=8, max_length=128)`（`admin_api.py:138`）；pbkdf2-SHA256 600k 迭代、存档格式 `pbkdf2_sha256$iters$salt$hash`（`auth.py:56-64`）。
- 会话存 config `admin_sess:*`（`auth.py:139-142`）；`POST /auth/setup` 成功路径会 `DELETE FROM config WHERE key LIKE 'admin_sess:%'` 全量吊销（`admin_api.py:206`）——新端点**不做**这件事。
- 前端 `api()`（`index.html:558-584`）在**已登录态吃到 401 时会 `clearSession()` 踢回登录页**——改密码的「当前密码错」返回 401，若不处理会被误判为会话失效。本次给 `api()` 加一个请求级旁路标志（见 F4.2）。
- 分组管理页（`index.html:265-312`）展示的是 `groupModels(g.id)`（模型名/供应商/权重），**不展示模型启用状态** → 无需按同一口径改动（已读现有代码确认）。
- 设置页（`:400-439`）目前一张 card 内 4 个 h4 分组（请求与并发 / 日志与保留 / 定时任务）+ 底部「保存设置」按钮；`settings` 状态与 `loadSettings/saveSettings` 在 `:872-906`。

---

## 2. 功能点

### F1 后端：`GET /admin/models` 与 `GET /admin/models/{id}` 附带 `provider_enabled`

**位置**：`admin_api.py:421-438`（两个端点）+ 模块内新增一个小 helper。

**行为**：

1. 新增模块级私有 helper `_models_with_provider_enabled(db, models) -> list[dict]`：
   - `await db.list_providers_raw()` 一次取全部供应商，建 `{id: is_enabled}` 映射；
   - 对每个 model 返回 `{**m.model_dump(), "provider_enabled": bool(mapping.get(m.provider_id, False))}`；
   - 供应商不存在（孤儿 `provider_id`）→ `False`；映射值按 `bool()` 归一（DB 是 int 0/1，`Provider.is_enabled` 声明为 bool，仍显式 `bool()` 兜底）。
2. `list_models` 返回 `"models": [附带字段的 dict 列表]`（原 `model_dump()` 键全部保留，只**新增** `provider_enabled` 一个键）。
3. `get_model` 返回 `"model": {..., "provider_enabled": bool}`（同上）。
4. **绝不修改 models 表数据**：无任何 INSERT/UPDATE；`provider_enabled` 只是响应字段。

**不做**：不改 `enabled_only` 查询参数语义（它仍只按 `models.is_enabled` 过滤——供应商停用的模型依然会出现在 `enabled_only=true` 结果里，只是 `provider_enabled=false`）；不动 `storage/models.py` 的 `Model` Pydantic 模型；不动路由层 SQL。

**边界**：

| 场景 | `provider_enabled` |
|---|---|
| 供应商存在且启用 | `true` |
| 供应商存在但停用 | `false` |
| 供应商行不存在（孤儿引用） | `false`，响应正常不崩 |
| `provider_id` 查询异常（list_providers_raw 抛错） | 不捕获，按既有行为向上抛 500（与其它端点一致，不加防御分支） |

### F2 前端：模型页状态列改「有效状态」

**位置**：`index.html:252`（状态列单元格）+ script 新增 helper（放在 `modalityText` 附近）。

**行为**：

| `m.is_enabled` | `m.provider_enabled` | 显示 | 样式 |
|---|---|---|---|
| true | true（或字段缺失） | 「启用」 | `badge badge-green` |
| true | false | 「已禁用（随供应商）」 | `badge badge-gray`（灰色标注） |
| false | 任意 | 「已禁用」 | `badge badge-red` |

- 实现为 `modelStatus(m)` helper 返回 `{cls, text}`，模板状态列绑定它。
- **字段缺失兜底**：`m.provider_enabled === false` 才判随供应商——`undefined`（旧缓存/后端未返回）按正常处理显示「启用」，不误报。
- **模型自身的启用开关行为保持不变**：`:255` 按钮仍按 `m.is_enabled` 显示禁用/启用、仍 `PATCH {is_enabled: !m.is_enabled}`（`:1020-1023`）。管理员在供应商停用期间仍可切换模型自身标志，供应商恢复后自动回到原状态。
- 分组管理页：**不改**（读现有代码确认其不展示模型启用状态，见 §1.3）。

### F3 后端：`POST /admin/auth/change-password`

**位置**：`admin_api.py` Auth 段（`LogoutReq` 之后、auth endpoints 之前加 body 模型；端点加在 `auth_logout` 之后）。

**请求体** `ChangePasswordReq`（显式 Pydantic 模型）：

| 字段 | 类型 | 约束 |
|---|---|---|
| `current_password` | `str` | `Field(..., min_length=1, max_length=128)` |
| `new_password` | `str` | `Field(..., min_length=8, max_length=128)`（与 `AuthReq.password` 同约束） |

**鉴权**：`Depends(verify_admin_key)`（Bearer admin key 或会话 token 双通道，不加任何参数）。

**语义（分支顺序写死）**：

1. **404**：`admin_account` 不存在（`get_config` 返回空）**或** JSON 解析失败 / 解析结果非 dict → `detail = "Admin account not configured."`。调用者已过鉴权，可区分，不算信息泄露；损坏行不当场修数据（与 `auth_login`/`auth_setup` 一致）。
2. **401**：`verify_password(current_password, account["pwd_hash"])` 为假（含 `pwd_hash` 键缺失/存档损坏 → `verify_password` 返回 False）→ `detail = "Invalid current password."`。**必须跑完 pbkdf2 再判**，不靠耗时区分（与 `auth_login:231` 注释同理）。
3. **400**：新旧密码相同（`secrets.compare_digest` 前转 utf-8 bytes，非 ASCII 不吃 TypeError）→ `detail = "New password must differ from current password."`。放在 verify 之后：省掉 `hash_password` 的 600k 迭代 + 一次写库；同时保持 401 语义优先（当前密码错时先报 401，不因「新旧相同」提前泄底）。
4. **200 成功**：保持 `username` 不变（从既有 account 读回原值），只更新 `pwd_hash`（`hash_password(new_password)` 新 salt 重新 hash）→ `db.set_config("admin_account", json.dumps({"username":..., "pwd_hash":...}))`，返回 `{"success": true}`。
5. **不删除、不重签任何会话**：`admin_sess:*` 一行不动。当前会话与其他已登录会话继续有效。

**422 分支**（pydantic 层，端点执行前拦截，P1-6）：`new_password` 为非 str / 长度 <8 / >128；`current_password` 为空串或 >128；缺字段。超长密码进不到 pbkdf2。

**不做**：不改 `admin_account` 的 username；不改 `auth.py`；不清理 setup token；不写日志含明文密码。

### F4 前端：设置页「账号」分组

#### F4.1 页面结构

**位置**：`index.html` 设置页（`:400-439`），在现有「运行参数」card **之后追加一张新 card**（账号）。

- 选独立 card 而非在原 card 里加 h4 分组：原 card 底部的「保存设置」按钮语义是「保存上方全部运行参数」，把密码输入插到它上面会让用户误以为该按钮会提交密码——独立 card + 独立「修改密码」按钮无歧义。
- card 内 3 个 password 输入（`v-model` 绑 `pwdForm.current / next / confirm`）：
  - 当前密码
  - 新密码（placeholder 提示「至少 8 位」）
  - 确认新密码
- 「修改密码」按钮：`loading.changePwd` 时显示「修改中...」并 disabled。
- 头部提示文案说明「修改后已登录会话不受影响」（与 D2 一致）。

#### F4.2 前端逻辑

**新增状态**：`const pwdForm = reactive({current:'', next:'', confirm:''})`、`loading.changePwd`。

**`changePassword()` 本地校验（不发请求，直接 toast error）**：

1. 当前密码为空 → 「请输入当前密码」。
2. 新密码长度 < 8 → 「新密码至少 8 位」。
3. `next !== confirm` → 「两次输入的新密码不一致」。
4. `next === current` → 「新密码不能与当前密码相同」（与后端 400 同口径，先于请求拦截）。

**请求**：`api('/auth/change-password', {method:'POST', body: JSON.stringify({current_password, new_password}), kickOn401: false})`。

- **`api()` 加请求级旁路标志**：`opts.kickOn401 === false` 时跳过 401 的 `clearSession()` 分支（`:577-580`），错误照常 throw。理由：改密码的 401 是「当前密码错」，不是会话失效；不加这个旁路，用户输错一次当前密码就会被踢回登录页。**默认行为（不传该标志）完全不变**——其它所有调用点不受影响。
- 成功：toast 「密码已修改」，清空三个输入框。
- 失败：`toast(e.message, 'error')`，复用 `api()` 既有可读化（401/404/400 是字符串 detail 直出；422 数组拼「字段: 原因」，`:571`）。

**导出**：`setup()` return 增加 `pwdForm, changePassword`（`loading` 已导出）。

---

## 3. 用例清单（正例 / 反例 / 边界值）

### 3.1 需求1：`provider_enabled` 组合（后端）

| # | 用例 | 前置 | 请求 | 期望 |
|---|---|---|---|---|
| P1-1 | 供应商启用+模型启用 | provider.is_enabled=1, model.is_enabled=1 | `GET /admin/models` | 该 model `provider_enabled=true`，`is_enabled=true`，其余键与改前一致 |
| P1-2 | 供应商停用+模型启用 | provider.is_enabled=0, model.is_enabled=1 | `GET /admin/models` | `provider_enabled=false`，`is_enabled=true`（**两字段独立**） |
| P1-3 | 供应商停用+模型自身停用 | provider=0, model=0 | `GET /admin/models` | `provider_enabled=false`，`is_enabled=false` |
| P1-4 | 供应商启用+模型自身停用 | provider=1, model=0 | `GET /admin/models` | `provider_enabled=true`，`is_enabled=false` |
| P1-5 | 孤儿 provider_id | model.provider_id 指向不存在的供应商 | `GET /admin/models` | `provider_enabled=false`，200 不崩 |
| P1-6 | 单模型详情 | 任意组合 | `GET /admin/models/{id}` | `model.provider_enabled` 同口径 |
| P1-7 | 单模型 404 | id 不存在 | `GET /admin/models/{id}` | 404，既有文案 `{"success":false,"error":...}` |
| P1-8 | **零写库** | 供应商停用 | `GET /admin/models` 前后各 dump 一次 models 表 | 逐字节一致（GET 不产生任何 UPDATE） |
| P1-9 | 路由层不受影响 | 供应商停用 | 分组加载（既有 group SQL） | 该供应商模型仍被 `AND p.is_enabled = 1` 排除——**本次未改动**，回归确认 |
| P1-10 | enabled_only 语义不变 | provider=0, model=1 | `GET /admin/models?enabled_only=true` | 该模型仍在结果里（enabled_only 只看 models 表），`provider_enabled=false` |
| P1-11 | 鉴权 | 无 Bearer/无 token | `GET /admin/models` | 401 `Invalid admin key.`（依赖未变） |

### 3.2 需求1：前端 W 系列（人工核对）

| # | 用例 | 期望 |
|---|---|---|
| W1-1 | 供应商启用、模型启用 | 状态列绿色「启用」 |
| W1-2 | 停用供应商后刷新模型页（该模型自身仍启用） | 状态列灰色「已禁用（随供应商）」 |
| W1-3 | 模型自身停用 | 红色「已禁用」（无论供应商状态） |
| W1-4 | W1-2 状态下点模型行「禁用」再点「启用」 | 请求仍发 `models/{id}` PATCH `is_enabled`，文案回到「已禁用（随供应商）」；**供应商状态未被改动** |
| W1-5 | W1-2 状态下到供应商页重新启用该供应商 | 模型页回到绿色「启用」（模型自身 is_enabled 一直是 true，自动恢复） |
| W1-6 | 模型行开关按钮文字 | 始终按 `m.is_enabled`（供应商停用不影响按钮文案——按钮管的是模型自身标志） |
| W1-7 | 分组管理页 | 无模型启用状态列，页面与改前一致（无需按有效状态口径改动） |
| W1-8 | 后端未返回 `provider_enabled`（旧缓存） | 显示「启用」，不误报「随供应商」 |

### 3.3 需求2：改密码端点（后端）

| # | 用例 | 前置 | 请求体 | 期望 |
|---|---|---|---|---|
| P2-1 | 正常改密 | 账号已开通 | `{current_password: 正确, new_password: 新8位+}` | 200 `{"success":true}`；`admin_account.pwd_hash` 变化（新 salt）、`username` 不变；**`admin_sess:*` 行数不变**；旧密码 login 401、新密码 login 200 |
| P2-2 | 当前密码错 | 账号已开通 | current 错 | 401 `Invalid current password.`；`admin_account` 不变；会话不吊销 |
| P2-3 | 账号不存在 | 删掉 `admin_account` 行 | 任意（鉴权另走 admin key/旧会话） | 404 `Admin account not configured.` |
| P2-4 | 账号 JSON 损坏 | `admin_account` = `"{not json"` | 任意 | 404 `Admin account not configured.`；**损坏行不被修复/覆盖** |
| P2-5 | account 非 dict | `admin_account` = `"[1,2]"` | 任意 | 404 同上 |
| P2-6 | 新旧密码相同 | current 正确 | `new == current` | 400 `New password must differ from current password.`；**未发生写库**（pwd_hash 逐字节不变） |
| P2-7 | 新密码过短 | — | `new_password` 7 位 | 422（pydantic `min_length=8`），端点不执行、不跑 pbkdf2 |
| P2-8 | 新密码超长 | — | `new_password` 129 位 | 422（`max_length=128`），**不进哈希计算**（P1-6） |
| P2-9 | current 空串 | — | `current_password: ""` | 422（`min_length=1`） |
| P2-10 | 缺字段 | — | `{}` 或缺 `new_password` | 422 |
| P2-11 | 非 Pydantic body 坑 | — | — | 端点签名必须是 `req: ChangePasswordReq`（非 query 参数），发 JSON body 能被读到（验证：body 有值时不是「422 缺 body.current_password」这种 query 缺参形态以外的错法） |
| P2-12 | 未鉴权 | 无 Bearer、无 token | 合法 body | 401 `Invalid admin key.` |
| P2-13 | admin key 通道 | `Authorization: Bearer <BOTFLOW_ADMIN_KEY>` | 合法 body | 200 |
| P2-14 | 会话 token 通道 | login 拿到的 token | 合法 body | 200，且**该 token 改密后仍有效**（D2） |
| P2-15 | 其他会话不吊销 | 另开一个登录会话 | 在会话 A 改密 | 会话 B 继续 200（不被 DELETE） |
| P2-16 | `pwd_hash` 字段缺失 | `admin_account` = `{"username":"a"}` | 合法 body | 401（`verify_password` 对 `""` 返回 False）不崩 |
| P2-17 | 非 ASCII 密码 | — | current/new 含中文 | 200（compare_digest 走 utf-8 bytes，不 TypeError 500） |
| P2-18 | 顺序：current 错 + new==current | — | 两者皆违 | **401 优先**（verify 在 same 检查之前） |

### 3.4 需求2：前端 W 系列（人工核对）

| # | 用例 | 期望 |
|---|---|---|
| W2-1 | 设置页结构 | 「运行参数」card 之后有「账号」card：3 个 password 输入 + 「修改密码」按钮；原 9 字段与「保存设置」按钮不受影响 |
| W2-2 | 新密码 <8 位 | 本地 toast「新密码至少 8 位」，**不发请求** |
| W2-3 | 两次不一致 | toast「两次输入的新密码不一致」，不发请求 |
| W2-4 | 新密码 == 当前密码 | toast「新密码不能与当前密码相同」，不发请求 |
| W2-5 | 当前密码为空 | toast「请输入当前密码」，不发请求 |
| W2-6 | 正常成功 | toast「密码已修改」，三个输入框清空；**页面不被踢回登录页** |
| W2-7 | 当前密码错（401） | toast 展示 `Invalid current password.`（可读文案），**不清 token、不回登录页**（`kickOn401:false` 生效），输入框保留可重试 |
| W2-8 | 账号不存在/损坏（404） | toast `Admin account not configured.`，不被踢出 |
| W2-9 | 新旧相同（400，绕过本地校验直发） | toast `New password must differ from current password.`，不被踢出 |
| W2-10 | 422 | toast 拼接的「字段: 原因」（复用 `:571` 可读化），不被踢出 |
| W2-11 | 改密后刷新页面 | 会话仍在（未吊销），面板正常进入；用新密码重新 login 成功 |
| W2-12 | 其它页面 401 行为回归 | 任意其它请求吃到 401 仍会 `clearSession()` 踢出（旁路只对 `kickOn401:false` 生效） |
| W2-13 | 会话期间改密 | 当前会话不中断，无需重新登录 |

---

## 4. 给验证子 agent 的建议用例清单（本任务不写测试）

- **后端**：`tests/test_admin_api.py` 风格追加两组：
  - `TestModelsProviderEnabled`：P1-1 ~ P1-11（P1-8 用 `db.list_models_raw()` 前后对比；P1-5 用裸 SQL 插孤儿 model 或手工改 provider_id）。
  - `TestChangePassword`：P2-1 ~ P2-18（P2-6/P2-16 直接 `db.set_config` 造状态；P2-15 断言 `SELECT COUNT(*) FROM config WHERE key LIKE 'admin_sess:%'` 改密前后不变）。
- **前端**：W 系列为人工核对项（单文件 SPA 无前端测试基建），至少覆盖 W1-2、W1-5、W2-6、W2-7、W2-12。
- 重点盯的风险点见交付汇报（§5）。

## 5. 交付说明

改动文件、行号与自测记录见提交给 team-lead 的汇报；本文件与代码同步维护，任何语义变更须先改本文档。
