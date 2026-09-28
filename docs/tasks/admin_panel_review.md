# admin 面板 6 项需求 — 验证子 agent 审核报告

> 审核对象：编码子 agent 的工作区改动（6 文件 +386/-38，未提交）
> 对照文档：`docs/tasks/admin_panel_features.md`（v1，415 行）
> 审核结论：**通过（PASS）** —— 实现与清单语义一致，未发现需打回的实现 bug；
> 文档行号存在小幅漂移（实现落地后行号前移），不影响语义；清单有 2 处覆盖缺口，
> 已由验证侧补测试闭合（见 §4）。
> 测试证据：mq3 远端全量 `1286 passed / 0 failed`，`TOTAL 4508 行 0 missed 100%`（见 §5）。

---

## 1. 审核范围与方法

- **只读审核**：未改动 `src/` 任何一行（硬约束）；发现的问题以报告形式交主 agent。
- 逐条核对 §6 测试清单 47 条 + §6.5 回归锚点 + §7 前端 13 条人工核对项，
  对每条功能点核对：实现落点是否与文档描述的语义一致、关键行号引用是否真实存在、
  三条已拍板决策（D1/D2/D3）是否被贯彻。
- 行号核对方式：`grep -n` 实测当前工作区行号 vs 文档「行号为当前工作区实测」声明。

## 2. 六项功能逐项核对

### F1 日志/创建时间北京时间 — **通过**

- `index.html:1090-1094` `fmtTime`：`replace(' ','T')+'Z'` 按 UTC 解析 →
  `toLocaleString('zh-CN',{timeZone:'Asia/Shanghai', ...})` 展示，选项与文档 §F1-3 一字不差。
- 空值兜底 `!t → '-'` 保留；原 `try/catch` 死分支已删（与文档 §F1-4 一致）。

### F2 设置页（GET/PATCH + 启动合并） — **通过**

- `GET /admin/settings`（`admin_api.py:280`）：`_settings_snapshot()` 按
  `ADMIN_SETTINGS_FIELDS`（`config.py:92`，恰 9 键白名单）取值，无密钥字段。
- `PATCH /admin/settings`（`admin_api.py:285`）：`UpdateSettingsReq`（`:114`）
  9 字段全 Optional，校验约束与文档 §2 表逐条相符（`log_level` Literal、
  `daily_summary_hour ge=0 le=23`、`stream_timeout gt=0`、其余 `ge=0`、
  `summary_group` 无约束空串合法）→ 非法值 pydantic 层 422，不落 KV。
  `model_dump(exclude_unset=True, exclude_none=True)` 取真实变更集 →
  部分更新、空 body no-op、`null`=不改 三条语义均成立。
  双写：KV `set_config(SETTINGS_KEY, ...)` + `setattr(get_config(), ...)` 内存单例立即生效。
- 启动合并（`core.py:1469-1478`，在 `await _db.initialize()` 之后）：
  `json.loads` 失败按 `{}`、非 dict 跳过、按 `ADMIN_SETTINGS_FIELDS` 白名单
  setattr —— **白名单而非 hasattr**（防 KV 混入 `admin_key` 被覆盖），与文档 §F2.3 一致。
- `SETTINGS_KEY = "admin_settings"`（`admin_api.py:270`）与 `core.py` 共用，
  不会被 `admin_sess:%` / `dedup:%` / `cooldown:%` 任一清理前缀误删（§0-3 达标）。

### F3 供应商「获取模型」同步 — **通过**

- `POST /admin/providers/{id}/sync`（`admin_api.py:380`）：404 形状
  `{"success":False,"error":"Provider {id} not found"}` 与既有 404 一致；
  函数内**惰性导入** `from botflow.core import sync_models_from_provider`
  （避免 core↔admin_api 循环 import，与文档 §F3.1-2 一致）；
  成功路径 `invalidate_all_caches()`；`success = not errors`，HTTP 恒 200（除 404/401）。
- 无「全部同步」端点（§0-10 达标）；`sync_all_models` 与定时任务零改动。

### F4 供应商 type 隐藏 + extra_config + 启停 — **通过（D1 贯彻）**

- `CreateProviderReq.extra_config: dict = {}`（`admin_api.py:49`）、
  `UpdateProviderReq.extra_config: Optional[dict] = None`（`:85`）；
  端点层 `extra_config=req.extra_config if req.extra_config is not None else provider.extra_config`
  （`:362`）—— `None`=不改、给出（含 `{}`）=整体覆盖，与 §4 汇总表一致。
- `type` 零变化：create 仍 `req.type` 缺省 `"openai"`（`:47`），PATCH 不带 type 回填原值（`:360`）。
- `db.create_provider_raw`（`db.py:1147`）/ `update_provider_raw`（`:1163`）加
  `extra_config=None`：`None` 不拼进 SQL 列集合，非 `None` `json.dumps` 整列覆盖；
  create 侧 `extra_config or {}`。既有调用方（`test_db_full.py:95` 不传参）行为不变。
- 前端：`saveProvider`（`index.html:937-947`）create/PATCH **均不发 `type` 与 `is_enabled`**，
  发 `{req:{...extra_config}}` / `{...extra_config}`；「类型」列与 select 已删；
  `providerEditForm`（`:915`）回填 proxy/timeout/extraJson；
  `toggleProvider`（`:948`）`{is_enabled: !p.is_enabled}` 照抄 toggleModel。

### F5 模型 temperature + 模态 — **通过（D2/D3 贯彻）**

- `_resolve_temperature`（`langgraph_engine.py:254-264`）与文档 §F5.2 代码块**逐字符一致**：
  请求非 None 优先（含 0.0）→ 回落 `ep.detail.extra_config["temperature"]`
  （`isinstance((int,float)) and not isinstance(bool)`）→ 都没有 None。
  `temperature:null` 歧义按「未传」处理的口径写进了 docstring（文档 §F5.2 ⚠️ 一致）。
- 两个调用点均在循环体内、按 endpoint 解析：
  `try_call` `:297` `call_llm(..., _resolve_temperature(temperature, ep), ...)`、
  `try_stream` `:404` `ep.provider.chat_stream(..., temperature=_resolve_temperature(temperature, ep), ...)`。
- `UpdateModelReq.extra_config`（`admin_api.py:97`）/ `CreateModelReq.extra_config`（`:58`）、
  `db.create_model_raw`（`db.py:1180`）/ `update_model_raw`（`:1200`）与 F4 同构。
- `modalities` 后端零消费（仅存取展示，D3）；`temperature` 只在上述两调用点被读。
- 前端 `modelEditForm`（`index.html:973`）/ `saveModel` 组装规则（原对象为底 →
  温度/模态有值覆盖、为空 delete）与文档 §F5.4 一致；列表「模态/温度」列（`:983-984`）。

### F6 分组两级选择 — **通过（后端零改动）**

- `newProviderForGroup`（`:299`）`@change` 清空已选模型；第二级
  `:disabled="!newProviderForGroup[g.id]"`（`:303`）；占位项文案与 §F6 一致；
  `doAddModelToGroup` 未改；`submitModal` 形状识别未破坏（model 表单仍含
  `context_window`，无 `base_url`/`raw_key`/`fallback_group_id`，§0-7 达标）。

## 3. 文档行号漂移（仅记录，不打回）

文档声明「行号为当前工作区实测」，但其实现落地前测的，落地后 +386 行导致前移：

| 文档写 | 实测 | 说明 |
|---|---|---|
| `CreateModelReq :50` | `admin_api.py:51` | 漂 1 行 |
| `UpdateProviderReq :76` | `admin_api.py:78` | 漂 2 行 |
| `UpdateModelReq :83` | `admin_api.py:87` | 漂 4 行 |
| `admin_api.py:249-308` providers CRUD | 实际 315-376 | settings 段插入后整体下移 |
| `admin_api.py:316-384` models CRUD | 实际 404-474 | 同上 |
| `db.py:1161/:1192` update raw | 实际 1163/:1200 | 签名行漂 1-8 行 |
| `core.py:1460` 启动合并位置 | 实际 1469 | 合并块在 `:1469-1478` |

语义均一致，**不影响任何断言**（测试全部按语义/函数名定位，不按文档行号）。

## 4. 清单缺口与处理（验证侧补测）

1. **§6.1 用例 1-17 未覆盖 `update_settings` 的两个容错分支**：
   KV 值被手改坏（`except ValueError` → 以本次变更为准重建）与
   KV 是非 dict JSON（`isinstance(stored, dict)` 兜底）。
   若不补，`admin_api.py:298-301` 这 4 行会 missed。
   → 已补 2 条：`test_patch_settings_corrupt_kv_rebuilds_from_change`、
   `test_patch_settings_non_dict_kv_rebuilds_from_change`。
2. **用例 22（invalidate 断言）归并**：为避免与 18/19/23 重复建用例，
   `invalidate_all_caches()` 断言并入三条——404 路径 `assert_not_called`、
   成功路径 `assert_called_once_with()`，覆盖与清单意图等价。
3. **422 可读化分支**（清单 §2 F2.4）位于前端 `index.html:563-571`
   （`Array.isArray(d)` → `"字段: msg; ..."`），属 §7 人工核对项 W3，
   Python 测试清单无对应用例，跳过合理。
4. **用例 23** 文档标「可选 integration marker」，验证侧按普通用例实现
   （`httpx.AsyncClient` mock，无网络），跑在主套件内。

## 5. 测试落点与执行证据

### 5.1 新增用例（合计 +52）

| 文件 | 新增 | 覆盖清单 |
|---|---|---|
| `tests/test_admin_settings_sync.py`（新建） | 28 | 用例 1-23（22 归并）+ 上述 2 条补缺口 |
| `tests/test_admin_api.py`（扩 TestProviderExtraConfig / TestModelExtraConfig） | 10 | 用例 24-30、34-36 |
| `tests/test_db_full.py`（扩） | 4 | 用例 31-33、37 |
| `tests/test_langgraph_engine.py`（扩） | 10 | 用例 38-47（38-42 单元 + 43/44/46 try_call 图级 + 45 try_stream + 47 节点级） |

说明：
- 用例 47 若走 `engine.run` 会被策略层 `weighted_random_select` 随机排序破坏顺序，
  改为节点级直驱 `try_call`（与既有 T4.x/G2 同手法），断言确定。
- 用例 45/46 的 `chat_stream` kwargs 断言通过给既有 `_StreamingProvider` 增加
  `kwargs_log` 实现，不改任何既有断言。
- fixture 风格全部照抄邻近用例（`client`/`AUTH`/`_ep`/`_StreamingProvider`/`_run_try_stream_node`）；
  新文件 `client` fixture 额外保存/还原 `config_mod._config`（PATCH 会 setattr 内存单例，
  不还原会跨文件泄漏）。

### 5.2 回归锚点（§6.5）— 全部仍绿

`test_admin_api.py` CRUD/401、`test_db_full.py:86-155` raw CRUD 默认参数路径、
`test_p0_1_admin_invalidation.py` PATCH→invalidate、
`test_langgraph_engine.py:291/:366` 与 `test_pipeline_engine.py:366` 既有 temperature 透传、
`test_core_runtime.py` sync 真实路径、`test_config.py` 字段默认值、
`test_daily_summary.py` retention 读取 —— 远端全量 0 failed 即证。

### 5.3 远端执行（mq3，Linux / Python 3.13.15）

本机 Windows 无法跑 async（loopback 禁用挂死，本机仅做静态检查与同步冒烟：
`py_compile` 4 文件通过、`--collect-only` 141 条收集通过、
`_resolve_temperature` 5 条同步单元本机 5 passed）。

远端流水线：打包 → `scp qbot@100.88.88.88:/tmp/` → `uv venv --python 3.13` →
`pytest tests/ --cov=botflow --cov-report=term-missing -m "not integration"`
（隔离在 `/tmp/bt_cov`，**未触碰生产目录 `/srv/botflow`**）：

```
1286 passed, 23 deselected, 10 warnings in 113.81s
PYTEST_RC=0
TOTAL    4508    0    100%      ← 0 failed，0 missed，达成放行门槛
```

- 基线 1234 passed / 4430 行 → 本次 **1286 passed / 4508 行**（+52 用例 / +78 语句）。
- 全仓 36 个模块全部 100%，**0 missed**，未使用 `# UNCOVERED` / `pragma no cover`
  掩盖任何新增行。
- 附注：跑测含 `-m "not integration"` 排除 23 条 integration 用例（与基线口径一致）。

## 6. 遗留风险（与编码侧自报 7 项对照）

| # | 风险 | 判定 |
|---|---|---|
| 1 | `stream_timeout` 死配置（src 无读取点） | **属实**（全仓 grep 复核）。页面能存、能合并，但不改任何行为；前端已如实标注「当前运行时未读取」。是否接链超本任务，交主 agent。 |
| 2 | `upstream_semaphore_size` 只能重启生效 | 属实（`invalidate_all_caches` 刻意不清信号量）。标注正确，无需改。 |
| 3 | provider/model `extra_config` 整字典覆盖（无键级合并） | 属实，属拍板语义；API/db 层测试 26/27/28/35/36/31/32/37 固化。脚本调用方需知悉。 |
| 4 | sync `success = not errors`，HTTP 恒 200 | 属实，与文档 §F3.1-4 一致；若主 agent 想要 5xx 需改端点（一行）。 |
| 5 | `sync_models_from_provider` upsert 不会冲掉人工配置 | 用例 23 集成链路已断言「只加新不改旧」。 |
| 6 | 422 可读化分支波及其它端点错误文案 | 属实（仅更可读、不改语义）；如不希望波及可退化为 JSON 串。 |
| 7 | （编码侧其余自报项） | 均已在上文核对，无新增偏差。 |

## 7. §7 前端人工核对项（W1-W13）— 静态核对结论

无前端测试框架，本报告只做静态核对（grep 关键落点），**实机点击验收留给主 agent**：

- W1 `fmtTime` `Asia/Shanghai` 已落地（`index.html:1094`）。
- W2 设置页入 `pages`（`:602`）、`switchPage` 拉取 `loadSettings`（`:872-886`）。
- W3 保存 toast（`:903`）、422 可读化（`:569-571`）——待实机点。
- W5/W6 类型列与 select 已删；启停按钮在（`:225`）。
- W8 `syncProvider`（`:959`）loading/toast/`loadAll` 齐。
- W10 「模态/温度」列（`:983-984`）。
- W11 temperature 链路由 Python 用例 43-47 保证；上游 echo 实测待实机。
- W12 两级下拉（`:299-304`）。
- W13 形状识别回归由 `TestWriteEndpointsUseJsonBody` 既有用例 + 本轮全量绿守住。

---

**最终结论：实现通过审核；新增 52 条测试；远端 1286 passed / 0 failed / TOTAL 4508 行 100%（missed 0），达成放行门槛。可交主 agent 集成验收。**
