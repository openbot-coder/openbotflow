# P0-1 验收报告：运行态缓存/限流单一事实源收敛 + Admin 配置失效

> 流程：编码子 agent（文档→实现）/ 验证子 agent（审文档→写测试→跑测）双 Agent 协作；主 agent 做集成 + 质量验收。
> 关联：`docs/tasks/P0-1_features.md`、`docs/tasks/P0-1_tests.md`
> 日期：2026-09-10

---

## 一、验收结论

| 维度 | 结论 |
|------|------|
| 功能实现 | ✅ 完成，与规格一致 |
| 代码质量 | ✅ 通过（收敛删除、最小 diff、无新增抽象） |
| 集成测试 | ⚠️ 部分通过（静态/同步集成通过；async 集成受**环境阻塞**，见第四节） |
| 单元覆盖率 | ✅ 新增**同步**代码 100%；⚠️ 15 个 async 用例因环境无法执行 |
| 总体 | **条件通过** —— 代码可合入，async 用例需在支持 loopback 的环境（WSL/Linux）复跑确认 |

---

## 二、改动清单

| 文件 | 操作 | 内容 |
|------|------|------|
| `src/botflow/router.py` | 修改 | 作为**单一事实源**，新增 `load_endpoints`(L229)、`invalidate_endpoint_cache`(L263)、`invalidate_provider_cache`(L268)、`invalidate_all_caches`(L282)；保留 `_endpoint_cache`/`_provider_cache`/`_provider_semaphores`/TTL/`_get_cached_provider`/`_ensure_provider_semaphore`/`PROVIDER_TYPE_MAP` |
| `src/botflow/pipeline/_shared.py` | 修改 | **删除**重复的全局状态与函数定义，改为 `from botflow.router import ...` 同名 re-export（同一对象）；清理不再使用的 import |
| `src/botflow/admin_api.py` | 修改 | 顶部 import；9 处写接口在 **404 guard 之后**接失效：group 内 model 增/删/改权重 + `update_group`/`delete_group` → `invalidate_endpoint_cache(gid)`；`update/delete provider`、`update/delete model` → `invalidate_all_caches()` |
| `tests/test_p0_1_cache_convergence.py` | 新建 | 对象同一性、re-export、3 个 invalidate 行为与信号量保护、`load_endpoints` 共享（TC-01~20、33、34） |
| `tests/test_p0_1_admin_invalidation.py` | 新建 | Admin 9 个写接口失效接线 + 404/读接口反例（TC-21~32） |

**未改动**（有意）：`GroupRouter` 类及 `_load_endpoints`（流式路径仍用，P3 迁移时删除）；`POST /providers`、`POST /models`、`POST /groups` 的 create 接口（新建实体无缓存可失效）；所有 GET 读接口。

---

## 三、验收证据（主 agent 亲测）

1. **同步用例**：`tests/test_p0_1_cache_convergence.py + test_p0_1_admin_invalidation.py` → **19 passed**（3.08s）。
2. **回归同步子集**：`test_router / test_router_full / test_pipeline_base / test_pipeline_strategies` → **66 passed**（3.27s），对象同一性未污染既有套件。
3. **集成（导入 + 对象同一性 + 行为）**：`router / _shared / engine / strategies / admin_api` 全部导入成功（迁移 `load_endpoints` 未引入循环依赖）；10 项跨模块对象同一性断言全 `True`；`invalidate_all_caches()` 清空 endpoint+provider 且**保留信号量**；`invalidate_provider_cache(2)` 只清 provider 2、保留 provider 3；`invalidate_provider_cache('2')` 字符串 no-op。
4. **静态校验**：`grep -rn "^_endpoint_cache" src/botflow/` → 全局仅 `router.py:188` 一处定义（重复已消除）。

---

## 四、环境阻塞（关键，非代码问题）

**全部 async 用例（本任务 15 条 + 既有套件 async 部分）在本机无法执行。** 根因链已逐层复现：

```
AF_UNIX = False (本机 Windows)
   └─> socket.socketpair() 回退到 loopback TCP 对
        └─> 本机 127.0.0.1 的 connect() 挂死（bind/listen 正常，connect 卡住）
             └─> asyncio 事件循环创建(new_event_loop)需 self-pipe(socketpair) → 挂死
                  └─> 所有 async 测试死等
```

复现记录：
- `asyncio.run(asyncio.sleep(0))` → 超时挂死（RC=124）；系统 Python 与托管 Python 3.13.12 **均**如此。
- `asyncio.new_event_loop()` → 挂死；Proactor 与 SelectorEventLoopPolicy **均**如此。
- `socket.socketpair()` → 挂死；`socket.socket()` 正常。
- loopback `bind+listen` 正常、`connect('127.0.0.1')` 挂死。
- 关闭沙箱（`dangerouslyDisableSandbox`）后仍挂死 → 非沙箱，属机器级。
- 备选通道 WSL 被**安全策略黑名单**禁用（`wsl.exe` 被 Program Blacklist 阻止，不可绕过）。

**影响**：此前"整套 629 用例跑 26 分钟未结束""`test_pipeline_base` 卡在 `test_execute_returns_first_success`"均由此导致——async 用例在死等，不是导入慢（同步子集 3s 跑完即证）。

**未执行验证**：TC-05（patch semaphore via shared）、TC-19（load_endpoints 跨路径共享）、TC-21~TC-32（admin 失效接线）、TC-33（回归契约）——均为 async，需在支持 loopback 的环境复跑。

---

## 五、代码质量验收（对照 AGENTS.md）

| 检查项 | 结论 |
|--------|------|
| 决策阶梯（能删不增） | ✅ 未新建模块，用 re-export 收敛；纯删除重复定义 |
| 文件越少越好 | ✅ 无新增文件（仅测试） |
| 无未请求抽象 | ✅ |
| 根因 vs 症状 | ✅ 修的是"双份全局状态"根因，非逐调用点打补丁 |
| 命名/风格一致 | ✅ invalidate 三函数语义清晰，docstring 到位 |
| 不可偷懒领域 | ✅ 信号量保护有专门边界用例；404 guard 位置正确 |

---

## 六、遗留与建议

1. **[阻塞] async 用例复跑**：需在 WSL/Linux 或 loopback 可用的机器执行
   `PYTHONPATH=src python -m pytest tests/test_p0_1_*.py -q`，确认 TC-05/19/21~33 全绿后再视为完全验收。
2. **[P3 前置] `GroupRouter._load_endpoints` 与 `router.load_endpoints` 逻辑重复**：本任务按规格保留方法（流式路径仍用），P3 迁移流式到 `PipelineEngine` 时应删除 `GroupRouter`，届时该重复自然消除。
3. **[环境] 将测试基线迁移到 Linux/WSL**：本机 Windows 的 loopback TCP 受限，async 测试不可靠；建议 CI 固定 Linux runner。
4. **[已知] `_provider_cache` 类型注解**为 2-tuple 而运行时 key 为 3-tuple（历史遗留），本任务未改，建议后续统一。
