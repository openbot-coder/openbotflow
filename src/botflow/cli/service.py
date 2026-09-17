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
    try:
        if sys.platform == "win32":
            # On Windows, os.kill with SIGTERM calls TerminateProcess (irrevocable)
            os.kill(pid, signal.SIGTERM)
        else:
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
    startup_grace: float = 2.0,
) -> dict:
    """Restart: stop then start as a detached background process."""
    # No guard on stop_service() result: every ok=False path (only "no PID file")
    # means the service isn't running — the whole point of restart is to start it.
    # Real stop failures (e.g. PermissionError from os.kill) raise, not return ok=False.
    stop_service(workspace)

    # --workspace must precede the subcommand for argparse to accept it
    cmd = [sys.executable, "-m", "botflow", "--workspace", str(workspace),
           "run", "--host", host, "--port", str(port)]
    if config_path:
        cmd.extend(["--config", config_path])

    err_log = workspace / "logs" / "botflow.err.log"
    err_log.parent.mkdir(parents=True, exist_ok=True)

    if sys.platform == "win32":
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=open(err_log, "ab"),
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    else:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=open(err_log, "ab"),
            start_new_session=True,
        )

    try:
        proc.wait(timeout=startup_grace)
    except subprocess.TimeoutExpired:
        write_pid(workspace, proc.pid)
        return {
            "ok": True,
            "message": f"Service restarted (PID {proc.pid}).",
            "pid": proc.pid,
        }

    # Subprocess exited before grace period — it failed to start
    msg = (
        f"Service failed to start (exit code {proc.returncode}). "
        f"Check logs: {err_log}"
    )
    return {"ok": False, "message": msg}


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
