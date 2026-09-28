# admin_panel 功能点与测试用例：管理面板 6 项需求

> 任务类型：管理面板增强（前端单文件 Vue3 SPA + REST admin API，零迁移、零新依赖）
> 范围（3 条已拍板决策 + 主 agent 摸底事实，全部硬约束，见 §0）：① 日志/创建时间按北京时间展示；② 新增「设置」页（9 个管理参数的读写 + 启动合并）；③ 供应商「获取模型」按钮（单供应商同步）；④ 供应商隐藏 type + extra_config + 启用/禁用；⑤ 模型 temperature 默认值 + 模态勾选（纯元数据）；⑥ 分组添加模型改为「供应商 → 模型」两级选择。
> 关联（行号为当前工作区实测）：`admin_api.py:43/:50/:76/:83`（4 个请求体模型）、`:249-308`（providers CRUD）、`:316-384`（models CRUD）、`storage/db.py:298/:304`（config KV）、`:373-403`（provider CRUD + `_PROVIDER_UPDATE_COLUMNS:387`）、`:445-480`（model CRUD + `_MODEL_UPDATE_COLUMNS:461`）、`:1147/:1161/:1172/:1192`（4 个 raw 方法）、`:726-776`（`get_group_models` 带 `model_extra_config`）、`core.py:215/:266`（`sync_models_from_provider` / `sync_all_models`）、`:284-477`（lifespan 三个后台任务）、`:1077/:1167`（`_drive_chat` / `_drive_stream`）、`:1435-1464`（`create_app`，`set_config` 在 `:1451`）、`config.py:10-68`（`BotflowSettings` 9 参数）、`pipeline/langgraph_engine.py:254/:282`（`try_call`）、`:343/:388`（`try_stream`）、`pipeline/_shared.py:98/:128`（`call_llm`）、`storage/daily_summary.py:140/:176/:190/:200`（retention 读取点）、`static/admin/index.html:208-232`（供应商页）、`:234-261`（模型页）、`:263-306`（分组页）、`:401-416`（弹窗模板）、`:520-528`（`pages`）、`:803/:820`（`saveProvider`/`saveModel`）、`:900`（`fmtTime`）、`:928`（`submitModal`）、`:938`（`switchPage`）。
> **版本**：v1（交验证子 agent 审核）。

---

## 0. 硬约束（违反即打回）

1. **三条拍板决策不可更改**（见 §1.2）：供应商 type 前端隐藏/后端保留；temperature 是**默认值语义**（请求未显式传才用模型值）；图/音/视频/文字模态标志**仅元数据**（不参与路由、不改请求）。
2. **零 DB 迁移**：只用既有列 `providers.extra_config`、`models.extra_config`（`db.py:47/:63` 建表已有）与既有 KV 表 `config`（`:120-124`）。不加列、不加表、不加索引。
3. **新设置 KV key 定死 `admin_settings`**（单 key 存 JSON）：不会被 `cleanup_config_by_prefix("admin_sess:%")`（`admin_api.py:218`）、`"dedup:%"`（`core.py:410`）、`"cooldown:%"` 任何一条清理误删。
4. **不碰 `protocol_adapter.py`**（已知挂起 bug AD-1，不在本任务范围）；**不碰 auth 流程**（`auth.py` 零改动，新增端点一律 `Depends(verify_admin_key)`，401 文案沿用既有）。
5. **不改 `tests/`**（验证子 agent 写测试）；新增 Python 代码必须可被单测覆盖，不写防御性死代码、不用 `# pragma no cover`；确实不可覆盖的行按仓库惯例标 `# UNCOVERED: [原因]`。
6. **零新依赖**：只用 stdlib + 既有 fastapi/pydantic；前端仍是单文件 Vue3 CDN（`index.html:7-8`），不引入构建步骤、不引新 CDN。
7. **`submitModal`（`index.html:928`）的表单形状识别不许破**：`raw_key/key_hash_prefix`→apikey、`fallback_group_id`→group、`base_url`→provider、兜底→model。新增字段不得让任一实体误判（尤其 model 表单必须继续含 `context_window`、不得出现 `base_url`/`raw_key`/`fallback_group_id`）。
8. **后端 `type` 字段语义零变化**：`CreateProviderReq.type` 默认 `"openai"` 保留（`admin_api.py:47`），`UpdateProviderReq.type` 保留（`:80`），`router.py:208` SDK 兜底、`/v1/models` 的 `owned_by`、CLI 的 provider type 消费方全部不动。
9. **本机 Windows 跑不了 async 测试**：编码子 agent 不跑 `tests/`（验证阶段在 Linux 远端跑）；临时文件进 `.workbuddy/tmp/`，仓根不放散文件；文档与代码注释中文。
10. **只做单供应商同步**：不做「全部同步」按钮（未请求）；`sync_all_models()`（`core.py:266`）与既有定时任务（`:419-437`）一行不改。

---

## 1. 背景与定案

### 1.1 背景

管理面板（`static/admin/index.html`，992 行单文件 SPA）目前：时间显示按浏览器时区解析 UTC 串（慢 8 小时）；无设置页（9 个管理参数只能改 `.env`）；供应商列表必须靠 CLI `model sync` 才能拉模型；供应商表单暴露 `type`（易被误改）且无法配代理/超时；模型只能改 3 个字段（temperature/模态无处配置）；分组添加模型是平铺单下拉（模型多了找不到属于哪个供应商）。

### 1.2 定案（3 条拍板决策，硬约束）

| # | 决策 | 落点 |
|---|------|------|
| D1 | **供应商 type：前端隐藏、后端保留** | 前端删「类型」列与 select；`saveProvider` 不再发 `type`（create 走后端默认 `"openai"`，PATCH 不发 → 保留原值）。后端字段、DB 列 `provider_type`、SDK 兜底、CLI 一律不动。 |
| D2 | **temperature = 默认值语义** | 客户端请求**未显式传** temperature → 用 `models.extra_config.temperature`；请求传了 → 以请求为准；都没有 → 不传（与现状逐字节一致）。 |
| D3 | **图/音/视频/文字模态 = 仅元数据** | `models.extra_config.modalities`（子集），页面勾选 + 列表展示；**不参与路由、不改请求、后端不消费**。 |

### 1.3 摸底事实（直接采信，均已在当前工作区复核）

- `GroupModelWithDetails.extra_config` 已在 SQL 里带出（`db.py:734` `m.extra_config AS model_extra_config` → `:775`），temperature 解析**不需要改任何 SQL**。
- `_MODEL_UPDATE_COLUMNS`（`:461`）与 `_PROVIDER_UPDATE_COLUMNS`（`:387`）都已含 `extra_config`（动态列更新那条路径支持），但 admin 用的是 `update_model_raw` / `update_provider_raw` 这两个**平铺 SQL** 方法（`:1161/:1192`）——它们不含 extra_config，需要扩。
- `sync_models_from_provider(provider_id, db=None)`（`core.py:215`）：fetch `{base_url}/models` 并 upsert（只加新不改旧），返回 `{"added","skipped","errors"}`；admin 无端点，CLI `model sync` 是唯一入口。
- `invalidate_all_caches()`（`router.py:282`）清 `_endpoint_cache` + `_provider_cache`，**不清** `_provider_semaphores`（信号量身份不能打断）。
- `GroupModelWithDetails` / `Model.extra_config` 已回传给前端（`model_dump()`），前端可直接读。
- 供应商 `extra_config` 已被消费的键：`proxy`（`providers/base.py:15`、`_make_http_client`）、`timeout`（`base.py:20`、`openai_compat.py:115/:124`、`anthropic_provider.py:81`、`deepseek_provider.py:98`）、anthropic 私有 `default_model`。
- 模型 `extra_config` 已被消费的键：`proxy`（`db.py:774` → `router.py:255`）、`strip_params` / `reasoning_mode`（`_shared.py:70-90` `_apply_model_extra_config`）。**新增 temperature / modalities 两键不得挤掉这些既有键**（见 §5 合并规则）。
- ⚠️ `stream_timeout`（`config.py:38`）**当前在 `src/` 内没有任何读取点**（全仓 grep 只命中 `config.py` 与文档）——本任务按需求照常页面化，但生效时机表必须如实标注（§3）。

---

## 2. 功能点

### F1 日志/创建时间改为北京时间

**位置**：`index.html:900-904` `fmtTime(t)`（调用点 `:322` API Keys 创建时间、`:361` 调用日志时间，均不改）。

**行为**：
1. 空值兜底不变：`!t → '-'`。
2. 解析：`new Date(String(t).replace(' ','T') + 'Z')` —— DB 里是 UTC 的 `YYYY-MM-DD HH:MM:SS`（`db.py:49` `datetime('now')` 写 UTC），经 FastAPI 序列化后前端拿到 `2026-09-28 03:00:00`（空格）或 `2026-09-28T03:00:00`（T）两种形态，`replace` 对两者都产出合法 ISO+Z → **按 UTC 解析**，浏览器时区无关。
3. 展示：`toLocaleString('zh-CN', {timeZone:'Asia/Shanghai', month/day/hour/minute/second 均2位})` —— 选项与现实现一字不差，只**新增 `timeZone`**，显示格式保持「月-日 时:分:秒」。
4. 解析失败兜底：`isNaN(d.getTime()) → 返回原始字符串 t`（不显示 "Invalid Date"）。原 `try/catch` 删除（`replace`/`toLocaleString` 对非法 Date 不抛异常，catch 是死分支）。

**不做**：不改调用点、不改后端时间存储口径、不引入时区数据库依赖（沿用 stdlib 固定 +8，与 `admin_api.py:524` `CN_TZ` 同思路）。

### F2 设置页（新增侧边栏「设置」）

#### F2.1 后端 `GET /admin/settings`

- 返回 `{"success": true, "settings": {…9 键…}}`，值 = `botflow.config.get_config()` 当前生效值（即启动合并后的结果）。
- 键集**恰为 9 个**（顺序不敏感）：`stream_timeout`、`upstream_semaphore_size`、`call_log_detail_days`、`raw_session_retention_days`、`call_logs_retention_days`、`daily_summary_hour`、`summary_group`、`model_sync_interval`、`log_level`。
- 鉴权：`Depends(verify_admin_key)`；无 key → 401 `Invalid admin key.`（既有文案）。**不含任何密钥字段**（`admin_key`/`llm_key`/`api_keys` 绝不回传）。

#### F2.2 后端 `PATCH /admin/settings`

- 请求体 `UpdateSettingsReq`：9 个字段**全部 Optional**，`None`（或缺省）= 不改。
- 校验（pydantic 层，非法 → **422** + 字段明细）：

| 字段 | 约束 |
|---|---|
| `log_level` | `Literal["DEBUG","INFO","WARNING","ERROR","CRITICAL"]` |
| `daily_summary_hour` | `int, ge=0, le=23` |
| `stream_timeout` | `float, gt=0` |
| `upstream_semaphore_size` | `int, ge=0` |
| `call_log_detail_days` / `raw_session_retention_days` / `call_logs_retention_days` / `model_sync_interval` | `int, ge=0` |
| `summary_group` | `str`（空串 `""` 合法 = 默认组） |

- 处理（`exclude_unset=True, exclude_none=True` 取本次真实变更集）：
  1. **持久化**：读 `config["admin_settings"]`（无则 `{}`）→ `dict.update(changed)` → 写回 `db.set_config("admin_settings", json.dumps(...))`。**部分更新**：只覆盖本次传入的键，其余键原样保留。
  2. **立即生效**：`cfg = get_config()` 后逐键 `setattr(cfg, k, v)`。`config._config` 是模块级单例，与 `core.create_app` `:1451` `set_config(config)` 注入的是**同一个对象**，故 setattr 后所有 `get_config()` 读取点立即看到新值。
  3. 返回 `{"success": true, "settings": {…9 键最新值…}}`。
- 空 body `{}` → 200，不写 KV、不改任何值（幂等 no-op）。

#### F2.3 启动合并（`core.create_app`，`:1460` `await _db.initialize()` 之后）

```
读 config["admin_settings"] → JSON 解析（解析失败按 {}）→
对键 ∈ ADMIN_SETTINGS_FIELDS（白名单，定义在 config.py）的键 setattr 到传入的 config 对象
```

- 白名单而非 `hasattr`：防止 KV 被手工塞入 `admin_key` 之类被覆盖（安全）。
- 顺序发生在 `set_config(config)`（`:1451`）之后、`lifespan` 的 `setup_logging`（`:300`）与三个后台任务（`:362/:419`）之前 → 页面值在进程启动时即成为权威。

**优先级语义（写死，文档 + 代码注释一致）**：

```
env / .env（pydantic-settings 初值）
   → 启动合并：DB config.admin_settings 覆盖同名键（页面是权威）
      → 运行期 PATCH /admin/settings 覆盖内存单例 + 回写 KV（再启动仍是页面值）
```

即：**页面设过的键永远赢**；页面没设过的键回落到 env/.env 默认；页面上把值改回默认也**不会**回落 env（显式存的就是页面值，`admin_settings` 里有该键即以它为准）。

#### F2.4 前端

- `pages`（`:520`）末尾追加 `{id:'settings', label:'设置', icon: 齿轮 SVG}`（沿用既有内联 SVG 风格：`viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"`）。
- `switchPage`（`:938`）加 `if(p==='settings') await loadSettings();`。
- 状态：`const settings = reactive({})`；`loadSettings()` → `GET /admin/settings` → `Object.assign(settings, r.settings)`；失败 toast 错误。
- 页面：**内联表单（不用 modal）**，一张 card 内分三组：
  - 「请求与并发」：`stream_timeout`（number）、`upstream_semaphore_size`（number）
  - 「日志与保留」：`log_level`（select，5 个选项）、`call_log_detail_days`、`raw_session_retention_days`、`call_logs_retention_days`（number）
  - 「定时任务」：`daily_summary_hour`（number 0-23）、`summary_group`（text，占位「留空 = 默认组」）、`model_sync_interval`（number）
  - 每个数字输入 `type="number"`（`v-model.number`），字段旁按 §3 表标注生效时机（「立即生效 / 次日维护生效 / 重启后生效」）。
- 保存按钮「保存设置」：客户端先校验——9 个数字字段必须是有限数字（`Number.isFinite`），否则 toast `'xxx 必须是数字'` 并**不发请求**；通过则 `PATCH /admin/settings` 带**全部 9 键**（数字键发 number、`summary_group` 发 string、`log_level` 发 string）→ 成功 toast「设置已保存」并用响应回填 `settings`；失败 toast 错误明细。
- `api()`（`:487`）错误分支补一条：`detail` 是**数组**（pydantic 422 校验明细）时映射成 `"字段: msg; 字段: msg"`，否则 toast 会显示 `JSON.stringify(...)` 全串。（纯增量分支，字符串/对象既有分支不动。）

### F3 供应商「获取模型」按钮

#### F3.1 后端 `POST /admin/providers/{provider_id}/sync`

1. `Depends(verify_admin_key)`；provider 查不到 → **404** `{"success": false, "error": "Provider {id} not found"}`（与既有 404 形状一致，`admin_api.py:274`）。
2. 函数内惰性导入 `from botflow.core import sync_models_from_provider`（`admin_api` 被 `core.py:30` 顶层导入，模块级 import 会成环），调用 `sync_models_from_provider(provider_id, db=get_db())`——传 admin 端点拿到的 db 实例，测试可 `dependency_overrides[dbmod.get_db]`。
3. `invalidate_all_caches()`（同步结果新增了模型，端点缓存要失效）。
4. 返回 `{"success": bool, "added": int, "skipped": int, "errors": [...]}`，其中 **`success = not errors`**（上游不可达/HTTP 失败时 `core.py:236-238` 把异常塞进 `errors` 并返回 → `success=false`，但 HTTP 仍 200，让前端能读到 errors 文案）。

#### F3.2 前端

- 供应商行操作区（`:223-226`）在「编辑」前加「获取模型」按钮：`:disabled="loading['sync_'+p.id]"`，点击时显示 `同步中...`。
- `syncProvider(p)`：`POST /admin/providers/{id}/sync`（不带 body）→
  - `errors.length` → `toast('同步失败: ' + errors.join('; '), 'error')`
  - 否则 → `toast('新增 ' + added + ' · 跳过 ' + skipped)`
  - finally 关 loading，成功/失败都 `await loadAll()`（刷新模型列表）。
- 不做「全部同步」。

### F4 供应商：隐藏 type + 额外配置 + 启用/禁用

#### F4.1 后端

- `CreateProviderReq`（`:43`）加 `extra_config: dict = {}`；`UpdateProviderReq`（`:76`）加 `extra_config: Optional[dict] = None`（`None` = 不改）。
- `db.create_provider_raw`（`:1147`）加关键字参 `extra_config=None` → 透传给 `Provider(extra_config=extra_config or {})`（`create_provider` `:373` 本就 `json.dumps`）。
- `db.update_provider_raw`（`:1161`）加关键字参 `extra_config=None`：**`None` = 不写该列**（动态拼进 SQL 的列集合），非 `None`（含 `{}`）= `json.dumps` 整体覆盖。既有调用方（`tests/test_db_full.py:95` 不传该参）行为逐字节不变。
- `update_provider` 端点（`:288`）按既有「None→取原值」风格：`extra_config=req.extra_config if req.extra_config is not None else provider.extra_config`。
- `type` 不动：create 仍 `req.type`（缺省 `"openai"`），PATCH 仍 `req.type if req.type is not None else provider.provider_type`（前端不发 → 保留原值）。
- `is_enabled` PATCH 本就支持（`:81/:294`），启用/禁用**后端零改动**。

#### F4.2 前端

- 删「类型」列（表头 `:216`、单元格 `:220`）与表单 select（`:403`）；`openModal('新建供应商', {name:'',base_url:'',api_key:''})` 初值去掉 `type`。
- `saveProvider`（`:803`）：create 发 `{req:{name, base_url, api_key, extra_config}}`（**无 type**）；PATCH 发 `{name, base_url, api_key, extra_config}`（**无 type、无 is_enabled**）。
- 表单新增三项（模板 `:401-406`）：
  - `代理 proxy`（text，可空，占位 `http://user:pass@host:port`）
  - `超时 timeout（秒）`（number，可空）
  - `高级 extra_config（JSON）`（textarea，默认 `{}`
- 打开编辑时的回填：新增 helper `providerEditForm(p)` 返回 `{...p, proxy: p.extra_config?.proxy||'', timeout: p.extra_config?.timeout??'', extraJson: JSON.stringify(p.extra_config||{}, null, 2)}`；新建时初值 `proxy:'', timeout:'', extraJson:'{}'`。
- **提交合并规则（定死）**：
  1. `JSON.parse(extraJson)` 得底稿对象（空串/空白 → `{}`）；**解析失败 → toast `'extra_config 不是合法 JSON'` 并阻止提交**（不发请求）。
  2. `proxy` 输入**非空** → `base.proxy = 输入值`；**为空 → `delete base.proxy`**（输入框对这两个键是权威，textarea 负责其它键）。
  3. `timeout` 输入非空 → 必须 `Number.isFinite(Number(v))`，否则 toast `'超时必须是数字'` 阻止提交；通过 → `base.timeout = Number(v)`；为空 → `delete base.timeout`。
  4. 最终对象作为 `extra_config` 整体提交（后端整体覆盖）。
  5. 因此：textarea 里的 `proxy/timeout` 与输入框不同步时，**以输入框为准**（编辑回填时两者天然一致）。
- 行操作区加 `启用/禁用` 按钮：照抄 `toggleModel`（`:835`）——`PATCH /providers/{id} {is_enabled: !p.is_enabled}` → toast「已更新」→ `loadAll()`。

### F5 模型设置：temperature + 模态勾选

#### F5.1 存储（零迁移）

`models.extra_config`（既有列 `db.py:63`）新增两键，**与既有键 `proxy`/`strip_params`/`reasoning_mode` 并存**：

| 键 | 类型 | 语义 |
|---|---|---|
| `temperature` | `float` | 模型默认温度（D2 默认值语义）；缺失/为 `null` = 未声明 |
| `modalities` | `["text"\|"image"\|"audio"\|"video"]` 子集 | 仅元数据（D3）；缺失/空数组 = 未声明 |

#### F5.2 temperature 默认值注入（核心行为）

**解析规则（D2）**：`请求 temperature 非 None → 用请求的；否则 → ep.detail.extra_config["temperature"]（须为非 bool 的数值）；都没有 → None（不传，与现状一致）`。

⚠️ **`temperature: null` 的歧义**：`protocol_adapter.py:23/:54/:389` 用 `body.get("temperature")`，显式传 `null` 与未传都得到 `None` → **一律按「未传」处理，回落到模型默认值**（无法区分，也不尝试区分；文档与代码注释同此口径）。

**实现位置（链路证据，见 §6）**：`langgraph_engine.py` 新增 4 行 helper + 两个调用点：

```python
def _resolve_temperature(request_temperature: float | None, ep) -> float | None:
    """请求优先；未传(None) → 模型 extra_config.temperature 默认值；都没有 → None(不传)。"""
    if request_temperature is not None:
        return request_temperature
    t = (ep.detail.extra_config or {}).get("temperature")
    return float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) else None
```

- `try_call`（`:254`）：`call_llm(ep, truncated, group_id, ctx.cooldown, _resolve_temperature(temperature, ep), max_tokens, **extra)` —— **按 endpoint 解析**（同一组内不同模型各有各的默认值，所以解析必须在 while 循环体内，不能沿用 `:271` 循环外的 `temperature`）。
- `try_stream`（`:343`）：`ep.provider.chat_stream(..., temperature=_resolve_temperature(temperature, ep), ...)`（`:391`）。
- helper 放 `langgraph_engine.py` 模块级（两处共用，`from botflow.router import ModelEndpoint` 仅用于注解），**不抽更多抽象**。

**覆盖范围结论（证据见 §6）**：服务路径只有这两个节点——`core._drive_chat/_drive_stream` → `PipelineEngine.run` → `LangGraphEngine.run` → 图 `try_call` / `try_stream`；`PipelineEngine.route`（`engine.py:54`）与 `LangGraphEngine.route`（`langgraph_engine.py:817`）在 `src/` 内**无调用方**（SG-1_review 已核：`grep '\.route(' src/` 仅命中自身），`pipeline/engine.py:59/:79` 因此**不在服务路径上，零改动**；`langgraph_strategy.py:144` 的 `call_llm` 同理不在服务路径（group.type=="langgraph" 会被 `_build_strategy:127` 直接 `ConfigurationError` 拒绝）。

#### F5.3 后端字段

- `UpdateModelReq`（`:83`）加 `extra_config: Optional[dict] = None`（`None`=不改，**整体覆盖**语义）；`CreateModelReq`（`:50`）加 `extra_config: dict = {}`（对称，默认 `{}`）。
- `db.create_model_raw`（`:1172`）加 `extra_config=None` → `Model(extra_config=extra_config or {})`。
- `db.update_model_raw`（`:1192`）加 `extra_config=None`：`None` = 不写该列（既有调用 `tests/test_db_full.py:106/115` 不传 → 行为不变），非 `None`（含 `{}`）= `json.dumps` 整体覆盖。
- `update_model` 端点（`:362`）：`extra_config=req.extra_config if req.extra_config is not None else model.extra_config`。
- 后端**不消费** `modalities`；`temperature` 只在 §F5.2 的请求路径上被读。

#### F5.4 前端

- **模型编辑弹窗**（模板 `:408-416`）扩成完整表单（仅 `modal.mode==='edit'` 显示新增项，create 保持精简：供应商/名称/上下文窗口）：
  - 供应商（edit 时 `disabled`，既有）、名称、显示名 `display_name`、`api_format`、上下文窗口 `context_window`
  - 重试 `max_retries`、冷却秒 `cooldown_seconds`、冷却阈值 `cooldown_failure_threshold`（number）
  - 温度 `temperature`（number，可空 = 不设默认）
  - 模态 4 个 checkbox：text / image / audio / video（绑定 `f.modalities` 数组）
  - 启用状态 `is_enabled`（select 启用/禁用，仅 edit）
- 打开编辑的回填：新增 helper `modelEditForm(m)` 返回 `{...m, temperature: m.extra_config?.temperature ?? '', modalities: [...(m.extra_config?.modalities||[])]}`。
- `saveModel`（`:820`）：
  - create 不变：`{req:{provider_id, name, context_window}}`。
  - edit 发**全部字段**：`{name, display_name, api_format, context_window, max_retries, cooldown_seconds, cooldown_failure_threshold, is_enabled, extra_config}`；其中 `extra_config` 组装规则：**以 `f.extra_config`（编辑打开时的原对象）为底**（保住 `proxy`/`strip_params` 等既有键）→ `temperature` 有值（非空且 `Number.isFinite`，否则 toast 阻止提交）则 `base.temperature = Number(v)`、为空则 `delete base.temperature`；`modalities` 非空则 `base.modalities = [...选中项]`、为空则 `delete base.modalities`。
  - `context_window` 字段保证在 form 里（`submitModal` 形状识别，§0-7）。
- **模型列表**加一列「模态/温度」（插在「冷却」与「状态」之间，保持紧凑）：
  - `m.extra_config?.modalities?.join(',')` 与 `m.extra_config?.temperature != null ? 'temp=' + t : ''` 用 `' · '` 连接；两者皆空 → `-`。
- **不改**：模型创建仍是 `{provider_id,name,context_window}` 精简弹窗（未请求全量创建表单）。

### F6 分组添加模型：按 provider 两级选择（后端零改动）

- 状态（与 `newModelForGroup` 并列，`:885`）：`const newProviderForGroup = reactive({})`（按 gid 存）。
- 模板（`:296-302`）：两级 select
  1. 供应商：选项 = **全部 providers**（显示 `p.name`，首项「选择供应商...」值 `''`）；`@change` 时 `newModelForGroup[g.id] = ''`（**变更即清空已选模型**）。
  2. 模型：`:disabled="!newProviderForGroup[g.id]"`（未选供应商前禁用）；选项 = `models.filter(m => m.provider_id === newProviderForGroup[g.id])`，显示 `{{m.name}} · {{m.context_window||'-'}}`（模型名 + 上下文窗口）；未选供应商时首项「先选供应商...」。
- 选中供应商但其无模型 → 第二级只有占位项，「添加」按钮因 `!newModelForGroup[g.id]` 保持 disabled（既有逻辑）。
- `doAddModelToGroup`（`:915`）与 `addModelToGroup` 零改动；helper `groupPickModels(gid)` 一个。

---

## 3. 9 个设置参数：读取点与生效时机（逐参数 trace 结论）

| 参数 | 真实读取点（grep 全仓复核） | 生效时机 | 前端标注 |
|---|---|---|---|
| `stream_timeout` | **`src/` 内无读取点**（仅 `config.py:38` 定义 + `design.md:375` 文档） | **当前不生效**（存了也没人读） | 「当前运行时未读取」 |
| `upstream_semaphore_size` | `_shared.py:118`（每次上游调用读 `get_config()`） | **重启后完全生效**：读取是即时的，但 `router.py:38-52 _ensure_provider_semaphore` 按 provider 缓存信号量/`None`，`invalidate_all_caches` 明确不清它（`router.py:285`）→ 已建缓存的 provider 要等进程重启 | 重启后生效 |
| `call_log_detail_days` | `daily_summary.py:176`（`purge_old_detail` 每次运行读） | 下一次每日维护（≤24h） | 次日维护生效 |
| `raw_session_retention_days` | `daily_summary.py:190`（同上） | 下一次每日维护 | 次日维护生效 |
| `call_logs_retention_days` | `daily_summary.py:200`（同上） | 下一次每日维护 | 次日维护生效 |
| `daily_summary_hour` | `core.py:365`（`_daily_maintenance` **启动时读一次**算首次 sleep） | **重启生效** | 重启后生效 |
| `summary_group` | `daily_summary.py:140`（每次运行读） | 下一次每日维护 | 次日维护生效 |
| `model_sync_interval` | `core.py:420`（`_periodic_model_sync` **启动时读一次**，`interval<=0` 直接 return） | **重启生效** | 重启后生效 |
| `log_level` | `core.py:300`（lifespan 启动 `setup_logging` 一次）、`cli/main.py:83/:98` | **重启生效** | 重启后生效 |

**PATCH 后立即生效的机制**：F2.2 第 2 步 setattr 内存单例 → 上表「即时读取」的参数下一次读取即新值；「重启生效」的参数只是把新值先落到内存 + KV，进程重启时由启动合并再次装载（不会被 env 冲掉）。

---

## 4. extra_config 合并规则汇总（前后端一张表）

| 层 | 规则 |
|---|---|
| 后端 PATCH（provider/model 共用） | `extra_config` 缺省/`null` → **不改**（端点层回填原值）；显式给出（含 `{}`）→ **整体覆盖**（SQL 整列写入）。不实现键级合并（后端不猜语义）。 |
| 前端 provider 表单 | textarea 解析出的对象为底 → proxy 输入非空覆盖/为空删除 → timeout 同理 → 整体提交。JSON 非法、timeout 非数字 → toast + **阻止提交**。 |
| 前端 model 编辑 | 原 `m.extra_config` 为底（保 `proxy`/`strip_params`/`reasoning_mode`）→ 温度/模态两键按输入覆盖或删除 → 整体提交。温度非数字 → toast + 阻止提交。 |
| 消费端 | provider：`proxy`/`timeout`（`providers/base.py:15-20` 等）；model：`proxy`、`strip_params`/`reasoning_mode`（`_shared.py:70-90`）、**新增** `temperature`（`langgraph_engine._resolve_temperature`）、`modalities`（不消费，纯展示）。 |

---

## 5. temperature 解析链路证据（调用链从头到尾）

```
客户端 body
  └ protocol_adapter.py:23 openai_to_internal / :54 anthropic / :389 responses
      → internal["temperature"] = body.get("temperature")      # null/缺失 → None（不可区分，见 F5.2）
  └ core.py:1096 (_drive_chat) / :1190 (_drive_stream)
      → engine.run(..., temperature=internal.get("temperature"))
          ⚠️ 服务入口唯一两条：_handle_chat_non_stream(:1291→_drive) 与 _stream_common(:1344→_drive)
  └ pipeline/engine.py:72 PipelineEngine.run (:88-93 原样委托)   →  零改动
  └ langgraph_engine.py:774 LangGraphEngine.run
      ├ mode="chat"  → :803 _run_one  → 图 ainvoke → 节点 try_call
      └ mode="stream"→ :802 _run_stream → 图 astream → 节点 try_stream
  └ 【注入点】
      ├ try_call   :282-285  call_llm(ep, …, _resolve_temperature(temperature, ep), …)
      └ try_stream :388-394  ep.provider.chat_stream(…, temperature=_resolve_temperature(temperature, ep), …)
  └ 上游 provider
      ├ anthropic_provider.py:102-103 / :128-129   if temperature is not None → 才放进 params
      ├ google_provider.py:114-115                 同上
      ├ deepseek_provider.py:214-215               同上
      └ openai_compat.py:142 / :167                直接透传（None 时行为与现状一致，不属本改动）
```

**不在服务路径（有证据，零改动）**：
- `PipelineEngine.route`（`engine.py:54`）与 `LangGraphEngine.route`（`langgraph_engine.py:817`）：`grep '\.route(' src/` 仅命中二者自身与互相委托（`docs/tasks/SG-1_review.md:271` 已核），服务入口只走 `run()`。
- `pipeline/engine.py:59/:79` 两个 `temperature` 形参因此不承载服务流量。
- `langgraph_strategy.py:144` 的 `call_llm`：`_build_strategy`（`langgraph_engine.py:127-131`）对 `group.type=="langgraph"` 直接抛 `ConfigurationError`，该策略的 `execute()` 不可达（`docs/pipeline-single-graph-design.md:327` 同结论）。
- `storage/daily_summary.py:157 strategy.execute(…, temperature=0.3)`：**显式**传 0.3（非 None）→ 走「请求优先」分支，不受模型默认值影响，日报口径不变。
- `BaseStrategy.execute`（`base.py:57`）：唯一调用方是 `daily_summary.py:157`（`grep '\.execute(' src/`）。

---

## 6. 测试用例清单

> 用例名可直接作为测试函数名；「类型」= 正例 / 反例 / 边界 / 回归。前端无测试框架（现状如此），见 §7 人工核对项。
> 测试执行在 Linux 远端：`PYTHONPATH=src python -m pytest tests/ --cov=botflow --cov-report=term -m "not integration"`。

### 6.1 设置端点（建议文件：`tests/test_admin_settings.py` 新建；fixture 照抄 `tests/test_admin_api.py:24-38`）

| # | 用例名 | 类型 | 断言点 |
|---|---|---|---|
| 1 | `test_get_settings_returns_exactly_nine_keys` | 正例 | 200；`settings` 键集 == 9 键集合；值 == `BotflowSettings()` 默认值 |
| 2 | `test_get_settings_requires_admin_key` | 反例 | 无 Authorization → 401，detail `"Invalid admin key."` |
| 3 | `test_get_settings_never_leaks_secrets` | 反例 | 响应 JSON 全文不含 `admin_key`/`llm_key`/`api_keys`/`pwd_hash` 键 |
| 4 | `test_patch_settings_updates_kv_and_memory` | 正例 | `PATCH {stream_timeout:45.5}` → 200；响应 `settings.stream_timeout==45.5`；`db.get_config("admin_settings")` 解析出 `{"stream_timeout":45.5}`；`botflow.config.get_config().stream_timeout == 45.5`（内存立即生效） |
| 5 | `test_patch_settings_partial_only_touches_sent_keys` | 边界 | 先 `PATCH {log_level:"DEBUG"}` 再 `PATCH {call_log_detail_days:3}` → 两键都在 KV，`stream_timeout` 仍为默认（**部分更新不丢已存键**） |
| 6 | `test_patch_settings_rejects_bad_log_level` | 反例 | `log_level:"VERBOSE"` → 422，`detail[0].loc` 含 `log_level` |
| 7 | `test_patch_settings_daily_summary_hour_bounds` | 边界 | `-1`→422、`24`→422、`0`→200、`23`→200 |
| 8 | `test_patch_settings_stream_timeout_must_be_positive` | 边界 | `0`→422、`-0.5`→422、`0.5`→200 |
| 9 | `test_patch_settings_non_negative_ints` | 反例 | `upstream_semaphore_size=-1` / `call_log_detail_days=-1` / `raw_session_retention_days=-1` / `call_logs_retention_days=-1` / `model_sync_interval=-1` 各 → 422（参数化） |
| 10 | `test_patch_settings_empty_body_is_noop` | 边界 | `{}` → 200；KV 中**不新增** `admin_settings` 行（`db.get_config` 返回 None）；`get_config()` 各值不变 |
| 11 | `test_patch_settings_summary_group_empty_string_saved` | 边界 | `summary_group:""` → 200 且 KV 里是 `""`（空串合法，≠ 不改；`exclude_none` 不吃掉 `""`） |
| 12 | `test_patch_settings_null_field_means_unchanged` | 边界 | `{"stream_timeout": null, "log_level":"WARNING"}` → 200；`stream_timeout` 保持原值（None=不改）、log_level 更新 |
| 13 | `test_patch_settings_requires_admin_key` | 反例 | 无 key → 401 |
| 14 | `test_settings_startup_merge_overrides_env` | 正例 | 往 KV 写 `admin_settings={"stream_timeout":99}` → `await core.create_app(tmp_path, BotflowSettings(workspace=…, stream_timeout=1.0))` → `core._config.stream_timeout == 99`（DB 覆盖 env 初值），且 `core._config is config.get_config()`（同一对象，参考 `tests/test_core_runtime.py:1290-1302` 的保存/还原写法） |
| 15 | `test_settings_startup_merge_ignores_unknown_keys` | 反例 | KV 写 `{"admin_key":"hacked","stream_timeout":5}` → 合并后 `stream_timeout==5` 且 `admin_key` **未被改**（白名单，非 hasattr） |
| 16 | `test_settings_startup_merge_bad_json_is_tolerated` | 反例 | KV 值写 `"{not json"` → `create_app` 不抛异常，settings 保持传入初值 |
| 17 | `test_settings_startup_merge_missing_key_keeps_defaults` | 边界 | 无 `admin_settings` 行 → `create_app` 后 9 参数全为 env/默认值 |

### 6.2 同步端点（建议文件：`tests/test_admin_api.py` 扩 `class TestProviderSync`）

| # | 用例名 | 类型 | 断言点 |
|---|---|---|---|
| 18 | `test_sync_provider_not_found_404` | 反例 | `POST /admin/providers/9999/sync` → 404，`detail=={"success":False,"error":"Provider 9999 not found"}` |
| 19 | `test_sync_provider_success` | 正例 | `monkeypatch.setattr("botflow.core.sync_models_from_provider", fake)`，fake 断言收到 `provider_id` 与传入的 `db`，返回 `{"added":2,"skipped":1,"errors":[]}` → 200，`{"success":True,"added":2,"skipped":1,"errors":[]}` |
| 20 | `test_sync_provider_upstream_error_success_false` | 反例 | fake 返回 `{"added":0,"skipped":0,"errors":["connect timeout"]}` → HTTP 200 但 `success is False`、`errors==["connect timeout"]` |
| 21 | `test_sync_provider_requires_admin_key` | 反例 | 无 key → 401 |
| 22 | `test_sync_provider_invalidates_caches` | 回归 | 照 `tests/test_p0_1_admin_invalidation.py:125-131` 的 `_patch_invalidate["all"]` 断言：成功路径 `invalidate_all_caches()` 恰被调 1 次；404 路径 0 次 |
| 23 | `test_sync_provider_real_upstream_via_transport`（可选，integration marker） | 正例 | 用 `httpx.MockTransport`/本地 stub 提供 `GET /v1/models` → `core.sync_models_from_provider` 真跑一遍落库，`added==N`（守「只加新不改旧」） |

### 6.3 供应商 extra_config / type / 启用禁用（建议：`tests/test_admin_api.py` 扩 + `tests/test_db_full.py` 扩）

| # | 用例名 | 类型 | 断言点 |
|---|---|---|---|
| 24 | `test_create_provider_with_extra_config` | 正例 | `POST {req:{name,base_url,extra_config:{proxy:"http://p:1",timeout:30}}}` → 200；`GET /providers/{id}` 的 `provider.extra_config == {proxy,timeout}` |
| 25 | `test_create_provider_defaults_type_openai` | 回归 | 不传 `type` → `provider_type=="openai"`（拍板 D1：前端不再发 type） |
| 26 | `test_patch_provider_extra_config_none_keeps_existing` | 边界 | 先建带 `extra_config` 的 provider，再 `PATCH {name:"n2"}`（不带 extra_config）→ `extra_config` 原值不变 |
| 27 | `test_patch_provider_extra_config_replaces_whole_dict` | 边界 | 原 `{proxy,timeout}`，`PATCH {extra_config:{"timeout":5}}` → 变成**只有** `{"timeout":5}`（整体覆盖语义，proxy 消失） |
| 28 | `test_patch_provider_empty_extra_config_clears` | 边界 | `PATCH {extra_config:{}}` → 存为 `{}`（非 None → 必须写列） |
| 29 | `test_patch_provider_without_type_preserves_type` | 回归（D1） | 建 `type:"anthropic"` → `PATCH {base_url:"x"}`（不带 type）→ `provider_type` 仍 `"anthropic"` |
| 30 | `test_patch_provider_toggles_is_enabled` | 正例 | `PATCH {is_enabled:false}` → GET `is_enabled is False`；再 `true` → `True` |
| 31 | `test_update_provider_raw_default_keeps_extra_config` | 回归 | `db.update_provider_raw(pid, name=…, type=…, base_url=…, api_key=…, is_enabled=True)`（**不传** extra_config，同 `tests/test_db_full.py:95`）→ 原 `extra_config` 不变 |
| 32 | `test_update_provider_raw_writes_extra_config_when_given` | 正例 | 传 `extra_config={"a":1}` → 重查为 `{"a":1}` |
| 33 | `test_create_provider_raw_with_extra_config` | 正例 | `create_provider_raw(..., extra_config={"timeout":9})` → `get_provider_raw().extra_config=={"timeout":9}`；不传 → `{}` |

### 6.4 模型 extra_config + temperature 注入（建议：`tests/test_admin_api.py`、`tests/test_langgraph_engine.py` 扩）

| # | 用例名 | 类型 | 断言点 |
|---|---|---|---|
| 34 | `test_create_model_with_extra_config` | 正例 | `POST /models {req:{…, extra_config:{temperature:0.2}}}` → GET 返回该 dict；不传 → `{}` |
| 35 | `test_patch_model_extra_config_replaces` | 正例 | `PATCH {extra_config:{temperature:0.7,modalities:["text","image"]}}` → GET 原样返回 |
| 36 | `test_patch_model_extra_config_none_keeps` | 边界 | `PATCH {name:"n"}` → extra_config 原值不变 |
| 37 | `test_update_model_raw_default_keeps_extra_config` | 回归 | `db.update_model_raw(mid, name=…, is_enabled=…)`（同 `tests/test_db_full.py:106`）→ extra_config 不变；传了则覆盖 |
| 38 | `test_resolve_temperature_request_wins` | 正例 | `_resolve_temperature(0.9, ep(cfg temp=0.1)) == 0.9` |
| 39 | `test_resolve_temperature_falls_back_to_model_default` | 正例 | `_resolve_temperature(None, ep(cfg temp=0.1)) == 0.1` |
| 40 | `test_resolve_temperature_absent_returns_none` | 边界 | `_resolve_temperature(None, ep(cfg={})) is None`（→ 上游不带该字段，现状不变） |
| 41 | `test_resolve_temperature_ignores_non_number` | 反例 | `ep(cfg temp="hot")` → `None`（DB 里手填字符串不外泄到上游） |
| 42 | `test_resolve_temperature_ignores_bool` | 反例 | `ep(cfg temp=True)` → `None`（`bool` 是 `int` 子类，必须显式排除） |
| 43 | `test_try_call_injects_model_default_temperature` | 正例 | 走 `engine.run(mode="chat")`（temperature 缺省 None）+ endpoint 的 `extra_config.temperature=0.3` → stub provider 的 `chat` 收到 `temperature=0.3` |
| 44 | `test_try_call_request_overrides_model_default` | 正例 | `engine.run(..., temperature=0.9)` + 同上模型默认 → `chat` 收到 `0.9` |
| 45 | `test_try_stream_injects_model_default_temperature` | 正例 | `engine.run(mode="stream")` → stub `chat_stream` 收到 `temperature=0.3`；请求给了则收到请求值 |
| 46 | `test_no_temperature_anywhere_sends_none` | 回归 | 两侧皆无 → `chat`/`chat_stream` 收到 `None`（与现状逐字节一致） |
| 47 | `test_per_endpoint_temperature_in_same_group` | 边界 | 同组两个 endpoint，各自 `extra_config.temperature` 不同 → 各自解析（证明解析在 while 循环内、非组级一次） |

### 6.5 回归锚点（不改测试，必须仍全绿）

- `tests/test_admin_api.py`：provider/model/group/apikey CRUD、401、`GET /models?provider_id=`（`admin_api.py:334`）。
- `tests/test_db_full.py:86-155`：raw CRUD 默认参数路径（extra_config 加参后**不传参行为不变**）。
- `tests/test_p0_1_admin_invalidation.py:125-157`：PATCH → `invalidate_all_caches()`（mock 的 `update_*_raw` 是 `AsyncMock`，多收一个 `extra_config` kwarg 不影响断言）。
- `tests/test_langgraph_engine.py:291`（`route(..., temperature=0.7, max_tokens=256)`）与 `:366`、`tests/test_pipeline_engine.py:366`：既有 temperature 透传用例（请求优先 → 值不变）。
- `tests/test_core_runtime.py:190-260`：`sync_models_from_provider` / `sync_all_models` 真实路径（本任务不改 `core.py` 这两个函数，只新增 admin 端点调用它们）。
- `tests/test_config.py`：`BotflowSettings` 字段默认值（本任务不改 9 个字段定义，只加模块级常量 `ADMIN_SETTINGS_FIELDS`）。
- `tests/test_daily_summary.py`：retention/summary_group 读取路径零改动。

---

## 7. 前端人工核对项（无前端测试框架，逐条截图/手点验收）

| # | 核对项 | 期望 |
|---|---|---|
| W1 | 调用日志 + API Keys 的时间列 | 显示东八时间（与后端 `_resolve_range` 的东八口径一致）；空值显示 `-`；浏览器时区改 UTC 后显示**不变** |
| W2 | 侧边栏出现「设置」 | 8 项；点击才拉 `GET /admin/settings` |
| W3 | 设置页保存 | 改 `log_level` → 保存 toast 成功 → 刷新页面值回显；把 `daily_summary_hour` 填 24 → 前端/后端 422，toast 显示 `daily_summary_hour: ...`（不是 `JSON.stringify` 全串） |
| W4 | 设置页字段标注 | `stream_timeout` 标「当前运行时未读取」；`upstream_semaphore_size`/`daily_summary_hour`/`model_sync_interval`/`log_level` 标「重启后生效」；3 个 retention + `summary_group` 标「次日维护生效」 |
| W5 | 供应商列表 | **无「类型」列**；有「获取模型」与「启用/禁用」按钮；行内状态 badge 正确翻转 |
| W6 | 新建供应商 | 弹窗无「类型」select；创建成功（`provider_type=openai`） |
| W7 | 编辑供应商 | proxy/timeout 两项从 `extra_config` 回填、textarea 显示完整 JSON；把 textarea 改成 `{"a":` → 保存被拦截且 toast 提示 JSON 非法；清空 timeout 输入 → 保存后 `extra_config` 里 timeout 键消失 |
| W8 | 获取模型 | 点击按钮 loading → toast `新增 X · 跳过 Y`；拔网线/坏 base_url → toast 同步失败且含 errors 文案；完成后模型列表刷新 |
| W9 | 模型编辑弹窗 | edit 模式 11 类字段齐全、供应商 disabled；create 模式仍只有 3 项；弹窗实体识别仍正确（编辑模型不会被当成供应商/分组） |
| W10 | 模型列表「模态/温度」列 | 勾了 text+image 且 temp=0.7 → `text,image · temp=0.7`；没配置 → `-` |
| W11 | temperature 实测 | 模型设 0.3，请求不传 → 上游请求带 0.3（可打上游 echo）；请求传 0.9 → 上游收 0.9；请求传 `"temperature":null` → 上游收 0.3 |
| W12 | 分组添加模型 | 两级下拉；换供应商已选模型被清空；未选供应商时第二级 disabled；选项显示 `模型名 · 上下文窗口`；添加后分组详情刷新 |
| W13 | 形状识别回归 | 依次新建/编辑 供应商、模型、分组、API Key 各一次，提交均进对的保存函数 |

---

## 8. 风险与未决点（交主 agent 拍板）

1. **`stream_timeout` 是死配置**（`src/` 无读取点）：页面能存、启动能合并，但改了不影响任何行为。本任务按需求照常页面化并在字段旁如实标注「当前运行时未读取」。若要求它真正生效，需要另行决定接到哪条读取链（超出本任务范围）。
2. **`upstream_semaphore_size` 无法热生效**：`invalidate_all_caches` 刻意不清 `_provider_semaphores`（`router.py:285` 注释：信号量身份不能打断），所以只能标「重启后生效」。若要热生效需引入「尺寸变更时替换信号量」的并发安全逻辑（未请求，不做）。
3. **provider `extra_config` 后端是整字典覆盖**（不做键级合并）：若有外部脚本 PATCH 只想改一个键，必须带全量 dict。前端已按「底稿 + 两输入框」组装，管理面板路径无损；CLI/脚本路径需知悉。
4. **`sync` 端点的 `success` 语义**定为 `not errors`（HTTP 恒 200，除非 404/401）：若主 agent 期望「上游失败 → HTTP 5xx」，需要改端点（一行）。
5. **`modalities`/`temperature` 写进 `models.extra_config` 后，`sync_models_from_provider` 的 upsert 路径不受影响**（`core.py:245-258` 只 `create_model` 新增、从不覆盖既有行）——已核，模型同步不会冲掉人工配置。
6. 前端 `api()` 新增「422 detail 是数组」的可读化分支（F2.4），会同时影响其它端点的错误文案展示（更可读、不改语义）。若不希望波及，可退化为显示 JSON 串。
