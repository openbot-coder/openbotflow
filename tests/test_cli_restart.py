"""Tests for `botflow restart`: entry point (1/2), stderr diagnostics (3a),
bounded liveness check (3b) and removal of the stop-result guard (F9).

Every test here is **synchronous on purpose**: this machine cannot run a test
that creates an asyncio event loop (loopback sockets are blocked), so nothing
in this file imports asyncio or binds a socket.

Source of truth: `docs/tasks/fix-restart_features.md`
"""

from __future__ import annotations

import inspect
import runpy
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, call

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
