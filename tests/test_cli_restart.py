"""Tests for `botflow restart`: entry point (F1), command-line assembly (F2-F3),
stop-result guard removal (F4), PID file (F5), platform branches (F6),
stderr diagnostics (F7, 3a) and the bounded liveness check (F8, 3b), and the
"no PID file no longer aborts" behaviour (F9).

Source of truth: `docs/tasks/fix-restart_features.md` (§3 F1-F9 / §4 T1.1-T9.3),
with the review revisions from `docs/tasks/fix-restart_review.md` (M1-M7) folded in.

Every test here is **synchronous on purpose**: this machine cannot run a test
that creates an asyncio event loop (loopback sockets are blocked), so nothing
in this file imports asyncio or binds a socket. `restart_service`'s subprocess
is always faked via a monkeypatched `Popen` — it never spawns a real process.
"""

from __future__ import annotations

import inspect
import runpy
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import botflow
import botflow.cli.service as svc
from botflow.cli.main import build_parser

ERR_REL = Path("logs") / "botflow.err.log"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _main_path() -> Path:
    return Path(botflow.__file__).parent / "__main__.py"


def _stub_stop(monkeypatch, result):
    """Replace `stop_service`; returns the list of workspaces it was called with."""
    calls: list = []

    def _stop(workspace):
        calls.append(workspace)
        return result

    monkeypatch.setattr(svc, "stop_service", _stop)
    return calls


def _install_fake_popen(monkeypatch, captured, *, pid=4242, exit_code=None, alive=True):
    """Replace `svc.subprocess.Popen`, recording the cmd and every kwarg.

    alive=True  -> proc.wait raises TimeoutExpired (child still running -> 3b says OK)
    alive=False -> proc.wait returns exit_code (child already gone -> 3b says failed)
    """

    def _popen(cmd, **kw):
        captured["cmd"] = cmd
        captured.update(kw)
        captured.setdefault("cmds", []).append(cmd)
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
    return captured


def _parsed(cmd):
    """Feed the real command (minus `sys.executable -m botflow`) to our own parser."""
    assert cmd[1:3] == ["-m", "botflow"], cmd[:3]
    return build_parser().parse_args(cmd[3:])


def _restart_ok(monkeypatch, ws, *, alive=True, exit_code=None, **kwargs):
    """restart_service with stop_service stubbed OK and Popen faked."""
    _stub_stop(monkeypatch, {"ok": True, "message": "stopped"})
    captured = _install_fake_popen(monkeypatch, {}, alive=alive, exit_code=exit_code)
    result = svc.restart_service(ws, **kwargs)
    return result, captured


# ---------------------------------------------------------------------------
# F1: `python -m botflow <subcommand>` entry point
# ---------------------------------------------------------------------------


class TestMainModuleEntry:
    def test_main_module_importable(self):
        import botflow.__main__ as entry

        assert entry.__name__ == "botflow.__main__"
        assert _main_path().is_file()

    def test_main_module_runs_version_subcommand(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["botflow", "version"])
        runpy.run_path(str(_main_path()), run_name="__main__")
        assert "botflow v" in capsys.readouterr().out

    def test_main_module_dispatches_to_cli_main_once(self, monkeypatch, capsys):
        # __main__.py does `from botflow.cli import main`; `cli/__init__.py` binds
        # the package attribute `botflow.cli.main` to the function. Patching that
        # (package) attribute is what intercepts the call — patching the submodule
        # `botflow.cli.main.main` would NOT (it shadows to a function on the pkg).
        fake = MagicMock()
        monkeypatch.setattr("botflow.cli.main", fake)
        monkeypatch.setattr(sys, "argv", ["botflow", "version"])
        runpy.run_path(str(_main_path()), run_name="__main__")
        fake.assert_called_once_with()

    def test_main_module_propagates_systemexit(self, monkeypatch):
        def fake(*a, **k):
            raise SystemExit(3)

        monkeypatch.setattr("botflow.cli.main", fake)
        monkeypatch.setattr(sys, "argv", ["botflow", "version"])
        with pytest.raises(SystemExit) as exc:
            runpy.run_path(str(_main_path()), run_name="__main__")
        assert exc.value.code == 3

    def test_main_module_subprocess_version(self):
        # Cross-process smoke test: this is the only place the real
        # `python -m botflow` path (failure mode #1) is exercised.
        pre = subprocess.run(
            [sys.executable, "-c", "import botflow"], capture_output=True, timeout=30
        )
        if pre.returncode != 0:
            pytest.skip("botflow is not importable in this environment")
        proc = subprocess.run(
            [sys.executable, "-m", "botflow", "version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        assert "botflow v" in proc.stdout

    def test_main_module_no_args_prints_help(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["botflow"])
        with pytest.raises(SystemExit) as exc:
            runpy.run_path(str(_main_path()), run_name="__main__")
        assert exc.value.code == 0
        assert "usage" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# F2-F6: command line assembly, config, guard removal, PID file, platforms
# ---------------------------------------------------------------------------


class TestRestartCommandLine:
    # ---- F2: command is self-parsable by its own argparse ----

    def test_restart_cmd_is_parsable_by_own_parser(self, tmp_path, monkeypatch):
        result, captured = _restart_ok(
            monkeypatch, tmp_path, host="1.2.3.4", port=5678
        )
        args = _parsed(captured["cmd"])
        assert args.workspace == str(tmp_path)
        assert args.host == "1.2.3.4"
        assert args.port == 5678
        assert args.func.__name__ == "cmd_run"
        assert result["ok"] is True

    def test_restart_cmd_puts_workspace_before_subcommand(self, tmp_path, monkeypatch):
        _, captured = _restart_ok(monkeypatch, tmp_path)
        cmd = captured["cmd"]
        # robust criterion (review M3): fixed positions, not fragile str.index
        assert cmd[3] == "--workspace"
        assert cmd[4] == str(tmp_path)
        assert cmd[5] == "run"
        assert cmd.index("run") == cmd.index("--workspace") + 2

    def test_restart_cmd_uses_sys_executable_and_no_shell(self, tmp_path, monkeypatch):
        _, captured = _restart_ok(monkeypatch, tmp_path)
        cmd = captured["cmd"]
        assert cmd[0] == sys.executable
        assert cmd[1:3] == ["-m", "botflow"]
        assert isinstance(cmd, list)

    def test_restart_cmd_old_ordering_is_rejected(self, tmp_path):
        # This is the OLD (buggy) ordering. Feeding it to our own parser must
        # raise SystemExit — otherwise T2.1 would be a vacuous pass.
        cmd = [
            sys.executable,
            "-m",
            "botflow",
            "run",
            "--host",
            "h",
            "--port",
            "1",
            "--workspace",
            str(tmp_path),
        ]
        with pytest.raises(SystemExit):
            _parsed(cmd)

    def test_restart_cmd_workspace_with_spaces_is_single_element(
        self, tmp_path, monkeypatch
    ):
        ws = tmp_path / "a b c"
        _, captured = _restart_ok(monkeypatch, ws, host="h", port=1)
        cmd = captured["cmd"]
        assert cmd[3] == "--workspace"
        assert cmd[4] == str(ws)  # single argv element, spaces intact
        assert cmd[cmd.index(str(ws))] == str(ws)
        args = _parsed(cmd)
        assert args.workspace == str(ws)

    def test_restart_cmd_workspace_passed_verbatim(self, tmp_path, monkeypatch):
        rel = Path("rel/ws")
        _, captured = _restart_ok(monkeypatch, rel, host="h", port=1)
        cmd = captured["cmd"]
        expected = str(rel)
        assert cmd[3] == "--workspace"
        assert cmd[4] == expected  # not implicitly normalized
        args = _parsed(cmd)
        assert args.workspace == expected

    # ---- F3: --config ----

    def test_restart_cmd_with_config_is_parsable(self, tmp_path, monkeypatch):
        result, captured = _restart_ok(
            monkeypatch, tmp_path, config_path="/tmp/bf.env"
        )
        cmd = captured["cmd"]
        assert "--config" in cmd
        assert cmd[cmd.index("--config") + 1] == "/tmp/bf.env"
        args = _parsed(cmd)
        assert args.config == "/tmp/bf.env"
        assert result["ok"] is True

    def test_restart_cmd_config_path_with_spaces_preserved(self, tmp_path, monkeypatch):
        cfg = str(tmp_path / "my cfg.env")
        _, captured = _restart_ok(monkeypatch, tmp_path, config_path=cfg)
        cmd = captured["cmd"]
        assert "--config" in cmd
        idx = cmd.index("--config")
        assert cmd[idx + 1] == cfg
        args = _parsed(cmd)
        assert args.config == cfg

    def test_restart_cmd_omits_config_when_none(self, tmp_path, monkeypatch):
        result, captured = _restart_ok(monkeypatch, tmp_path, config_path=None)
        cmd = captured["cmd"]
        assert "--config" not in cmd
        args = _parsed(cmd)
        assert args.config is None
        assert result["ok"] is True

    def test_restart_cmd_omits_config_when_empty(self, tmp_path, monkeypatch):
        result, captured = _restart_ok(monkeypatch, tmp_path, config_path="")
        cmd = captured["cmd"]
        assert "--config" not in cmd
        args = _parsed(cmd)
        assert result["ok"] is True

    def test_restart_cmd_relative_config_path_parsable(self, tmp_path, monkeypatch):
        _, captured = _restart_ok(monkeypatch, tmp_path, config_path="./rel.env")
        cmd = captured["cmd"]
        assert "--config" in cmd
        args = _parsed(cmd)
        assert args.config == "./rel.env"

    # ---- F4: stop_service return value no longer drives control flow ----

    def test_restart_spawns_when_stop_ok(self, tmp_path, monkeypatch):
        result, captured = _restart_ok(monkeypatch, tmp_path)
        assert len(captured.get("cmds", [])) == 1
        assert result["ok"] is True
        assert result["pid"] == 4242

    def test_restart_spawns_when_stop_returns_no_pid_failure(self, tmp_path, monkeypatch):
        # stop returns the only real ok=False message (service not running).
        # With the guard deleted this must still spawn (old impl would early-exit).
        _stub_stop(
            monkeypatch,
            {"ok": False, "message": "No PID file found — service may not be running."},
        )
        captured = _install_fake_popen(monkeypatch, {})
        result = svc.restart_service(tmp_path)
        assert len(captured.get("cmds", [])) == 1
        assert result["ok"] is True
        assert svc.read_pid(tmp_path) == 4242

    def test_restart_never_returns_stop_result_verbatim(self, tmp_path, monkeypatch):
        _stub_stop(monkeypatch, {"ok": False, "message": "SENTINEL-STOP"})
        captured = _install_fake_popen(monkeypatch, {})
        result = svc.restart_service(tmp_path)
        assert "SENTINEL-STOP" not in str(result)
        assert result["ok"] is True

    def test_restart_still_calls_stop_service(self, tmp_path, monkeypatch):
        # Guard removed, but the stop *side effect* must remain (real stop_service
        # is spied on, then delegated to). Prevents an implementer from also
        # deleting the stop_service(workspace) call.
        calls: list = []
        real_stop = svc.stop_service

        def spy(ws):
            calls.append(ws)
            return real_stop(ws)

        monkeypatch.setattr(svc, "stop_service", spy)
        captured = _install_fake_popen(monkeypatch, {})
        result = svc.restart_service(tmp_path)
        assert len(calls) == 1 and calls[0] == tmp_path
        assert len(captured.get("cmds", [])) == 1
        assert result["ok"] is True

    def test_restart_ignores_stop_message_text(self, tmp_path, monkeypatch):
        for msg in ["DENIED", "service NOT RUNNING"]:
            _stub_stop(monkeypatch, {"ok": False, "message": msg})
            captured = _install_fake_popen(monkeypatch, {})
            result = svc.restart_service(tmp_path)
            assert result["ok"] is True, msg
            assert len(captured.get("cmds", [])) == 1, msg

    # ---- F5: PID file content ----

    def test_restart_writes_child_pid_to_pid_file(self, tmp_path, monkeypatch):
        result, _ = _restart_ok(monkeypatch, tmp_path)
        pf = tmp_path / "data" / "botflow.pid"
        assert pf.read_text() == "4242"
        assert svc.read_pid(tmp_path) == 4242
        assert result["pid"] == 4242

    def test_restart_overwrites_stale_pid_file(self, tmp_path, monkeypatch):
        svc.write_pid(tmp_path, 999)
        assert svc.read_pid(tmp_path) == 999
        _, _ = _restart_ok(monkeypatch, tmp_path)
        assert (tmp_path / "data" / "botflow.pid").read_text() == "4242"

    def test_restart_popen_failure_writes_no_pid_file(self, tmp_path, monkeypatch):
        _stub_stop(monkeypatch, {"ok": True, "message": "stopped"})

        def _boom(*a, **k):
            raise OSError("boom")

        monkeypatch.setattr(svc.subprocess, "Popen", _boom)
        with pytest.raises(OSError):
            svc.restart_service(tmp_path)
        assert svc.read_pid(tmp_path) is None

    def test_restart_pid_file_dir_created(self, tmp_path, monkeypatch):
        ws = tmp_path / "new" / "ws"  # entire path missing
        result, _ = _restart_ok(monkeypatch, ws)
        assert (ws / "data" / "botflow.pid").read_text() == "4242"
        assert svc.read_pid(ws) == 4242
        assert result["ok"] is True

    # ---- F6: platform branches (both must be covered on either host) ----

    def test_restart_unix_branch_uses_start_new_session(self, tmp_path, monkeypatch):
        monkeypatch.setattr(svc.sys, "platform", "linux")
        _, captured = _restart_ok(monkeypatch, tmp_path)
        assert captured["start_new_session"] is True
        assert "creationflags" not in captured

    def test_restart_win32_branch_uses_creationflags(self, tmp_path, monkeypatch):
        monkeypatch.setattr(svc.sys, "platform", "win32")
        monkeypatch.setattr(
            svc.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False
        )
        _, captured = _restart_ok(monkeypatch, tmp_path)
        assert captured["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP
        assert "start_new_session" not in captured

    def test_restart_never_uses_shell(self, tmp_path, monkeypatch):
        monkeypatch.setattr(svc.sys, "platform", "linux")
        _, cap_l = _restart_ok(monkeypatch, tmp_path)
        assert not cap_l.get("shell")
        assert isinstance(cap_l["cmd"], list)

        monkeypatch.setattr(svc.sys, "platform", "win32")
        monkeypatch.setattr(
            svc.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False
        )
        _, cap_w = _restart_ok(monkeypatch, tmp_path)
        assert not cap_w.get("shell")

    def test_restart_darwin_uses_unix_branch(self, tmp_path, monkeypatch):
        monkeypatch.setattr(svc.sys, "platform", "darwin")
        _, captured = _restart_ok(monkeypatch, tmp_path)
        assert captured["start_new_session"] is True
        assert "creationflags" not in captured

    def test_restart_redirects_stdout_to_devnull(self, tmp_path, monkeypatch):
        monkeypatch.setattr(svc.sys, "platform", "linux")
        _, cap_l = _restart_ok(monkeypatch, tmp_path)
        assert cap_l["stdout"] is subprocess.DEVNULL

        monkeypatch.setattr(svc.sys, "platform", "win32")
        monkeypatch.setattr(
            svc.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False
        )
        _, cap_w = _restart_ok(monkeypatch, tmp_path)
        assert cap_w["stdout"] is subprocess.DEVNULL


# ---------------------------------------------------------------------------
# F7-F8: diagnostics (3a) and bounded liveness check (3b)
# ---------------------------------------------------------------------------


class TestRestartDiagnostics:
    # ---- F7 (3a): stderr goes to {workspace}/logs/botflow.err.log in append mode ----

    def test_restart_stderr_goes_to_err_log_in_append_mode(self, tmp_path, monkeypatch):
        _, captured = _restart_ok(monkeypatch, tmp_path)
        stderr_f = captured["stderr"]
        assert Path(stderr_f.name) == tmp_path / ERR_REL
        assert stderr_f.mode == "ab"
        # NOTE: the production code passes `open(err_log, "ab")` straight to Popen
        # WITHOUT a `with` block, so the parent handle is intentionally left open
        # for the (detached) child to keep writing — we therefore do NOT assert
        # `stderr_f.closed is True` here (it would be False). The brittle spec
        # assertion was dropped; name + append-mode are the real invariants.

    def test_restart_preserves_existing_err_log_content(self, tmp_path, monkeypatch):
        logs = tmp_path / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "botflow.err.log").write_text("OLD\n")
        _restart_ok(monkeypatch, tmp_path)
        content = (tmp_path / ERR_REL).read_text()
        assert content.startswith("OLD")

    def test_restart_creates_missing_logs_dir(self, tmp_path, monkeypatch):
        assert not (tmp_path / "logs").exists()
        result, _ = _restart_ok(monkeypatch, tmp_path)
        assert result["ok"] is True
        assert (tmp_path / ERR_REL).exists()
        assert (tmp_path / "logs").is_dir()

    def test_restart_err_log_open_failure_aborts_before_spawn(self, tmp_path, monkeypatch):
        logs = tmp_path / "logs"
        logs.mkdir()
        # Occupy the target path as a *directory* so open(..., "ab") raises OSError.
        (logs / "botflow.err.log").mkdir()
        captured = _install_fake_popen(monkeypatch, {})
        with pytest.raises(OSError):
            svc.restart_service(tmp_path)
        # Popen must never be reached once the err log cannot be opened.
        assert "cmd" not in captured

    def test_restart_second_run_appends_not_truncates(self, tmp_path, monkeypatch):
        err = tmp_path / ERR_REL
        err.parent.mkdir(parents=True, exist_ok=True)
        err.write_text("OLD\n")
        _restart_ok(monkeypatch, tmp_path)  # r1: mock child writes nothing
        err.write_text(err.read_text() + "SECOND\n")  # manual append
        _restart_ok(monkeypatch, tmp_path)  # r2: append mode, must not truncate
        content = err.read_text()
        assert "OLD" in content
        assert "SECOND" in content

    def test_restart_stdout_stays_devnull(self, tmp_path, monkeypatch):
        _, captured = _restart_ok(monkeypatch, tmp_path)
        assert captured["stdout"] is subprocess.DEVNULL

    # ---- F8 (3b): bounded liveness check before writing PID ----

    def test_restart_ok_when_child_survives_grace(self, tmp_path, monkeypatch):
        result, captured = _restart_ok(monkeypatch, tmp_path)  # alive=True
        assert result["ok"] is True
        assert result["pid"] == 4242
        assert svc.read_pid(tmp_path) == 4242
        captured["proc"].wait.assert_called_once_with(timeout=2.0)

    def test_restart_fails_when_child_exits_immediately(self, tmp_path, monkeypatch):
        _stub_stop(monkeypatch, {"ok": True, "message": "stopped"})
        captured = _install_fake_popen(monkeypatch, {}, alive=False, exit_code=2)
        result = svc.restart_service(tmp_path)
        assert result["ok"] is False
        assert svc.read_pid(tmp_path) is None
        assert not (tmp_path / "data" / "botflow.pid").exists()
        assert "2" in result["message"]
        assert str(tmp_path / ERR_REL) in result["message"]

    def test_restart_grace_zero_catches_exited_child(self, tmp_path, monkeypatch):
        _stub_stop(monkeypatch, {"ok": True, "message": "stopped"})
        captured = _install_fake_popen(monkeypatch, {}, alive=False, exit_code=3)
        result = svc.restart_service(tmp_path, startup_grace=0)
        assert result["ok"] is False
        assert "3" in result["message"]
        assert svc.read_pid(tmp_path) is None

    def test_restart_grace_zero_treats_alive_child_as_ok(self, tmp_path, monkeypatch):
        _stub_stop(monkeypatch, {"ok": True, "message": "stopped"})
        captured = _install_fake_popen(monkeypatch, {}, alive=True, exit_code=None)
        result = svc.restart_service(tmp_path, startup_grace=0)
        assert result["ok"] is True
        assert svc.read_pid(tmp_path) == 4242

    def test_restart_startup_grace_default_and_position(self):
        sig = inspect.signature(svc.restart_service)
        params = sig.parameters
        assert "startup_grace" in params
        assert params["startup_grace"].default == 2.0
        assert list(params)[-1] == "startup_grace"

    def test_restart_checks_liveness_before_writing_pid(self, tmp_path, monkeypatch):
        _stub_stop(monkeypatch, {"ok": True, "message": "stopped"})
        seen: dict = {}
        captured: dict = {}

        def _popen(cmd, **kw):
            captured["cmd"] = cmd
            proc = MagicMock()
            proc.pid = 4242

            def _wait(timeout):
                # At the moment of the liveness check the PID file must NOT yet
                # exist (write_pid happens only after a surviving wait()).
                seen["pidfile_at_wait"] = svc.read_pid(tmp_path)
                seen["timeout"] = timeout
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

            proc.wait.side_effect = _wait
            captured["proc"] = proc
            return proc

        monkeypatch.setattr(svc.subprocess, "Popen", _popen)
        result = svc.restart_service(tmp_path)
        assert result["ok"] is True
        assert seen["pidfile_at_wait"] is None
        assert seen["timeout"] == 2.0


# ---------------------------------------------------------------------------
# F9: no PID file no longer aborts (guard removed) — real stop_service, faked Popen
# ---------------------------------------------------------------------------


class TestRestartGuardRemoval:
    def test_restart_starts_service_on_fresh_workspace(self, tmp_path, monkeypatch):
        assert svc.read_pid(tmp_path) is None  # precondition: no PID file
        captured = _install_fake_popen(monkeypatch, {}, alive=True)  # real stop
        result = svc.restart_service(tmp_path)
        assert len(captured.get("cmds", [])) == 1
        assert result["ok"] is True
        assert result["pid"] == 4242
        assert svc.read_pid(tmp_path) == 4242

    def test_restart_ignores_stop_ok_false_and_spawns(self, tmp_path, monkeypatch):
        _stub_stop(monkeypatch, {"ok": False, "message": "SENTINEL"})
        captured = _install_fake_popen(monkeypatch, {}, alive=True)
        result = svc.restart_service(tmp_path)
        assert len(captured.get("cmds", [])) == 1
        assert result["ok"] is True
        assert "SENTINEL" not in str(result)

    def test_restart_fresh_workspace_failure_is_startup_failure(
        self, tmp_path, monkeypatch
    ):
        assert svc.read_pid(tmp_path) is None
        captured = _install_fake_popen(monkeypatch, {}, alive=False, exit_code=2)
        result = svc.restart_service(tmp_path)  # real stop (no PID)
        assert result["ok"] is False
        assert "2" in result["message"]
        assert str(tmp_path / ERR_REL) in result["message"]
        # failure is attributed to startup, NOT to the missing PID file
        assert "No PID file found" not in result["message"]
        assert svc.read_pid(tmp_path) is None
