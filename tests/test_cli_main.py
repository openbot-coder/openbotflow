"""Coverage tests for the CLI (cli/main.py). Service-blocking `run` is excluded."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from botflow.cli.main import _bool_arg, main


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "wf"
    ws.mkdir(parents=True, exist_ok=True)
    yield ws
    shutil.rmtree(ws, ignore_errors=True)


def _args(workspace, *cmd):
    return ["--workspace", str(workspace), *cmd]


def test_version(capsys, monkeypatch):
    monkeypatch.setattr("sys.exit", lambda code=0: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit):
        main(["--version"])
    assert "botflow" in capsys.readouterr().out


def test_no_command_prints_help(capsys, monkeypatch):
    monkeypatch.setattr("sys.exit", lambda code=0: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit):
        main([])
    out = capsys.readouterr().out
    assert "usage" in out.lower() or "Commands" in out


def test_set_get(workspace, capsys):
    main(_args(workspace, "set", "k1", "v1"))
    main(_args(workspace, "get", "k1"))
    assert "k1 = v1" in capsys.readouterr().out


def test_get_missing_exits(workspace, capsys, monkeypatch):
    monkeypatch.setattr("sys.exit", lambda code=0: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit):
        main(_args(workspace, "get", "nope"))
    assert "not set" in capsys.readouterr().out


def test_config_lists(workspace, capsys):
    main(_args(workspace, "set", "a", "1"))
    main(_args(workspace, "config"))
    assert "a = 1" in capsys.readouterr().out


def test_provider_add_list(workspace, capsys):
    main(_args(workspace, "provider", "add", "p1", "--type", "openai", "--api-key", "k"))
    main(_args(workspace, "provider", "list"))
    assert "p1" in capsys.readouterr().out


def test_provider_get_missing(workspace, capsys, monkeypatch):
    monkeypatch.setattr("sys.exit", lambda code=0: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit):
        main(_args(workspace, "provider", "get", "999"))
    assert "not found" in capsys.readouterr().out


def test_provider_update_delete(workspace, capsys):
    main(_args(workspace, "provider", "add", "p2", "--type", "anthropic"))
    main(_args(workspace, "provider", "update", "1", "--name", "p2b", "--enabled", "false"))
    main(_args(workspace, "provider", "delete", "1"))
    main(_args(workspace, "provider", "list"))
    assert "p2b" not in capsys.readouterr().out


def test_provider_update_no_args(workspace, capsys, monkeypatch):
    main(_args(workspace, "provider", "add", "p3", "--type", "openai"))
    monkeypatch.setattr("sys.exit", lambda code=0: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit):
        main(_args(workspace, "provider", "update", "1"))


def test_model_add_list(workspace, capsys):
    main(_args(workspace, "provider", "add", "pp", "--type", "openai"))
    main(_args(workspace, "model", "add", "m1", "--provider-id", "1"))
    main(_args(workspace, "model", "list"))
    assert "m1" in capsys.readouterr().out


def test_model_get_missing(workspace, capsys, monkeypatch):
    monkeypatch.setattr("sys.exit", lambda code=0: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit):
        main(_args(workspace, "model", "get", "999"))


def test_model_update_delete(workspace, capsys):
    main(_args(workspace, "provider", "add", "pp", "--type", "openai"))
    main(_args(workspace, "model", "add", "m2", "--provider-id", "1"))
    main(_args(workspace, "model", "update", "1", "--name", "m2b", "--enabled", "false"))
    main(_args(workspace, "model", "delete", "1"))
    main(_args(workspace, "model", "list"))
    assert "m2b" not in capsys.readouterr().out


def test_model_update_no_args(workspace, capsys, monkeypatch):
    main(_args(workspace, "provider", "add", "pp", "--type", "openai"))
    main(_args(workspace, "model", "add", "m3", "--provider-id", "1"))
    monkeypatch.setattr("sys.exit", lambda code=0: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit):
        main(_args(workspace, "model", "update", "1"))


def test_group_add_list(workspace, capsys):
    main(_args(workspace, "group", "add", "g1", "--description", "d"))
    main(_args(workspace, "group", "list"))
    assert "g1" in capsys.readouterr().out


def test_group_get_missing(workspace, capsys, monkeypatch):
    monkeypatch.setattr("sys.exit", lambda code=0: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit):
        main(_args(workspace, "group", "get", "999"))


def test_group_update_delete(workspace, capsys):
    main(_args(workspace, "group", "add", "g2"))
    main(_args(workspace, "group", "update", "1", "--name", "g2b", "--enabled", "false"))
    main(_args(workspace, "group", "delete", "1"))
    main(_args(workspace, "group", "list"))
    assert "g2b" not in capsys.readouterr().out


def test_group_update_no_args(workspace, capsys, monkeypatch):
    main(_args(workspace, "group", "add", "g3"))
    monkeypatch.setattr("sys.exit", lambda code=0: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit):
        main(_args(workspace, "group", "update", "1"))


def test_apikey_list_add_disable(workspace, capsys):
    main(_args(workspace, "apikey", "add", "secret-key-123", "--label", "t"))
    # Raw key is only shown once on creation (masked); list shows hash, not plaintext.
    assert "secr" in capsys.readouterr().out
    out = capsys.readouterr().out
    main(_args(workspace, "apikey", "list"))
    assert "secret-key-123" not in capsys.readouterr().out
    main(_args(workspace, "apikey", "disable", "1"))
    main(_args(workspace, "apikey", "list"))
    main(_args(workspace, "apikey", "enable", "1"))


def test_apikey_delete(workspace, capsys):
    main(_args(workspace, "apikey", "add", "k-delete", "--label", "x"))
    main(_args(workspace, "apikey", "delete", "1"))
    main(_args(workspace, "apikey", "list"))
    assert "No API keys" in capsys.readouterr().out


def test_stats_recent(workspace, capsys):
    main(_args(workspace, "stats", "recent", "-n", "5"))
    out = capsys.readouterr().out
    assert "call logs" in out


def test_stats_cost(workspace, capsys):
    main(_args(workspace, "stats", "cost", "--days", "30"))
    out = capsys.readouterr().out
    assert "Cost summary" in out or "No cost" in out


def test_stats_model(workspace, capsys):
    main(_args(workspace, "stats", "model", "1"))
    capsys.readouterr()


def test_stats_group(workspace, capsys):
    main(_args(workspace, "stats", "group", "1"))
    capsys.readouterr()


def test_cleanup(workspace, capsys):
    main(_args(workspace, "cleanup", "--days", "0"))
    assert "Cleaned up" in capsys.readouterr().out


def test_status(workspace, capsys, monkeypatch):
    from botflow.cli import service as svc
    import sys
    cm = sys.modules["botflow.cli.main"]
    monkeypatch.setattr(cm, "get_status", lambda ws, port: {"pid": 5, "running": True, "health": {"status": "ok"}})
    main(_args(workspace, "status", "--port", "9999"))
    assert "Running:  yes" in capsys.readouterr().out


def test_logs(workspace, capsys, monkeypatch):
    import sys
    cm = sys.modules["botflow.cli.main"]
    monkeypatch.setattr(cm, "tail_logs", lambda ws, lines: "log line here")
    main(_args(workspace, "logs"))
    assert "log line here" in capsys.readouterr().out


def test_stop(workspace, capsys, monkeypatch):
    import sys
    cm = sys.modules["botflow.cli.main"]
    monkeypatch.setattr(cm, "stop_service", lambda ws: {"ok": True})
    monkeypatch.setattr("sys.exit", lambda code=0: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit):
        main(_args(workspace, "stop"))


def test_restart(workspace, capsys, monkeypatch):
    import sys
    cm = sys.modules["botflow.cli.main"]
    monkeypatch.setattr(cm, "restart_service", lambda ws, host, port, config_path: {"ok": True})
    monkeypatch.setattr("sys.exit", lambda code=0: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit):
        main(_args(workspace, "restart", "--host", "x", "--port", "1"))


def test_summary(workspace, capsys, monkeypatch):
    import botflow.storage.daily_summary as ds
    async def _mock(db, day=None):
        return None
    monkeypatch.setattr(ds, "run_daily_summary", _mock)
    main(_args(workspace, "summary", "--day", "2026-01-01"))
    # No assertion needed; ensure no exception.


def test_bool_arg():
    assert _bool_arg("true") is True
    assert _bool_arg("false") is False
    import pytest as _p
    with _p.raises(Exception):
        _bool_arg("maybe")
