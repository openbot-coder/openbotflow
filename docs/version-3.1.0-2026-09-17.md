# v3.1.0 发版记录（2026-09-17）

> `3.0.0` → **`3.1.0`**（行为修复级）。三件事：① 补齐 fix-restart 缺失用例；② 修复 8 个既有失败；
> ③ 版本号统一。全部在 **Linux（mq3.vxquant.com）** 上实测，本机（Windows）无法跑 async 用例。

---

## 1. 变更清单

| 提交 | 内容 |
|---|---|
| `fix(pipeline)` | 修复 8 个既有失败：`RoundRobinStrategy` 计数器 wrap / 路由异常类型保真 / 过时用例改写 |
| `test(cli)` | 补齐 fix-restart 42 个缺失用例（4 → **46**），`cli/service.py` 达 **100%** |
| `chore(release)` | `3.0.0` → `3.1.0`，6 处硬编码同步 |
| `docs` | 8 个既有失败根因分析 + mq3 部署记录更正 |

### 1.1 版本号 6 处（比原记录的 5 处多 1 处：README）

| 文件 | 位置 |
|---|---|
| `pyproject.toml` | `[project].version` |
| `src/botflow/__init__.py` | `__version__` |
| `src/botflow/core.py` | `FastAPI(version=...)` |
| `src/botflow/static/admin/index.html` | 顶栏 `Botflow vX`（**最易漏**） |
| `docs/design.md` | 头部「版本 / 最后更新」+ 校对声明 |
| `README.md` | 「当前版本」行 |

---

## 2. ① 补齐 fix-restart 缺失用例

`docs/tasks/fix-restart_features.md §4` 规划 **46** 个用例（T1.1–T9.3），此前只落地 **4** 个（全是 F1）。
本次补齐，按规格分四类：

| 类 | 功能点 | 数量 |
|---|---|---|
| `TestMainModuleEntry` | F1 `python -m botflow` 入口 | 6（T1.1–T1.6） |
| `TestRestartCommandLine` | F2–F6 命令行拼装 / `--config` / 守卫删除 / PID 文件 / 平台分支 | 25 |
| `TestRestartDiagnostics` | F7–F8 stderr 落盘 / 有界存活校验 | 12 |
| `TestRestartGuardRemoval` | F9 无 PID 文件时不再提前放弃 | 3 |

另在 `tests/test_cli_service.py` 补 4 个**历史缺口**用例
（`83-85`：`is_running` 真→`os.kill` 抛 `ProcessLookupError`；`94-106`：假时钟 + 双平台强制 kill；
`tail_logs` 的 `except` 分支）。

**两处「规格与实现不符」，按实现为准并诚实记录**：

1. features 文档 §4 的 T7.1 要求断言 `stderr.closed is True`（基于「用 `with open(...)` 包裹」的设想），
   但**最终实现并没有用 `with`**（`stderr=open(err_log, "ab")` 直接传给 `Popen`），该断言不可满足 →
   用例只保留可验证的不变量（`name` 指向 `logs/botflow.err.log`、`mode == "ab"`）。
2. features 文档 §5.5 推荐用 `monkeypatch.setattr(sys.modules["botflow.cli.main"], "main", fake)`
   拦截入口调用，实测**拦不到**（`__main__.py` 走的是 `from botflow.cli import main`，
   而 `cli/__init__.py` 把包属性 `botflow.cli.main` 遮蔽成了函数）→ 正确目标是包属性
   `monkeypatch.setattr("botflow.cli.main", fake)`。

**实测**（mq3，`/tmp/bfcopy` + `/tmp/bfi` venv）：

```
src/botflow/cli/service.py   105 stmts  0 miss  100%
```

`service.py` 的改动**只有 2 行**：删掉两处已变得可覆盖的 `# UNCOVERED:` 注释
（强制 kill 分支现已由新用例覆盖）。`restart_service` / `stop_service` 的逻辑、签名、命令拼装一字未动。

---

## 3. ② 修复 8 个既有失败

完整根因见 **`docs/router-test-failures-2026-09-17.md`**。摘要：

| # | 根因 | 涉及失败数 | 修复 |
|---|---|---|---|
| A | `RoundRobinStrategy` 实现漏了设计规定的计数器 wrap（`next_idx` 少了 `% (len(available) * 1000)`） | 1 | 恢复取模 |
| B | LangGraph 节点把异常压成字符串 → **异常类型丢失**（非流式统一 `ProviderError`，流式统一泛化 `NoAvailableModelError`） | 6 | `_load_and_select` 存异常对象 + 新增 `_raise_routing_error()` 按原类型重抛；`_try_call` 不再用字符串覆盖已有成因 |
| C | `test_route_stream_all_on_cooldown_fallback` 与 `docs/design.md §3.5`（流式只做端点选择、fallback 归 `core.py`）**冲突** | 1 | 改写为 `..._raises_typed_error`，断言具体异常类型 |

> ⚠️ 同时**推翻**了 `docs/deploy-mq3-2026-09-17.md` §5.1 当时的结论「疑似跨用例全局冷却状态污染」：
> 在 mq3 上单独跑那 8 条用例**同样全挂**，「单独跑 / 按文件跑 / 合并跑」失败集合逐字一致 → 无污染。

---

## 4. 实测证据（Linux，`mq3.vxquant.com`）

环境：Ubuntu 24.04 / Python **3.13.15**（`uv venv` 下载的管理版）/ pytest **9.1.1**；
隔离副本 `/tmp/bfcopy`（`git archive c993667` 导出后覆盖本次改动）+ `/tmp/bfi` venv + `PYTHONPATH=src`。
**未在部署目录 `/srv/botflow` 内跑测试**（避免写 `./data`）。

### 4.1 失败数：8 → 0

| 范围 | 改前 | 改后 |
|---|---|---|
| 三个 router 相关文件 | **8 failed** / 77 passed | **85 passed / 0 failed** |
| **全量 `pytest tests/`** | **716 passed / 8 failed** / 11 deselected（239s） | **770 passed / 0 failed** / 11 deselected（151s） |

`770 − 716 = 54 = 8（转绿）+ 42（新增 restart 用例）+ 4（新增 service 历史缺口用例）` ✓

### 4.2 覆盖率（全量 `--cov=botflow`）

```
src/botflow/cli/service.py      105      0   100%
src/botflow/common/context.py    59      0   100%
src/botflow/__main__.py           1      0   100%
src/botflow/router.py           195      3    98%
src/botflow/pipeline/strategies.py          58      4    93%
src/botflow/pipeline/langgraph_engine.py   180     12    93%
src/botflow/core.py             663    252    62%
TOTAL                          4007    778    81%
```

**`pipeline/langgraph_engine.py` 的 12 个 missing 行全部是既有缺口，本次未新增也未恶化**（已逐行核对）；
其中 5 行是 `route()` 那条「图没产出 result」的兜底分支，其余是
`_resolve_group` 的「fallback 组不存在」、`_load_and_select` 的 `fatal_error` 短路、
langgraph / 未知策略的 `ConfigurationError`、`_finalize_error` 的 `isinstance(raw, Exception)` 分支。
**本次新增的所有行都被覆盖。**

---

## 5. 顺带发现、**未修**（建议另开任务）

**流式路径在「路由阶段就无可用模型」时不会尝试 fallback 分组。**

- 非流式：图内 `resolve_group` 会降级 → 主组不可用时自动用备用组。
- 流式：`_route_after_load` 流式模式直接 `END`，不经 `resolve_group`；而调用方
  `core.py::_stream_common` 的 `fallback_attempted` 只在 `route_stream()` **成功返回之后**才生效。
- 结果：主组全部冷却时，**流式请求直接 502，永不尝试备用组**；同一请求走非流式则会成功降级。
- 与 `docs/pipeline_router_design.md:15`「所有策略共享 cooldown、retry、**fallback**」的原则不一致。
- 影响面在 `core.py` 流式热路径，比本次修复大 → **本次不动**，建议单独评估。

---

## 6. 发版前置（与 P9-6 相同，仍需注意）

- 目标机需预置 `tiktoken` 的 `o200k_base` 词表（`TIKTOKEN_CACHE_DIR`）；mq3 已预置 `/home/qbot/.cache/tiktoken`。
- api.vxquant.com 仍为 **3.0.0**，尚未升级到本版本。
