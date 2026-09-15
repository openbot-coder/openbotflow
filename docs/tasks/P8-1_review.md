# P8-1 验收报告

## 范围

| 项 | 内容 |
|---|---|
| 需求 | ① 首包超时设为 90s；② `openai_compat.py` 支持 `extra_config.headers`，给 provider 2 配 UA + session 头，救活 OpenCode 模型 |
| 改动文件 | `src/botflow/providers/openai_compat.py`（+45 行）、`tests/test_openai_compat_headers.py`（新增） |
| 配置改动 | 生产 `.env` 增 `BOTFLOW_STREAM_TIMEOUT=90`；provider 2 的 `extra_config` 写入 headers |
| 不涉及 | 分组组成、nginx、数据库 schema、其余 6 个 provider |

## 决策阶梯核对

| 阶梯 | 判断 |
|---|---|
| ① 真的需要建吗 | 需要：上游硬性要求会话头，网关无注入口子 |
| ② 代码库是否已有 | **无**。`extra_config` 只读 `mode`/`api_version`/`timeout`/`proxy` |
| ③ 标准库能做吗 | `hashlib`（标准库）+ openai SDK 原生 `extra_headers` 参数 |
| ④ 原生平台特性 | openai SDK 的 `extra_headers` 原生支持，无需自建 HTTP 层 |
| ⑤ 已装依赖能解决 | 是，未引入任何新依赖 |
| ⑥ 能写成一行吗 | 朴素一行 `default_headers=...` 不够：SDK client 被 `router._get_cached_provider` 缓存，静态 session 会**全站共用一个会话**，与上游「每会话一个稳定 ID」的要求相悖。故取 `extra_headers` 按请求注入 |
| ⑦ 最少代码 | 新增 2 个模块级函数（`_conversation_id` / `_apply_headers`）+ 2 处参数，无新类、无新依赖、无新配置项 |

## 规则清单核对

| 规则 | 结论 |
|---|---|
| 不创建未经请求的抽象 | ✅ 仅 2 个模块级函数，无类、无配置项、无插件机制 |
| 不引入可避免的依赖 | ✅ 0 新依赖（`hashlib` 是标准库，`extra_headers` 是 SDK 原生参数） |
| 不搭投机脚手架 | ✅ 未加"未来可能需要的 header 模板语法"，只支持需求明确的 `{conversation_id}` |
| 优先删除而非添加 | 无删除空间（本模块原本就不支持 headers）；未顺手重构 Azure/usage 等既有分支 |
| 宁可无聊不耍聪明 | ✅ 摘要算法就是 `sha256(...)[:32]`，不引入 UUID/加密/缓存 |
| 文件越少越好 | ✅ 未新建生产文件 |
| 最短 diff 胜出 | ✅ 生产代码 +45 行，含 docstring |
| 边界情况正确 | ✅ 见下 |

## 不可偷懒领域核对

| 领域 | 结论 |
|---|---|
| 校验 | ✅ `headers` 非 dict / 空 dict / `None` 一律降级为 `None`，不抛异常、不改行为；键值强转 `str` |
| 错误处理 | ✅ 既有 `try/except → ProviderError` 包装未被改动（T3.3/T3.4 覆盖） |
| 安全 | ✅ header 值仅来自 DB 中管理员写入的 `extra_config`，非用户请求可控；不透传任何客户端 header，无注入面 |
| 数据保护 | ✅ 升级前 `conn.backup()` 一致性快照（339,935,232 字节）+ 代码包 + 行数基线，存 `/mnt/deploy/backups/botflow-20260915-135958.*` |
| 边界情况 | ✅ 空 messages、无 user 消息、`content=None`、多模态 list 内容、未闭合 `{conversation_id`、同一 header 混合静态值 |
| 理解 | ✅ 追踪了 `core._stream_common → engine.route_stream → ep.provider.chat_stream → client.create` 全链路，确认 client 被缓存才选择按请求注入 |
| 测试 | ✅ 27 个可运行检查，100% 覆盖新增代码 |

## 遗留风险

1. **`{conversation_id}` 依赖客户端保持消息历史前缀不变**。若某客户端每轮重写 system 提示词
   （或截断历史导致首个 user 消息变化），同一会话会得到不同 id。
   上游对此的惩罚仅是 prompt 缓存命中率下降，**不影响可用性**（静态 session 已实测可复用）。
2. **provider 1（`vex`）与 provider 2 同 base_url**，但 provider 1 的 key 已
   `CreditsError: Insufficient balance`。本次未处理，未影响 `fast`（只挂 provider 2 的模型）。
3. **`fast` / `smart` 分组仍各只挂 1 个模型**。主模型恢复后已能正常服务，但单点无冗余；
   若 OpenCode Go 侧限流，仍会降级到 `backup`。
4. **OpenCode Go 条款面向 coding agent 且监控滥用流量**。本次按官方文档如实上报
   自有 UA（`botflow-gateway/1.0`）+ 会话头，未伪装为特定客户端，符合其要求。
5. 18 个历史遗留测试失败仍在，真实回归信号会被噪声淹没，建议单独修一版。

## 验收结论

| 项 | 结论 |
|---|---|
| 单元测试 | ✅ 27/27 通过，新增代码 100% 覆盖 |
| 全量套件 | ✅ 696 passed / 18 failed（失败集合与基线完全一致，零回归） |
| 集成/端到端 | ✅ 生产 11 项检查全绿，600KB 长上下文不再超时，`fast` 主模型恢复服务 |
| 数据完整性 | ✅ 7 providers / 567 models / 5 groups / 7 group_models / 1 api_key 不变 |
| **放行** | ✅ **通过** |

## 回滚

```bash
sudo supervisorctl stop botflow
tar xzf /mnt/deploy/backups/botflow-code-20260915-135958.tgz -C /mnt/deploy/botflow
cp /mnt/deploy/backups/botflow-20260915-135958.db /mnt/deploy/botflow/data/botflow.db
# 还原 provider 2 extra_config（可选，单独回滚配置时）
sqlite3 /mnt/deploy/botflow/data/botflow.db "UPDATE providers SET extra_config='{}' WHERE id=2;"
# 去掉 .env 中的 BOTFLOW_STREAM_TIMEOUT=90（可选）
sudo supervisorctl start botflow
```
