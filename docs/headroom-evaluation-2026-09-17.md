# headroom 适用性评估报告（botflow 视角）

- 日期：2026-09-17
- 评估对象：`headroom-ai` 0.37.0（Apache-2.0，PyPI 包名 `headroom-ai`，非 `headroom`）
- 被评估接口：`from headroom import compress`（library 模式）
- 取证方式：**用 botflow 生产库的真实请求体喂给 headroom 实测** + 合成对照 + 生产环境探测
- 是否改动生产：**否**（全程只读；headroom 安装在本地隔离 venv）

---

## 一、结论（TL;DR）

| 判断 | 内容 |
|---|---|
| **对 botflow 流量是否有用** | **基本没有价值。** 用唯一一条完整真实 agent 请求（499,511 字符 / 121 条消息）实测：**只省 2.30%**。把 `target_ratio` 压到 0.1、强制压缩 user/system、`protect_recent=0`、`min_tokens_to_compress=50` —— **仍然是 2.3%** |
| **根因** | headroom 的**轻量路径**（SmartCrusher=JSON / LogCompressor=日志）只在「高重复 JSON / 结构化日志」上有效。而 botflow 的 group 1（占 7 天 token 的 **97%**）是 **MiMoCode agent 轨迹**，内容是 `ls` 列表 / 命令帮助 / 状态文本 / 文件内容 —— 被正确路由为 **`noop` / `protected`**，一动不动 |
| **处理散文的 Kompress** | 需要 `[ml]` extra（torch + transformers + HF 模型）。实测本机 `torch` **DLL 加载失败**（`WinError 1114 c10.dll`），每条文本类消息抛一次异常并静默降级。且**官方自己承认「散文压缩率极低」** |
| **经济性反证（决定性）** | group 1 七天内 **86.3% 的 prompt token 已命中上游前缀缓存**（670.3M / 776.6M，75.4% 的请求有命中）。能省的那 2.3% 里大部分本来就是缓存价；而压缩会改变前缀字节 → 反而**有打穿缓存的风险**（缓存读价 → 全价） |
| **净收益估计** | 账单节省 **≈ 2.3% 上限**（且以缓存不被破坏为前提）；换来的是热路径 +87ms CPU、依赖面 119MB → 757MB、冷启动 33.7 秒 |
| **建议** | **网关侧不引入。** 若真要用，正确落点是**客户端侧**（`headroom wrap` MiMoCode），那里才有完整上下文与 `headroom_retrieve` 取回工具 |

> ⚠️ 样本量说明：完整请求体全库只有 **1 条**（botflow 对成功请求只存前 2000 字符，仅失败请求存全文）。但另有 400 条大请求首块抽样**全部**是 `You are MiMoCode, ...`，与该方法一致，故「以散文/文档为主、非重复 JSON」这一判断置信度为**中高**。

---

## 二、真实请求实测

被测样本：`call_logs.id = 48930`，group 1 / model 559 / `stream=true`，body **499,511 字符**，121 条消息。

**内容构成：**

| role | 类型 | 条数 | 字符合计 |
|---|---|---|---|
| tool | 散文（shell 输出 / 目录列表 / 帮助文本） | 62 | 145,582 |
| assistant | 散文 | 36 | 5,901 |
| tool | markdown/文档 | 9 | 14,986 |
| tool | code | 6 | 6,273 |
| system | markdown/文档（MiMoCode 系统提示词） | 1 | 49,473 |
| assistant | markdown/文档 | 2 | 4,890 |
| user | 散文 | 1 | 22 |

**压缩结果：**

| 项 | 值 |
|---|---|
| tokens before | 83,937 |
| tokens after | 82,003 |
| tokens saved | **1,934（2.30%）** |
| 字符 432,093 → 427,231（1.1%） |
| **被改动的消息** | **6 / 121** |
| transform 统计 | `router:excluded` ×25、`router:search` ×4、`router:tabular` ×2、`router:protected` ×1 |
| 延迟（稳定态） | 83.5 / 86.7 / 86.8 ms |
| 延迟（首次调用） | **33,738.5 ms**（尝试下载/初始化 HF 模型） |

**被改动的 6 条：**

| # | role | 类型 | 压缩前 | 压缩后 |
|---|---|---|---|---|
| 46 | tool | code | 1,689 | 1,557 |
| 49 | tool | 散文 | 1,458 | 1,241 |
| 52 | tool | 散文 | 1,107 | 893 |
| 59 | assistant | markdown | 3,484 | 482 |
| 71 | tool | 散文 | 3,214 | 2,729 |
| 79 | assistant | markdown | 1,406 | 543 |

**参数加码无效（实测）：**

| 参数 | saved | 比例 |
|---|---|---|
| 默认 | 1,934 | 2.30% |
| `target_ratio=0.2` | 1,934 | 2.30% |
| `target_ratio=0.2, compress_user_messages=True, compress_system_messages=True, protect_recent=0` | 1,934 | 2.30% |
| `target_ratio=0.1, ..., min_tokens_to_compress=50` | 1,939 | 2.31% |

> 结论：**不是参数没调对，是没有可压之物。**

---

## 三、内容类型适用性矩阵（合成对照，同量级）

| 内容类型 | 字符 | tokens 前 | tokens 后 | 省 | 命中压缩器 |
|---|---|---|---|---|---|
| 重复 JSON 数组（API/DB 返回） | 82,450 | 34,797 | 17,545 | **49.6%** | `smart_crusher:0.40` |
| 结构化日志文本 | 66,570 | 27,914 | 19,823 | **29.0%** | `log:0.71` |
| CSV 表格 | 53,424 | 29,886 | 376 | **98.7%** | `search:0.01` |
| 英文散文（技术文档） | 56,800 | 8,416 | 8,416 | 0.0% | （无） |
| 中文散文（金融分析） | 49,500 | 36,015 | 36,015 | 0.0% | （无） |
| Markdown 文档 | 38,409 | 8,595 | 8,595 | 0.0% | （无） |
| HTML 页面 | 56,513 | 18,222 | 18,222 | 0.0% | （无） |
| Python 源码 | 38,499 | 8,375 | 8,375 | 0.0% | `protected:recent_code` |
| **★ 真实 MiMoCode agent 请求** | — | 83,937 | 82,003 | **2.3%** | — |

> 合成样本为人工构造的高重复数据，代表「上限」而非你的实际分布。**矩阵的意义在于：headroom 的能力边界很清晰 —— 结构化数据行，散文/代码/文档一律不动。**

---

## 四、成本

### 4.1 延迟

| 场景 | 实测 |
|---|---|
| 稳定态（499KB 真实请求） | **83.5–86.8 ms** |
| 稳定态（散文类，冷启动后） | 3–7 ms |
| 首次调用 / 冷启动 | **33.7 s**（触发 HF 模型拉取） |
| 无 `[ml]` 时遇到文本内容 | 1.1–8.3 s 抖动（尝试连 HuggingFace） |

对比：botflow 自身网关侧 CPU 对 96% 流量是 **2–14 ms/请求**。headroom 会让**单个大请求的开销变成 botflow 全流程的 6–40 倍**，且是**同步 CPU 密集调用**（会阻塞事件循环，除非丢线程池）。

### 4.2 依赖与体积

| 项 | 值 |
|---|---|
| headroom base 强制依赖 | `tiktoken`、`pydantic`、**`litellm>=1.86.2`**、`click`、`rich`、`opentelemetry-api`、**`ast-grep-cli`**（原生二进制）、`pyyaml`、`tomlkit` |
| `[ml]` extra 追加 | `torch>=2.12.1`、`transformers`、`huggingface-hub` |
| 隔离环境 site-packages 实测 | **756.7 MB** |
| botflow 本地 `.venv` | **119.3 MB** |
| 生产 `.venv` | **149 MB** |

→ 引入 headroom 会把依赖面放大 **约 5–6 倍**，并且**新增 litellm 一整棵 provider 依赖树**（与 botflow 现有的 `openai` SDK 功能重叠）。

### 4.3 生产环境资源约束（探测结果）

| 项 | 值 |
|---|---|
| CPU | **4 核**，load average **12.21 / 14.04 / 13.25**（已 3 倍超载） |
| 内存 | 7,425 MB 总量，已用 4,958 MB，**available 仅 2,157 MB** |
| swap | 8,191 MB（已用 467 MB） |

**在 4 核 + load 13 的机器上再跑一个 CPU 密集的 transformer 推理，是不可行的。**

> 附带发现（与本次评估无关但更紧急）：**`tailscaled` 占 98% CPU、3.0 GB RSS（39.6% 内存）**，已运行 6 天；`nats-server` 占 46.6% CPU。botflow 自身仅 1.1% CPU / 349 MB RSS。**这把机器拖垮的不是 botflow**，但它会通过调度竞争推高 botflow 的尾延迟。

---

## 五、网关视角的安全性

以「透明网关」的身份静默改写第三方客户端的请求，有三个不可接受的属性：

| # | 问题 | 实测证据 |
|---|---|---|
| **S-1** | **有损且不可逆**（library 模式下） | 压缩后的 tool 消息键只有 `['role','tool_call_id','content']`，**不含任何 retrieval 句柄**；JSON 内容 13,918 → 5,541 字符，模型无法知道原文被丢过、也无法取回 |
| **S-2** | **CCR「可逆」依赖调用方有取回工具** | 官方机制是模型调用 `headroom_retrieve`。botflow 的客户端是 MiMoCode，**它并不知道 headroom 存在**，不会调用 → 可逆性在网关场景下等于不存在 |
| **S-3** | **压缩比可能极端激进** | CSV 用例被压到 **1.3%**（`search:0.01`）。对网关而言，这等于单方面替客户决定「你的数据 98.7% 不重要」 |

结构完整性方面 headroom 是干净的（`tool_calls` / `tool_call_id` 关联保留、消息条数不变、role 序列一致），问题不在结构，在**语义**。

---

## 六、如果仍要使用：可行边界

1. **不要放在 botflow 热路径上。** 若一定要用，放在**客户端侧**（`headroom wrap mimocode`），那里有完整上下文、有 retrieval 工具、失败了也不影响共享网关。
2. **不要开 proxy 模式再加一跳。** botflow 本身就是代理，套一层 headroom proxy = 两个代理串联 + 一个额外进程。
3. **不要装 `[ml]`。** 除非有 GPU 且能预置 HF 模型并做启动预热（否则首个请求挂 30 秒）。
4. **若坚持在网关上做**，只能采取**窄口径 + 保守参数**：仅对 `role=tool` 且 sniff 到明显 JSON-array / 结构化日志特征的消息启用，`target_ratio` 固定保守值，**并保证压缩是内容确定性函数**（同一输入必得同一输出），否则会打穿上游前缀缓存。
5. **优先级更低。** 同样想省钱，先做 `docs/efficiency-audit-2026-09-17.md` 里的 P0/P1 修复，收益更确定、风险更低。

---

## 七、顺带产出的两个对 botflow 自身可落地的结论

### 7.1 用 `tiktoken` 替换 `_cjk_ratio` 启发式 —— 更快且更准

| 载荷 | botflow `estimate_tokens` | tiktoken `o200k_base` | 估算偏差 | `_cjk_ratio` 耗时 | tiktoken 耗时 |
|---|---|---|---|---|---|
| 真实 agent 请求（227,773 字符） | 57,859 | 70,091 | **−17.5%** | 33.6 ms | **22.9 ms** |
| 真实中英混合请求（908 字符） | 276 | 563 | **−51.0%** | 0.1 ms | 0.2 ms |
| 合成中文（98,000 字符） | 45,736 | 77,000 | **−40.6%** | 13.4 ms | 15.4 ms |
| 合成英文（202,100 字符） | 50,527 | 37,601 | **+34.4%** | 28.7 ms | **5.3 ms** |

**两个结论：**

1. 现有启发式误差 **−51% ~ +34%（双向）**，远超此前预估的 ±30%。中文严重**低估**（按 2 字符/token，实际约 1.6），长英文严重**高估**。用于「是否要截断」的判定时，会同时出现**该截没截**（上游报错）和**不该截却截了**（丢信息）。
2. tiktoken 是 Rust 实现，**比纯 Python 逐字符循环更快**（英文 2–5×，混合 1.5×；仅纯中文略慢），同时把误差压到接近 0（相对 o200k 口径）。

→ 这一条同时解决 `efficiency-audit` 里的 **P0-3（CPU）** 和**估算精度**两个问题。代价：新增 `tiktoken` 依赖（纯 wheel + 小型 BPE 数据文件），需处理离线部署时的 BPE 缓存。

### 7.2 上游前缀缓存必须当成一等公民

group 1 七天 **86.3% 的 prompt token 命中缓存**（75.4% 的请求有命中），group 3 更高（92.9%）。这意味着：

- **任何会改变历史消息字节的优化（压缩、重排、去重、截断），都要先证明不会打穿前缀缓存**，否则省下的 token 远不足以抵消「缓存读价 → 全价」的损失。
- 反过来，**保持前缀稳定本身就是最大的省钱手段**——比任何压缩算法都便宜。

---

## 八、验证方法与脚本

| 脚本（本地 `.workbuddy/tmp/`） | 用途 |
|---|---|
| `perf_hr_fit2.py` | 逐日/分组 prompt_tokens 分布、大请求 Top、tool 浓度 |
| `perf_hr_cache.py` | 分组前缀缓存命中率、大请求首块内容分类、`context_window` 核对 |
| `hr_dump.py` | 从生产库导出真实请求体（含 499KB 完整样本）到 `/tmp/hrdata` |
| `hr_bench.py` | headroom 基础自检 + 真实请求/合成对照的压缩率与延迟 |
| `hr_detail.py` | 逐条消息 diff：121 条里改了哪 6 条 |
| `hr_matrix.py` | 内容类型适用性矩阵 + 结构完整性 / CCR 句柄检查 |
| `hr_ml.py` | 装齐 `[ml]` 后的冷热延迟、HF 连通性、CCR 句柄复核 |
| `hr_tok.py` | `estimate_tokens` vs `tiktoken` 的精度与速度对照 |

环境：本地隔离 venv `.workbuddy/tmp/hr`（Python 3.13 + `headroom-ai[ml]` 0.37.0）。
生产数据：`api.vxquant.com:/mnt/deploy/botflow/data/botflow.db`（只读打开，`mode=ro`）。
