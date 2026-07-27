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

    # Force kill
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
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
    """Read last N lines from the error log."""
    log_dir = workspace / "logs"
    err_log = log_dir / "botflow.err.log"
    if not err_log.exists():
        return f"No log file found at {err_log}"

    try:
        result = subprocess.run(
            ["tail", "-n", str(lines), str(err_log)],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout or "(empty log)"
    except Exception as e:
        return f"Error reading log: {e}"
