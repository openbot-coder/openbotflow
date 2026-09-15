# P8-1 测试执行报告

## 环境

| 项 | 值 |
|---|---|
| 执行机 | `api.vxquant.com`（Ubuntu，CPython 3.13.13） |
| 隔离 venv | `/tmp/p81venv`（`uv venv --python 3.13`，**未污染生产 venv**） |
| 代码副本 | `/tmp/p81`（从本机 tar 包解出，**未在生产部署目录跑 pytest**） |
| 命令 | `PYTHONPATH=/tmp/p81/src pytest tests/ -o addopts="" -q` |

> 本机 Windows 因 `AF_UNIX=False` → `socket.socketpair()` 走 loopback TCP 挂死，
> 全部 async 用例必须在 Linux 复跑，故测试在远端隔离环境执行。

## 1. 新增用例

`tests/test_openai_compat_headers.py` —— **27 passed**

| 分组 | 用例数 | 覆盖 |
|---|---|---|
| `TestHeaderPassthrough`（F1） | 9 | 透传、非 str 值强转、未配置/None/空 dict/非 dict → `None` |
| `TestConversationIdPlaceholder`（F2） | 13 | 替换、前后缀保留、跨轮稳定、跨会话不同、system 区分、多模态、空/无 user、未闭合括号 |
| `TestBackwardsCompatible`（F3） | 5 | 统一输出格式、流式 chunk、异常包装、`stream_options` 注入不变 |

用例通过注入 fake client（替换 `provider._client`）捕获 `create()` 实参，
**不发起真实网络请求**，可在 CI 稳定执行。

## 2. 覆盖率

```
src/botflow/providers/openai_compat.py   104 stmts   16 miss   85%
missing: 110-118, 180-185, 204, 206, 241, 243, 245, 265-266
```

**缺口全部为改动前既有分支**，与本次改动无关：

| 行 | 内容 | 说明 |
|---|---|---|
| 110-118 | `AsyncAzureOpenAI` 分支 | 无 Azure 用例，历史遗留 |
| 180-185 | `list_models()` | 历史遗留 |
| 204 / 206 | `_to_unified` 的 `tool_calls` / `function_call` 分支 | 历史遗留 |
| 241 / 243 / 245 | `_chunk_to_unified` 的 `tool_calls` / `function_call` 分支 | 历史遗留 |
| 265-266 | 完成块 usage 分支 | 历史遗留 |

**本次新增代码（`_conversation_id`、`_apply_headers`、两处 `extra_headers=`）100% 覆盖。**

## 3. 全量套件对照

| 版本 | 结果 |
|---|---|
| 部署前基线 | 669 passed / 18 failed |
| 本次改动后 | **696 passed / 18 failed**（+27 新用例，失败集合**完全一致**） |

18 个失败均为历史遗留，与本次改动无关：

- 7 个断言过时：期望 `NoAvailableModelError`，实际抛 `ProviderError("No fallback group available")`
- 3 个需真实服务端口的 integration 用例（`httpx.ConnectError` / 401）
- 1 个 RoundRobin 计数器回绕
- 其余为旧 `GroupRouter` 语义的遗留断言

## 4. 生产端到端验证

| 检查项 | 结果 |
|---|---|
| supervisor 状态 | ✅ `RUNNING` pid 516550 @ :4000 |
| `/health` | ✅ `{"status":"ok","service":"botflow"}` |
| 数据完整性 | ✅ providers 7 / models 567 / groups 5 / group_models 7 / api_keys 1（与升级前一致） |
| 非流式 `fast` | ✅ 返回 `P81-OK`，**由 `deepseek-v4.1-flash`(model 559 @ provider 2) 服务** |
| 流式 `fast` | ✅ 52 行 SSE |
| **80KB prompt 流式** | ✅ 首包 8.0s |
| **600KB prompt 流式** | ✅ HTTP 200，首包 32.0s，总 34.0s（**旧 30s 阈值下必死**） |
| **600KB prompt 非流式** | ✅ HTTP 200 in 37.9s，`content=BIGOK` |
| 落库归属 | ✅ 最近 5 条请求全部 `group_id=1, model_id=559, provider_id=2, status=success`（改动前全部落在 backup 组的 540/8、14/4） |
| 重启后日志 14:0x | ✅ 12 行，`WARNING`/`ERROR` **0 条** |
| 重启后首包超时 | ✅ **0**（13:12 事故那 4 条在重启前） |
| 重启后 `MissingSessionID` | ✅ **0** |
| 重启后 `error code: 1010` | ✅ **0** |
| 重启后 `ProviderError` | ✅ **0** |

## 结论

新增代码 **27/27 通过、100% 覆盖、零回归**；生产端 `fast` 分组主模型已恢复，
历史故障形态（600KB 长上下文 + 首包超时）已不再复现。
