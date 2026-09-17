# botflow 运行效率审计报告

- **审计对象**：`E:\src\openbotflow`（botflow 3.0.0，部署于 `openbot@api.vxquant.com:/mnt/deploy/botflow`）
- **审计日期**：2026-09-17
- **审计方式**：源码静态审计 + 生产环境真实遥测（`call_logs` 50,749 行 / 运行约 2 天）+ Linux 端微基准实测
- **审计性质**：**只读审计，未改动任何代码/配置/数据库**

---

## 一、结论（TL;DR）

> **整体结论：网关自身不是当前延迟的主要来源，但存在 4 处确定的实现级缺陷，会在规模上来后先成为瓶颈。**

| 维度 | 结论 |
|---|---|
| **延迟构成** | 端到端 p50≈7.7s、p95≈47s，**几乎全部由上游模型（prompt 平均 45.7K token）占据**；网关侧每请求 CPU 实测 **约 2ms(p50) ~ 14ms(p99)**（详见表 4.1），占比很小 |
| **稳定性** | v3.0.0 上线后 **成功率 99.94%**（5077 次调用仅 3 次失败）；历史错误（NameError/Cooldown）均已失效，**当前无活跃故障** |
| **最需修复** | ① provider 缓存过期不关旧连接（**连接池泄漏，长期运行必现**）；② `call_log` 逐行 commit 使批量写入器失效；③ `truncate_messages` 走 `estimate_tokens` 时单请求耗 21–142ms（**仅当分组含 `context_window>0` 的端点才触发**，当前生产为 `backup` 组，约 3.9% 流量）；④ 每 chunk 冗余的 `is_disconnected()` 探测 |
| **数据库** | 功能无碍、索引覆盖良好；但 **324MB 文件里 216MB 是空洞**、从未 `ANALYZE`、`synchronous=FULL` 使写入能力仅 ~306 行/秒（当前峰值 ~289/小时，暂无压力，但为扩展天花板） |
| **风险总评** | **低**。无 P0 级线上故障；下述 4 项属"迟早要还的技术债"，建议在下一迭代随其他改动一并修掉 |

---

## 二、问题清单（按影响排序）

### 🔴 P0-1 provider 缓存过期时覆盖实例、不关闭旧客户端 → httpx 连接池泄漏

| 项 | 内容 |
|---|---|
| **位置** | `src/botflow/router.py` `_get_cached_provider()`（约 L192） |
| **现象** | 缓存命中判断过期后，直接 `cache[key] = (new_instance, now)` **覆盖**，旧 `AsyncOpenAI` 实例与其底层 httpx 连接池未被 `close()`/`aclose()` |
| **放大因素** | `providers/base.py` 的 `BaseProvider` **没有定义 `close()`**；`openai_compat.py` 的 `client` 为惰性创建，`_make_http_client()` 在无代理时返回 `None`（即用 SDK 自带连接池）——旧池无处回收 |
| **证据** | 源码逐行审计（`_provider_cache` TTL=300s，每 5 分钟为每个活跃 provider 重建一次实例）；`openai._constants.DEFAULT_CONNECTION_LIMITS = Limits(max_connections=1000, max_keepalive_connections=100, keepalive_expiry=5.0)`（池本身宽裕，**故不是池太小，而是旧池被丢弃**） |
| **影响** | 进程内 FD / 内存随运行时间缓慢单调增长；长跑后可能出现连接耗尽或 RSS 抬升。生产当前 RSS=347MB、FD=16，**尚未表现，但属确定性泄漏** |
| **改法** | 覆盖前对旧实例调用异步关闭（`await old.client.close()` 或 `aclose()`）；给 `BaseProvider` 加 `async def aclose()` 抽象，`_provider_cache` 驱逐路径统一走它。注意 `invalidate_*` 现有契约：**不得清 `_provider_semaphores`** |
| **置信度** | **已确认**（源码可证）；泄漏的绝对量级 **待压测验证** |

### 🔴 P0-2 `create_call_log()` 逐行 commit，令 `CallLogWriter` 的批量缓冲形同虚设

| 项 | 内容 |
|---|---|
| **位置** | `src/botflow/storage/db.py` `create_call_log()`（**L763**，commit 在 L781）；调用方 `core.py` `CallLogWriter._flush_unlocked()`（L152-164，逐条循环） |
| **现象** | `CallLogWriter` 已做「攒够 100 条或每 5 秒 flush」的缓冲，但 flush 时**对每条记录各执行一次 `execute()` + `await conn.commit()`** → 100 条 = 100 次事务提交，批量的收益被抵消 |
| **实测** | 单条 `INSERT + COMMIT`（FULL 同步）= **3.27ms** → 写上限约 **306 行/秒** |
| **放大因素** | 同一连接上 `synchronous=FULL`（见 P1-8），每次 commit 都要 fsync |
| **证据** | 生产 DB `PRAGMA synchronous=2`；基准脚本 `perf_bench.py` 用例 D2 |
| **影响** | 高并发写入时会成为尾延迟来源；按当前速率（历史峰值 ~289 调用/小时）**远未触顶**，但为明确的扩展天花板 |
| **改法** | 提供批量写入：`executemany()` 收集所有行后**一次 commit**；`save_cooldown_state()` 同理（现逐 key 调 `set_config`，每次 commit） |
| **置信度** | **已确认**（源码 + 实测） |

### 🔴 P0-3 `truncate_messages` 命中 `estimate_tokens` 时单请求耗 21–142ms（**⚠️ 已更正，非"每请求"**）

> **更正说明（2026-09-17 复核）**：本节初稿称 `estimate_tokens` 被"每个请求"调用，**该表述有误**。复核后确认：它只在**分组中存在 `context_window > 0` 的端点**时才会执行（`truncate_messages` 的 `if not context_windows: return messages` 早退）。当前生产仅 `backup` 组满足（≈3.9% 流量），其余 96% 请求**完全不调用**该函数。

| 项 | 内容 |
|---|---|
| **位置** | `src/botflow/common/context.py`：`_cjk_ratio()`（**L98** 逐字符 for 循环）、`estimate_tokens()`（L25 逐 message 调用）、`truncate_to_context_window()`（L46 首次估算 + 二分反复估算）；触发链 `pipeline/_shared.py truncate_messages()`（L168）← `pipeline/strategies.py`（L46/93/129） |
| **触发条件（关键）** | `truncate_messages` 先取 `[ep.detail.context_window for ep in endpoints if > 0]`；**全部为 0 时直接返回，零开销**。实测 `context_window=0` 时该函数耗时 **0.001ms** |
| **实测**（真实路径，`ctx=1e6` 时） | p50 规模 **21.1ms**；p95 规模(155K tok / 1.5MB body) **79.9ms(p95 105.1)**；p99 规模(235K tok / 2.3MB) **142.4ms(p95 156.4)** |
| **生产实况** | `models.context_window`：**552 个模型为 0**，16 个为 1,000,000，2 个为 131,072。分组挂载：`fast`/`smart`(模型 559)=0、`free`/`fast-text`(模型 539/540)=0、**`backup` 含模型 14 `mimo-v2.5`=1,000,000** → 仅 `backup` 组走估算路径 |
| **受影响流量** | `backup` 组 1,979 / 50,783 次调用 ≈ **3.9%**（且该组另一端点 540 为 ctx=0，若它单独被选中则不触发） |
| **影响** | 触发时是**单进程同步阻塞**（解释循环），会卡事件循环；但覆盖面小。另有隐患：ctx=0 意味着**不做任何上下文保护**，超长 prompt 会原样透传给上游（由上游报长度错误），见"待确认" |
| **改法** | ① 用 `str.translate` / 正则批量统计 CJK 替换逐字符循环；② 按消息内容 hash 缓存估算结果；③ 若希望只对 ctx=0 的分组也做保护，需先给模型补 `context_window` |
| **置信度** | **已确认**（源码 + 真实路径实测三档 + 生产 `context_window` 实况） |

### 🔴 P0-4 流式响应中每 chunk 调用一次 `request.is_disconnected()`（冗余且与框架争抢通道）

| 项 | 内容 |
|---|---|
| **位置** | `src/botflow/core.py:1158`（`async for chunk in _chain_first(...)` 循环内） |
| **现象** | 框架层已在监听断开；此处再逐 chunk `await request.is_disconnected()` |
| **根因** | Starlette 1.6 + uvicorn 0.52.4 → ASGI `spec_version=2.3`（< 2.4）→ `StreamingResponse.__call__` 走 `create_collapsing_task_group` 分支，**已启动 `listen_for_disconnect(receive)`**；应用侧再调 `is_disconnected()` 是在**同一个 `receive()` 通道**上重复监听 → 冗余，且属于潜在竞争 |
| **实测**（基准 C） | 单次 46.63us → 500 chunk 的长流 = **约 23ms**；超长流可达 90ms+ |
| **证据** | uvicorn `h11_impl.py:207` 上报 `spec_version="2.3"`；Starlette `StreamingResponse.__call__` L273 分支源码；基准 C |
| **影响** | 每个流式请求白白多耗 20–90ms CPU（**仅在 ASGI spec < 2.4 时冗余**；若依赖升级到 2.4 会变为必要，见"待确认"） |
| **改法** | 移除该行，依赖框架的 `listen_for_disconnect`；若担心兼容性，可加版本判断 |
| **置信度** | **已确认冗余**（针对当前部署栈）；改法需注意未来 spec 升级 |

### 🟠 P1-5 流式请求也计算 `request_id`（流式路径根本不使用）

| 项 | 内容 |
|---|---|
| **位置** | `core.py:775`：`request_id = body.get("request_id") or _generate_request_id(body, _api_key_id)`，**在流式/非流式分流之前**对每个请求都算 |
| **实测**（基准 B） | p95 body 622KB → 计算耗时 **约 4ms（最高 6.5ms）** |
| **改法** | 下沉到非流式分支内计算，或对 `None` 结果按需惰性计算 |
| **置信度** | **已确认** |

### 🟠 P1-6 每个请求都重查 `list_api_keys()` 做鉴权

| 项 | 内容 |
|---|---|
| **位置** | `src/botflow/auth.py resolve_api_key()`（L37）→ `db.list_api_keys()`；由 `AuthMiddleware`（`core.py:562`）每请求调用 |
| **现象** | 无缓存，逐请求读库 |
| **改法** | 加 TTL 缓存（与 router 的 `*_cache` 同风格），Admin 改 api_key 时失效 |
| **置信度** | **已确认**（源码）；实际耗时未单独基准 **待验证** |

### 🟠 P1-7 数据库从未 `VACUUM` / `ANALYZE`

| 项 | 内容 |
|---|---|
| **证据** | DB 文件 **324MB**，其中有效仅约 **108MB**、**216MB 为空闲页（空洞）**；`sqlite_stat*` 表**不存在**（从未 ANALYZE）；`call_logs` 占有效空间 85.8%（92.8MB） |
| **成因** | `call_log_detail_days=1` 会定期清掉大字段 + 全表行持续增删 → 页空洞累积；从未回收 |
| **影响** | ① 磁盘浪费约 216MB；② 查询规划器缺统计信息，复杂查询可能选错执行计划（当前 EXPLAIN 显示常用查询均走索引，暂未暴露） |
| **改法** | 低峰期执行一次 `VACUUM`（注意需要约等量临时空间、会持写锁）；此后 `ANALYZE` 并纳入定期维护 |
| **置信度** | **已确认**（实测 dbstat + pragma） |

### 🟠 P1-8 `synchronous=FULL`，写提交比 NORMAL 慢约 40 倍

| 项 | 内容 |
|---|---|
| **证据** | `PRAGMA synchronous=2`（FULL）；基准 D4：**NORMAL = 0.08ms/commit（约 11,975/s） vs FULL = 3.44ms/commit（约 291/s）** |
| **权衡** | WAL 模式下 `NORMAL` 仅在断电瞬间可能丢最后若干事务（不损库），对本项目（日志类写入）通常可接受 |
| **改法** | 评估后改 `synchronous=NORMAL`，与 P0-2 的批量提交叠加收益最大 |
| **置信度** | **已确认**（实测 40× 差距） |

### 🟠 P1-9 `upstream_semaphore_size` 默认 0（单 provider 无限并发）

| 项 | 内容 |
|---|---|
| **位置** | `src/botflow/config.py`（`upstream_semaphore_size` 默认 0）；`router.py` 的 `_provider_semaphores` |
| **现象** | 默认不限制单个 provider 的并发上游请求数（配置注释里已提示"惊群/雪崩"风险，但默认关闭） |
| **改法** | 生产按 provider 承载能力设一个上限（如 32~64），避免流量突发时把上游打到 429/超时连锁 |
| **置信度** | **已确认**（配置默认值）；是否需要 **取决于上游限流策略，待评估** |

---

## 三、生产遥测实录

### 3.1 延迟分位（全量 `call_logs`）

| 分位 | 全站 | `fast` 组 | `smart` 组 | `backup` 组 |
|---|---|---|---|---|
| p50 | 7,655ms | 7,881ms | 25,268ms | — |
| p90 | 30,298ms | — | — | 44,028ms |
| p95 | 47,152ms | — | — | — |
| p99 | 118,638ms | — | — | — |
| max | 3,320,171ms | — | — | — |

> **解读**：p50≈7.7s 而 p95≈47s，长尾极重。结合"平均 prompt 45.7K token"，**延迟主体是上游模型推理**，网关侧开销（毫秒级）可忽略。`smart` 组 p50 明显高于 `fast`，符合其挂载更大模型的预期。

### 3.2 成功率与错误

| 时间窗 | 调用数 | 失败数 | 成功率 |
|---|---|---|---|
| 全量历史 | 50,749 | 1,091 | 97.85% |
| **v3.0.0 之后（09-15 起）** | **5,077** | **3** | **99.94%** |

历史错误类型（**均已失效，非当前故障**）：

| 错误类型 | 数量 | 时间分布 | 状态 |
|---|---|---|---|
| `NameError: name 'get_config' is not defined` | 356 | **仅 2026-09-08** | 已修复（临时 bug） |
| `AllModelsCooldownError` | 344 | 09-05 / 09-13 | 历史 |
| `ProviderError` | 175 | 分散 | 上游侧 |
| `TypeError` | 8 | 分散 | 历史 |

> 超长请求：>300s 共 140 次（max 3320s），集中在 09-10/09-12、model 14；>30s 共 5,122 次。

### 3.3 数据库与进程

| 指标 | 值 |
|---|---|
| DB 文件 / 有效 / 空闲页 | 324MB / ~108MB / **216MB** |
| 最大表 | `call_logs` 92.8MB（有效空间 85.8%） |
| 索引 | 常用查询命中索引；`admin` 列表（无过滤）为全表 SCAN（分页，可接受） |
| `synchronous` | 2（FULL） |
| `sqlite_stat*` | **不存在**（从未 ANALYZE） |
| 进程 RSS / 线程 / FD | 347MB / 10 / 16 |
| TIME_WAIT 连接 | 2,388（外部连接，非本进程缺陷） |
| 规模 | 7 providers / 567 models / 5 groups / 1 api key |

---

## 四、实测：网关侧每请求开销逐阶段拆解

### 4.1 真实请求处理路径（Linux 生产机实测，单位 ms）

> 数据来自 `perf_path.py`（按 `json.loads` → `openai_to_internal` → `_generate_request_id` → `truncate_messages` 的真实顺序逐步计时）。

| 阶段 | p50 规模<br>(35.5K tok / 342KB) | p95 规模<br>(155K tok / 1.5MB) | p99 规模<br>(235K tok / 2.3MB) | 是否每请求 |
|---|---|---|---|---|
| `json.loads(raw_body)` | 1.01 | 2.79 | 5.64 | ✅ 是 |
| `openai_to_internal(body)` | 0.001 | 0.001 | 0.001 | ✅ 是（可忽略） |
| `_generate_request_id(body)` | 0.81 | 5.47 | 8.28 | ✅ 是 |
| `truncate_messages`（**ctx=0**，`fast`/`free`/`smart`/`fast-text`） | **0.001** | **0.001** | **0.001** | ✅ 但不耗 CPU |
| `truncate_messages`（**ctx=1e6**，仅 `backup`） | **21.06** | **79.90** | **142.35** | ⚠️ 仅 3.9% 流量 |
| `_openai_serialize`（每 chunk） | 0.0074 | — | — | 流式，逐 chunk |

- **占 96.1% 流量的 `fast`/`free`/`smart`/`fast-text` 组，每请求网关侧 CPU ≈ 2ms(p50) ~ 14ms(p99)**（json 解析 + request_id），`truncate_messages` 因全部端点 `context_window=0` 而直接早退。
- **仅 `backup` 组（3.9%）** 走 token 估算，单请求 21–142ms，全部落在 `estimate_tokens` 的纯 Python `_cjk_ratio` 逐字符循环上。
- **DB 读开销**（`list_api_keys` 3.5us、`list_groups` 10.3us、`find_groups_by_model_name` 6.5us、`get_config` 4.1us）均为**微秒级**，可忽略。

### 4.2 其余微基准

| 编号 | 用例 | 结果 |
|---|---|---|
| **A** | `estimate_tokens`（全量估算，仅 ctx>0 时） | p50 规模 25.3ms；p95 规模 **84.1ms(p95 102.4)**；p99 规模 **145.1ms(p95 166.1)** |
| **A'** | `truncate_to_context_window`（真截断时，二分多次估算） | ctx=64K：279–486ms；ctx=128K：439–486ms（p95/p99 规模） |
| **B** | `_generate_request_id`（按 body 规模） | p50 0.81ms；p95 5.47ms；p99 8.28ms |
| **C** | `request.is_disconnected()` | **46.63us/次** → 500 chunk≈23ms |
| **D1** | 单连接 SELECT | 112.9us/op |
| **D2** | 单条 INSERT+COMMIT（FULL） | **3.27ms** → ~306 写/秒 |
| **D3** | 50 并发 × 20 查询（单连接） | 1,000 查询 / 0.069s = 14,564/s，**但被单连接串行化，并发并不真正并行** |
| **D4** | commit：FULL vs NORMAL | FULL 3.44ms（291/s） vs **NORMAL 0.08ms（11,975/s）**，差 **~40×** |
| **E** | ANALYZE 检查 | `sqlite_stat*` 表缺失 |

> **关键洞察**：`db.py` 全项目共用**单个** aiosqlite 连接（WAL + `busy_timeout=5000`）。D1 单次查询 112.9us、D3 千次查询仍是串行，说明**所有 DB 操作在一个连接上排队**——读不是瓶颈，但写入（P0-2）叠加 FULL 同步（P1-8）后，吞吐天花板明确。

---

## 五、建议修复优先级

| 优先级 | 动作 | 预期收益 | 风险 |
|---|---|---|---|
| **1** | 修 `_provider_cache` 驱逐时关闭旧 client（+ `BaseProvider.aclose()`） | 消除确定性连接池泄漏 | 低（需注意不动 `_provider_semaphores`） |
| **2** | `create_call_log`/`save_cooldown_state` 改批量 `executemany` + 单次 commit | 写入吞吐提升数十倍 | 低 |
| **3** | 优化 `_cjk_ratio`/`estimate_tokens`（去逐字符循环 + 缓存） | 命中时单请求省 21–142ms（仅 `backup` 组） | 低 |
| **4** | 移除流式每 chunk 的 `is_disconnected()` | 每流请求省 ~23ms/500 chunk | 低（注意 ASGI spec 版本） |
| **5** | `request_id` 惰性计算 | 每请求省 0.8–8.3ms | 极低 |
| **6** | `synchronous=NORMAL`（配合 #2） | commit 快 40× | 低（断电或丢末尾数事务，不损库） |
| **7** | 低峰 `VACUUM` + 定期 `ANALYZE` | 回收 ~216MB、改善执行计划 | 中（VACUUM 持写锁，需低峰） |
| **8** | `resolve_api_key` 加缓存 | 减少每请求读库 | 低 |
| **9** | 给 provider 设并发上限 | 防突发雪崩 | 需评估上游限额 |

> 遵循项目「决策阶梯 / 能删不增」原则：**第 4、5 项属"删除冗余"，优先做**；第 1、2、3 项是修 bug 性质，改动小、收益确定。

---

## 六、待确认 / 未验证事项

1. **`_provider_cache` 泄漏的绝对量级**：需长跑压测或统计 FD/RSS 随时间曲线方可量化（当前 2 天 RSS 347MB 尚平稳）。
2. **`resolve_api_key` 每请求读库的真实耗时**：已补测 —— `list_api_keys` 中位 3.5us / p95 3.9us，**可忽略**（原判断"待验证"已结案）。
3. **移除 `is_disconnected()` 的前向兼容**：当前 ASGI spec=2.3 下冗余；若未来 uvicorn/Starlette 升到 spec 2.4，则需保留。建议改法带版本判断。
4. **`upstream_semaphore_size` 是否应开启**：取决于 OpenCode Go 等上游的限流策略，需与上游实测对齐。
5. **超长请求（>300s，max 3320s）**：本次仅归类时点，未逐条归因，建议后续单独排查（是否卡在特定 model 14 / 特定调用方）。
6. **`idx_call_logs_model_time`**：生产库存在但**源码 `CREATE_INDEXES_SQL` 中没有**（schema 漂移），建议核对来源。
7. **ctx=0 的上下文保护缺口**（复核时新发现）：占 96% 流量的分组所有模型 `context_window=0`，`truncate_messages` 直接早退 → 超长 prompt 原样透传上游。是否需为这些模型补 `context_window` 以启用保护，取决于上游是否强制长度上限。

---

## 七、审计方法与脚本

| 脚本（本地 `.workbuddy/tmp/`） | 用途 |
|---|---|
| `perf_telemetry.py` | 生产遥测：进程/FD/SQL pragma/索引/按组按模型的延迟分位/吞吐/错误类型/EXPLAIN QUERY PLAN |
| `perf_errors.py` | 错误分类下钻、>300s 请求、dbstat 空间占用 |
| `perf_extra.py` | 逐日成功率与延迟、09-15 后窗口、EXPLAIN 计划、表大小、峰值速率 |
| `perf_bench.py` | 微基准 A–E（token 估算 / request_id / is_disconnected / aiosqlite 单连接与同步模式 / ANALYZE 检查） |
| `perf_overhead.py` | **逐阶段拆解**：三档规模下 `estimate_tokens` / `_generate_request_id` / `truncate@ctx` + DB 读开销 |
| `perf_path.py` | **真实路径复刻**：`json.loads` → `openai_to_internal` → `request_id` → `truncate_messages`(ctx=0 vs ctx=1e6) + 每 chunk 序列化 |
| `perf_ctx.py` | **触发条件核对**：各分组挂载模型的 `context_window` 实况 + prompt_tokens 分位 + 分组流量分布 |

> 所有脚本经 `scp` 上传至 `api.vxquant.com` 就地运行，只读取证，不写业务库。
