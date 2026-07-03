"""Tests for the database layer: CRUD, queries, cleanup."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from botflow.storage.cleanup import cleanup_call_logs
from botflow.storage.db import Database
from botflow.storage.models import CallLog, Model, ModelGroup, Provider


@pytest.fixture
async def db():
    """Create a temporary database for each test."""
    f = Path(tempfile.mktemp(suffix=".db"))
    database = Database(f)
    await database.initialize()
    yield database
    await database.close()
    if f.exists():
        f.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------

class TestConfig:
    @pytest.mark.asyncio
    async def test_set_and_get(self, db):
        await db.set_config("llm_key", "test-key-123")
        assert await db.get_config("llm_key") == "test-key-123"

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, db):
        assert await db.get_config("nonexistent") is None

    @pytest.mark.asyncio
    async def test_overwrite(self, db):
        await db.set_config("key1", "value1")
        await db.set_config("key1", "value2")
        assert await db.get_config("key1") == "value2"

    @pytest.mark.asyncio
    async def test_multiple_keys(self, db):
        await db.set_config("k1", "v1")
        await db.set_config("k2", "v2")
        assert await db.get_config("k1") == "v1"
        assert await db.get_config("k2") == "v2"


# ---------------------------------------------------------------------------
# Provider CRUD tests
# ---------------------------------------------------------------------------

class TestProviderCRUD:
    @pytest.mark.asyncio
    async def test_create_and_get(self, db):
        p = Provider(name="test-openai", provider_type="openai", api_key="sk-test", base_url="https://test.api.com")
        pid = await db.create_provider(p)
        assert pid > 0
        fetched = await db.get_provider(pid)
        assert fetched is not None
        assert fetched.name == "test-openai"
        assert fetched.provider_type == "openai"

    @pytest.mark.asyncio
    async def test_update(self, db):
        pid = await db.create_provider(Provider(name="old-name", provider_type="openai"))
        await db.update_provider(pid, {"name": "new-name", "base_url": "https://new.url"})
        fetched = await db.get_provider(pid)
        assert fetched.name == "new-name"
        assert fetched.base_url == "https://new.url"

    @pytest.mark.asyncio
    async def test_delete(self, db):
        pid = await db.create_provider(Provider(name="to-delete", provider_type="openai"))
        await db.delete_provider(pid)
        assert await db.get_provider(pid) is None

    @pytest.mark.asyncio
    async def test_list(self, db):
        await db.create_provider(Provider(name="p1", provider_type="openai"))
        await db.create_provider(Provider(name="p2", provider_type="anthropic"))
        providers = await db.list_providers()
        assert len(providers) == 2

    @pytest.mark.asyncio
    async def test_list_enabled_only(self, db):
        await db.create_provider(Provider(name="enabled", provider_type="openai", is_enabled=True))
        await db.create_provider(Provider(name="disabled", provider_type="openai", is_enabled=False))
        enabled = await db.list_providers(enabled_only=True)
        assert len(enabled) == 1
        assert enabled[0].name == "enabled"

    @pytest.mark.asyncio
    async def test_unique_name(self, db):
        await db.create_provider(Provider(name="unique", provider_type="openai"))
        with pytest.raises(Exception):
            await db.create_provider(Provider(name="unique", provider_type="anthropic"))


# ---------------------------------------------------------------------------
# Model CRUD tests
# ---------------------------------------------------------------------------

class TestModelCRUD:
    @pytest.mark.asyncio
    async def test_create_and_get(self, db):
        pid = await db.create_provider(Provider(name="prov", provider_type="openai"))
        m = Model(name="gpt-4o", provider_id=pid, display_name="GPT-4o", max_retries=5, cooldown_seconds=120,
                  cooldown_failure_threshold=2)
        mid = await db.create_model(m)
        assert mid > 0
        fetched = await db.get_model(mid)
        assert fetched is not None
        assert fetched.name == "gpt-4o"
        assert fetched.max_retries == 5
        assert fetched.cooldown_seconds == 120
        assert fetched.cooldown_failure_threshold == 2

    @pytest.mark.asyncio
    async def test_delete_cascades(self, db):
        pid = await db.create_provider(Provider(name="prov", provider_type="openai"))
        mid = await db.create_model(Model(name="m", provider_id=pid))
        await db.delete_model(mid)
        assert await db.get_model(mid) is None


# ---------------------------------------------------------------------------
# Group CRUD tests
# ---------------------------------------------------------------------------

class TestGroupCRUD:
    @pytest.mark.asyncio
    async def test_create_and_get_group_models(self, db):
        pid = await db.create_provider(Provider(name="prov", provider_type="openai"))
        mid = await db.create_model(Model(name="gpt-4o", provider_id=pid))
        gid = await db.create_group(ModelGroup(name="primary"))

        await db.add_model_to_group(gid, mid, weight=2.0)
        models = await db.get_group_models(gid)
        assert len(models) == 1
        assert models[0].model_name == "gpt-4o"
        assert models[0].weight == 2.0

    @pytest.mark.asyncio
    async def test_delete_removes_associations(self, db):
        """Verify delete_group removes group_models entries (simulate cascade)."""
        pid = await db.create_provider(Provider(name="prov", provider_type="openai"))
        mid = await db.create_model(Model(name="m", provider_id=pid))
        gid = await db.create_group(ModelGroup(name="temp"))
        await db.add_model_to_group(gid, mid, 1.0)
        assert len(await db.get_group_models(gid)) == 1
        await db.delete_group(gid)
        assert len(await db.get_group_models(gid)) == 0


# ---------------------------------------------------------------------------
# CallLog tests
# ---------------------------------------------------------------------------

class TestCallLog:
    @pytest.mark.asyncio
    async def test_create_and_query(self, db):
        log = CallLog(
            group_id=1,
            model_id=1,
            provider_id=1,
            request_body='{"messages": [{"role": "user"}]}',
            response_body='{"choices": [{"message": {"content": "hi"}}]}',
            status="success",
            duration_ms=150,
            prompt_tokens=10,
            completion_tokens=20,
            cache_tokens=5,
            total_tokens=35,
        )
        log_id = await db.create_call_log(log)
        assert log_id > 0

        logs = await db.query_call_logs(limit=10)
        assert len(logs) == 1
        assert logs[0].status == "success"
        assert logs[0].cache_tokens == 5

    @pytest.mark.asyncio
    async def test_query_with_filters(self, db):
        l1 = CallLog(status="success", group_id=1)
        l2 = CallLog(status="error", group_id=2)
        await db.create_call_log(l1)
        await db.create_call_log(l2)

        success_logs = await db.query_call_logs(status="success")
        assert len(success_logs) == 1

        group1_logs = await db.query_call_logs(group_id=1)
        assert len(group1_logs) == 1


# ---------------------------------------------------------------------------
# Stats tests
# ---------------------------------------------------------------------------

class TestStats:
    @pytest.mark.asyncio
    async def test_model_stats_with_data(self, db):
        # Need a model record for the JOIN
        pid = await db.create_provider(Provider(name="stats-prov", provider_type="openai"))
        mid = await db.create_model(Model(name="stats-model", provider_id=pid))
        log = CallLog(model_id=mid, status="success", prompt_tokens=100, completion_tokens=50, cache_tokens=10)
        await db.create_call_log(log)

        stats = await db.get_model_stats(mid)
        assert stats is not None
        assert stats.total_calls == 1
        assert stats.success_calls == 1
        assert stats.total_prompt_tokens == 100
        assert stats.total_cache_tokens == 10

    @pytest.mark.asyncio
    async def test_stats_nonexistent(self, db):
        assert await db.get_model_stats(999) is None
        assert await db.get_group_stats(999) is None

    @pytest.mark.asyncio
    async def test_group_stats_with_data(self, db):
        pid = await db.create_provider(Provider(name="gs-prov", provider_type="openai"))
        mid = await db.create_model(Model(name="gs-model", provider_id=pid))
        gid = await db.create_group(ModelGroup(name="gs-group"))
        await db.add_model_to_group(gid, mid, 1.0)
        log = CallLog(model_id=mid, group_id=gid, status="success", cost=0.05)
        await db.create_call_log(log)

        stats = await db.get_group_stats(gid)
        assert stats is not None
        assert stats.group_name == "gs-group"
        assert stats.total_calls == 1
        assert stats.total_cost == 0.05

    @pytest.mark.asyncio
    async def test_cost_summary(self, db):
        summary = await db.get_cost_summary(days=30)
        # No data yet
        assert isinstance(summary, list)

        # Add a log and verify
        await db.create_call_log(CallLog(status="success", total_tokens=100, cost=0.01))
        summary = await db.get_cost_summary(days=30)
        assert len(summary) >= 1
        assert summary[0]["total_calls"] >= 1


# ---------------------------------------------------------------------------
# Cleanup tests
# ---------------------------------------------------------------------------

class TestCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_removes_old_logs(self, db):
        await db.create_call_log(CallLog(status="success"))
        # 0-day retention should delete everything
        deleted = await cleanup_call_logs(db, retention_days=0)
        assert deleted >= 1

    @pytest.mark.asyncio
    async def test_cleanup_preserves_recent_logs(self, db):
        await db.create_call_log(CallLog(status="success"))
        # 365-day retention should keep today's log
        deleted = await cleanup_call_logs(db, retention_days=365)
        assert deleted == 0

    @pytest.mark.asyncio
    async def test_delete_old_call_logs(self, db):
        deleted = await db.delete_old_call_logs("2099-01-01")
        assert deleted == 0
        await db.create_call_log(CallLog(status="success"))
        deleted = await db.delete_old_call_logs("2099-01-01")
        assert deleted == 1
