# botflow 运行效能提升方案（P9）

- **目标版本**：`3.0.0` → `3.1.0`（行为修复级；版本号 5 处硬编码需同步）
- **依据**：`docs/efficiency-audit-2026-09-17.md`（源码静态审计 + 生产遥测 50,749 行 + Linux 端微基准三路取证）
- **性质**：**方案评审稿 —— 未改动任何代码 / 配置 / 数据库**
- **原则来源**：`AGENTS.md`「决策阶梯」「规则清单」「双 Agent 流程」

---

## 一、结论与总账（TL;DR）

> **本期不靠"加功能换性能"，只做三件事：删冗余、修确定性缺陷、清容量天花板。**
> 改动集中在 6 个文件，预计**净减代码**；不引入新硬依赖。

| 维度 | 现状基线 | 本期目标 | 归属批次 |
|---|---|---|---|
| 96% 流量（ctx=0 分组）每请求网关 CPU | 2ms(p50) ~ 14ms(p99) | 再降 **1–8ms**（去掉流式白算 `request_id`） | 批 1 |
| 流式每请求 CPU | 每 chunk 46.6us × chunk 数（500 chunk ≈ 23ms，长流 90ms+） | **≈0**（删掉冗余探测） | 批 1 |
| `backup` 组每请求（ctx=1e6） | 21–142ms（真截断时 279–486ms） | **<5ms**（去逐字符循环 + 缓存 + 短路） | 批 3 |
| DB 写入吞吐 | **306 行/秒**（FULL 同步 + 逐行 commit） | **×10 以上**（批量 commit + NORMAL） | 批 2 |
| DB 体积 | 324MB（**216MB 是空洞**） | 回收 ~216MB + 建立定期维护 | 批 5 |
| 确定性泄漏 | provider 缓存过期不关旧 client | **归零** | 批 2 |

**三条铁律（违反任一条即打回）：**

1. **绝不改变写给上游的字节。** group1 前缀缓存命中 **86.3%**、group3 **92.9%**——任何"改变历史消息字节"的优化（压缩/重排/去重/截断）都必须先证明不打穿缓存，否则"缓存读价→全价"的损失远大于省下的 token。
2. **先删后加**（决策阶梯）。不新增可避免的依赖 —— 唯一例外是用户拍板的 `tiktoken`（见决策点 D4，2026-09-17 已定：**进硬依赖**）。
3. **每批独立可回滚**，且性能批必须给出 bench 前后对比（脚本已就绪，见附录 A）。

**明确不做（含理由）：**

| 不做 | 理由 |
|---|---|
| headroom 类上下文压缩 | 用生产真实请求实测**只省 2.30%**（121 条消息仅 6 条被改）；散文/中文/代码压缩率 **0%**；且 library 模式有损不可逆。详见 `docs/headroom-evaluation-2026-09-17.md` |
| 网关内同步调 LLM 做摘要 | 延迟翻倍、不可控、摘要本身也占上下文窗口；已有 `storage/daily_summary.py` 在批处理侧 |
| ~~`tiktoken` 进硬依赖~~ **已于 2026-09-17 由用户拍板改为采纳**（见 D4 与 P9-6 状态） | 用户明确要求"用 tiktoken，准确一些、速度也快"。原"先纯标准库"的顾虑（决策阶梯第 2/5 级）被实测数据推翻：纯标准库方案只能改善**速度**，改善不了**精度**（误差 −51% ~ +34% 是算法问题，不是实现问题）。 |
| 任何"重排 / 改写历史消息"的优化 | 直接威胁前缀缓存（铁律 1） |

---

## 二、基线钉桩（改之前先固定参照）

| 指标 | 基线值 | 测量脚本 |
|---|---|---|
| `json.loads(body)` | 1.01 / 2.79 / 5.64 ms（p50/p95/p99 规模） | `perf_path.py` |
| `_generate_request_id`（含流式白算） | 0.81 / 5.47 / 8.28 ms | `perf_path.py` |
| `truncate_messages`（ctx=0，96% 流量） | **0.001 ms**（早退） | `perf_path.py` |
| `truncate_messages`（ctx=1e6，3.9% 流量） | **21.1 / 79.9 / 142.4 ms** | `perf_path.py` |
| `request.is_disconnected()` | **46.63 us/chunk** | `perf_bench.py` C |
| 单条 `INSERT + COMMIT`（FULL） | **3.27 ms** → 306 写/秒 | `perf_bench.py` D2 |
| commit：FULL vs NORMAL | 3.44ms vs **0.08ms**（**~40×**） | `perf_bench.py` D4 |
| DB 读（`list_api_keys`/`list_groups`） | 3.5us / 10.3us（**可忽略**） | `perf_path.py` |
| 端到端延迟 | p50 7.66s / p95 47.2s（**上游模型主导**） | 生产遥测 |
| 成功率（v3.0.0 之后） | **99.94%**（5077 次 / 3 失败） | 生产遥测 |

> 结论：**网关不是当前延迟的来源**。本方案的目的不是"把 7.7s 变成 1s"（不可能），而是**消除确定性缺陷、拆掉容量天花板、并为将来的上下文保护留出开销空间**。

---

## 三、批次计划

### 批 0 · 前置收口（非性能，但必须先行）

| 项 | 内容 |
|---|---|
| 现状 | HEAD = `2c728be`（`v3.0.0-5-g`），**领先 `origin/main` 1 个提交未推**；工作树另有 fix-restart 未提交改动：`src/botflow/cli/service.py`(+49/−19)、`tests/test_cli_service.py`、新增 `src/botflow/__main__.py`、`tests/test_cli_restart.py` |
| 动作 | 先收口 fix-restart（跑测 → 提交 → 推送），再开 P9 |
| 理由 | P9 会改 `core.py`/`db.py`/`router.py`。与未收敛的改动混在一起，回归无法归因 |

### 批 1 · 删冗余（**零行为变化**，收益确定 —— 建议最先做）

| 编号 | 位置 | 改法 | 预期收益 | 风险 |
|---|---|---|---|---|
| **P9-1** | `core.py:1158`（`async for chunk in _chain_first(...)` 内） | 删除逐 chunk 的 `await request.is_disconnected()`，改由框架负责 | 流式请求省 **20–100ms**；同时消除与 `StreamingResponse` 在同一个 `receive()` 通道上的争抢 | 低。当前栈 ASGI `spec_version=2.3`，Starlette 已启动 `listen_for_disconnect`（uvicorn `h11_impl.py:207`）。**需加集成测试**：客户端中断 → 上游生成器在 N ms 内被关闭 |
| **P9-2** | `core.py:775` | `request_id` 惰性计算：流式分支不计算，或统一改为"仅日志落库前按需计算" | 每请求省 **0.8ms(p50) ~ 8.3ms(p99)** | 极低。注意 `_request_ctx` 与 `_log_call` 的取值链（`core.py:682-686`）需保持一致 |

### 批 2 · 修确定性缺陷

| 编号 | 位置 | 改法 | 预期收益 | 风险 |
|---|---|---|---|---|
| **P9-3** | `providers/base.py:25`（`BaseProvider` 无 `aclose()`）、`router.py:192` `_get_cached_provider`（L225 覆盖式写入）、`router.py:263/268/282` 三个 `invalidate_*` | ① `BaseProvider` 加 `async def aclose()`（基类空实现）；`OpenAICompatProvider` 关闭其惰性创建的 client；② `_provider_cache` 驱逐/失效路径统一走 `aclose()` | 消除**确定性连接池泄漏**（FD/RSS 单调增长） | 低，但**改造点在于"同步失效 vs 异步关闭"**（见决策点 D2）。红线：`invalidate_*` **永远不得清 `_provider_semaphores`** |
| **P9-4** | `db.py:763` `create_call_log`（L781 逐行 commit）、`core.py:152-164` `_flush_unlocked`（逐条循环）、`db.py:306` `save_cooldown_state`（逐 key 调 `set_config`，每次 commit） | 新增 `create_call_logs_bulk(entries)`：`executemany` + **一次 commit**；`_flush_unlocked` 改调它；`save_cooldown_state` 同样一次 `executemany` | 写入吞吐从 **306/s** 提升至 **×10 以上**（与 P9-5 叠加约 40×） | 低。保留单条 `create_call_log` 供兼容调用（不删公开方法） |
| **P9-5** | `db.py:240-242`（`_ensure_connection` 的 PRAGMA） | 增加 `PRAGMA synchronous=NORMAL`；启动后执行一次 `ANALYZE` | commit 快 **~40×**；查询规划器获得统计信息 | 低（WAL 下断电仅可能丢末尾若干事务，**不损库**）。见决策点 D3 |

### 批 3 · CPU 与精度（**P9-7 严格依赖 P9-6**）

| 编号 | 位置 | 改法 | 预期收益 | 风险 |
|---|---|---|---|---|
| **P9-6** ✅ **已完成**（2026-09-17，未提交） | `common/context.py`；`pyproject.toml:28` | **实际改法（与原计划不同，已按 D4 决策调整）**：① 用 `tiktoken`（`o200k_base`）真分词替换 `4 - cjk_ratio*2` 启发式；② **删除** `_cjk_ratio` 死代码；③ `_encoding()` 用 `lru_cache` 惰性单例（避免启动即加载词表）；④ 快路径改为 `_token_upper_bound()` **上界短路**（严格上界 = Σ(字节数 + 每条固定开销)，故无需编码即可判定"肯定不超预算"）；⑤ **不**做内容 hash 缓存（measure first，见"跳过了什么"） | 精度：真实 agent 载荷误差 **−17.5%**（旧 −51% ~ +34%）；速度：长英文 28.7→**5.3ms**、真实 agent 载荷 33.6→**22.9ms**；短路路径**完全不编码** | 低（**不改消息字节**）。产物：`docs/tasks/P9-6_features.md`、`P9-6_tests.md`、`P9-6_review.md`；`tests/test_context.py` 33 passed / `context.py` **100% 覆盖** |
| **P9-7** ⛔ **仍待 D5 拍板**（占 96% 流量，现在解除了"前置未完成"的阻塞，但**必须先答上游是否有硬长度上限**） | 同上链路 + DB `models.context_window` | 给 ctx=0 的分组补 `context_window`（或加一个全局兜底上限） | 让占 **96% 流量**的分组也有上下文保护；P9-6 已就位，开启后每请求成本 = **一次真 BPE 编码**（不再有逐字符循环的 20–140ms） | ⚠️ **中高**：一旦生效就会**真的开始截断** → 改变发给上游的字节 → 影响 86.3% 前缀缓存 + 上游行为。见决策点 D5 |

> **P9-6 是 P9-7 的前置 —— 已解除**：原风险是"先开保护会让 96% 流量背上 20–140ms 的逐字符循环"，P9-6 落地后该成本已降为一次 Rust BPE 编码（约 5–23ms/大请求）+ 未超限时零编码。
>
> **⚠️ P9-6 引入了两条新的部署前置条件（发版前必须处理）**：
> 1. **词表预置**：`o200k_base` 首次调用需联网下载并缓存于 `TIKTOKEN_CACHE_DIR`。生产机（Linux，无外网时）首次请求会阻塞下载或失败 → **镜像里需预置该缓存**，或确认生产出网可达。
> 2. **`backup` 组截断行为会变**：旧启发式系统性**低估**中文（−40.6%）→ 现在按真实分词判定，同一 prompt **更容易触发截断**。属预期收益（消除真实超窗风险），但需回归验证。

> **P9-6 是 P9-7 的前置**：当前 ctx=0 早退，`estimate_tokens` 的开销被掩盖。若先开保护再优化，96% 的流量会立刻背上 20–140ms/请求。

### 批 4 · 可观测性（不提升速度，但决定"以后能不能查"）

| 编号 | 位置 | 改法 | 价值 | 风险 |
|---|---|---|---|---|
| **P9-8** | `core.py:660-705` `_log_call`（构造 `CallLog` 时**从不传** `tool_calls`）、`db.py:763` 的 `tool_calls` 列 | 明确该列语义（建议：本轮请求中的 `role=tool` 消息数 + `extra.tools` 定义数），并真正写入 | 该列实测 **50,787 行全为 NULL**——**是日志缺失，不是"没有工具流量"** | 低（需先定语义，见决策点 D6） |
| **P9-9** | `core.py:917` `_request_summary`（`full=False` 时截 2,000 字符）、`core.py:82` `SAFE_EXTRA_KEYS`、`:96` `_filter_safe_extra` | 只改 **JSON 键序**（把标量与 `extra` 前置、`messages` 后置），不增长度 | 现在 2,947 条截断样本里只有 **55 条**能看到 `model` 键，**98% 截在 messages 数组内部**；调序后日志立刻可诊断 | 低（不改上游字节，只改落库文本） |
| **P9-10** | `core.py:82` `SAFE_EXTRA_KEYS` | 白名单补**驼峰变体**（如 `promptCacheKey`），或在入口做键名归一化 | 生产客户端 MiMoCode 发的 `promptCacheKey` **被静默丢弃**（不报错、不告警）——若上游支持，放行可能**提升**前缀缓存命中 | 低，但属"行为变更"，见决策点 D7 |
| **P9-11** | `protocol_adapter.py:20-27` 入口切分 + `_shared.py:168` 截断 + `core.py:917` 日志 | 在 `*_to_internal()` 里一次性产出 `RequestParts`（system / history / **tools** / 其他），挂到 `internal["_parts"]` 供下游共用 | 现在 **`tools` 完全不进 token 计量**：小样本实测**漏算 71.8%**；MCP 工具集常 20–50KB。且 `role=tool` 与 user 消息同权、`image_url` 计 0 token | 中。**只"计量"不"压缩"**（压缩工具描述会改变模型行为）；且 `tools` 是纯 ASCII JSON，**必须绕开 CJK 统计**（直接 `len/4`） |

### 批 5 · 容量与运维

| 编号 | 位置 | 改法 | 预期收益 | 风险 |
|---|---|---|---|---|
| **P9-12** | `config.py:47` `upstream_semaphore_size`（默认 0 = 无限并发） | 生产按上游承载设上限（建议 32–64） | 防突发惊群把上游打到 429/超时连锁 | 需上游限流事实，见决策点 D8 |
| **P9-13** | DB 维护 | ① 低峰 `VACUUM`（回收 **216MB**）；② `ANALYZE` 纳入定期维护；③ 失败请求的 `request_body` 加上限（现为全文，超大 prompt 可达 2MB+） | 回收 ~216MB；抑制空洞再生；单次失败写盘从 MB 级降到百 KB 级 | 中：`VACUUM` 持写锁、需等量临时空间 → **必须低峰 + 备份后执行**；③ 会让"失败取全文"的诊断能力打折，需权衡 |
| **P9-14** | 生产 DB vs 源码 `CREATE_INDEXES_SQL`（`db.py:152`） | 核对 schema 漂移 | 生产库存在 `idx_call_logs_model_time`，**源码里没有** | 低 |
| **P9-15** | `src/` 目录卫生 | 清理 `src/botflow/**/*.cover`（26 个，覆盖率注解残留，**均未入库**）与 `__pycache__`，并把 `covout*/` 之类的例外写进 `.gitignore`（如缺） | 减少 `src/` 噪音、避免误入库 | 低 |

---

## 四、验收口径（每批都必须过）

| 项 | 要求 |
|---|---|
| 流程 | 按 `AGENTS.md` 双 Agent：编码子 agent 出 `docs/tasks/P9-x_features.md`（功能点 + 用例清单，覆盖正例/反例/边界值）→ 验证子 agent 审核并并行写 `_tests.md` + `_review.md` → 主 agent 集成 + 验收 |
| 覆盖率 | 单元测试 **100%**；不可覆盖行标 `# UNCOVERED: [原因]` |
| 集成测试 | **必做**。批 1 必须有"客户端中断流式 → 上游生成器被关闭"的用例；批 2 必须有"批量写入真正落库"的用例 |
| 跑测环境 | **本机跑不了 async 测试**（`AF_UNIX=False` → `socketpair()` 走 loopback TCP 挂死）→ async 一律在 **Linux 远端隔离环境**跑（`uv venv /tmp/x` + `PYTHONPATH=/tmp/copy/src pytest`），**绝不在部署目录跑 pytest** |
| 性能证据 | 每批附 `perf_bench.py` / `perf_path.py` 的前后对比（同一脚本、同一机器） |
| 回归红线 | ① `/openapi.json` 正常；② Admin body 契约不变（create 嵌 `req` / PATCH 扁平）；③ `invalidate_*` 不清 `_provider_semaphores`；④ **发给上游的字节不变**（批 3 的 P9-7 除外，且需单独评审） |

---

## 五、建议排期（小步快跑）

| 批次 | 内容 | 风险 | 提交形态 |
|---|---|---|---|
| **第 0 步** ⚠️ **当前卡点** | **批 0：先收口**。工作树挂着**未提交的 `fix-restart`**（`src/botflow/cli/service.py`、`src/botflow/__main__.py`、`tests/test_cli_service.py`、`tests/test_cli_restart.py`、`docs/tasks/fix-restart_*`）与**本次 P9-6**（`pyproject.toml`、`src/botflow/common/context.py`、`tests/test_context.py`、`docs/tasks/P9-6_*`），且 HEAD `2c728be` **领先 `origin/main` 1 个提交未推**。**两者必须先各自独立提交**，否则无法按批回滚、性能改动无法归因 | — | 两次独立提交：① `fix-restart` ② `P9-6` |
| **第 1 步** | 批 1（P9-1/2，纯删除，零风险） | **极低** | 独立提交 |
| **第 2 步** | 批 2（P9-3/4/5 三个缺陷） | 低 | 一次提交（或拆 3 次） |
| **第 3 步** | 批 3 的 **P9-7**（需先答 D5）+ 批 5（P9-13/14/15） | 中 / 低 | 分开提交。（**P9-6 已完成，见批 3 状态**） |
| **第 4 步** | 批 4（P9-8~11，可穿插） | 低 | 逐项提交 |
| 发版 | 升 `3.1.0`（**5 处版本号**：`pyproject.toml:3`、`src/botflow/__init__.py:3`、`core.py` `FastAPI(version=)`、`docs/design.md:3`、`static/admin/index.html`）+ tag + 部署 | — | 按「生产原地升级」脚本走。**发版前必须处理 P9-6 的两条部署前置**（词表预置 / backup 组截断回归） |

---

## 六、需要拍板的决策点

| # | 决策 | 选项 | 建议 |
|---|---|---|---|
| **D1** | 是否从批 1 起步 | ① 先做批 1（零风险）② 三批一起 ③ 只做批 2 | **①** |
| **D2** | P9-3 的关闭时机 | ① 把 `invalidate_*` 改成 `async`（显式，5 处调用点改 `await`）② 保留同步 + 退役队列，由后台任务关闭 | **②**（改动更小，不动 Admin 调用点） |
| **D3** | 是否接受 `PRAGMA synchronous=NORMAL` | ① 接受 ② 保持 FULL | **①**（日志类写入，断电丢末尾数条、不损库） |
| **D4** | 是否引入 `tiktoken` | ① 纯标准库（先） ② 直接上 `tiktoken` | ✅ **用户已定：②（2026-09-17）** —— "用 tiktoken 库吧，准确一些，速度也快。" 走**硬依赖**（非可选 extra），理由同 P6 的 `langgraph`：「未安装则静默降级」= 精度无声退化、无法被监控发现。**原"先纯标准库"的建议已被实测推翻**：标准库只能改速度，改不了 −51% ~ +34% 的精度问题。<br>**已知近似**：`o200k_base` 只是 DeepSeek 的**代理**词表（DeepSeek 未公开 tiktoken 词表）→ 可宣称"更准"，**不可宣称"精确"**。<br>**新增部署前置**：首次使用需联网拉词表（缓存 `TIKTOKEN_CACHE_DIR`）；`encode_ordinary` 取代 `encode` 以防特殊 token 字面量抛错。 |
| **D5** | 是否给 96% 流量的分组补 `context_window` | ① 补（开启截断保护）② 暂不补 | 需你先确认**上游是否有硬长度上限**：若无，补了反而白改字节、伤前缀缓存 |
| **D6** | `tool_calls` 列的语义 | ① `role=tool` 消息数 ② `extra.tools` 定义数 ③ 两者都记（JSON） | **③** |
| **D7** | 是否放行驼峰 `promptCacheKey` | ① 补进白名单 ② 入口做键名归一化 ③ 维持现状（丢弃） | **②**（归一化更通用） |
| **D8** | 上游并发上限值 | 需 OpenCode Go 等上游的限额事实 | 先探测再定 |

---

## 附录 A · 可复用为回归 bench 的脚本（已就绪，本地 `.workbuddy/tmp/`）

| 脚本 | 用途 |
|---|---|
| `perf_bench.py` | 微基准 A–E：token 估算 / `request_id` / `is_disconnected` / aiosqlite 单连接与同步模式 / ANALYZE |
| `perf_path.py` | 真实请求路径逐阶段计时（`json.loads` → 切分 → `request_id` → `truncate`） |
| `perf_overhead.py` | 三档规模下的 `estimate_tokens` / 截断 / DB 读开销 |
| `perf_telemetry.py` | 生产遥测：进程 / FD / PRAGMA / 索引 / 延迟分位 / 吞吐 / 错误类型 / EXPLAIN |

> 所有脚本经 `scp` 到 `api.vxquant.com` 就地运行（只读，不写业务库）。

## 附录 B · 环境侧注意事项（与本方案相关但不属于代码改动）

| 项 | 事实 | 影响 |
|---|---|---|
| **本机 E: 卷慢 60–150×** | 300 个 8KB 文件：E: 创建 12.5s / 删除 13.1s，C: 0.20s / 0.11s（≈**40ms/文件**）；E: 是 NVMe 却比 SATA 的 C: 慢 100 倍 → **软件拦截**（已装腾讯电脑管家系统防护，Defender RTP 已禁用）。另有文件出现"可读但写/改名/删除被拒、ACL 正常" | 拖慢**本地** `uv sync`/pytest/`git status`。建议把 `E:\src`、`E:\utils` 加入信任/排除目录 |
| **生产机 load 12–14** | 4 核，`tailscaled` 占 **98% CPU / 3.0GB RSS**、`nats-server` 46.6%；botflow 自身 1.1% CPU | **压测前先处理它**，否则尾延迟数据不可信 |
| 本机 async 测试不可执行 | `AF_UNIX=False` 导致事件循环挂死 | 性能验证一律上 Linux 远端 |
