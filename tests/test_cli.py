"""Tests for CLI subcommands (provider/model/group/stats/config/cleanup)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture()
def cli_db(tmp_path: Path):
    """Create a temp workspace with a fresh botflow.db and return db + metadata."""
    from botflow.storage.db import Database
    from botflow.storage.models import Provider, Model, ModelGroup

    db_path = tmp_path / "data" / "botflow.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = Database(db_path)

    async def _setup():
        await db.initialize()
        pid = await db.create_provider(Provider(
            name="test-provider",
            provider_type="openai",
            api_key="sk-test",
            base_url="https://api.test.com",
        ))
        mid = await db.create_model(Model(
            name="test-model",
            provider_id=pid,
            display_name="Test Model",
        ))
        gid = await db.create_group(ModelGroup(
            name="test-group",
            description="Test group",
        ))
        await db.add_model_to_group(gid, mid, weight=2.0)
        return {"db": db, "provider_id": pid, "model_id": mid, "group_id": gid}

    info = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(_setup())
    yield info
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(db.close())


# ---------------------------------------------------------------------------
# CRUD integration tests
# ---------------------------------------------------------------------------


def test_provider_list(cli_db):
    loop = asyncio.get_event_loop_policy().new_event_loop()
    providers = loop.run_until_complete(cli_db["db"].list_providers())
    assert len(providers) == 1
    assert providers[0].name == "test-provider"
    assert providers[0].provider_type == "openai"


def test_provider_crud(cli_db):
    from botflow.storage.models import Provider
    loop = asyncio.get_event_loop_policy().new_event_loop()
    db = cli_db["db"]

    # Create
    pid = loop.run_until_complete(db.create_provider(Provider(
        name="crud-provider", provider_type="anthropic", api_key="sk-crud",
    )))
    assert pid is not None

    # Get
    p = loop.run_until_complete(db.get_provider(pid))
    assert p.name == "crud-provider"

    # Update
    loop.run_until_complete(db.update_provider(pid, {"name": "updated-provider"}))
    p = loop.run_until_complete(db.get_provider(pid))
    assert p.name == "updated-provider"

    # Delete
    loop.run_until_complete(db.delete_provider(pid))
    p = loop.run_until_complete(db.get_provider(pid))
    assert p is None


def test_model_crud(cli_db):
    from botflow.storage.models import Model
    loop = asyncio.get_event_loop_policy().new_event_loop()
    db = cli_db["db"]
    provider_id = cli_db["provider_id"]

    mid = loop.run_until_complete(db.create_model(Model(
        name="crud-model", provider_id=provider_id,
    )))
    assert mid is not None

    m = loop.run_until_complete(db.get_model(mid))
    assert m.name == "crud-model"

    loop.run_until_complete(db.update_model(mid, {"name": "updated-model"}))
    m = loop.run_until_complete(db.get_model(mid))
    assert m.name == "updated-model"

    loop.run_until_complete(db.delete_model(mid))
    m = loop.run_until_complete(db.get_model(mid))
    assert m is None


def test_group_crud(cli_db):
    from botflow.storage.models import ModelGroup
    loop = asyncio.get_event_loop_policy().new_event_loop()
    db = cli_db["db"]
    model_id = cli_db["model_id"]

    gid = loop.run_until_complete(db.create_group(ModelGroup(name="crud-group")))
    assert gid is not None

    g = loop.run_until_complete(db.get_group(gid))
    assert g.name == "crud-group"

    # Add model
    loop.run_until_complete(db.add_model_to_group(gid, model_id, weight=3.0))
    models = loop.run_until_complete(db.get_group_models(gid))
    assert len(models) == 1
    assert models[0].weight == 3.0

    # Update weight
    loop.run_until_complete(db.update_model_weight(gid, model_id, 5.0))
    models = loop.run_until_complete(db.get_group_models(gid))
    assert models[0].weight == 5.0

    # Remove model
    loop.run_until_complete(db.remove_model_from_group(gid, model_id))
    models = loop.run_until_complete(db.get_group_models(gid))
    assert len(models) == 0

    # Delete group
    loop.run_until_complete(db.delete_group(gid))
    g = loop.run_until_complete(db.get_group(gid))
    assert g is None


def test_config_crud(cli_db):
    loop = asyncio.get_event_loop_policy().new_event_loop()
    db = cli_db["db"]

    loop.run_until_complete(db.set_config("test.key", "test.value"))
    val = loop.run_until_complete(db.get_config("test.key"))
    assert val == "test.value"

    val = loop.run_until_complete(db.get_config("nonexistent"))
    assert val is None


def test_stats_cost_summary(cli_db):
    loop = asyncio.get_event_loop_policy().new_event_loop()
    db = cli_db["db"]
    summary = loop.run_until_complete(db.get_cost_summary(days=30))
    assert summary == []


def test_call_log_with_model_id(cli_db):
    loop = asyncio.get_event_loop_policy().new_event_loop()
    db = cli_db["db"]

    conn = loop.run_until_complete(db._ensure_connection())
    loop.run_until_complete(conn.execute(
        """INSERT INTO call_logs
        (group_id, model_id, provider_id, status,
         total_tokens, prompt_tokens, completion_tokens, cache_tokens,
         duration_ms, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
        (cli_db["group_id"], cli_db["model_id"],
         cli_db["provider_id"], "success", 1000, 800, 200, 0, 1500),
    ))
    loop.run_until_complete(conn.commit())

    logs = loop.run_until_complete(db.query_call_logs(limit=5))
    assert len(logs) == 1
    assert logs[0].model_id == cli_db["model_id"]


def test_group_list_with_fallback(cli_db):
    from botflow.storage.models import ModelGroup
    loop = asyncio.get_event_loop_policy().new_event_loop()
    db = cli_db["db"]

    fallback_id = loop.run_until_complete(db.create_group(ModelGroup(name="fallback")))
    loop.run_until_complete(db.update_group(cli_db["group_id"], {"fallback_group_id": fallback_id}))

    g = loop.run_until_complete(db.get_group(cli_db["group_id"]))
    assert g.fallback_group_id == fallback_id


def test_provider_list_enabled_only(cli_db):
    from botflow.storage.models import Provider
    loop = asyncio.get_event_loop_policy().new_event_loop()
    db = cli_db["db"]

    pid = loop.run_until_complete(db.create_provider(Provider(
        name="disabled-provider", provider_type="openai",
    )))
    loop.run_until_complete(db.update_provider(pid, {"is_enabled": False}))

    enabled = loop.run_until_complete(db.list_providers(enabled_only=True))
    assert all(p.name != "disabled-provider" for p in enabled)

    all_p = loop.run_until_complete(db.list_providers(enabled_only=False))
    assert any(p.name == "disabled-provider" for p in all_p)


def test_cleanup_call_logs(cli_db):
    loop = asyncio.get_event_loop_policy().new_event_loop()
    db = cli_db["db"]

    conn = loop.run_until_complete(db._ensure_connection())
    # Old log (300 days ago)
    loop.run_until_complete(conn.execute(
        """INSERT INTO call_logs
        (group_id, provider_id, status, created_at)
        VALUES (?, ?, ?, datetime('now', '-300 days'))""",
        (1, 1, "success"),
    ))
    # Recent log
    loop.run_until_complete(conn.execute(
        """INSERT INTO call_logs
        (group_id, provider_id, status, created_at)
        VALUES (?, ?, ?, datetime('now'))""",
        (1, 1, "success"),
    ))
    loop.run_until_complete(conn.commit())

    from botflow.storage.daily_summary import purge_old_call_logs
    deleted = loop.run_until_complete(purge_old_call_logs(db, retention_days=180))
    assert deleted == 1

    logs = loop.run_until_complete(db.query_call_logs(limit=10))
    assert len(logs) == 1
    assert logs[0].status == "success"


# ---------------------------------------------------------------------------
# Parser tests (no DB)
# ---------------------------------------------------------------------------


class TestParser:
    def test_build_parser(self):
        from botflow.cli.main import build_parser
        assert build_parser() is not None

    def test_version_flag(self):
        from botflow.cli.main import build_parser
        args = build_parser().parse_args(["version"])
        assert args.command == "version"

    def test_run_subcommand(self):
        from botflow.cli.main import build_parser
        args = build_parser().parse_args(["run", "--port", "9999", "--host", "127.0.0.1"])
        assert args.command == "run"
        assert args.port == 9999
        assert args.host == "127.0.0.1"

    def test_provider_list(self):
        from botflow.cli.main import build_parser
        args = build_parser().parse_args(["provider", "list", "--enabled"])
        assert args.provider_action == "list"
        assert args.enabled is True

    def test_provider_add(self):
        from botflow.cli.main import build_parser
        args = build_parser().parse_args([
            "provider", "add", "my-provider",
            "--type", "anthropic", "--api-key", "sk-123",
        ])
        assert args.provider_action == "add"
        assert args.name == "my-provider"
        assert args.type == "anthropic"

    def test_model_add(self):
        from botflow.cli.main import build_parser
        args = build_parser().parse_args([
            "model", "add", "claude-4",
            "--provider-id", "1", "--display-name", "Claude 4", "--max-retries", "5",
        ])
        assert args.model_action == "add"
        assert args.name == "claude-4"
        assert args.provider_id == 1
        assert args.max_retries == 5

    def test_group_add_model(self):
        from botflow.cli.main import build_parser
        args = build_parser().parse_args(["group", "add-model", "1", "3", "--weight", "2.5"])
        assert args.group_action == "add-model"
        assert args.id == 1
        assert args.model_id == 3
        assert args.weight == 2.5

    def test_stats_recent(self):
        from botflow.cli.main import build_parser
        args = build_parser().parse_args(["stats", "recent", "-n", "10"])
        assert args.stats_action == "recent"
        assert args.limit == 10

    def test_cleanup(self):
        from botflow.cli.main import build_parser
        args = build_parser().parse_args(["cleanup", "--days", "90"])
        assert args.days == 90

    def test_set_config(self):
        from botflow.cli.main import build_parser
        args = build_parser().parse_args(["set", "llm_key", "sk-new-key"])
        assert args.key == "llm_key"
        assert args.value == "sk-new-key"

    def test_logs(self):
        from botflow.cli.main import build_parser
        args = build_parser().parse_args(["logs", "-n", "100"])
        assert args.lines == 100

    def test_group_set_weight(self):
        from botflow.cli.main import build_parser
        args = build_parser().parse_args(["group", "set-weight", "1", "3", "5.0"])
        assert args.group_action == "set-weight"
        assert args.weight == 5.0

    def test_restart(self):
        from botflow.cli.main import build_parser
        args = build_parser().parse_args(["restart", "--port", "9999"])
        assert args.command == "restart"
        assert args.port == 9999


# ---------------------------------------------------------------------------
# Service management tests
# ---------------------------------------------------------------------------


class TestServiceManagement:
    def test_pid_file_write_read_clear(self, tmp_path):
        from botflow.cli.service import write_pid, read_pid, clear_pid
        write_pid(tmp_path, 12345)
        assert read_pid(tmp_path) == 12345
        clear_pid(tmp_path)
        assert read_pid(tmp_path) is None

    def test_read_pid_nonexistent(self, tmp_path):
        from botflow.cli.service import read_pid
        assert read_pid(tmp_path) is None

    def test_read_pid_invalid(self, tmp_path):
        from botflow.cli.service import read_pid
        pf = tmp_path / "data" / "botflow.pid"
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text("not-a-number")
        assert read_pid(tmp_path) is None

    def test_is_running_false(self):
        from botflow.cli.service import is_running
        assert is_running(99999999) is False

    def test_is_running_self(self):
        import os
        from botflow.cli.service import is_running
        assert is_running(os.getpid()) is True

    def test_stop_no_pid(self, tmp_path):
        from botflow.cli.service import stop_service
        result = stop_service(tmp_path)
        assert result["ok"] is False

    def test_stop_stale_pid(self, tmp_path):
        from botflow.cli.service import write_pid, stop_service
        write_pid(tmp_path, 99999999)
        result = stop_service(tmp_path)
        assert result["ok"] is True

    def test_get_status_not_running(self, tmp_path):
        from botflow.cli.service import get_status
        status = get_status(tmp_path, port=8080)
        assert status["running"] is False
        assert status["pid"] is None
