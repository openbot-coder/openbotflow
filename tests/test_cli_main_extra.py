"""Supplementary coverage tests for cli/main.py.

Covers the argument branches and success paths that the baseline
``test_cli_main.py`` did not reach: ``run`` wiring, config auto-registration,
api-key update, provider/model/group read+update paths, ``model sync`` fan-out,
and the stats subcommands with real data shapes.
"""

from __future__ import annotations

import shutil
import sys
import types
from types import SimpleNamespace

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


@pytest.fixture
def exit_raises(monkeypatch):
    """Make sys.exit raise so we can assert the exit path was taken."""
    monkeypatch.setattr(
        "sys.exit", lambda code=0: (_ for _ in ()).throw(SystemExit(code))
    )


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def test_cmd_run_wires_uvicorn_and_clears_pid(workspace, monkeypatch):
    """cmd_run builds a uvicorn server, serves it, and always clears the PID."""
    cm = sys.modules["botflow.cli.main"]
    seen = {}

    class _FakeConfig:
        def __init__(self, app, **kwargs):
            seen["app"] = app
            seen["config_kwargs"] = kwargs

    class _FakeServer:
        def __init__(self, cfg):
            seen["server_cfg"] = cfg

        async def serve(self):
            seen["served"] = True

    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        types.SimpleNamespace(Config=_FakeConfig, Server=_FakeServer),
    )

    async def _fake_create_app(ws, cfg):
        seen["create_app"] = (ws, cfg)

    import botflow.core as core_mod

    monkeypatch.setattr(core_mod, "create_app", _fake_create_app)

    cleared = {}
    monkeypatch.setattr(cm, "clear_pid", lambda ws: cleared.setdefault("ws", ws))

    main(_args(workspace, "run", "--host", "127.0.0.1", "--port", "1234"))

    assert seen["served"] is True
    assert seen["app"] is core_mod.app
    assert seen["config_kwargs"]["host"] == "127.0.0.1"
    assert seen["config_kwargs"]["port"] == 1234
    assert seen["config_kwargs"]["forwarded_allow_ips"] == "*"
    assert cleared["ws"] == workspace


def test_cmd_run_with_config_path(workspace, monkeypatch):
    """--config resolves the config from the given file's parent directory."""
    cm = sys.modules["botflow.cli.main"]

    class _FakeConfig:
        def __init__(self, app, **kwargs):
            pass

    class _FakeServer:
        def __init__(self, cfg):
            pass

        async def serve(self):
            pass

    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        types.SimpleNamespace(Config=_FakeConfig, Server=_FakeServer),
    )

    import botflow.config as cfg_mod
    captured = {}
    real_load_config = cfg_mod.load_config

    def _fake_load_config(path):
        captured["path"] = path
        return real_load_config(path)

    monkeypatch.setattr(cfg_mod, "load_config", _fake_load_config)

    async def _noop(ws, cfg):
        pass

    import botflow.core as core_mod

    monkeypatch.setattr(core_mod, "create_app", _noop)
    monkeypatch.setattr(cm, "clear_pid", lambda ws: None)

    env_file = workspace / "custom.env"
    env_file.write_text("", encoding="utf-8")
    main(_args(workspace, "run", "--config", str(env_file)))

    assert captured["path"] == workspace


# ---------------------------------------------------------------------------
# status / logs
# ---------------------------------------------------------------------------


def test_status_without_health(workspace, capsys, monkeypatch):
    cm = sys.modules["botflow.cli.main"]
    monkeypatch.setattr(
        cm,
        "get_status",
        lambda ws, port: {"pid": None, "running": False, "health": None},
    )
    main(_args(workspace, "status"))
    out = capsys.readouterr().out
    assert "PID:      N/A" in out
    assert "Running:  no" in out
    assert "not reachable" in out


# ---------------------------------------------------------------------------
# set / get / config
# ---------------------------------------------------------------------------


def test_set_llm_key_autoregisters_api_key(workspace, capsys):
    main(_args(workspace, "set", "llm_key", "legacy-secret-value"))
    out = capsys.readouterr().out
    assert "llm_key = legacy-secret-value" in out
    assert "Auto-registered as API key" in out


def test_config_empty_workspace(workspace, capsys):
    main(_args(workspace, "config"))
    assert "No config values set." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# apikey update
# ---------------------------------------------------------------------------


def test_apikey_update_label_and_enabled(workspace, capsys):
    main(_args(workspace, "apikey", "add", "k-update-123456", "--label", "old"))
    main(
        _args(
            workspace,
            "apikey",
            "update",
            "1",
            "--label",
            "new-label",
            "--enabled",
            "false",
        )
    )
    out = capsys.readouterr().out
    assert "updated" in out
    assert "label=new-label" in out
    assert "enabled=False" in out


def test_apikey_update_enabled_only(workspace, capsys):
    main(_args(workspace, "apikey", "add", "k-update-abcdef", "--label", "x"))
    main(_args(workspace, "apikey", "update", "1", "--enabled", "true"))
    out = capsys.readouterr().out
    assert "enabled=True" in out
    assert "label=" not in out


def test_apikey_update_not_found(workspace, capsys):
    main(_args(workspace, "apikey", "update", "999", "--label", "nope"))
    assert "not found" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


def test_summary_prints_wiki_text(workspace, capsys, monkeypatch):
    import botflow.storage.daily_summary as ds

    async def _mock_run(db, day=None):
        return None

    monkeypatch.setattr(ds, "run_daily_summary", _mock_run)

    from botflow.storage.db import Database

    class _FakeSummary:
        summary_md = "# Daily digest"

    async def _get_summary(self, day):
        return _FakeSummary()

    monkeypatch.setattr(Database, "get_daily_summary", _get_summary)

    main(_args(workspace, "summary", "--day", "2026-01-01"))
    assert "# Daily digest" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# provider get / update
# ---------------------------------------------------------------------------


def test_provider_get_found(workspace, capsys):
    main(_args(workspace, "provider", "add", "prov-get", "--type", "openai"))
    main(_args(workspace, "provider", "get", "1"))
    assert "prov-get" in capsys.readouterr().out


def test_provider_update_api_key_and_base_url(workspace, capsys):
    main(_args(workspace, "provider", "add", "prov-upd", "--type", "openai"))
    main(
        _args(
            workspace,
            "provider",
            "update",
            "1",
            "--api-key",
            "fresh-key",
            "--base-url",
            "http://upstream.local",
        )
    )
    assert "updated" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# model get / add-proxy / update
# ---------------------------------------------------------------------------


def test_model_get_found(workspace, capsys):
    main(_args(workspace, "provider", "add", "mp", "--type", "openai"))
    main(_args(workspace, "model", "add", "model-get", "--provider-id", "1"))
    main(_args(workspace, "model", "get", "1"))
    assert "model-get" in capsys.readouterr().out


def test_model_add_with_proxy(workspace, capsys):
    main(_args(workspace, "provider", "add", "mp2", "--type", "openai"))
    main(
        _args(
            workspace,
            "model",
            "add",
            "model-proxy",
            "--provider-id",
            "1",
            "--proxy",
            "http://127.0.0.1:7890",
        )
    )
    out = capsys.readouterr().out
    assert "Model created" in out
    assert "proxy=http://127.0.0.1:7890" in out


def test_model_update_all_scalar_fields(workspace, capsys):
    main(_args(workspace, "provider", "add", "mu", "--type", "openai"))
    main(_args(workspace, "model", "add", "model-upd", "--provider-id", "1"))
    main(
        _args(
            workspace,
            "model",
            "update",
            "1",
            "--display-name",
            "Display",
            "--api-format",
            "deepseek",
            "--max-retries",
            "5",
            "--cooldown",
            "30",
        )
    )
    assert "updated" in capsys.readouterr().out


def test_model_update_with_proxy_existing_model(workspace, capsys):
    main(_args(workspace, "provider", "add", "mu2", "--type", "openai"))
    main(_args(workspace, "model", "add", "model-upd2", "--provider-id", "1"))
    main(_args(workspace, "model", "update", "1", "--proxy", "http://proxy.local:1"))
    assert "updated" in capsys.readouterr().out


def test_model_update_with_proxy_missing_model(workspace, capsys):
    main(_args(workspace, "model", "update", "999", "--proxy", "http://proxy.local:1"))
    assert "updated" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# model sync
# ---------------------------------------------------------------------------


def test_model_sync_specific_provider(workspace, capsys, monkeypatch):
    import botflow.core as core_mod

    async def _sync(pid, db=None):
        return {"added": 2, "skipped": 1, "errors": []}

    monkeypatch.setattr(core_mod, "sync_models_from_provider", _sync)

    main(_args(workspace, "provider", "add", "sync-p", "--type", "openai"))
    main(_args(workspace, "model", "sync", "--provider-id", "1"))
    out = capsys.readouterr().out
    assert "added=2" in out
    assert "skipped=1" in out
    assert "Sync complete: total added=2 skipped=1" in out


def test_model_sync_all_enabled_and_reports_errors(workspace, capsys, monkeypatch):
    import botflow.core as core_mod

    async def _sync(pid, db=None):
        return {"errors": ["boom"]}

    monkeypatch.setattr(core_mod, "sync_models_from_provider", _sync)

    main(_args(workspace, "provider", "add", "sync-all", "--type", "openai"))
    main(_args(workspace, "model", "sync"))
    assert "errors=" in capsys.readouterr().out


def test_model_sync_no_changes(workspace, capsys, monkeypatch):
    import botflow.core as core_mod

    async def _sync(pid, db=None):
        return {}

    monkeypatch.setattr(core_mod, "sync_models_from_provider", _sync)

    main(_args(workspace, "provider", "add", "sync-empty", "--type", "openai"))
    main(_args(workspace, "model", "sync"))
    assert "no changes" in capsys.readouterr().out


def test_model_sync_missing_provider_id(workspace, capsys, monkeypatch):
    import botflow.core as core_mod

    async def _sync(pid, db=None):
        return {"added": 1}

    monkeypatch.setattr(core_mod, "sync_models_from_provider", _sync)

    main(_args(workspace, "model", "sync", "--provider-id", "999"))
    out = capsys.readouterr().out
    assert "#999" in out


def test_model_sync_records_exception(workspace, capsys, monkeypatch):
    import botflow.core as core_mod

    async def _sync(pid, db=None):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(core_mod, "sync_models_from_provider", _sync)

    main(_args(workspace, "provider", "add", "sync-boom", "--type", "openai"))
    main(_args(workspace, "model", "sync"))
    assert "Error: upstream down" in capsys.readouterr().out


def test_model_sync_no_enabled_providers(workspace, capsys, exit_raises):
    with pytest.raises(SystemExit):
        main(_args(workspace, "model", "sync"))
    assert "No enabled providers found." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# group get / update / membership
# ---------------------------------------------------------------------------


def test_group_get_with_models(workspace, capsys):
    main(_args(workspace, "provider", "add", "gp", "--type", "openai"))
    main(_args(workspace, "model", "add", "group-model", "--provider-id", "1"))
    main(_args(workspace, "group", "add", "group-get"))
    main(_args(workspace, "group", "add-model", "1", "1", "--weight", "2.0"))
    main(_args(workspace, "group", "get", "1"))
    out = capsys.readouterr().out
    assert "group-get" in out
    assert "Models:" in out
    assert "group-model" in out


def test_group_update_description_and_fallback(workspace, capsys):
    main(_args(workspace, "group", "add", "g-a"))
    main(_args(workspace, "group", "add", "g-b"))
    main(
        _args(
            workspace,
            "group",
            "update",
            "1",
            "--description",
            "primary pool",
            "--fallback",
            "2",
        )
    )
    assert "updated" in capsys.readouterr().out


def test_group_set_weight_and_remove_model(workspace, capsys):
    main(_args(workspace, "provider", "add", "gp2", "--type", "openai"))
    main(_args(workspace, "model", "add", "gm2", "--provider-id", "1"))
    main(_args(workspace, "group", "add", "group-rm"))
    main(_args(workspace, "group", "add-model", "1", "1"))
    main(_args(workspace, "group", "set-weight", "1", "1", "3.5"))
    main(_args(workspace, "group", "remove-model", "1", "1"))
    out = capsys.readouterr().out
    assert "weight in group 1 set to 3.5" in out
    assert "removed from group 1" in out


# ---------------------------------------------------------------------------
# stats with data
# ---------------------------------------------------------------------------


def test_stats_cost_with_data(workspace, capsys, monkeypatch):
    from botflow.storage.db import Database

    async def _cost(self, days=30, api_key_id=None):
        return [
            {
                "day": "2026-01-01",
                "total_calls": 3,
                "total_tokens": 100,
                "total_cost": 0.5,
            }
        ]

    monkeypatch.setattr(Database, "get_cost_summary", _cost)
    main(_args(workspace, "stats", "cost"))
    out = capsys.readouterr().out
    assert "2026-01-01" in out
    assert "0.5000" in out


def test_stats_model_with_data(workspace, capsys, monkeypatch):
    from botflow.storage.db import Database

    class _ModelStats:
        def model_dump(self, mode="json"):
            return {"model_id": 1, "total_calls": 7}

    async def _stats(self, model_id, api_key_id=None):
        return _ModelStats()

    monkeypatch.setattr(Database, "get_model_stats", _stats)
    main(_args(workspace, "stats", "model", "1"))
    assert "total_calls" in capsys.readouterr().out


def test_stats_group_with_data(workspace, capsys, monkeypatch):
    from botflow.storage.db import Database

    class _GroupStats:
        def model_dump(self, mode="json"):
            return {"group_id": 2, "total_calls": 9}

    async def _stats(self, group_id, api_key_id=None):
        return _GroupStats()

    monkeypatch.setattr(Database, "get_group_stats", _stats)
    main(_args(workspace, "stats", "group", "2"))
    assert "total_calls" in capsys.readouterr().out


def test_stats_recent_with_data(workspace, capsys, monkeypatch):
    from botflow.storage.db import Database

    async def _query(self, **kwargs):
        return [
            SimpleNamespace(
                status="ok",
                model_id=3,
                duration_ms=42,
                total_tokens=15,
                created_at="2026-01-01T00:00:00",
            ),
            SimpleNamespace(
                status="error",
                model_id=None,
                duration_ms=None,
                total_tokens=None,
                created_at="2026-01-02T00:00:00",
            ),
        ]

    monkeypatch.setattr(Database, "query_call_logs", _query)
    main(_args(workspace, "stats", "recent", "-n", "5"))
    out = capsys.readouterr().out
    assert "Recent 2 calls" in out
    assert "model=3" in out
    assert "model=?" in out
    assert "42ms" in out


# ---------------------------------------------------------------------------
# boolean arg
# ---------------------------------------------------------------------------


def test_bool_arg_accepts_aliases():
    assert _bool_arg("1") is True
    assert _bool_arg("yes") is True
    assert _bool_arg("0") is False
    assert _bool_arg("no") is False


def test_bool_arg_rejects_garbage():
    with pytest.raises(Exception):
        _bool_arg("maybe")
