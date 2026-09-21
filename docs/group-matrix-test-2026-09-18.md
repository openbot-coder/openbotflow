# mq3 网关 · 分组演示测试记录（2026-09-18）

## 0. 拓扑与前置

| 项 | 值 |
|---|---|
| 被测服务 | mq3 `/srv/botflow`，`HEAD=34eca7d`，`DIRTY=0`，**v3.1.0** |
| 客户端位置 | `api.vxquant.com` = tailnet `100.88.88.2`（hostname `botflow-gateway`） |
| 被测地址 | `http://100.88.88.88:4000`（**Tailscale 明文，无 TLS**） |
| 鉴权 | `/v1/*` 用 DB `config.llm_key`；`/health`、`/admin/*` 豁免 |
| 公网暴露 | `35.220.217.33:4000` → **000 不可达** 🔒 |

`/v1/models` 正确暴露 5 个分组名：`['backup', 'fast', 'fast-text', 'free', 'smart']`

## 1. 分组 → 模型 映射（来自 mq3 DB）

| 组 | id | fallback | 模型 | provider | 权重 |
|---|---|---|---|---|---|
| `fast` | 1 | → 4 | 559 `deepseek-v4.1-flash` | 2 `zd` | 100 |
| `free` | 2 | — | 539 `agnes-3.0-flash`<br>540 `agnes-3.0-flash` | 9 `agnes`<br>8 `agnes-cn` | 1<br>1 |
| `smart` | 3 | → 4 | 559 `deepseek-v4.1-flash` | 2 `zd` | 1 |
| `backup` | 4 | — | 14 `mimo-v2.5`<br>540 `agnes-3.0-flash` | 4 `xiaomi`<br>8 `agnes-cn` | 50<br>50 |
| `fast-text` | 5 | — | 540 `agnes-3.0-flash` | 8 `agnes-cn` | 1 |

> `zd` 的 `extra_config.headers` 含 `x-opencode-session: {conversation_id}`，按请求注入 —— 本轮
> `fast`/`smart` 均成功，说明该 P8-1 机制在 mq3 生效。

## 2. 结果矩阵

### 2.1 非流式（每个分组各一次）

| # | 请求 `model` | HTTP | 耗时 | 返回 `model` 字段 | finish | usage (p/c/total) | 正文 |
|---|---|---|---|---|---|---|---|
| F | `fast` | 200 | 3.14s | `fast` | **length** | 35/200/235 | **空** —— 200 tok 全被 `reasoning_content`（330 字）吃满 |
| F2 | `fast`（重跑，max_tokens=800） | 200 | 4.21s | `fast` | stop | 47/446/493 | 「我是由深度求索开发的人工智能助手…6×7=42」 |
| FT | `fast-text` | 200 | 0.96s | `fast-text` | stop | 79/2/81 | `你好` |
| R | `free` | 200 | 3.25s | `free` | stop | 79/2/81 | `你好` |
| S | `smart` | 200 | 1.85s | `smart` | stop | 39/48/87 | `391`（17×23 正确，`reasoning_content` 147 字） |
| B | `backup` | 200 | 4.15s | `backup` | stop | 253/47/300，**cache 192** | `你好` |

### 2.2 流式（SSE）

| 分组 | HTTP | 耗时 | data 行 / JSON chunk | 拼接正文 | usage | `[DONE]` |
|---|---|---|---|---|---|---|
| `fast` | 200 | 2.31s | 168 / 167 | `12345` | 39/166/205 | ✅ |
| `fast-text` | 200 | 1.22s | 9 / 8 | `A B C D E` | 83/6/89 | ✅ |

## 3. 落库核对（`call_logs`，buffer=100 / flush=5s）

| id | 组 | 模型 | provider | status | ms | tok | cache |
|---|---|---|---|---|---|---|---|
| 14 | 5 `fast-text` | 540 `agnes-3.0-flash` | 8 `agnes-cn` | success | 1030 | 89 | 0 |
| 13 | 1 `fast` | 559 `deepseek-v4.1-flash` | 2 `zd` | success | 4174 | 493 | 0 |
| 12 | 1 `fast` | 559 `deepseek-v4.1-flash` | 2 `zd` | success | 2258 | 205 | 0 |
| 11 | 4 `backup` | **14 `mimo-v2.5`** | 4 `xiaomi` | success | 3808 | 300 | 192 |
| 10 | 3 `smart` | 559 `deepseek-v4.1-flash` | 2 `zd` | success | 1744 | 87 | 0 |
| 9 | 2 `free` | 539 `agnes-3.0-flash` | 9 `agnes` | success | 2760 | 81 | 0 |
| 8 | 5 `fast-text` | 540 `agnes-3.0-flash` | 8 `agnes-cn` | success | 920 | 81 | 0 |
| 7 | 1 `fast` | 559 `deepseek-v4.1-flash` | 2 `zd` | success | 2959 | 235 | 0 |

按组聚合（含 2026-09-17 的历史调用）：

| 组 | 调用数 | status | 平均耗时 |
|---|---|---|---|
| 1 `fast` | 3 | 全 success | 3130 ms |
| 2 `free` | 4 | 全 success | 8127 ms |
| 3 `smart` | 2 | 全 success | 1880 ms |
| 4 `backup` | 3 | 全 success | 2236 ms |
| 5 `fast-text` | 2 | 全 success | 975 ms |

累计 `total=14`、**`non_success=0`**。

## 4. 观察点

1. **`fast`/`smart` 是 reasoning 模型**：`max_tokens` 给小了（200）会出现 `finish_reason=length`
   且 `content` 为空 —— token 全耗在 `reasoning_content`。演示此类分组需给 ≥ 500。
   **不是网关缺陷**，但客户端调这两个组时应把上限调大或容忍空正文。
2. **加权轮询在工作**：`backup` 组 50/50 的两个模型，历史两次都命中 540，本轮命中 **14 `mimo-v2.5`**
   → 说明选择器确实在轮换而非固定取首个。
3. **前缀缓存命中**：id 11 的 `cache_tokens=192`（xiaomi 侧上报）→ 上游前缀缓存生效，
   印证「不要随意改动历史消息字节」这条优化约束。
4. **`config` 表运行态自写**：本轮后新增 6 条 `dedup:*`（服务自己写的去重缓存），
   当前无 `cooldown:*` → **没有任何模型处于冷却**。
5. `api_key_id` 已在 3.1.0 落库（`call_logs` 有该列），可用于按客户端统计。

## 5. 未覆盖的行为（说明原因）

- **组级 fallback（`fast` → `backup`）本轮未触发**：需要把 `fast` 组的模型全部打入冷却才会走
  该路径。构造方式只有人为制造失败（如临时改 base_url 造错），会污染 cooldown 状态、需事后
  手动清理，故未在本轮演示中做。`fallback_group_id=4` 已在 DB 中确认配置存在。
- **流式路径的组级 fallback 缺口**：`route_stream()` 在路由阶段无可用模型时**直接短路**，
  不尝试 `fallback_group_id`；非流式经 `resolve_group` 会退到 backup 组。
  与 `pipeline_router_design.md:15` 冲突，**已发现未修**（建议单独立项）。

## 6. 复现方式

客户端从 `api.vxquant.com` 执行（`llm_key` 取自 DB `config.llm_key`，脚本内不打印）：

```bash
curl -s -X POST http://100.88.88.88:4000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"<组名>","messages":[{"role":"user","content":"你好"}],"max_tokens":800,"stream":false}'
```

`model` 字段语义：先按**组名精确匹配** → 再找「包含该模型的组」→ 兜底 `list_groups(enabled_only=True)[0]`
（⚠️ 该列表顺序不按 id，实测未知组名会落到 `backup`(4)）。
