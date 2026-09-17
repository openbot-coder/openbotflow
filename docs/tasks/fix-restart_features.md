# fix-restart 功能点与测试用例：让 `botflow restart` 真的把服务拉起来

> 任务类型：bug 修复（4 组改动）
> 范围（用户已拍板）：**①** 新增 `src/botflow/__main__.py`；**②** `restart_service` 命令拼装顺序；
> **③** 静默失败机制（**③a** 子进程 stderr 落盘可诊断 + **③b** 有界存活校验）；
> **④/F9** 删除 `restart_service` 顶部那道基于消息字符串的 stop 守卫
> 关联：`docs/design.md:440`（§10.1 已把本 bug 记为「高」）、`.workbuddy/memory/MEMORY.md:67`（err log 约定）

---

## 1. 背景：四组实证故障点

`botflow restart`（`cli/main.py:121-132` → `cli/service.py:109-148`）先 `stop_service()` 停服务，
再用 `python -m botflow run ...` 以脱离终端的子进程形式启动，最后 `write_pid()` 并
`return {"ok": True, ...}`。实测：**命令拼错 → 子进程秒死 → 父进程毫无察觉 → 仍然报成功写 PID**。

### 1.1 实证 0（③ 的真根因）：命令即便能起，父进程也不检查子进程死活

前置一个 stale PID 文件（让 `stop_service` 走 `ok=True` 分支）后实测本机 `restart_service()`：

```
--- scenario B: stale PID file present ---
restart -> {'ok': True, 'message': 'Service restarted (PID 29040).', 'pid': 29040}
pid file -> 29040
child alive after 2s -> False
```

`cli/service.py:127-148`：spawn 之后**没有任何存活判断**，`write_pid()`（142 行）与
`ok: True`（144-148 行）是**无条件**的；同时 `stderr=subprocess.DEVNULL`（131-132 / 138-139 行）
把子进程的报错直接扔进黑洞——`tail_logs()` 读的却是 `logs/botflow.err.log`（`cli/service.py:154`），
所以连 `botflow logs` 也看不到启动失败的原因。**这才是「静默失败」的真根因**：① ② 只是让子进程
必死，③ 让死亡无声无息。

### 1.2 故障一：`src/botflow/` 没有 `__main__.py`

`ls src/botflow/__main__.py` → `No such file or directory`。实测（`rc=1`）：

```
$ .venv/Scripts/python.exe -m botflow version
E:\src\openbotflow\.venv\Scripts\python.exe: No module named botflow.__main__; 'botflow' is a package and cannot be directly executed
```

`python -m botflow` 需要包内 `__main__.py`（CPython runpy 语义），与
`[project.scripts] botflow = "botflow.cli:main"`（`pyproject.toml:39-40`）无关。

### 1.3 故障二：`--workspace` 被放在子命令 `run` **之后**

`--workspace` 只定义在根 parser（`cli/main.py:648`）；`run` 子 parser（`cli/main.py:654-658`）
只有 `--host` / `--port` / `--config`。而 `cli/service.py:121-122` 拼出的是
`... botflow run --host … --port … --workspace …`。实测：

```
$ ... build_parser().parse_args(['run','--host','h','--port','1','--workspace','/tmp/x'])
botflow: error: unrecognized arguments: --workspace /tmp/x
SystemExit: 2
```

`--workspace` 前移到子命令之前即可解析（实测，`--config` 追加在末尾也 OK）：

```
parse_args(['--workspace','/tmp/x','run','--host','h','--port','1'])          -> workspace=/tmp/x host=h port=1 func=cmd_run
parse_args(['--workspace','/tmp/x','run','--host','h','--port','1','--config','/tmp/c.env']) -> config=/tmp/c.env
```

子进程退出码取决于修到哪一步：**只修 ①** 时 `-m botflow` 已可解析、子进程死于 argparse → `rc=2`；
**当前（①未修）** 死于 runpy → `rc=1`。故 ③b 的消息**不得硬编码退出码**。

### 1.4 为什么长期没人发现

1. 失败发生在**子进程内部**，父进程只看到 `Popen()` 成功；
2. 子进程 stderr 被 `DEVNULL` 丢弃（③a 修）；
3. 生产不是用 `botflow restart` 而是 `supervisorctl restart botflow`（`.workbuddy/memory/MEMORY.md`：
   `supervisor 管理：.venv/bin/botflow --workspace /mnt/deploy/botflow run ...`）——
   `-m botflow` 这条路径在生产几乎无人走；
4. 既有单测把 `Popen` 与 `restart_service` 双双 mock 掉（详见 §6）。

### 1.5 故障三（**已纳入本次修复**）：无 PID 文件时 `restart` 直接放弃

`cli/service.py:116-118` 的守卫判定是：

```python
stop_result = stop_service(workspace)
if not stop_result["ok"] and "not running" not in stop_result["message"].lower():
    return stop_result
```

但 `stop_service` 唯一的 `ok=False` 消息是 `cli/service.py:75`：
`"No PID file found — service may not be running."` —— **不含子串 `"not running"`**（原文是 `not be running`）。实测：

```
substring check on line-75 message: False      # 'not running' in msg.lower()
--- scenario A: fresh workspace (no PID file) ---
restart -> {'ok': False, 'message': 'No PID file found — service may not be running.'}
```

即：**在一个从未启动过的 workspace 上，`restart` 直接返回失败、根本不去 spawn**；
而 `stop_service` 其余所有路径都是 `ok=True`（79/85/93/106 行，且都已清理 PID 文件），
所以白名单支路**永不可达**，此守卫想拦的情况**根本不存在**；
`tests/test_cli_service.py:123-127` 用的是手工消息 `"denied"`（`stop_service` 永不产出）。

采定修法：**删掉守卫**（不是改字符串）——见 F9（§3.9）。行为变化：服务本来没在跑时，
`botflow restart` 从「不干活 + 退出码 1」变成「直接启动服务」。

---

## 2. 修复方案

**(a) 新建 `src/botflow/__main__.py`**

```python
"""python -m botflow entry point."""

from botflow.cli.main import main

if __name__ == "__main__":
    main()
```

与 `cli/main.py:895-896` 同款守卫；不改 `pyproject.toml`、不动 `cli/__init__.py:3`。

**(b) `cli/service.py:121-122` 调整 cmd 顺序**（`--workspace` 前移，其余语义不变；
`--config` 仍追加末尾——它是 `run` 子 parser 的选项）：

```python
cmd = [sys.executable, "-m", "botflow", "--workspace", str(workspace), "run",
       "--host", host, "--port", str(port)]
if config_path:
    cmd.extend(["--config", config_path])
```

**(c) ③a：子进程 stderr 改道 `{workspace}/logs/botflow.err.log`（追加）**

```python
log_dir = workspace / "logs"
log_dir.mkdir(parents=True, exist_ok=True)          # logs/ 可能不存在
err_log = log_dir / "botflow.err.log"               # 与 tail_logs():154 同一路径
with open(err_log, "ab") as err_file:               # 必须 "ab"：不截断 supervisord/历史内容
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err_file, **platform_kwargs)
```

- 复用理由（**不是新接口**）：`tail_logs()` 读的就是这个文件（`cli/service.py:154`），
  `scripts/deploy.sh:67` 的 supervisord `stderr_logfile` 用的也是同一路径（`:90` 的失败提示也指它）。
  loguru 也把 WARNING/ERROR 写到 stderr（`common/logger.py:28-38`）→ 子进程启动期的报错会落进同一个文件，
  `botflow logs` 零改动就能看到。
- `stdout` 保持 `DEVNULL`（uvicorn access 日志由 supervisord 的 out.log 管，不越界）。
- **句柄生命周期**：`Popen` 会把 `err_file` 的 fd `dup` 给子进程，父进程在自己的
  `with` 块退出时关闭自己那一份——子进程继续写，父进程不留 fd 泄漏。
- 打开失败（如路径被目录占用）**不吞异常**：宁可见的崩溃，也不要回到静默成功（见 §8 残留风险）。

**(d) ③b：`write_pid()` 之前做有界存活校验**

```python
def restart_service(workspace, host="0.0.0.0", port=8080, config_path=None,
                    startup_grace: float = 2.0) -> dict:
    ...
    try:
        proc.wait(timeout=startup_grace)     # 抛 TimeoutExpired = 还在跑 = 好
    except subprocess.TimeoutExpired:
        pass
    else:                                    # 自己退出了 = 启动失败
        return {"ok": False,
                "message": f"Service failed to start (exit code {proc.returncode}); "
                           f"see {err_log} (or run `botflow logs`)."}
    write_pid(workspace, proc.pid)
    return {"ok": True, "message": f"Service restarted (PID {proc.pid}).", "pid": proc.pid}
```

参数名/默认值决策（**`startup_grace: float = 2.0`**）：

- 名字描述的是「启动宽限期」，**刻意不叫 `health_timeout` / `readiness_*`**：我们做的是「有没有秒死」，
  不是 `/health` 就绪探测（一个被明确排除的新功能）。
- 放在签名**最后**且有默认值 → `cli/main.py:124-129` 的 kwargs 调用与所有既有调用零改动。
- `float` 便于测试传 `0` / `0.01`；2.0 的理由：本机实测 `python -m botflow version` 全链路 < 0.4s，
  2s 留出解释器启动 + `import botflow` 富余；远小于 `stop_service` 的 10s 超时与 supervisord 的 `startsecs=5`。
- 代价（必须知道）：**成功路径也会多阻塞最多 `startup_grace` 秒**。`restart` 是运维手工命令，
  2s 可接受；不为此引入异步/后台校验。
- `startup_grace <= 0` 的语义由 `wait(timeout=…)` 决定：立即返回 `TimeoutExpired`（除非子进程已退出）
  → 等价「不等，只抓已经死了的」。**不额外做参数校验**：这是内部关键字参数、无外部输入，
  `wait()` 已给出确定语义，加一个 `if grace < 0: raise` 只是多一条要测的分支（决策阶梯第 1/3 级）。

**(e) ④/F9：删掉 `restart_service` 顶部那道 stop 守卫**（`cli/service.py:116-118`）

```python
# 删除前（3 行）
stop_result = stop_service(workspace)
if not stop_result["ok"] and "not running" not in stop_result["message"].lower():
    return stop_result

# 删除后（1 行，纯调用、不读返回值）
stop_service(workspace)
```

理由（决策阶梯第 4 级「优先考虑删除而非添加」）：

- `stop_service` 的**唯一** `ok=False` 路径是 `cli/service.py:75`「无 PID 文件」（服务本来就已停），
  而 `:79 / :85 / :93 / :106` 四条路径全是 `ok=True` 且都已 `clear_pid` → 守卫想拦的情况**根本不存在**；
- 靠人类可读消息做控制流，是本 bug 的**同类病根**（② 是参数顺序与 parser 定义不一致，这里是代码与消息文本不一致）；
- 行数净减（-2 语句），且删掉后 `startup_grace`/③b 的失败路径成为唯一的失败出口，归因清晰。

**行为变化（必须知道）**：服务本来没在跑时，`botflow restart` 从「不干活 + 退出码 1」（`cli/main.py:131`）
变成「**直接启动服务**」（退出码 0，或启动失败时退出码 1 且原因来自 ③b）。
建议在删除处留一行注释（说明唯一 `ok=False` 路径已停 + 指向本文档 F9），防止有人重新加回守卫。

---

## 3. 功能点清单

### F1 `python -m botflow <子命令>` 可执行（新建 `src/botflow/__main__.py`）

- 文件：`src/botflow/__main__.py`（新建）。
- `__main__.py` 只做一件事：导入 `main` 后在 `__main__` 分支调用 `main()`。
- **不吞、不转换退出码**：`main()` 内抛出的 `SystemExit`（`cmd_stop` / `cmd_restart` 用
  `sys.exit(0/1)` 传结果，`cli/main.py:118,131`）必须原样冒泡，shell 才能拿到成败。
- 不传 argv：`main()` 默认 `argv=None` 读 `sys.argv[1:]`（`cli/main.py:884-886`），与 console script 一致。
- 保留 `cli/main.py:895-896` 的既有守卫（`python -m botflow.cli.main` 后门不受影响）。

### F2 `restart` 拼出的命令行必须能被自己的 argparse 接受

- 文件：`src/botflow/cli/service.py:121-124`。
- 判据（不是「等于某个字面量」，而是**自解析**）：把 `Popen` 实际收到的 `cmd` 去掉
  `cmd[:3]`（`sys.executable` + `-m` + `botflow`）后喂给 `botflow.cli.main.build_parser().parse_args()`，
  **必须不抛 `SystemExit`**，且 `args.workspace` / `args.host` / `args.port` / `args.func` 正确。
- `--workspace` 必须在子命令 `run` **之前**（`cmd.index("--workspace") < cmd.index("run")`）。
- `cmd[0]` 必须是 `sys.executable`；`cmd` 必须是 **list**（无 shell 展开），`str(workspace)` 是单个
  argv 元素（含空格也安全）。真实链路里 `cmd_restart` 先经 `_get_workspace` → `workspace.py:27-29`
  的 `.resolve()` 得到绝对路径，`restart_service` 只逐字透传（T2.6）。

### F3 带 `--config` 时完整命令行仍合法

- 文件：`src/botflow/cli/service.py:123-124`。
- `config_path` 为真值时追加 `["--config", config_path]`，位置在 `run` 之后；追加后整条 cmd 仍可解析，
  且 `args.config == config_path`（`cli/main.py:129` 以 `config_path=getattr(args, "config", None)` 传入）。
- `None` / `""`（falsy）时不出现 `--config`，仍可解析——现有 `if config_path:` 语义不变。

### F4 `stop_service` 的返回值不再参与控制流（守卫删除）

- 文件：`src/botflow/cli/service.py:116-118` → 收缩为一行 `stop_service(workspace)`（**不带返回值**）。
- 语义：无论 `stop_service` 返回什么（`ok=True` 的 4 种、`ok=False` 的 1 种），`restart` 都**继续**走
  spawn → ③b 存活校验 → 写 PID。**不存在**「因为 stop 报告失败而提前返回」的路径。
- 仍然正常处理「服务在跑」的场景：`stop_service` 会 `SIGTERM` + 等退出 + `clear_pid`，
  之后 spawn 拿到干净状态。
- 「启动失败」的**唯一**出口是 ③b（F8）——归因单一、可诊断，这正是删守卫的收益。
- 注意：`stop_service` 自身契约**不变**（无 PID 仍返回 `ok=False`，`tests/test_cli_service.py:72-73` 不动），
  被删的只是 `restart_service` 对它的**读取**。

### F5 进程启动后 PID 文件内容正确

- 文件：`src/botflow/cli/service.py:142` + `:15-22`。
- 路径 `{workspace}/data/botflow.pid`，内容 `str(proc.pid)`（子进程 PID，非调用方 PID），无多余字符；
  `read_pid()` 原样读回。
- `data/` 不存在时由 `write_pid` 的 `mkdir(parents=True, exist_ok=True)` 创建；覆盖旧 PID 文件。
- **写入时机**：必须在 ③b 的存活校验**之后**（F8）；spawn 或校验失败都不得留下 PID 文件。

### F6 平台分支：`creationflags` / `start_new_session` 都可达

- 文件：`src/botflow/cli/service.py:127-141`。
- `sys.platform == "win32"` → `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP`，不传 `start_new_session`；
  其它平台（`linux` / `darwin`）→ `start_new_session=True`，不传 `creationflags`。
- 两分支均 `stdout=subprocess.DEVNULL`、`shell` 不为真。
- 要求：**两个平台分支在 Windows 与 Linux 上都要 100% 覆盖**，靠 monkeypatch `svc.sys.platform`
  显式走到「另一侧」（T6.1 / T6.2），不靠宿主机平台碰运气。
- ③a 之后 kwargs 可合并成一份 dict 再单次 `Popen`（win/unix 只差一个键），既少一次重复调用，
  也让 `with open(...)` 只包一层；测试断言方式不变。

### F7（③a）子进程 stderr 可诊断：追加写入 `{workspace}/logs/botflow.err.log`

- 文件：`src/botflow/cli/service.py`（spawn 段）。
- stderr **不再** `DEVNULL`，而是以 `"ab"` **追加**打开 `{workspace}/logs/botflow.err.log`；
  `logs/` 缺失时递归创建。
- 追加语义硬要求：**不得截断**，否则会抹掉 supervisord（`deploy.sh:67`）与历史 restart 写的内容。
- `stdout` 保持 `DEVNULL`。
- 父进程只持有打开期间的句柄，`with` 退出即关闭（子进程持有自己的 dup 副本）。
- 打开失败 → 异常向上传播，**不 spawn**（宁可吵，不要静默）。

### F8（③b）有界存活校验：`write_pid()` 之前确认子进程还活着

- 文件：`src/botflow/cli/service.py`（spawn 段 + 新分支）。
- 新关键字参数 `startup_grace: float = 2.0`，签名末位、默认值 2.0（理由见 §2(d)）。
- 判定：`proc.wait(timeout=startup_grace)` 抛 `subprocess.TimeoutExpired` → 仍在运行 → 写 PID、
  `ok: True`、返回 `pid`；`wait()` 正常返回 → 子进程已退出（`proc.returncode` 为退出码）→
  **不写 PID 文件**、`ok: False`，消息必须同时含**退出码**与 `logs/botflow.err.log` 路径
  （并提示 `botflow logs`）。失败路径**不需要** `clear_pid`：`stop_service` 所有 `ok=True` 分支
  都已 `clear_pid`（`:78,84,92,105`）。
- 覆盖要求：`except TimeoutExpired` 与 `else:` 两条分支都必须有用例（T8.1 / T8.2）。

### F9 无 PID 文件时不应直接放弃（已纳入本次修复）

- 文件：`src/botflow/cli/service.py:117-118`（两行守卫删除）。
- 修法（唯一采定）：`restart_service` 里 `stop_result = stop_service(workspace)` +
  `if not stop_result["ok"] and "not running" not in stop_result["message"].lower(): return stop_result`
  **整段删除**，只保留纯调用 `stop_service(workspace)`（不读返回值，`stop_result` 变量随之消失）。
- 依据：`stop_service` 唯一的 `ok=False` 路径是 `:75`「无 PID 文件」（服务本就已停），
  其余四条（`:79 / :85 / :93 / :106`）全为 `ok=True` 且都已 `clear_pid` → 守卫想拦的情况**根本不存在**，
  删胜于加（决策阶梯第 4 级）；靠人类可读消息做控制流本身是本 bug 的同类病根。
- **行为变化（必须知道）**：服务未在跑时，`botflow restart` 从「不干活 + 退出码 1」
  （原 `cli/main.py:131`）变为「**直接启动服务**」——全新 workspace 上 Popen 会被调用、PID 文件会被写入。
- 失败归因随之单一化：`restart` 报失败只可能来自 ③b（F8，子进程秒死），不再有两种失败原因混在一个
  `ok:False` 里。
- 建议在删除处留一行注释（写明唯一 `ok=False` 路径即「已停」+ 指向本文档 F9），防止守卫被重新加回。

---

## 4. 测试用例清单

落点约定（**全部同步，无 asyncio**）：

- 新建 `tests/test_cli_restart.py`，四个类：`TestMainModuleEntry`（F1）、
  `TestRestartCommandLine`（F2–F6）、`TestRestartDiagnostics`（F7–F8）、
  `TestRestartGuardRemoval`（F9；这一类的用例**只假掉 `Popen`**，`stop_service` 用真的）。
- 理由：单文件、单目的、本机可跑；`tests/test_cli_main.py` 本机挂死（§8.1），
  既有 `tests/test_cli_service.py` 其余用例必须保持绿。
- 通用夹具：`tmp_path` 作 workspace；默认把 `stop_service` 打桩成
  `{"ok": True, "message": "stopped"}`（**仅 F2–F8 用**；F4 按用例改返回值，F9 用真 `stop_service`）；
  捕获式假 Popen：

```python
def _install_fake_popen(monkeypatch, captured, *, pid=4242, exit_code=None, alive=False):
    """替换 svc.subprocess.Popen，记录 cmd 与全部 kwargs。

    alive=True  -> proc.wait 抛 TimeoutExpired（子进程仍在跑，③b 判成功）
    alive=False -> proc.wait 返回 exit_code（子进程已退出，③b 判失败）
    """
    def _popen(cmd, **kw):
        captured["cmd"] = cmd
        captured.update(kw)
        proc = MagicMock()
        proc.pid = pid
        proc.returncode = exit_code
        if alive:
            proc.wait.side_effect = subprocess.TimeoutExpired(cmd=cmd, timeout=2.0)
        else:
            proc.wait.return_value = exit_code
        captured["proc"] = proc
        return proc
    monkeypatch.setattr(svc.subprocess, "Popen", _popen)
```

helper：`_parsed(cmd) -> argparse.Namespace`（`build_parser().parse_args(cmd[3:])`）。

### F1 `python -m botflow` 入口

| 编号 | 类型 | 用例名 | 输入 | 预期 |
|---|---|---|---|---|
| T1.1 | 正例 | `test_main_module_importable` | `import botflow.__main__` | 导入成功；该模块 `__name__ == "botflow.__main__"`（非 `__main__`，不执行 `main()`） |
| T1.2 | 正例 | `test_main_module_runs_version_subcommand` | `runpy.run_path(<botflow.__main__.py 真实路径>, run_name="__main__")` + `monkeypatch.setattr(sys, "argv", ["botflow", "version"])` + `capsys` | 无异常；stdout 含 `botflow v` |
| T1.3 | 正例 | `test_main_module_dispatches_to_cli_main_once` | 同上，但先 `monkeypatch.setattr(sys.modules["botflow.cli.main"], "main", fake)` | `fake` 恰好被调用 1 次、无参调用 |
| T1.4 | 反例 | `test_main_module_propagates_systemexit` | `fake` 抛 `SystemExit(3)` | `pytest.raises(SystemExit)` 且 `e.value.code == 3`（入口不吞退出码） |
| T1.5 | 边界 | `test_main_module_subprocess_version` | `subprocess.run([sys.executable, "-m", "botflow", "version"], capture_output=True, text=True, timeout=30)`（**必须 `text=True`**，否则 `stdout` 是 `bytes`、「含 `botflow v`」的断言写法未定） | `rc == 0` 且 `"botflow v" in r.stdout`；用例开头用 `subprocess.run([sys.executable, "-c", "import botflow"], capture_output=True).returncode != 0` 预检，失败即 `pytest.skip`（§8.4） |
| T1.6 | 边界 | `test_main_module_no_args_prints_help` | `argv == ["botflow"]` + `runpy` | `pytest.raises(SystemExit)`、`code == 0`、stdout 含 `usage`（对齐 `cli/main.py:888-890`） |

> T1.5 = 跨进程冒烟（真实复现故障一）；T1.2/T1.3/T1.6 = 进程内覆盖载体（§5）。

### F2 `restart` 命令行自解析

| 编号 | 类型 | 用例名 | 输入 | 预期 |
|---|---|---|---|---|
| T2.1 | 正例 | `test_restart_cmd_is_parsable_by_own_parser` | `restart_service(tmp_path, host="1.2.3.4", port=5678)` | `_parsed(cmd[3:])` 不抛；`args.workspace == str(tmp_path)`、`host == "1.2.3.4"`、`port == 5678`、`args.func.__name__ == "cmd_run"` |
| T2.2 | 正例 | `test_restart_cmd_puts_workspace_before_subcommand` | 同上 | **稳健判据**（不依赖 `cmd.index("run")` 的字符串位置）：`cmd[3] == "--workspace"` **且** `cmd[5] == "run"`（即 `cmd.index("run") == cmd.index("--workspace") + 2`）**且** `cmd[4] == str(tmp_path)` |
| T2.3 | 正例 | `test_restart_cmd_uses_sys_executable_and_no_shell` | 同上 | `cmd[0] == sys.executable`；`cmd[1:3] == ["-m", "botflow"]`；`isinstance(cmd, list)` |
| T2.4 | **反例（关键）** | `test_restart_cmd_old_ordering_is_rejected` | 手工构造**旧**顺序 `[exe, "-m", "botflow", "run", "--host", "h", "--port", "1", "--workspace", str(tmp_path)]` → `_parsed` | `pytest.raises(SystemExit)`（证明 T2.1 的断言真能抓住本 bug，而非恒真） |
| T2.5 | 边界 | `test_restart_cmd_workspace_with_spaces_is_single_element` | workspace = `tmp_path / "a b c"` | 该路径是**单个** argv 元素；解析出的 `args.workspace` 等值 |
| T2.6 | 边界 | `test_restart_cmd_workspace_passed_verbatim` | `restart_service(Path("rel/ws"), host="h", port=1)` | cmd 中 workspace 元素**逐字等于** `str(Path("rel/ws"))`（不隐式规范化）；仍可解析 |

### F3 `--config`

| 编号 | 类型 | 用例名 | 输入 | 预期 |
|---|---|---|---|---|
| T3.1 | 正例 | `test_restart_cmd_with_config_is_parsable` | `config_path="/tmp/bf.env"` | cmd 含 `--config /tmp/bf.env`；`_parsed(cmd[3:]).config == "/tmp/bf.env"` |
| T3.2 | 正例 | `test_restart_cmd_config_path_with_spaces_preserved` | `config_path = str(tmp_path / "my cfg.env")` | 单个 argv 元素、值原样、可解析 |
| T3.3 | 反例 | `test_restart_cmd_omits_config_when_none` | `config_path=None` | `"--config" not in cmd`；`args.config is None` |
| T3.4 | 边界 | `test_restart_cmd_omits_config_when_empty` | `config_path=""` | `"--config" not in cmd`；仍可解析 |
| T3.5 | 边界 | `test_restart_cmd_relative_config_path_parsable` | `config_path="./rel.env"` | 原样保留、可解析 |

### F4 `stop_service` 返回值不再参与控制流（守卫删除）

| 编号 | 类型 | 用例名 | 输入 | 预期 |
|---|---|---|---|---|
| T4.1 | 正例 | `test_restart_spawns_when_stop_ok` | `stop → {"ok": True, "message": "stopped"}` | Popen 调用 1 次；`ok is True`、`pid == 4242` |
| T4.2 | 正例 | `test_restart_spawns_when_stop_returns_no_pid_failure` | `stop → {"ok": False, "message": "No PID file found — service may not be running."}`（`:75` 原文；**删守卫前后行为分界点**） | Popen 调用 1 次；`ok is True`、PID 文件已写（旧实现在此早退） |
| T4.3 | 反例 | `test_restart_never_returns_stop_result_verbatim` | `stop → {"ok": False, "message": "SENTINEL-STOP"}` | 返回值中**不含** `"SENTINEL-STOP"`（证明 stop 的 dict 不再被透传；`stop_result` 已不存在） |
| T4.4 | 边界 | `test_restart_still_calls_stop_service` | spy 包装真 `stop_service`（记调用次数与入参）+ 存活假 Popen | `stop_service` **恰好被调 1 次**且入参为 `workspace`（**防止实现者顺手把 `stop_service(workspace)` 调用一起删掉**——守卫要删，调用要留）；同时 Popen 1 次 |
| T4.5 | 边界 | `test_restart_ignores_stop_message_text` | `stop → {"ok": False, "message": "DENIED"}` 与 `"service NOT RUNNING"` 两种消息 | 两者行为**完全一致**（都 spawn）——消息文本不再参与控制流（旧实现两种结果相反） |

> F4 只锁「守卫删除」的机制；「全新 workspace 上真的启动服务」的端到端行为由 F9（T9.*）验收。

### F5 PID 文件

| 编号 | 类型 | 用例名 | 输入 | 预期 |
|---|---|---|---|---|
| T5.1 | 正例 | `test_restart_writes_child_pid_to_pid_file` | `proc.pid = 4242`；`data/` 不存在 | `(ws/"data"/"botflow.pid").read_text() == "4242"`（无空白/换行）；`read_pid(ws) == 4242` |
| T5.2 | 正例 | `test_restart_overwrites_stale_pid_file` | 先 `write_pid(ws, 999)`，再 restart | 文件内容 == `"4242"` |
| T5.3 | 反例 | `test_restart_popen_failure_writes_no_pid_file` | Popen 抛 `OSError("boom")` | `pytest.raises(OSError)`；`read_pid(ws) is None`（不写假 PID、不返回假成功） |
| T5.4 | 边界 | `test_restart_pid_file_dir_created` | workspace = `tmp_path/"new"/"ws"`（整条不存在） | 目录递归创建、PID 文件写入成功（`service.py:21`） |

### F6 平台分支

| 编号 | 类型 | 用例名 | 输入 | 预期 |
|---|---|---|---|---|
| T6.1 | 正例 | `test_restart_unix_branch_uses_start_new_session` | `monkeypatch.setattr(svc.sys, "platform", "linux")` | `captured["start_new_session"] is True`；`"creationflags" not in captured`（**在 Windows 上也覆盖 `service.py:136-141`**） |
| T6.2 | 正例 | `test_restart_win32_branch_uses_creationflags` | `platform="win32"`；非 Windows 先 `monkeypatch.setattr(svc.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)` | `captured["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP`；`"start_new_session" not in captured`（**在 Linux 上也覆盖 `service.py:127-134`**） |
| T6.3 | 反例 | `test_restart_never_uses_shell` | 两个平台分支各一次 | `captured.get("shell")` 不为真；cmd 是 list |
| T6.4 | 边界 | `test_restart_darwin_uses_unix_branch` | `platform="darwin"` | 走 `start_new_session`（只有 `win32` 被特判） |
| T6.5 | 边界 | `test_restart_redirects_stdout_to_devnull` | 两个平台分支各一次 | `captured["stdout"] is subprocess.DEVNULL` |

### F7（③a）stderr 落盘

| 编号 | 类型 | 用例名 | 输入 | 预期 |
|---|---|---|---|---|
| T7.1 | 正例 | `test_restart_stderr_goes_to_err_log_in_append_mode` | 捕获式假 Popen | `Path(captured["stderr"].name) == ws/"logs"/"botflow.err.log"`；`captured["stderr"].mode == "ab"`；函数返回后 `captured["stderr"].closed is True`（父句柄已释放） |
| T7.2 | 正例 | `test_restart_preserves_existing_err_log_content` | 预置 `logs/botflow.err.log` 内容 `"OLD\n"` | restart 后内容仍以 `"OLD\n"` 开头（未被截断） |
| T7.3 | 反例 | `test_restart_creates_missing_logs_dir` | `logs/` 完全不存在 | 目录与文件被自动创建；restart 正常返回（不因缺目录失败） |
| T7.4 | 反例 | `test_restart_err_log_open_failure_aborts_before_spawn` | 预先 `mkdir(ws/"logs"/"botflow.err.log")`（把目标路径占成目录） | `pytest.raises(OSError)`；Popen **未被调用** |
| T7.5 | 边界 | `test_restart_second_run_appends_not_truncates` | 连续两次 restart（第一次后手工追加 `"SECOND\n"`） | 第二次后仍同时含早先内容与新内容 |
| T7.6 | 边界 | `test_restart_stdout_stays_devnull` | 捕获式假 Popen | `captured["stdout"] is subprocess.DEVNULL`（③a 只改 stderr） |

### F8（③b）有界存活校验

| 编号 | 类型 | 用例名 | 输入 | 预期 |
|---|---|---|---|---|
| T8.1 | 正例 | `test_restart_ok_when_child_survives_grace` | `proc.wait.side_effect = subprocess.TimeoutExpired(cmd, 2.0)` | `ok is True`、`pid == 4242`、PID 文件已写；`proc.wait` 以 `timeout=startup_grace` 被调用 |
| T8.2 | 反例 | `test_restart_fails_when_child_exits_immediately` | `proc.wait.return_value = 2`（`proc.returncode = 2`） | `ok is False`；**PID 文件不存在**（`read_pid(ws) is None` 且路径 not exists）；message 同时含 `"2"` 与 `botflow.err.log` 路径 |
| T8.3 | 边界 | `test_restart_grace_zero_catches_exited_child` | `startup_grace=0`，`wait.return_value = 3` | `ok is False`；message 含 `"3"`；不写 PID |
| T8.4 | 边界 | `test_restart_grace_zero_treats_alive_child_as_ok` | `startup_grace=0`，`wait.side_effect = TimeoutExpired` | `ok is True`、PID 已写（grace=0 的语义：不等，只抓已死的） |
| T8.5 | 边界 | `test_restart_startup_grace_default_and_position` | `inspect.signature(svc.restart_service)` | `"startup_grace" in params`、`params["startup_grace"].default == 2.0`；它是最后一个关键字参数；`restart_service(tmp_path)` 仍可调用 |
| T8.6 | 正例 | `test_restart_checks_liveness_before_writing_pid` | `wait` 的 `side_effect` 内读 PID 文件 | 校验发生时 PID 文件**尚不存在**（证明顺序：wait → write_pid），且 `wait` 收到 `timeout=2.0` |

### F9（已纳入）无 PID 文件时不再提前放弃

> 这三个用例用**真实的** `stop_service`（不 mock）跑在全新 `tmp_path` 上，以获得「无 PID 文件」这一真前提；
> 只有 `Popen` 被替换成假进程。留意：**删守卫后 `stop_result` 变量已不存在**，用例不得引用它，
> 断言只针对 `Popen` 是否被调用、PID 文件、以及返回 dict 的内容。

| 编号 | 类型 | 用例名 | 输入 | 预期 |
|---|---|---|---|---|
| T9.1 | 正例 | `test_restart_starts_service_on_fresh_workspace` | 全新 `tmp_path`（`read_pid(ws) is None` 为前置断言）+ 假 Popen（存活） | Popen **被调用 1 次**；`ok is True`、`pid == 4242`；`read_pid(ws) == 4242`（**不再早退**，这是 F9 的验收点） |
| T9.2 | 反例 | `test_restart_ignores_stop_ok_false_and_spawns` | `stop_service` mock 成 `{"ok": False, "message": "SENTINEL"}` + 假 Popen（存活） | Popen **被调用 1 次**、`ok is True`；返回 dict 中**不含** `"SENTINEL"`（回归锁定：stop 的 `ok=False` **不再**导致提前返回，其 dict 也不再被透传） |
| T9.3 | 边界 | `test_restart_fresh_workspace_failure_is_startup_failure` | 全新 `tmp_path` + 假 Popen（`wait.return_value = 2`，秒死） | `ok is False`，且消息是**启动失败**（含退出码 `2` 与 `botflow.err.log` 路径），**不是** `"No PID file found …"`（失败归因不串味）；PID 文件不存在 |

**用例总数**：**46**（正例 19 / 反例 10 / 边界值 17）。

---

## 5. `__main__.py` 覆盖率策略（**结论：无需 `# UNCOVERED:`；覆盖率不是它的验收手段**）

1. `pyproject.toml:61-68` 的 `[tool.coverage.report] exclude_lines` **已含**
   `"if __name__ == .__main__.:"`（第 64 行）→ **不需要也不应该**给 `__main__.py` 加 `# UNCOVERED:`。
2. **命中 `exclude_lines` 的 `if` 语句，整个块（含 guard 体 `main()`）都被排除，不是只排 guard 那一行。**
   独立实测（coverage.py **7.16.1** + 本仓库 `exclude_lines`）——用仓库里现成的同类 guard
   `cli/main.py:895-896`（测试中**从不**以 `__main__` 身份执行）：
   ```
   $ PYTHONPATH=src .venv/Scripts/python.exe -m coverage run --source=botflow.cli.main <driver>
   $ PYTHONPATH=src .venv/Scripts/python.exe -m coverage report --show-missing
   src\botflow\cli\main.py  586  384  34%  ...  879-881, 885-892
   ```
   Missing 列表止于 `885-892`，**895 与 896 都不在其中**——若只排除 `if` 那一行，896（`    main()`）
   必然出现在 Missing 里。故：**`__main__.py` 只有 import 那一行可执行语句，`import botflow.__main__`（T1.1）
   即得 100%**；`runpy.run_path(..., run_name="__main__")`（T1.2/T1.3/T1.6）**不是覆盖率载体**。
   > 前提条件：依赖 coverage 的既有语义与当前 `exclude_lines` 配置；若将来升级 coverage 或改动
   > `exclude_lines`，`__main__.py` 可能重新变成 1 miss（已知可接受风险）。
3. **正因那两行被 coverage 排除，T1.2 / T1.3 / T1.6 不得因为「不是覆盖率载体」而删**：
   `__main__.py` 的 guard 体**漏写 `main()` 也会报 100%**。这三个用例是该文件**唯一的功能守卫**——
   T1.2（体存在于 `__main__` 身份下会打印版本）、T1.3（确实调用 `cli.main.main` 且恰好 1 次）、
   T1.6（无子命令时帮助 + `SystemExit(0)`）。覆盖率在这里给不出任何保护，只能靠行为断言。
4. 子进程方案（T1.5，`python -m botflow version`）**不贡献覆盖率**：`pytest-cov` 默认不统计子进程，
   `[tool.coverage.run]`（`pyproject.toml:54-55`）只有 `source = ["botflow"]`，无 `parallel`/`sigterm`，
   仓库也未设 `COVERAGE_PROCESS_START`。它作为**行为冒烟**保留，且是①的**唯一跨进程**验证
   （T1.2/T1.3/T1.6 都在进程内跑 `runpy`，绕过了 `python -m` 的模块解析），所以 §8.4 的 `pytest.skip`
   兜底不可成为常态。
5. **实测坑（务必写进实现）**：`monkeypatch.setattr("botflow.cli.main.main", fake)` **会失败**——
   `cli/__init__.py:3` 的 `from botflow.cli.main import main` 把包属性 `botflow.cli.main` 遮蔽成了**函数**，
   pytest 的字符串解析走 `getattr` 链后拿到 function：
   `AttributeError: 'function' object at botflow.cli.main`。
   正确写法（同 `tests/test_cli_main.py:218` 的 `sys.modules[...]` 手法）：
   `monkeypatch.setattr(sys.modules["botflow.cli.main"], "main", fake)`，并确保该子模块已在 `sys.modules`
   （先 `from botflow.cli.main import main` 或 `import botflow.cli`）。
6. `service.py` 覆盖率基线（本机实跑 `tests/test_cli_service.py`，`--cov=botflow.cli.service`）：
   `95 stmts / 10 miss / 89%`，缺失 `83-85, 94-106, 124, 136, 170-171`。其中 **124 与 136 正是本次改动行**
   （现为 0 覆盖）→ 由 T3.1 / T6.1 补上；③a/③b 新增块由 T7.* / T8.* 全分支覆盖
   （`except TimeoutExpired` 与失败 `else:` 各有用例）。`83-85 / 94-106 / 170-171` 是**历史缺口**（见 §8.6）。
7. F9 删守卫会让 `service.py` 净减 **2 条语句**（原 `116-118` 的 `if` 与 `return` 消失；`stop_result = …`
   变成裸调用，语句数不变），分母变小、比例基本不动；**被删的两行原本已被既有用例覆盖**，
   所以历史缺口集合（`83-85 / 94-106 / 170-171`）不变。删掉的分支**不允许**以 `# UNCOVERED:` 形式复活。

---

## 6. 既有用例的破坏面与改法（③b 与 F9 会打破谁）

| 文件:行 | 现状 | 改动之后 | 处理 |
|---|---|---|---|
| `tests/test_cli_service.py:99-106` `test_restart` | `fake_proc = MagicMock(); pid=4242`；断言 `ok is True`、`pid == 4242` | `MagicMock().wait(timeout=…)` **返回 MagicMock 而不抛 `TimeoutExpired`** → 新逻辑判「子进程已退出」→ 返回 `ok: False` 且**没有** `pid` 键 → 第一个断言 `ok is True` 先挂；即便放过，`res["pid"]` 也会 `KeyError` | **必须改**：给假进程配 `wait.side_effect = subprocess.TimeoutExpired(cmd=..., timeout=2.0)`（活路径）。③a 同时会让它创建 `logs/` 与 err log 文件（`tmp_path` 内，无害） |
| `tests/test_cli_service.py:123-127` `test_restart_when_stop_fails` | `monkeypatch.setattr(svc, "stop_service", lambda ws: {"ok": False, "message": "denied"})`，断言 `res["ok"] is False` | 该用例的**测试意图就是被 F9 删掉的那道守卫**（「stop 报失败 → restart 不干活的失败」）。删守卫后：(a) 前提不存在——`"denied"` 这种消息 `stop_service` 永不产出（它唯一的 `ok=False` 是 `:75`）；(b) 它**仍会碰巧通过**——流程走到 spawn + ③b，假 `wait()` 不抛 `TimeoutExpired` → 判「已死」→ 返回 `ok: False`，断言照样成立。**「碰巧还能过」= 断言在、意图已死**，比直接失败更危险（它会长期贡献零信号） | **删除**（推荐）：其意图已被 T4.2 / T4.4 / T9.2 以正确前提覆盖，保留只剩重复。若验证子 agent 为留变更痕迹坚持保留，**必须改名**为 `test_restart_spawns_despite_stop_failure` 并改写断言为「Popen 调用 1 次 + 假 `wait` 配 `TimeoutExpired` → `ok is True`」——**绝不允许保留原名原断言**（那是碰巧通过） |
| `tests/test_cli_main.py:216-222` `test_restart` | 把 `cm.restart_service` 整体 mock 成 `lambda ws, host, port, config_path` | `cmd_restart` 不传 `startup_grace`，lambda 签名仍匹配；F9 只改 `service.py` 内部 → **不用改**（该文件本机挂死，见 §8.1） |
| `tests/test_cli.py:340+` `TestServiceManagement` | 无 `restart_service` 引用（已 grep 确认；`328-332` 仅测 parser） | 不受影响 | 不用改 |
| `tests/test_cli_service.py:72-73` `test_stop_no_pid` | 断言 `ok is False` | F9 采定修法是**删 `restart_service` 里的守卫**，`stop_service` 契约一字未动 → **不用改**（此前担心的「方案 (i) 会改 stop 契约」已被否决） |

**③b 的注入方式与限制**：`restart_service` 的 cmd 里 `sys.executable -m botflow run …` 是硬编码，
**没有**可注入的假子进程入口；因此 ③b 的判定分支用**假 Popen + 精心配置的 `wait`** 覆盖，
真实子进程的「秒死」行为由 T2.1/T2.4（命令行合法）与集成测试（§8.2）兜底。
若有人想「顺手」加一个 `cmd` 注入参数以便真跑子进程——**不要**（为测试改动生产签名，越界）。

**③a 的句柄与断言方式**：`Popen` 在测试里被 mock，所以不真的启动任何进程；断言靠
`captured["stderr"]` 这个**真实文件对象**（`.name` / `.mode` / `closed`）+ 事后读文件内容，
既证明「写到了正确路径」，也证明「父进程没有泄漏句柄」。

**F9 的用例写在哪**：T9.* 用**真实 `stop_service`**（只有 `Popen` 被假掉），因为「无 PID 文件」这一前提
只能靠真实实现给出；不要像既有 `:123-127` 那样把 `stop_service` 也 mock 掉——那正是它意图死亡的原因。

---

## 7. 明确不做

| # | 不做的事 | 理由 |
|---|---|---|
| 1 | **不新增 `/health` 就绪探测**（spawn 后轮询 HTTP 直到 200） | 属新功能：需要就绪超时配置、重试策略、端口不一致时的语义，用户已知悉并选择不做。③b 只回答「有没有秒死」。 |
| 2 | 不把 `--workspace` 也加进 `run` 子 parser | **已实测有副作用**：同名 dest 且无 `default=argparse.SUPPRESS` 时子 parser 会把根 parser 的值**清成 `None`**（`--workspace A run --host h` → `workspace=None`），反而引入新 bug；`SUPPRESS` 可行但比换位置复杂。 |
| 3 | 不改用 console script / `shutil.which("botflow")` 拉起 | 依赖安装方式与 PATH（源码直跑 / venv / 生产目录各不相同），`-m` + `sys.executable` 更稳。 |
| 4 | 不删 `cli/main.py:895-896` 的既有守卫 | 与 bug 无关，且是 `python -m botflow.cli.main` 的现成调试入口。 |
| 5 | 不保留任何形式的「stop 失败即早退」语义 | F9 已把守卫**删除**（不是改写）；不要用返回值、异常或新字段把等价逻辑加回来。`restart_service` 的失败出口只应有 ③b 一个。 |
| 6 | 不改 `stop_service` 的契约（不采用曾被考虑过的「无 PID → `ok=True`」方案） | 那会把 `botflow stop` 对已停服务的退出码从 1 变 0（`cli/main.py:118`），属未获批的 CLI 契约变更，且会让 `tests/test_cli_service.py:72-73` 失效；删守卫不需要它。 |
| 7 | 不给 `startup_grace` 加负值校验/上限校验 | 内部关键字参数、无外部输入，`wait(timeout<=0)` 语义已确定（立即返回）——加分支只为测而测。 |
| 8 | 不重写 `stop_service` 的 SIGTERM→SIGKILL 与历史覆盖缺口（`83-85/94-106/170-171`） | 与本 bug 无因果；若放行门槛要求 `service.py` 100%，按 §8.6 补齐（**不允许**用 `# UNCOVERED:` 掩盖可测逻辑）。 |
| 9 | 不动 `cmd_restart` 的 `args.host or "0.0.0.0"` / 端口默认与 `config.py` 环境变量覆盖问题（`docs/design.md:443`） | 已登记的历史问题，混入会让 diff 无法评审。 |

---

## 8. 风险与未验证项

1. **本机不能跑任何建 event loop 的测试（已实测）**：`PYTHONPATH=src python -m pytest tests/test_cli_main.py -o addopts="" -q`
   只输出 2 个点（`test_version`、`test_no_command_prints_help`）后**永久挂起**——第 3 个用例 `test_set_get`
   （经 `_init_workspace_db` 走 aiosqlite）即卡死；`--collect-only` 正常（0.05s）。根因与仓库既有记载一致
   （`docs/tasks/P8-1_tests.md:12-13`：loopback socket 被拦 → `socket.socketpair()` 回退路径的 `accept()` 永久阻塞）。
   → **新增用例必须全部同步**：不得 import asyncio、不得用 `pytest.mark.asyncio`、不得连真实端口。
   → T1.5 用 `subprocess.run(..., timeout=30)` 自带保险丝（挂起会以 `TimeoutExpired` 失败而非卡死）。
2. **本机无法端到端证明「restart 之后服务真的起来了」**：需要真实服务 + 端口 + `/health`，本机 loopback 受限。
   唯一有力验证是在 Linux（CI `ubuntu-latest` 或生产，参考 `P8-1_tests.md` 的隔离 venv 模式）执行
   `botflow restart`，确认 ① `/health` 返回 `{"status":"ok",...}`、② `data/botflow.pid` 指向活进程、
   ③ `logs/botflow.err.log` 无新增启动错误、④ 故意把 `--config` 指向坏文件时 `restart` 返回 `ok: False`
   且消息含退出码。**留给主 agent 的集成测试环节，本文档不声称已验证。**
3. **③b 的残留风险（用户已知悉）**：进程在 `startup_grace`（默认 2s）之后才死，仍会被判为成功
   （例如端口后续被占用、依赖在 3s 时崩）。③b 只是把「秒死」从静默变成可见；彻底解决需要 /health 就绪探测（不做）。
4. **T1.5（子进程冒烟）的环境依赖**：放行命令是 `PYTHONPATH=src python -m pytest tests/ ...`（AGENTS.md），
   `PYTHONPATH` 会被子进程继承；本机 `.venv` 已 editable 安装（`site-packages/botflow.pth` 存在，
   实测 `import botflow` → `src/botflow/__init__.py`），故本机与 CI 都能跑。若某环境既未安装 botflow 又未设
   `PYTHONPATH`，`python -m botflow` 会 ImportError → 用例须 `subprocess.run([sys.executable, "-c", "import botflow"])`
   预检并在失败时 `pytest.skip`（**不得**变成永久失败，那是环境问题）。代价约 0.5–0.7s。
5. **③a 的句柄风险（未实测）**：生产 supervisord 也以追加方式持有 `logs/botflow.err.log`。
   CPython 在 Windows 走 CRT `_wopen`（`_SH_DENYNO`）默认允许多写者，但**未在本机实测两进程并发追加**
   （本机无 supervisord）。若 `open()` 真失败（`OSError`），当前设计是**向上抛**（不 spawn、不静默）——
   代价是服务保持停止，收益是不会再出现「假成功」。
6. **`service.py` 覆盖率基线 89% 的历史缺口**（`83-85, 94-106, 124, 136, 170-171`；124/136 属本次改动行，
   由 T3.1/T6.1 消除）。补法（若放行要求模块 100%）：
   `83-85` 需「`is_running` 先返回真、再让 `os.kill` 抛 `ProcessLookupError`」（现有
   `tests/test_cli_service.py:113-120` **并没有**覆盖到——它让 `os.kill` 无条件抛，于是 `stop_service:77`
   先返回 False 就走了另一条路，名字骗人）；`94-106` 需假时钟（递增序列）+ `is_running` 恒真穿过 deadline，
   再用 `sys.platform` monkeypatch 分别覆盖 `98-100`（win32）与 `101-102`（posix）；
   `170-171` 需让 `open()` 抛错。**不允许**用 `# UNCOVERED:` 掩盖可测逻辑。
7. **POSIX 分支未在真实 Linux 上跑过**：`service.py:136-141`（`start_new_session=True`）在本机只能靠 T6.1 的
   monkeypatch 覆盖，真实「脱离父终端/新会话」语义未验证。
8. **本机噪声（非失败）**：`.pytest_cache` 无写权限 → 只产生一条 `PytestCacheWarning`。

---

## 9. 收尾（非代码，合入时一并处理）

- `docs/design.md:440` 的「高」严重度条目（描述的正是本 bug）应更新或标注为已修复；
  `design.md:441-443` 的另两条与本修复无关，保留。
- F9 已纳入，故 `docs/design.md:440` 的修复说明需一并覆盖「无 PID 文件时 `restart` 不启动服务」
  这一条（原条目只写了 `__main__.py` 与 `--workspace` 两个原因）。
- `tests/test_cli_service.py:72-73`（`test_stop_no_pid`）**不用改**：F9 采定的修法不触碰 `stop_service` 契约。

## 10. 文件变更清单

| 文件 | 操作 | 内容 |
|---|---|---|
| `src/botflow/__main__.py` | **新建** | F1：导入 `main` + `__main__` 守卫（约 5 行） |
| `src/botflow/cli/service.py` | **修改** | F2–F9：`116-118` **删守卫**（只留 `stop_service(workspace)`）+ 留一行注释；`121-122` cmd 顺序（`--workspace` 前移）；新增 ③a（`logs/` mkdir + `open(...,"ab")` + `stderr=`）与 ③b（`startup_grace` + `wait()` 判定 + 失败早退，`142-148` 的 PID 写入移到校验之后） |
| `tests/test_cli_restart.py` | **新建** | 46 个同步用例（T1.1–T9.3） |
| `tests/test_cli_service.py:99-106` | **修改** | 既有 `test_restart` 必须给假 `proc.wait` 配 `TimeoutExpired`（§6） |
| `tests/test_cli_service.py:123-127` | **删除** | `test_restart_when_stop_fails` 的意图（被删的守卫）已不存在，且会「碰巧通过」——详见 §6；其意图由 T4.2 / T4.4 / T9.2 以正确前提覆盖 |
| `docs/tasks/fix-restart_features.md` | 本文件 | 功能点 + 测试用例 + 覆盖策略 |

**不修改**：`pyproject.toml`（`[project.scripts]` 与 `exclude_lines` 均无需动）、`src/botflow/cli/__init__.py`、
`src/botflow/cli/main.py`、`src/botflow/workspace.py`、`src/botflow/common/logger.py`、`scripts/deploy.sh`、
`src/botflow/cli/service.py` 的 `stop_service`（`:71-106` 契约一字不动）。
