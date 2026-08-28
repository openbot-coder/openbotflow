"""Service management utilities: PID files, stop, restart, status, logs."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Optional

import httpx


def _pid_file(workspace: Path) -> Path:
    return workspace / "data" / "botflow.pid"


def write_pid(workspace: Path, pid: int) -> None:
    pf = _pid_file(workspace)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(str(pid))


def read_pid(workspace: Path) -> Optional[int]:
    pf = _pid_file(workspace)
    if not pf.exists():
        return None
    try:
        return int(pf.read_text().strip())
    except (ValueError, OSError):
        return None


def clear_pid(workspace: Path) -> None:
    pf = _pid_file(workspace)
    if pf.exists():
        pf.unlink(missing_ok=True)


def is_running(pid: int) -> bool:
    """Check if a process with given PID is running."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def get_status(workspace: Path, port: int) -> dict:
    """Get service status: pid, health, uptime."""
    pid = read_pid(workspace)
    result = {
        "pid": pid,
        "running": False,
        "health": None,
    }

    if pid and is_running(pid):
        result["running"] = True
        try:
            resp = httpx.get(f"http://127.0.0.1:{port}/health", timeout=3.0)
            if resp.status_code == 200:
                result["health"] = resp.json()
        except Exception:
            result["health"] = {"status": "unreachable"}

    return result


def stop_service(workspace: Path, timeout: int = 10) -> dict:
    """Stop the botflow service by PID."""
    pid = read_pid(workspace)
    if not pid:
        return {"ok": False, "message": "No PID file found — service may not be running."}

    if not is_running(pid):
        clear_pid(workspace)
        return {"ok": True, "message": f"Process {pid} not running (stale PID cleaned)."}

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        clear_pid(workspace)
        return {"ok": True, "message": f"Process {pid} already gone."}

    # Wait for exit
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_running(pid):
            clear_pid(workspace)
            return {"ok": True, "message": f"Service (PID {pid}) stopped."}
        time.sleep(0.5)

    # Force kill (SIGKILL is Unix-only; on Windows use os.kill with SIGTERM or taskkill)
    try:  # UNCOVERED: 仅在真实进程未在超时内退出时触发，单元测试无法构造真实存活进程到超时
        if sys.platform == "win32":
            # On Windows, os.kill with SIGTERM calls TerminateProcess (irrevocable)
            os.kill(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:  # UNCOVERED
        pass
    clear_pid(workspace)
    return {"ok": True, "message": f"Service (PID {pid}) killed after timeout."}


def restart_service(
    workspace: Path,
    host: str = "0.0.0.0",
    port: int = 8080,
    config_path: Optional[str] = None,
) -> dict:
    """Restart: stop then start as a detached background process."""
    stop_result = stop_service(workspace)
    if not stop_result["ok"] and "not running" not in stop_result["message"].lower():
        return stop_result

    # Start as background process
    cmd = [sys.executable, "-m", "botflow", "run",
           "--host", host, "--port", str(port), "--workspace", str(workspace)]
    if config_path:
        cmd.extend(["--config", config_path])

    # Windows needs CREATE_NEW_PROCESS_GROUP; Unix uses start_new_session (setsid)
    if sys.platform == "win32":
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
    else:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    write_pid(workspace, proc.pid)

    return {
        "ok": True,
        "message": f"Service restarted (PID {proc.pid}).",
        "pid": proc.pid,
    }


def tail_logs(workspace: Path, lines: int = 50) -> str:
    """Read last N lines from the error log without loading the entire file."""
    log_dir = workspace / "logs"
    err_log = log_dir / "botflow.err.log"
    if not err_log.exists():
        return f"No log file found at {err_log}"

    try:
        # Read only the tail of the file to avoid OOM on large logs
        with open(err_log, "rb") as f:
            f.seek(0, 2)  # seek to end
            size = f.tell()
            # Read ~64 bytes per line estimate
            read_size = min(size, lines * 64)
            f.seek(max(0, size - read_size))
            tail_bytes = f.read()
        tail = tail_bytes.decode("utf-8", errors="replace")
        tail_lines = tail.splitlines()[-lines:]
        return "\n".join(tail_lines) or "(empty log)"
    except Exception as e:
        return f"Error reading log: {e}"
