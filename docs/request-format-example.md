# botflow 请求格式示例（OpenAI 协议）

> 本文所有输出均由 `src/botflow/protocol_adapter.py` 与 `core.py` 的真实代码执行产生，非手写示意。

## 一、客户端怎么发

botflow 兼容三种入口协议，最常用的是 OpenAI Chat Completions。

| 端点 | 协议 | 方法 |
|---|---|---|
| `/v1/chat/completions` | OpenAI Chat Completions（主力） | POST |
| `/v1/completions` | OpenAI 旧版补全 | POST |
| `/v1/messages` | Anthropic Messages | POST |
| `/v1/responses` | OpenAI Responses API | POST |

### curl

```bash
curl https://api.vxquant.com/v1/chat/completions \
  -H "Authorization: Bearer <BOTFLOW_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "smart",
    "messages": [{"role": "user", "content": "深圳今天天气怎么样？"}],
    "temperature": 0.3,
    "max_tokens": 1024
  }'
```

### Python（openai SDK）

```python
from openai import OpenAI

client = OpenAI(base_url="https://api.vxquant.com/v1", api_key="<BOTFLOW_KEY>")

resp = client.chat.completions.create(
    model="smart",
    messages=[{"role": "user", "content": "深圳今天天气怎么样？"}],
)
print(resp.choices[0].message.content)
```

### 关键点

- `model` 填的是 **botflow 的分组名**（如 `fast` / `smart` / `free`），不是上游真实模型名。
- 所有参数都在**顶层**，是扁平结构，没有嵌套包装。

## 二、完整示例请求体（带工具调用的多轮）

```json
{
  "model": "smart",
  "messages": [
    {"role": "system", "content": "你是一个助手。需要实时信息时必须调用工具，不要凭记忆回答。"},
    {"role": "user", "content": "深圳今天天气怎么样？适合穿什么？"},
    {"role": "assistant", "content": "", "tool_calls": [
      {"id": "call_00_abc123", "type": "function",
       "function": {"name": "get_weather", "arguments": "{\"city\":\"深圳\"}"}}]},
    {"role": "tool", "tool_call_id": "call_00_abc123",
     "content": "{\"city\":\"深圳\",\"temp_c\":29,\"desc\":\"多云\",\"humidity\":78}"}
  ],
  "tools": [
    {"type": "function", "function": {
      "name": "get_weather",
      "description": "查询指定城市的实时天气。",
      "parameters": {"type": "object",
                     "properties": {"city": {"type": "string", "description": "城市名"}},
                     "required": ["city"]}}},
    {"type": "function", "function": {
      "name": "get_forecast",
      "description": "查询未来若干天的天气预报。",
      "parameters": {"type": "object",
                     "properties": {"city": {"type": "string"}, "days": {"type": "integer"}},
                     "required": ["city"]}}}
  ],
  "tool_choice": "auto",
  "temperature": 0.3,
  "max_tokens": 1024,
  "stream": false,
  "response_format": {"type": "text"},
  "prompt_cache_key": "ses_demo_123"
}
```

### messages 里四种 role 的结构

| role | 字段 | 说明 |
|---|---|---|
| `system` | `role`, `content` | 系统提示词 |
| `user` | `role`, `content` | 用户输入 |
| `assistant` | `role`, `content`, `tool_calls`, `reasoning_content`（可选） | 模型回复；发起工具调用时 `content` 常为空串，工具调用在 `tool_calls` |
| `tool` | `role`, `tool_call_id`, `content` | 工具执行结果，用 `tool_call_id` 关联 `assistant.tool_calls[].id` |

`content` 有两种形态：

- 字符串：`"深圳今天天气怎么样？"`
- 数组（多模态）：`[{"type": "text", "text": "..."}, {"type": "image_url", "image_url": {"url": "..."}}]`

## 三、网关收到后变成什么

`openai_to_internal(body)` 把它切成三块（`protocol_adapter.py:20-27`）：

```
顶层键: ['messages', 'model', 'temperature', 'max_tokens', 'stream', 'extra']

  messages    -> 4 条（原样保留）
  model       -> 'smart'
  temperature -> 0.3
  max_tokens  -> 1024
  stream      -> False
  extra       -> {tools, tool_choice, response_format, prompt_cache_key}
```

切分规则是**排除法**，不是白名单：

```python
extra = {k: v for k, v in body.items()
         if k not in ("messages", "model", "temperature", "max_tokens", "stream")}
```

所以任何没见过的参数都会先进 `extra`。

## 四、白名单过滤（决定哪些真的发给上游）

`_filter_safe_extra()`（`core.py:96`）按 `SAFE_EXTRA_KEYS`（38 个键，`core.py:82`）过滤：

```
白名单大小: 38 个键
保留: ['tools', 'tool_choice', 'response_format', 'prompt_cache_key']
丢弃: （无）
```

最终调用上游所用的实参：

```python
provider.chat(
    messages    = <4 条>,
    model       = 'smart',
    temperature = 0.3,
    max_tokens  = 1024,
    tools         = [...],
    tool_choice   = "auto",
    response_format = {"type": "text"},
    prompt_cache_key = "ses_demo_123",
)
```

> ⚠️ **不在白名单的键会被静默丢弃**，不报错、不告警。注意白名单只认下划线写法（`prompt_cache_key`）；若客户端发驼峰 `promptCacheKey`，会被直接丢掉。

## 五、上下文三段拆分与 token 估算

```
system 段  : 1 条
history 段 : 3 条（其中 role=tool 1 条）
tools 定义 : 2 个  <-- 不在 messages 里，网关结构上看不到

estimate_tokens(messages) =      44   <-- 网关唯一看到的数字
工具定义实际占            =     112   (schema 433 字符)
漏算比例                  =   71.8%
```

`estimate_tokens()` 只接收 `messages`，**工具定义不计入上下文预算**。

## 六、响应格式

### 非流式

```json
{
  "id": "chatcmpl-xxx",
  "object": "chat.completion",
  "created": 1758000000,
  "model": "smart",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "深圳今天多云，气温约 29°C，湿度 78%。建议穿短袖配薄外套，注意补水和防晒。"
    },
    "finish_reason": "stop"
  }],
  "usage": {"prompt_tokens": 428, "completion_tokens": 41, "total_tokens": 469}
}
```

`model` 会被改回客户端请求的分组名（不是上游真实模型名）。

### 流式（`stream: true`）

SSE 分块，每块以 `data: ` 开头，最后以 `data: [DONE]` 结束：

```
data: {"id":"...","object":"chat.completion.chunk","created":1758000000,"model":"smart","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}

data: {"id":"...","object":"chat.completion.chunk","created":1758000000,"model":"smart","choices":[{"index":0,"delta":{"content":"深圳"},"finish_reason":null}]}

...

data: {"id":"...","object":"chat.completion.chunk","created":1758000000,"model":"smart","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":428,"completion_tokens":41,"total_tokens":469}}

data: [DONE]
```

## 七、其余两种协议的差异

| 协议 | 差异 |
|---|---|
| Anthropic `/v1/messages` | `system` 是**顶层独立字段**（不在 messages 里），网关会转成 `{"role":"system"}` 前插（`protocol_adapter.py:38-41`）；内容块从 Anthropic 格式转 OpenAI 格式 |
| Responses `/v1/responses` | 用 `input`（字符串或数组）+ `instructions`（即 system）；`max_output_tokens` 映射为 `max_tokens`（`:363-399`） |

## 八、排错提示

1. **`call_logs.request_body` 不是客户端原文。** 存的是 `json.dumps(internal)`，键序固定为 `messages → model → temperature → max_tokens → stream → extra`，`extra` 永远在最后。
2. **成功请求的 body 被截断到 2,000 字符**（`_request_summary`，`core.py:927`），`extra`（含 tools）在末尾，通常看不到。只有失败调用保留完整 body。
3. **参数不生效先查白名单。** 新参数若不在 `SAFE_EXTRA_KEYS` 里，会在 `_filter_safe_extra` 处被静默过滤掉。
