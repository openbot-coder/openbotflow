# P8-1 功能点与测试用例：`openai_compat` 支持 `extra_config.headers`

## 背景

生产网关 `api.vxquant.com` 的 `fast` / `smart` 分组各自只挂一个模型
`deepseek-v4.1-flash`（provider 2 = `zd`，base_url `https://opencode.ai/zen/go/v1/`）。
该上游是 **OpenCode Go（Zen Go）**，官方文档
（https://opencode.ai/docs/go/#where-can-i-use-it）明确要求：

1. 客户端用**自己的 user agent** 标识身份，例如 `my-coding-agent/1.0`，
   而不是通用 SDK / HTTP 库名（内置 SDK 名会被 Cloudflare 判 `error code: 1010`）；
2. **每个会话**在 `x-opencode-session` 里带一个**稳定的 session ID**，
   用于优化路由与 prompt 缓存。

生产实测（2026-09-15）：

| 请求方式 | 结果 |
|---|---|
| 裸请求（urllib 默认 UA，无 session） | `403 error code: 1010` |
| 自定义 UA，无 session | `400 MissingSessionID` |
| 自定义 UA + 静态 `x-opencode-session` | **HTTP 200** |
| 自定义 UA + 静态 session，连续 3 次 | **HTTP 200 ×3**（静态可复用） |

网关的 `openai_compat.py` 只读 `extra_config` 的 `mode` / `api_version` /
`timeout` / `proxy`，**不读 `headers`** → 无法注入上述两个头，导致
`deepseek-v4.1-flash` 100% 失败，每次请求先白跑 3 次重试再降级到 `backup` 分组。

## 功能点

### F1 `extra_config["headers"]` 透传到上游请求

- 取值：`dict[str, str]`（键值均强制转 `str`）。
- 生效位置：`OpenAICompatProvider.chat()` 与 `.chat_stream()` 的
  `chat.completions.create(..., extra_headers=...)`。
- 未配置 / 非 dict / 空 dict → 传 `None`，**不改变现有行为**。
- 由 openai SDK 在请求头层面与自身默认头合并，不影响 `Authorization`
  与 `Content-Type`。

### F2 `{conversation_id}` 占位符按会话解析

- 若任一 header 值包含字面量 `{conversation_id}`，替换为该会话的稳定摘要。
- 摘要种子 = **system 消息内容 + 首个 user 消息内容**（用
  `_extract_text_from_content` 提取纯文本，兼容多模态 list 形式），
  取 `sha256` 前 32 位十六进制。
- 选择依据：
  - OpenAI 风格的 messages 是**追加式**历史，`system`（若有）与**首个 user
    消息**在所有轮次里位置与内容都固定 → 同一会话任意轮次得到同一 id；
  - 不含 system 时仅用首个 user 消息，避免把「所有会话共享同一系统提示词」
    塌缩成同一个 id；
  - 若用 `messages[:2]` 会在第 1 轮（`[user]`）与第 2 轮（`[user, assistant, user]`）
    产生不同 id，**不稳定**，故不采用。
- 不含占位符时不计算摘要（零开销）。
- 上游 docs 要求「每个会话一个稳定 session ID」，故 provider 层配置用占位符；
  需要全局静态 session 时直接写死字符串即可。

### F3 向后兼容

- 未配置 `headers` 的 provider（生产上其余 6 个）行为与改动前**完全一致**。
- `chat` / `chat_stream` 其余参数、返回值、异常包装（`ProviderError`）不变。

## 测试用例

> 单测文件：`tests/test_openai_compat_headers.py`
> 通过注入 fake client（替换 `provider._client`）捕获 `create()` 实参，
> 不发起真实网络请求。

### F1 `extra_config["headers"]` 透传

| 编号 | 类型 | 用例 | 期望 |
|---|---|---|---|
| T1.1 | 正例 | `headers={"X-A":"1"}`，`chat()` | `create` 收到 `extra_headers={"X-A":"1"}` |
| T1.2 | 正例 | 同上，`chat_stream()` | `extra_headers={"X-A":"1"}` |
| T1.3 | 正例 | `headers={"X-N": 42}`（非 str 值） | 转成 `{"X-N": "42"}` |
| T1.4 | 反例 | 未配置 `headers` | `extra_headers is None` |
| T1.5 | 反例 | `headers=None` | `extra_headers is None` |
| T1.6 | 反例 | `headers={}` | `extra_headers is None` |
| T1.7 | 反例 | `headers=["not","a","dict"]` | `extra_headers is None` |
| T1.8 | 边界 | 多个 header 键 | 全部保留 |

### F2 `{conversation_id}` 占位符

| 编号 | 类型 | 用例 | 期望 |
|---|---|---|---|
| T2.1 | 正例 | 单 header 值 `{conversation_id}` | 替换为 32 位 hex，且不含 `{`/`}` |
| T2.2 | 正例 | 值含前后缀 `sess-{conversation_id}-x` | 仅替换占位符，保留前后缀 |
| T2.3 | 正例 | 同一会话第 1 轮与第 5 轮（首 user 消息不变） | **id 相同** |
| T2.4 | 正例 | 两个不同首 user 消息的会话 | **id 不同** |
| T2.5 | 正例 | 有 system 消息但首 user 相同 → 不同 system | **id 不同** |
| T2.6 | 边界 | 无 user 消息（仅 system） | 不抛异常，返回合法 hex |
| T2.7 | 边界 | 空 messages 列表 | 不抛异常，返回合法 hex |
| T2.8 | 边界 | 首 user 消息为多模态 list 内容 | 与等价纯文本得到**相同** id |
| T2.9 | 边界 | 一个 header 含占位符、另一个为静态值 | 静态值原样保留 |
| T2.10 | 反例 | 值含 `{conversation_id`（未闭合） | 不作替换，原样透传 |
| T2.11 | 边界 | 首 user 内容为 `None` | 不抛异常 |

### F3 向后兼容

| 编号 | 类型 | 用例 | 期望 |
|---|---|---|---|
| T3.1 | 正例 | 无 headers 时 `chat()` 正常返回统一格式 | `choices[0].message.content` 正确 |
| T3.2 | 正例 | 无 headers 时 `chat_stream()` 正常产出 chunk | chunk 序列化正确 |
| T3.3 | 反例 | 上游 `create` 抛异常 | 包装为 `ProviderError`（提示信息保留） |
| T3.4 | 边界 | `extra_config=None`（构造默认） | `extra_headers is None` |

## 覆盖率要求

- 新增模块级函数 `_apply_headers` 与两个改动方法分支须 **100% 覆盖**；
- 无法覆盖的行标注 `# UNCOVERED: [原因]`。
