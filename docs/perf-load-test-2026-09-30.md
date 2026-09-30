# botflow 性能压测报告（2026-09-30）

> 环境：mq3（2 核 / 3.9GB，tailnet 100.88.88.88）隔离实例 `/tmp/bt_bench`（端口 4100，独立 DB），**不动 `/srv/botflow` 生产实例**。  
> 被测版本：`307c41b`（v3.1.0 + 级联/改密码，与生产 `10ad192` 同核心链路）。  
> 压测端：同机 asyncio/httpx 负载生成器（共享单 client，分位统计 + /proc RSS/FD/CPU 逐秒采样）。  
> 上游：OpenAI 兼容 mock（端口 4200，`delay_ms` / `chunks` / `chunk_ms` 可热切）。  
> 数据：`qbot@100.88.88.88:/tmp/bt_bench/results/*.json`。

## 一、结论速览

1. **吞吐饱和点 ≈ 70–76 rps（非流式）**，瓶颈是**网关单进程 CPU 打满一个核**（不是 DB、不是上游、不是内存）。
   - 上游 50ms（S1）与 0ms（S2）两组饱和吞吐几乎相同（70.7 vs 75.8）→ 上游延迟不是约束，网关自身每请求 CPU 成本 ~13ms 才是。
2. **C≥64 后吞吐不升反降**（p95 从 0.5s 恶化到 3.4s/6.9s）→ 超发只排队，不增加吞吐。
3. **流式 TTFB 表现良好**：C=1 p50=13ms；C=8 p50=118ms。但流式整体吞吐 ~9.5 rps（mock 每流 100 chunk×5ms≈500ms，属上游节奏），TTFB 随并发线性抬升（C=32→583ms，C=64→1153ms），同样是 CPU 排队所致。
4. **长跑资源零泄漏**：RSS 151MB → 151MB（0 MB/min）、FD 24 → 24（0/min），11 分钟 / 29,882 请求无增长 → 既有 P0-1（provider 缓存过期不关旧 client）在本场景（单 provider、缓存不过期）**未被触发**，泄漏面未覆盖，不能据此证伪。
5. **写入链路不是瓶颈**：40s 风暴中 call_logs 落库 72.6 行/s ≈ 请求速率（5s 批量 flush 正常跟进），P0-2（逐行 commit）在当前量级无感；WAL 23MB / 87MB DB 稳定。
6. ⚠️ **新发现：连续负载 ~1 分钟后每请求 CPU 成本翻倍**（吞吐 79→40/s，CPU 恒定 ~100% 单核），可稳定复现、与进程年龄无关、清 dedup 键无效 → 详见 §五。

## 二、场景与结果

### S1 非流式阶梯（上游 delay=50ms，tiny payload，各 20s）

| C   | rps  | p50 (ms) | p95 (ms) | p99 (ms) | 网关 CPU（占单核） | 结论                      |
| --- | ---- | -------- | -------- | -------- | ----------- | ----------------------- |
| 1   | 13.5 | 66       | 70       | 84       | ~18%        | 串行基线：50ms 上游 + ~16ms 网关 |
| 8   | 62.8 | 107      | 133      | 261      | ~91%        | 接近饱和                    |
| 32  | 70.7 | 362      | 548      | 653      | ~97%        | **饱和点**                 |
| 64  | 48.6 | 818      | 3391     | 4900     | ~81%        | 超发劣化                    |
| 128 | 37.2 | 2150     | 6900     | 7649     | ~63%        | 严重排队                    |

### S2 非流式阶梯（上游 delay=0ms，纯网关开销，各 20s）

| C   | rps  | p50 (ms) | p95 (ms) | 网关 CPU | 结论                    |
| --- | ---- | -------- | -------- | ------ | --------------------- |
| 1   | 54.6 | 14.4     | 18.6     | ~73%   | **单请求纯网关开销 p50≈14ms** |
| 8   | 70.3 | 90       | 116      | ~99%   | 接近饱和                  |
| 32  | 75.8 | 344      | 498      | ~99%   | ***饱和点 75.8 rps***    |
| 64  | 58.0 | 800      | 2644     | ~87%   | 超发劣化（5 个连接错误）         |
| 128 | 36.1 | 2296     | 6844     | ~61%   | 严重排队（5 个连接错误）         |

> S1/S2 饱和吞吐几乎一致（70.7 vs 75.8）→ **吞吐上限由网关每请求 CPU 成本决定**，与上游延迟无关。  
> 换算：75.8 rps 时单请求 CPU ≈ 1000/75.8 ≈ **13.2ms**。

### S3 流式（100 chunk × 5ms ≈ 500ms/流，各 20s）

| C  | 完成流/s | TTFB p50 (ms) | 总耗时 p50 (ms) | 网关 CPU |
| -- | ----- | ------------- | ------------ | ------ |
| 1  | 1.6   | 13.2          | 553          | ~33%   |
| 8  | 9.3   | 118           | 761          | ~97%   |
| 32 | 9.5   | 583           | 3047         | ~100%  |
| 64 | 9.9   | 1153          | 5715         | ~100%  |

- C≥8 后完成流速率锁死在 ~9.5–9.9/s（受 CPU 饱和约束；mock 每流固有 500ms 也限制上限）。
- **TTFB 随 C 线性放大**（每 +24 并发 ≈ +500ms）→ 首包前的网关处理在排队。

### S4 写入风暴（C=32，40s，持续落库量化）

- rps 73.3，p50 368ms / p95 546ms；call_logs 落库 **72.6 行/s ≈ 请求速率**（5s 批量 flush 无积压）。
- DB 21.5MB + WAL 10.5MB（压测结束时）→ 写入链路**当前量级无瓶颈**。

### S5 长跑（C=8，660s，29,882 请求，零错误）

| 指标      | 起点     | 终点     | 斜率              |
| ------- | ------ | ------ | --------------- |
| VmRSS   | 151 MB | 151 MB | **0.00 MB/min** |
| FD      | 24     | 24     | **0.00 /min**   |
| Threads | 8      | 8      | 0               |

- 11 分钟连续负载**无内存/FD 泄漏迹象**。
- 注：本场景单 provider、无 admin 改动 → P0-1 的「缓存过期重建 client」路径未走到，**不能据此证伪 P0-1**。

## 三、CPU 归因（py-spy，30s 采样 / 2461 样本）

inclusive 占比（栈中出现即计）：

| 路径                                     | 占比         | 说明                       |
| -------------------------------------- | ---------- | ------------------------ |
| `pipeline/langgraph_engine.py`         | 74.5%      | 业务主链路（try_call→call_llm） |
| uvicorn + starlette + fastapi 中间件链     | ~50%       | 框架调度固定开销                 |
| `langgraph` 运行时（runnable+pregel+graph） | ~56%       | **单组执行也要走完整图调度**         |
| openai SDK + httpx2 + httpcore         | ~32% / 25% | 上游客户端序列化与网络              |
| botflow `core.py`                      | 49.3%      | 入口 + 驱动 + 落库             |

botflow 自身函数 inclusive TOP：

| 函数                                                 | 占比                    |
| -------------------------------------------------- | --------------------- |
| `core.chat_completions`                            | 40.1%                 |
| `langgraph_engine.try_call`                        | 39.8%                 |
| `_shared.call_llm` / `openai_compat.chat`          | 39.4% / 39.1%         |
| `core._handle_chat_non_stream` / `_drive_chat`     | 37.8% / 37.5%         |
| `engine.run` / `langgraph_engine.run` / `_run_one` | 34.4% / 34.2% / 34.1% |
| `storage` 系列（list_groups/get_group/…）              | 各 0.6–1.0%            |
| `_log_call` / `_cache_request_result` / dedup      | 各 ≤1%                 |

**解读**：每请求 ~13ms CPU 中，

- **~35–40% 在 LangGraph 图调度**（单组执行仍付完整 pregel/runnable 开销）——SG-1 把图瘦身后调度成本仍在；
- **~32% 在 openai SDK/httpx 客户端路径**（含 json 序列化、URL 构造——self 时间里 `__init__`/`write`/`urlparse` 靠前）；
- **~50% 是 ASGI 框架链**（uvicorn→proxy_headers→CORS→exceptions→routing，与业务无关的固定开销）；
- DB/storage 合计 <3%，**db.py 不是热点**（与 S4 结论互证）。

self 时间 TOP：`__init__` 7.5%（pydantic/对象构造）、`write` 4.5%、`_async_transform_recursive` 1.8%、`__hash__`/`__new__`/`get_origin` 等类型检查合计 ~5% → **对象构造与类型内省是最大的单点可优化簇**。

## 四、对照既有审计（efficiency-audit-2026-09-17）

| 既有项                           | 本次压测验证结论                                                                          |
| ----------------------------- | --------------------------------------------------------------------------------- |
| P0-2 逐行 commit                | S4 落库 72.6 行/s 跟得上请求速率，当前量级无感；吞吐上限与写库无关。量级上去后仍是隐患（`call_attempts` 才用 executemany） |
| P0-1 provider 缓存不关旧 client    | 本场景未触发（RSS/FD 零增长），**待长跑 + provider 切换场景专项验证**                                    |
| P0-4 `is_disconnected()` 多余调用 | 非流式路径未见热点；流式未单独归因（TTFB 排队主导）                                                      |
| P1-5 `request_id` 分流前白算       | dedup 哈希占比 ≤0.4%，非瓶颈（但与 P1-6 同属每请求固定开销）                                           |
| P1-6 每请求重查 api_keys           | `resolve_api_key` inclusive 0.93%，非瓶颈                                             |
| DB 单连接串行                      | 未构成瓶颈（storage 合计 <3%）                                                             |

## 五、新发现：连续负载 ~1 分钟后吞吐腰斩（⚠️ 根因已定位，非代码缺陷，见 §九）

> **【2026-09-30 修订】** 本节原"每请求 CPU 翻倍"的推理已被 A1 根因定位（R5–R13，10 轮复现 + 3 轮判别）**完全证伪**。真实根因是压测机 mq3 = GCE `e2-medium` 突发机型的 CPU token bucket 节流：满载 2.0 核约 115s 后 deschedule 回基线 1.0 核，可用 CPU 减半导致吞吐腰斩；"每请求 CPU 翻倍"是 guest ktime 记账把 vCPU 停摆记成"在跑"的**幻影**。botflow 代码无需为此修改。完整证据与判别见 `docs/tasks/A1_root_cause_perf_step.md`。以下保留原始观测记录供对照。

**现象**（S5 与 s5b 两次独立长跑均复现）：

| 分钟      | insert/s（≈rps） | 网关 CPU（占单核）      |
| ------- | -------------- | ---------------- |
| min1    | 71.7 / 78.9    | ~91–99%          |
| min2    | 44.4 / 46.0    | ~96–98%          |
| min3–10 | 稳态 37–40       | **~97–101%（不变）** |

**已排除（代码侧原因，仍成立）**：

- py-spy 采样干扰（s5b 无 py-spy 照样复现）；
- dedup 键堆积（清空 34,154 键后 68.0 → 70.0 rps，无差异）；
- 进程年龄（网关跑了 30+ 分钟后 ctl_180 前 120s 仍 85/s 再突降）；
- 网关日志无异常（仅 9 次压测客户端 `ClientDisconnect`）。

**原推理（已证伪，留档）**：CPU 恒定 100% 而吞吐腰斩 → 误判"每请求 CPU 成本从 ~12.6ms 涨到 ~25ms"，多出的一倍 CPU 花在某个随连续负载累积/切换的路径上。→ **实际是可用 CPU 减半（ktime 幻影记账），非每请求成本上升**（见 §九）。

**压测自身踩坑记录**（对产品无直接影响，但值得记录为平台行为）：

- `RateLimitMiddleware` 硬编码 **300 次/分钟/key**（core.py:579）且不可配置 → 压测需改 bench 副本；**生产同样受此限**，若真实流量 >5 rps/key 会开始收 429。
- **相同 payload 300s 去重**（core.py:842）：一字不差的重复请求直接回缓存不打上游 → 压测必须每请求加唯一 nonce；对下游也是隐性语义（重试同一 body 会拿到旧结果）。

## 六、快/慢两相差分（py-spy，50Hz × 15s/相）

实验：300s 持续负载中 t=20s（快相，~74 rps）与 t=240s（慢相，~31 rps）各采一次 py-spy；同时段 mock 直连 RTT（快 1.3–2.7ms vs 慢 1.4–2.3ms）与 TIME_WAIT（30 vs 42）**均无差异** → 排除上游与网络栈。

inclusive diff（慢相 − 快相，百分点）：

| 路径                                  | 快相    | 慢相    | Δ          | 函数级                                                      |
| ----------------------------------- | ----- | ----- | ---------- | -------------------------------------------------------- |
| `langgraph/pregel/_loop.py`         | 14.1% | 20.0% | **+5.9pp** | `tick` +5.4pp                                            |
| `langgraph/pregel/_algo.py`         | 8.3%  | 12.4% | **+4.1pp** | `prepare_next_tasks` +4.7pp、`prepare_single_task` +4.1pp |
| `langgraph/_internal/_config.py`    | 2.2%  | 5.6%  | **+3.3pp** | `merge_configs` self **+4.2pp**（self 涨幅最大）               |
| fastapi/starlette 框架链               | ~47%  | ~43%  | −4pp       | 相对占比下降（被 pregel 挤占）                                      |
| `pregel/_retry.py::arun_with_retry` | 11.1% | 8.1%  | −3.0pp     | —                                                        |

每样本栈结构：`tick` 出现 0/1 次分布（快 28/623=4.5% 样本含 tick → 慢 64/644=10.0%），`merge_configs` 0.5%→4.8%；**无嵌套 tick**（每栈最多 1 次）、**无 gc.* Python 帧**。`merge_configs` 的父帧恒为 `prepare_single_task` → 增量全部发生在**图调度的配置合并路径**。

**解读（已证伪，留档）**：曾据此推断慢相多出的 CPU 集中在 pregel `tick → prepare_next_tasks → prepare_single_task → merge_configs` 链条、`merge_configs` self 暴涨 14× → "config 体积随请求累积"。→ **实际是 py-spy 采样分母（vCPU 物理执行时间）被节流砍半，相对占比随之漂移**；R5b 探针实测 tick 总耗时两相持平（0.43s/10s）、merge 每调用恒 ~14µs、GC 恒 0.4s/10s，Python 路径无一翻倍。`tracemalloc` 定位"累积对象"已无必要（见 §九）。

**补充判别实验**：

- **闲置恢复**：突降总在连续负载 60–120s 后发生，且**停载数十秒后又能恢复快相**（s5b 后 A/B 短跑 68–70 rps）→ 累积对象在闲置时被释放/清理，进一步支持「可变结构累积」而非「永久泄漏」。
- **流式长跑 180s**（流式路径**跳过 dedup 读写**，core.py:843 `if not stream`）：完成流 9.4 → 6.2 → 7.5/s，衰减约 25%——比非流式的 50% 轻 → **dedup 写只解释约一半衰减**，另一半在两条路径共用的调度/框架层。
- **内部重试排除**：全程 118,927 条 call_logs 全部 `success`、`call_attempts=0` → 无重试/降级双倍干活。
- **native 帧采样**（`py-spy --native` 慢相 30s）：Python 层无 `gc.*` 帧；native 帧为未符号化的 libpython 地址，**GC 假设无法证实也无法排除**（需符号化或 `PYTHONgc` 专项验证）。
- 慢相 botflow 函数级最大变化：`resolve_api_key`/`list_api_keys` 0.48%→1.55%（**三倍，即已知 P1-6 每请求重查 api_keys**，缓存失效后查询变贵），但绝对值仅 +1pp，非主因。

**根因状态（已推翻）**：本节原"每请求 CPU 成本翻倍、待 `tracemalloc` 定位累积对象"的结论**不成立**。真实根因见 §九（平台侧 GCE e2-medium token bucket 节流 + ktime 幻影记账），已双向判别确认，无需代码修改。

## 七、优化建议（按预期收益排序）

> **【2026-09-30 修订】** 原 #1「定位修复 §五连续负载 CPU 翻倍、稳态吞吐 40→79 翻倍」**已作废**（根因见 §九：40 rps 是机型基线非应用瓶颈，botflow 无需修改）。下表重排为 #1–#6，均为**降低每请求 CPU 的真实优化**，在机型基线 1.0 核约束下能提升单位基线可扛 rps，仍有效。
>
> **【2026-09-30 落地状态】** 6 项已全部完成并通过双 Agent 验收（mq3 全量 1379 passed、改动文件 100% 覆盖）：
> #1 B4 `a69c8ec`（CallLog/CallAttempt model_construct）；#2 B6 `0166042`（LangGraph 单组旁路跳过 pregel）；
> #3 B5 `36f2c75`（ASGI 链瘦身，Bearer 主路径中间件耗时 17.45µs→4.51µs）；#4 B1 `09670d3`（限流/去重 TTL 可配置）；
> #5 B2 `32d858b`（修复**流式路径绕过 per-provider 信号量**的真实缺陷）；#6 B3 `ceb5897`（修复 **P0-1 fd 泄漏**：provider 替换/失效时旧连接池 300s grace 延迟关闭）。

| # | 建议                                                                                 | 预期收益                    | 依据                 |
| - | ---------------------------------------------------------------------------------- | ----------------------- | ------------------ |
| 1 | 热路径对象构造减量：pydantic 模型复用/`model_construct`、减少 `get_origin`/`__instancecheck__` 类型内省 | 单请求 CPU -10~15%         | py-spy self TOP    |
| 2 | LangGraph 单组执行旁路（或给 trivial 图做快路径），省 pregel/runnable 调度                            | 单请求 CPU -20~30%         | inclusive 56%      |
| 3 | ASGI 链瘦身（proxy_headers/CORS 按需挂载）                                                  | 单请求 CPU -10~20%         | 框架链 inclusive ~50% |
| 4 | 限流 300/min 与去重 TTL 改为可配置（env/config）                                               | 平台可用性                   | §五踩坑               |
| 5 | 超发保护：C≥64 吞吐反降，建议加并发上限/背压（`upstream_semaphore_size` 已有但 `=0` 无限）                   | 高并发下避免劣化                | S1/S2 阶梯           |
| 6 | P0-1 泄漏专项：provider 频繁切换 + 长跑场景补测                                                   | 待定                      | 本场景未触发             |

## 八、环境与方法附录

- 版本：`307c41b`；Python 3.12（venv）；uvicorn 单进程单 worker；`upstream_semaphore_size=0`、`model_sync_interval=60`。
- 隔离：bench 实例与 `/srv/botflow` 生产同机（mq3），生产 `botflow` 进程（PID 826807）持续占用少量 CPU；压测期间系统 load 1.6–1.8 / 2 核。
- 限流处理：bench 副本 `max_requests=300 → 10**9`（**仅压测目录，未动仓库代码**）；dedup 用每请求 `nonce` 破除。
- 负载端与被测同机 → 网络栈共享；S2 C=1 的 14ms 已包含同机回环开销。
- 每档之间 sleep 3s；S1/S2/S3 各档独立进程，无跨档污染。

## 九、根因结论：连续负载吞吐腰斩（A1 定位，2026-09-30，⚠️ 非代码缺陷）

**结论**：§五的"吞吐腰斩 + 每请求 CPU 翻倍"不是 botflow 缺陷，是**压测机 mq3 的 GCE `e2-medium` 突发机型 CPU token bucket 节流**。

- mq3 = `e2-medium`（zone `asia-east2-a`），2 vCPU 共享 1 物理核 SMT，**合计基线 1.0 核**；满载突发 2.0 核约 **115s** 后 token 耗尽，由 hypervisor 按时间片 deschedule vCPU 回基线（duty≈50%）。
- 节流在 hypervisor 层、不走 KVM steal MSR、不改 guest 内 cgroup → guest 看到的 `steal` 恒 ~0.3/s、cgroup `cpu.max=max`、`nr_throttled=0`，应用层完全无感知。
- "每请求 CPU 翻倍"是**记账幻影**：guest ktime 家族（`schedstat`/`utime`/task-clock）把 vCPU 停摆时间记成"在跑"，而 tick 家族（jiffies/LOC/中断）如实减半 → 同一段代码在快慢相 CPU-s 正好 2×（R8 标定循环 7.5→15.1s）。

**证据（R5–R13，10 轮饱和复现 + 3 轮判别）**：

1. 9/9 饱和轮 t+105~150s 阶跃 80→40 rps，与并发数无关（c8=c32）；
2. 三家族计数器分裂：tick 家族 ≈0.5×、ktime 家族恒定、外部 ping 零超时（gGuest 网络/时钟正常）→ 排除秒级停摆、指向 ms 级时间片节流；
3. 双向判别：R11 不限速（CPU 需求 1.4 核>基线）120s 复现阶跃；R13 限速 35 rps（需求 0.77 核<基线）300s **全程无阶跃**；
4. R12 部分回填：满回填 burst 120s、30s 部分回填 burst 60s → 确认**可回填 token 数量模型**（静置≥90s 可恢复满 burst）。

**处置**：

- botflow **无需改代码**；原 A1 方案（`tracemalloc` 定位累积对象）已撤销。
- 压测方法修正：饱和压测时长 <100s，或限速 ≤ 基线（R13 模式）；每轮间静置 ≥90s 回填 token；报告附机型与逐窗 busy 校验。
- 生产核查（A2）：确认 `api.vxquant.com` 机型是否突发型，评估真实流量尖峰等效基线。
- 完整证据链见 `docs/tasks/A1_root_cause_perf_step.md`。

