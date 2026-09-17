"""Coverage tests for cli/service.py (PID files, status, stop, restart, logs)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import subprocess

import botflow.cli.service as svc


def test_pid_roundtrip(tmp_path):
    svc.write_pid(tmp_path, 1234)
    assert svc.read_pid(tmp_path) == 1234
    svc.clear_pid(tmp_path)
    assert svc.read_pid(tmp_path) is None


def test_read_pid_missing(tmp_path):
    assert svc.read_pid(tmp_path) is None


def test_read_pid_corrupt(tmp_path):
    pf = svc._pid_file(tmp_path)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text("not-a-number")
    assert svc.read_pid(tmp_path) is None


def test_is_running(monkeypatch):
    calls = []
    monkeypatch.setattr(svc.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    assert svc.is_running(999) is True
    assert calls == [(999, 0)]


def test_is_running_dead(monkeypatch):
    import errno
    def _boom(pid, sig):
        raise OSError(errno.ESRCH, "no")
    monkeypatch.setattr(svc.os, "kill", _boom)
    assert svc.is_running(999) is False


def test_get_status_no_pid(tmp_path):
    assert svc.get_status(tmp_path, 8080)["running"] is False


def test_get_status_running_healthy(tmp_path, monkeypatch):
    svc.write_pid(tmp_path, 555)
    monkeypatch.setattr(svc, "is_running", lambda pid: True)
    fake = MagicMock()
    fake.status_code = 200
    fake.json.return_value = {"status": "ok"}
    monkeypatch.setattr(svc.httpx, "get", lambda url, timeout: fake)
    st = svc.get_status(tmp_path, 8080)
    assert st["running"] is True
    assert st["health"] == {"status": "ok"}


def test_get_status_running_unreachable(tmp_path, monkeypatch):
    svc.write_pid(tmp_path, 555)
    monkeypatch.setattr(svc, "is_running", lambda pid: True)
    def _raise(url, timeout):
        raise RuntimeError("conn refused")
    monkeypatch.setattr(svc.httpx, "get", _raise)
    st = svc.get_status(tmp_path, 8080)
    assert st["health"] == {"status": "unreachable"}


def test_stop_no_pid(tmp_path):
    assert svc.stop_service(tmp_path)["ok"] is False


def test_stop_stale_pid(tmp_path, monkeypatch):
    svc.write_pid(tmp_path, 555)
    monkeypatch.setattr(svc, "is_running", lambda pid: False)
    res = svc.stop_service(tmp_path)
    assert res["ok"] is True
    assert svc.read_pid(tmp_path) is None


def test_stop_running_term_then_exit(tmp_path, monkeypatch):
    svc.write_pid(tmp_path, 555)
    killed = []
    monkeypatch.setattr(svc.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(_t := __import__("time"), "time", lambda: 1000)  # fixed clock, no deadline
    calls = {"n": 0}
    def _is_running(pid):
        calls["n"] += 1
        return calls["n"] == 1  # alive on first check, gone afterwards
    monkeypatch.setattr(svc, "is_running", _is_running)
    res = svc.stop_service(tmp_path, timeout=1)
    assert res["ok"] is True
    assert (555, 15) in killed  # SIGTERM


def test_restart(tmp_path, monkeypatch):
    fake_proc = MagicMock()
    fake_proc.pid = 4242
    fake_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="x", timeout=2.0)
    monkeypatch.setattr(svc.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(svc, "stop_service", lambda ws: {"ok": True, "message": "x"})
    res = svc.restart_service(tmp_path, host="h", port=1)
    assert res["ok"] is True
    assert res["pid"] == 4242


def test_tail_logs_missing(tmp_path):
    assert "No log file" in svc.tail_logs(tmp_path, 10)


def test_stop_process_lookup_error(tmp_path, monkeypatch):
    svc.write_pid(tmp_path, 555)
    import errno
    def _boom(pid, sig):
        raise ProcessLookupError(errno.ESRCH, "gone")
    monkeypatch.setattr(svc.os, "kill", _boom)
    res = svc.stop_service(tmp_path)
    assert res["ok"] is True


def test_restart_ignores_stop_result_and_spawns(tmp_path, monkeypatch):
    """After F9 fix, restart always spawns regardless of stop_service result."""
    stop_calls = []
    fake_proc = MagicMock()
    fake_proc.pid = 100
    fake_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="x", timeout=2.0)
    monkeypatch.setattr(svc.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(svc, "stop_service", lambda ws: stop_calls.append(ws) or {"ok": False, "message": "No PID file found — service may not be running."})
    res = svc.restart_service(tmp_path)
    assert res["ok"] is True
    assert len(stop_calls) == 1  # stop_service still called for its side effect


def test_tail_logs_error(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "botflow.err.log").write_text("x")
    assert svc.tail_logs(tmp_path, 10) == "x"

    # Missing log file
    assert "No log file found" in svc.tail_logs(tmp_path / "nope", 10)


def test_tail_logs_reads(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "botflow.err.log").write_text("line1\nline2\n")
    fake = MagicMock()
    fake.stdout = "line1\nline2\n"
    monkeypatch.setattr(svc.subprocess, "run", lambda *a, **k: fake)
    assert "line2" in svc.tail_logs(tmp_path, 10)


def test_stop_process_lookup_error_after_term(tmp_path, monkeypatch):
    # Covers service.py:83-85 — SIGTERM is delivered, then os.kill raises
    # ProcessLookupError (process vanished between the liveness check and the kill).
    # NOTE: the name-sake test_stop_process_lookup_error does NOT cover this branch
    # because it makes `os.kill` unconditionally raise, so `is_running` returns False
    # and the code returns early at the stale-PID branch.
    svc.write_pid(tmp_path, 555)
    monkeypatch.setattr(svc, "is_running", lambda pid: True)
    import errno

    def _boom(pid, sig):
        raise ProcessLookupError(errno.ESRCH, "gone")

    monkeypatch.setattr(svc.os, "kill", _boom)
    res = svc.stop_service(tmp_path)
    assert res["ok"] is True
    assert svc.read_pid(tmp_path) is None


def _force_kill_test(tmp_path, monkeypatch, platform, sigkill_value):
    """Drive stop_service past its deadline so the force-kill branch executes."""
    svc.write_pid(tmp_path, 555)
    monkeypatch.setattr(svc, "is_running", lambda pid: True)
    # signal.SIGKILL is undefined on Windows; shim it so the posix branch is runnable.
    if sigkill_value is not None:
        monkeypatch.setattr(svc.signal, "SIGKILL", sigkill_value, raising=False)
    import errno

    calls = []

    def _kill(pid, sig):
        calls.append((pid, sig))
        # The 2nd (force-kill) call raises ProcessLookupError -> caught at except.
        if len(calls) >= 2:
            raise ProcessLookupError(errno.ESRCH, "gone")

    monkeypatch.setattr(svc.os, "kill", _kill)
    times = [1000, 1000, 2000, 2000]
    it = iter(times)
    time_mod = __import__("time")
    monkeypatch.setattr(time_mod, "time", lambda: next(it))
    monkeypatch.setattr(time_mod, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(svc.sys, "platform", platform)
    res = svc.stop_service(tmp_path, timeout=1)
    assert res["ok"] is True
    assert svc.read_pid(tmp_path) is None
    # force-kill call present (SIGTERM=15 on win32, SIGKILL=9 on posix)
    return calls


def test_stop_force_kill_win32(tmp_path, monkeypatch):
    calls = _force_kill_test(tmp_path, monkeypatch, "win32", None)
    assert (555, 15) in calls  # os.kill(pid, SIGTERM) in the win32 force branch


def test_stop_force_kill_posix(tmp_path, monkeypatch):
    calls = _force_kill_test(tmp_path, monkeypatch, "linux", 9)
    assert (555, 9) in calls  # os.kill(pid, SIGKILL) in the posix force branch


def test_tail_logs_read_error(tmp_path):
    # Covers service.py:183-184 — the read raises, the except branch returns an
    # error string instead of crashing.
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "botflow.err.log").mkdir()  # path is a directory -> open() raises
    result = svc.tail_logs(tmp_path, 10)
    assert "Error reading log" in result
