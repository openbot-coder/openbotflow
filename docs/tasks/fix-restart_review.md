# fix-restart 功能点/测试用例文档 —— 验证子 agent 审核报告

> 审核对象：`docs/tasks/fix-restart_features.md`（审核时 548 行，正在被编码子 agent 并发修改）
> 审核人：验证子 agent（本步**只读**，未修改 `src/`、`tests/`、features 文档）
> 环境：Windows / bash / `.venv/Scripts/python.exe` / `PYTHONPATH=src`；async 测试禁跑（建 event loop 即挂死）
> 审核时点实测：`tests/test_cli_service.py` 17 passed in 0.32s；`--collect-only` 0.05s

---

## 1. 审核结论：**打回（轻度修订）** —— 设计方向通过，必改 7 条

**设计主体（① ② ③a ③b）经独立实证，方案正确、可行、成本诚实，不需要返工。**
打回只针对**文档事实性错误与测试用例的失效前提**，全部可在一次修订内完成：

- **1 条真错误**：§5.2 关于 `__main__.py` guard 体的覆盖率断言**与 coverage.py 实际行为相反**（实测证据见 §3）。
- **4 条测试设计缺陷**：T1.5 缺 `text=True`、T2.2 的 `cmd.index("run")` 判据脆弱、T4/F9 用例集自相矛盾（T9.2 在新设计下**不可满足**）、用例总数需重算。
- **2 条 F9 相关（与并发修改重叠，由主 agent 协调）**：§6 `:123-127` 仍写「不用改」、§10 仍写「`116-118` 守卫不变」——这两条在采定「删守卫」后是**错误指令**，会直接误导实现。

> ⚠ **并发修改说明**：审核时文档处于半更新状态——头部（`:3-6`）、§1.5（`:96-102`）已改为「④/F9 采定删守卫」，但 §3.9（`:285`）、§4 F9（`:421`）、§4 F4（`:369-377`）、§6（`:466`）、§7 #5（`:487`）、§9（`:535`）、§10（`:542`）**仍是旧版「待裁决/不做」**。按主 agent 指示，此类差异**不作为打回依据**，仅在 §5 列出清单待协调。

---

## 2. 逐项审核表

| # | 审核点 | 判定 | 依据 / 实测 |
|---|---|---|---|
| 1 | F1–F8 功能点完整性 | **通过** | 8 个功能点逐条对应 ①②③a③b 的代码面，无遗漏、无跑题；F1→`__main__.py`、F2/F3→cmd 拼装、F4→（见 F9）、F5→PID、F6→平台分支、F7→stderr、F8→存活校验 |
| 2 | 正/反/边界覆盖 | **通过（除 F4）** | 每个 F 均有正例+反例+边界；F4 整体被 F9 取代（见 §6） |
| 3 | F2 强断言（假 Popen 的 cmd 喂回自家 parser） | **通过，且是本文档最好的设计** | 反例 T2.4 让断言**非恒真**：实测旧顺序 `parse_args` → `SystemExit 2`（`botflow: error: unrecognized arguments: --workspace /tmp/x`） |
| 4 | F7 `with` 句柄语义（「父关子不丢」） | **通过，已亲自实证** | 见 §3「实测 A」：父 `with` 退出后子进程继续写，两段内容 + 旧内容全在文件里 |
| 5 | F8 `wait(timeout=grace)` 语义与竞态 | **通过** | 见 §3「实测 B」：`grace=0` 对活子进程 `TimeoutExpired`(0.0000s)、对已退出子进程返回真实码 7；假阴性仅剩「grace 之后才死」（§8.3 已诚实记录） |
| 6 | `startup_grace=2.0` 取值与代价 | **通过** | 冷启动代理测量 0.30s；成功路径实测阻塞 **2.01s**（= 完整 grace），文档「最多 grace 秒」表述准确 |
| 7 | 覆盖率 100% 路径（§5） | **打回** | §5.1/§5.3/§5.4/**§5.5 全对**（§5.5 基线逐字复现），但 **§5.2 错误**（见 §3「实测 C」） |
| 8 | `exclude_lines` 是否真存在 | **通过（属实）** | `pyproject.toml:64` = `"if __name__ == .__main__.:"`；`:54-55` `source = ["botflow"]`，无 `parallel`/`sigterm`，仓库无 `COVERAGE_PROCESS_START` |
| 9 | `runpy` 手法可靠性/副作用 | **通过（无副作用，但非「必需」）** | 进程内**不启动服务**（只跑 `main()` 的 argparse 分支）；T1.2 实测打印 `botflow v3.0.0`。但按 §5.2 的更正，它**不是覆盖率必需载体** |
| 10 | §6 既有用例破坏面 | **部分打回** | `:99-106` 判断**正确**（含 `res["pid"]` KeyError 的预判）；`:123-127` 判断**错误**（F9 相关，§6.3）；`test_cli_main.py:216-222`「不用改」**正确**（`cmd_restart` 不传 `startup_grace`，lambda 签名匹配）；`tests/test_cli.py:328-331` 无 `restart_service` 引用**正确**（行号应写 328 而非 340+） |
| 11 | 范围纪律 | **通过** | F1–F9 全部可溯源到 ①②③④；§7「明确不做」8 条均有理由，未发现夹带改动 |
| 12 | 可执行性（用例名/落点） | **通过（3 个小修）** | 用例名全为合法 pytest 标识符；`tests/` 现无 `test_cli_restart.py`（无冲突）；无 `conftest.py`、无 `Makefile`，新文件须自包含夹具 |
| 13 | F9（已纳入范围） | **通过（实现判断正确）** | 见 §6 |

---

## 3. 三条关键实测证据（可复现）

**实测 A — ③a 句柄语义：文档说法正确。** 临时目录（仓库外）真实子进程，父进程 `with open(log,"ab")` 内 `Popen`，父块退出后子进程继续写：

```
spawned; err handle mode = ab
with-block exited -> parent handle closed = True
child rc: 0
FILE: b'OLD-FROM-SUPERVISORD\nCHILD-BEFORE-PARENT-CLOSECHILD-AFTER-PARENT-CLOSE'
```

→ 父关自己的 fd **不会**让子进程写入丢失（`Popen` 已把 fd dup 给子进程），旧内容未截断。`"ab"` 模式字符串实测为 `"ab"`，T7.1 的 `.mode == "ab"` 断言成立。

**实测 B — ③b `wait` 语义：文档说法正确。**

```
wait(timeout=0) after child already exited -> returncode = 7
grace=0:    TimeoutExpired after 0.0000s -> treated ALIVE
grace=0.01: TimeoutExpired after 0.0162s -> treated ALIVE
success path blocks 2.01s (startup_grace=2.0)
```

→ §2(d)「`startup_grace<=0` 语义由 `wait()` 决定、不加参数校验」站得住。T7.4 的 `open()` 失败路径实测为 `PermissionError`（`OSError` 子类，`pytest.raises(OSError)` 成立）。§7 #2 的「子 parser 同名 dest 会把根值清成 None」实测成立（`workspace = None`；加 `default=SUPPRESS` 才是 `'A'`）。

**实测 C — §5.2 错误（唯一真错误）。** coverage.py 7.16.1，`exclude_lines` 命中 `if __name__ == .__main__.:` 时，**整个 if 块（含 guard 体）都被排除**，不是只排 guard 行：

```
--- run_name='__main__'  executable_lines=[1] excluded=[2, 3] missing=[]
--- run_name='not_main'  executable_lines=[1] excluded=[2, 3] missing=[]
--- no-command path      executable=[1] excluded=[2, 3] missing=[]
```

（合成文件行号：1=`from ... import main`，2=`if __name__...`，3=`    main()`。）
**即使 guard 体从未执行（`run_name="not_main"`）也 `missing=[]`**，即该文件仅凭 `import botflow.__main__`（T1.1）就是 100%。
→ 文档「guard 体是独立语句，必须被覆盖…只剩被执行的 `main()` 行」**事实错误**。结论（不需要 `# UNCOVERED:`）仍正确，但**理由必须改写**。
→ 连带：**T1.5 成为唯一跨进程验证①的用例**，其 `pytest.skip` 兜底（§8.4）不可成为常态；T1.2/T1.3/T1.6 **不得因「不是覆盖率载体」而删除**——它们是 `__main__.py` 体（`main()` 调用、SystemExit 透传）的**唯一功能守卫**（那行已被 coverage 排除，漏写 `main()` 也报 100%）。

---

## 4. 必改项清单（可直接执行）

| # | 位置 | 必改内容 | 依据 |
|---|---|---|---|
| **M1** | §5.2（`:438-440`） | 改写：`exclude_lines` 命中后**整个 if 块**被排除（实测 `excluded=[2,3]`），`__main__.py` 只有 import 一行可执行语句，`import botflow.__main__` 即得 100%；T1.2/T1.3/T1.6 的定位从「覆盖率载体」改为「`__main__.py` 体的功能守卫（coverage 不覆盖那行，故不可删）」 | §3 实测 C |
| **M2** | T1.5（`:343`） | 补 `text=True`（否则 `stdout` 是 `bytes`，「含 `botflow v`」断言写法未定）；并明确子进程预检用 `subprocess.run([sys.executable,"-c","import botflow"])` | 代码细节，避免一轮返工 |
| **M3** | T2.2（`:353`） | `cmd.index("--workspace") < cmd.index("run")` 依赖字符串位置，建议以 `cmd[3] == "--workspace"` 与**子命令位置固定**为准（或断言 `cmd.index("run") == cmd.index("--workspace") + 2`） | 判据稳健性 |
| **M4** | T4.1–T4.5（`:373-377`） | 整块重写为 F9 语义：①**删除** T4.2/T4.5（新设计下不 spawn 永不发生）；②T4.3/T4.4 失去区分力（任何 message 都会 spawn），改写为「stop 的返回值被忽略」；③新增「stop_service 仍被调用 1 次」（防止实现者顺手删掉 stop 调用） | 采定删守卫 |
| **M5** | T9.2（`:426`） | **不可满足**：断言 `{"ok": False, "message": "kill failed: permission denied"}` 时不 spawn，而删守卫后必然 spawn。删除，或反转为「stop 返回值不影响 spawn 决策」 | 采定删守卫 |
| **M6** | §4 计数（`:429`） | F4 与 F9 合并后重算用例总数与正/反/边界配比（现「43+3」不再成立） | M4/M5 |
| **M7** | §10（`:542`） | `service.py` 行改为：**删除 `116-118` 守卫** + `stop_result` 改为裸调用 `stop_service(workspace)`；同时 §6（`:463`）`:123-127` 的「不用改」改为「**必须重写或删除**」（理由见 §6.3） | 与采定 ④ 直接冲突 |

**（F9 相关的 M4/M5/M7 与并发修改重叠，请主 agent 判定是并入编码子 agent 的本轮修订还是单独协调。）**

---

## 5. 已实测确认、无需修改的文档主张（逐条附证）

| 文档主张 | 实测结果 |
|---|---|
| §5.5 基线 `95 stmts / 10 miss / 89%`，缺失 `83-85, 94-106, 124, 136, 170-171` | **逐字复现**：`17 passed in 0.32s`，表体完全一致 |
| §5.4 `monkeypatch.setattr("botflow.cli.main.main", fake)` 会失败 | **复现**：`AttributeError 'function' object at botflow.cli.main has no attribute 'main'`；`sys.modules[...]` 写法 OK |
| §8.6 `tests/test_cli_service.py:113-120` 名字骗人 | **正确**：该用例让 `os.kill` 无条件抛 → `is_running` 返回 False → 在 `:79` 返回，故 `83-85` 未覆盖 |
| §7 #6 `wait(timeout<=0)` 语义确定 | 见 §3 实测 B |
| §8.4 本机 editable 安装，无需 `PYTHONPATH` 也能 `import botflow` | 实测 `import botflow` → `E:\src\openbotflow\src\botflow\__init__.py`，`PYTHONPATH` 未设 |
| F6 `svc.sys.platform` monkeypatch 可切换分支 | 可行（`service.py` 调用时读 `sys.platform`）；T6.2 用 `raising=False` 注入 `CREATE_NEW_PROCESS_GROUP` 是**必要**的（Linux 上该属性不存在） |
| F8 失败路径不需 `clear_pid` | **正确**：`stop_service` 的 `:78/:84/:92/:105` 四条 `ok=True` 路径都已 `clear_pid` |
| §1.3 退出码「只修①时 rc=2、当前 rc=1」 | 方向正确（详见 §7 需裁决） |

**关于 §7 #2（不把 `--workspace` 加进 run 子 parser）**：实测确认副作用真实存在（`workspace = None`），文档选「换位置」正确；`SUPPRESS` 可行但更复杂，同意不做。

---

## 6. F9 独立评审（已纳入范围）

### 6.1 前提核实：`stop_service` 全部返回路径（逐条）

| 行 | 条件 | 返回 | 是否 `clear_pid` |
|---|---|---|---|
| `:75` | `read_pid()` 为假（无 PID 文件） | **`ok=False`** `"No PID file found — service may not be running."` | 无（也不需要） |
| `:79` | PID 存在但进程不在 | `ok=True` `"Process {pid} not running (stale PID cleaned)."` | ✅ `:78` |
| `:85` | `os.kill` 抛 `ProcessLookupError` | `ok=True` `"Process {pid} already gone."` | ✅ `:84` |
| `:93` | deadline 内进程退出 | `ok=True` `"Service (PID {pid}) stopped."` | ✅ `:92` |
| `:106` | 超时后强制 kill | `ok=True` `"Service (PID {pid}) killed after timeout."` | ✅ `:105` |
| （异常） | T 其它 `OSError`（如 `PermissionError`）或 `clear_pid` 失败 | **抛出**，不是 dict | — |

**结论：前提成立——唯一 `ok=False` 就是 `:75`。** 且 `:75` 的文本确为 `not be running`，**不含子串 `"not running"`**，白名单支路 `:117` 对真实消息**永不可达**。

### 6.2 主 agent 的「直接删 `:117-118`」判断：**正确**

理由（三条独立成立）：

1. **守卫想拦的情况不存在**：唯一可触发 `not stop_result["ok"]` 的输入就是 `:75`，而它恰恰是「服务本来没跑」——此时**正应该 spawn**。守卫不是「未生效」，而是**方向反了**：唯一可达路径上做错了事。
2. **它也没在拦真正的停止失败**：`os.kill`（`:82`）只捕 `ProcessLookupError`，`PermissionError` 等**会抛异常**而非返回 `ok=False`。所以「停止失败不许 spawn」这条保护今天**不可能通过 `ok=False` 触发**——删守卫**不减少现有保护**。
3. **最小改动**：不改 `stop_service` 契约 → `botflow stop` 的退出码不变（`:118` 的 `sys.exit(0/1)` 不受影响），`tests/test_cli_service.py:72-73` 无需改，波及面小于文档原列的方案 (i)。

**但有三条必须一并落实**（否则是个半吊子删除）：

- **(a) 只删分支，不删调用**：必须保留 `stop_service(workspace)` 的**副作用**（真的把旧服务停掉），改为裸调用、丢弃返回值。文档若只删 `:117-118` 而保留赋值，会留下无用变量 → **ruff `F841`**（实测：`F841 Local variable 'x' is assigned to but never used`；`service.py` 现有 4 条 ruff 报告均为**既有**问题（UP045×2/BLE001/UP045），F841 会是**本次新增**）。
- **(b) 记录「为什么没有守卫」**：删后若将来给 `stop_service` 增加一条真实的 `ok=False` 失败路径（例如捕获 `PermissionError` 后返回 `ok=False`），`restart` 会**静默双开**（两个服务抢同一端口）。建议一行注释说明「今日停止失败以异常形式暴露，故无守卫；若改为返回 ok=False 需恢复守卫」。这是一行注释，不算越界。
- **(c) 失去的唯一东西**：`restart` 不再能报告「停不下来的旧服务」。既然该情况今天以**异常**形式暴露（进程非零退出 + traceback），可接受。
  附带说明：`main()` 不吞异常，故 F7 的 `open()` 失败会以 traceback + **退出码 1** 结束（而不是 JSON + `sys.exit(1)`）。shell 仍能拿到失败，与「宁可见的崩溃」一致，但文档没写清「退出形态从 JSON 变 traceback」，建议补一句。

### 6.3 `tests/test_cli_service.py:123-127`：**「断言还在、意图已死」——支持主 agent 打回**

现状（逐字核实，`:123-127`）：

```python
def test_restart_when_stop_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(svc.subprocess, "Popen", lambda *a, **k: MagicMock(pid=1))
    monkeypatch.setattr(svc, "stop_service", lambda ws: {"ok": False, "message": "denied"})
    res = svc.restart_service(tmp_path)
    assert res["ok"] is False
```

删守卫 + ③b 之后它**仍然通过**，但通过的理由完全变了：
`MagicMock().wait(timeout=2.0)` 返回 MagicMock（**不抛** `TimeoutExpired`）→ `else:` 分支判「子进程已退出」→ 返回 `ok: False` → 断言满足。
即：**它从「停止失败所以不启动」变成了「启动了但被③b判为秒死」——测试名与断言之间已经没有因果关系**，它不再保护任何东西，还会在实现者误改守卫时给出假绿灯。

**处理要求（必改）**：删除该用例，或重写为能表达新契约的用例，例如
`test_restart_ignores_stop_result_and_spawns`：mock `stop_service` 返回 `{"ok": False, ...}` → 断言 **Popen 被调用 1 次**（佐证守卫已删）**且 `stop_service` 被调用 1 次**（佐证副作用保留）；把「秒死 → ok False」的职责完全交给 T8.2。
文档 §6 当前写的「不 spawn → 不触及 wait/③a → **不用改**」在采定 F9 后是**错误指令**（M7）。

同时确认 §6 另两行判断无误：
- `:99-106` **必须改**（正确）：假 `proc` 的 `wait` 需配 `TimeoutExpired`，否则 `ok: False` 且**没有 `pid` 键** → 首断言先挂、第二个断言 `KeyError`。
- `test_cli_main.py:216-222` **不用改**（正确）：`cmd_restart` 传 `host=/port=/config_path=`，不传 `startup_grace`。
- 补充：`:126` 的 `svc.restart_service(tmp_path)` 在 ③a 后会创建 `tmp_path/logs/botflow.err.log`（库内、无害）。

---

## 7. 额外发现（文档未列出）

1. **成功路径会触发 `ResourceWarning: subprocess NNNN is still running`**（实测）。③b 判「存活」后函数返回，`Popen` 对象被回收而子进程仍在跑 → `__del__` 发警告。生产无碍（CLI 随即退出），但若将来有人加 `filterwarnings = ["error"]` 或在测试里真跑子进程，会变成失败。建议在 §8 记一条「已知无害噪声，不加 filterwarnings」。
2. **子进程持有 err log fd 至其整个生命周期**：日志不可 rotate（删除后子进程继续写已删除 inode）。与 supervisord 同性质，建议一句话记录，避免以后误判为 bug。
3. **`docs/tasks/fix-restart_features.md:11` 标题「三个实证故障点」**与头部「4 组改动」不一致（残留）。
4. **`tests/test_cli.py` 的行号引用**：文档写 `:340+ TestServiceManagement`，实际 `test_restart` 在 `:328-331`；结论（无 `restart_service` 引用）正确。
5. **T7.2/T7.5 的强度**：`Popen` 被 mock 时无人写文件，故「未被截断」是**真的**在测 `open` 模式（而非测写入），这是正确的测法；但应在 docstring 里写明「本用例测的是打开模式，不测子进程写入」，免得后来者误以为是端到端日志测试。
6. **③b 无法覆盖「启动成功但 2s 内进程已死」之外的情形**已由 §8.3 声明；但注意它**同样无法覆盖「退出码非 0 但进程仍存活」**（如 uvicorn 降级运行）——不作要求，仅提示边界。

---

## 8. 需主 agent 裁决的点

1. **覆盖率放行门槛的具体定义**：`service.py` 基线 89%，历史缺口 `83-85 / 94-106 / 170-171` 与本次改动**无因果**。§7 #7 声明「不重写历史缺口」，但若门槛是「模块 100%」，则必须补（§8.6 已给出可行配方：`83-85` 需 `is_running` 先真后让 `os.kill` 抛 `ProcessLookupError`；`94-106` 需假时钟 + `is_running` 恒真；`170-171` 需 `open()` 抛错）。**请明确二选一**：(a) 门槛 = 「无**新增** miss，历史缺口登记在案」；(b) 门槛 = 模块 100%，则历史缺口补测纳入本轮。另注意 `94-106` 中 `:97/:103` 已带 `# UNCOVERED:` 注释，与 §8.6「不新增 UNCOVERED」的一致性需一并说明。
2. **本机无法验证全项目覆盖率**：整套测试（含 async）在本机挂死，我**只能**以 `tests/test_cli_service.py` + 新 `tests/test_cli_restart.py` 为单位给出 `--cov=botflow.cli.service` 的证据。全项目 `--cov=botflow` 的 100% 只能由 CI/主 agent 认定。**请确认这个证据边界可接受。**
3. **§1.3 退出码说法**：文档称「只修①时子进程死于 argparse → `rc=2`」。方向上正确（②不改，`--workspace` 仍在 `run` 后），但①修好后 `python -m botflow run --host.. --workspace..` 的真实路径是 **runpy 找不到 `botflow.__main__` 消失 → argparse 报错 → rc=2**；而③b 的消息不硬编码退出码这一点是对的。**建议实际取值既不断言 rc==2 的具体性，只断言 rc != 0**（T8.2 用假退出码，不受影响）。
4. **F9 的 `docs/design.md` 同步**（§9 `:535`）：删守卫是**行为变更**（全新 workspace 上 `restart` 从「失败退出 1」变「直接启动」）。请确认是否要求本轮同时更新 `design.md:440` 条目。

---

## 9. 我的不确定项

- **§5.2 的结论是否会被 coverage 版本/配置改变**：我实测的是 coverage.py **7.16.1** + 本仓库 `exclude_lines`。「if 块整体排除」是其既有语义，但若将来升级 coverage 或改动 `exclude_lines`，`__main__.py` 可能重新变成 1 miss。M1 的改写会把这个前提写明，属可接受风险。
- **POSIX 分支（`start_new_session=True`）与 `CREATE_NEW_PROCESS_GROUP` 的真实语义**未在 Linux/真实脱离终端场景下验证（本机只有 Windows），只能靠 T6.1/T6.2 的 monkeypatch 覆盖 —— 与 §8.7 一致。
- **supervisord 并发追加同一 err log 文件**（§8.5）本机无法实测（无 supervisord）；但按实测 A，单写者追加与旧内容保留是成立的，并发仅剩「两进程交错写」的行级完整性风险，与本 bug 无关。
- **③b 在「子进程已死但退出码待取」的极窄窗口**无实测（假 Popen 掩盖）；因 `wait()` 返回即已 reap，判定确定，风险可忽略。

---

**审核签署**：验证子 agent ｜ 结论 = **打回（设计通过，M1–M7 修订后即可进第 2 步）**
