"""Tests for new botflow.storage.db methods (api keys, summaries, filters)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from botflow.storage.db import Database
from botflow.storage.models import CallLog, Provider


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "test.db"))
    await d.initialize()
    yield d
    await d.close()


async def _provider_model(db):
    pid = await db.create_provider(Provider(name="p", provider_type="openai"))
    mid = await db.create_model(__import__("botflow.storage.models", fromlist=["Model"]).Model(name="gpt-4", provider_id=pid))
    return pid, mid


class TestApiKeys:
    async def test_create_and_list(self, db):
        k = await db.create_api_key("raw-secret", label="team-a")
        assert isinstance(k, __import__("botflow.storage.models", fromlist=["ApiKey"]).ApiKey)
        assert k.id > 0
        keys = await db.list_api_keys()
        assert len(keys) == 1 and keys[0].label == "team-a"
        assert keys[0].key_hash == db.hash_key("raw-secret")

    async def test_get_enable_disable(self, db):
        k = await db.create_api_key("k", label="x")
        assert (await db.get_api_key(k.id)) is not None
        await db.set_api_key_enabled(k.id, False)
        assert (await db.get_api_key(k.id)).is_enabled is False
        await db.set_api_key_enabled(k.id, True)
        assert (await db.get_api_key(k.id)).is_enabled is True

    async def test_delete(self, db):
        k = await db.create_api_key("k", label="x")
        await db.delete_api_key(k.id)
        assert await db.get_api_key(k.id) is None

    async def test_nonexistent(self, db):
        assert await db.get_api_key(9999) is None

    async def test_hash_key(self, db):
        h = db.hash_key("abc")
        assert h != "abc" and db.hash_key("abc") == h


class TestCallLogNewFields:
    async def test_fields_persisted(self, db):
        _, mid = await _provider_model(db)
        k = await db.create_api_key("k", label="x")
        lid = await db.create_call_log(CallLog(model_id=mid, status="error", api_key_id=k.id,
                                               error_type="timeout", traceback="tb",
                                               request_id="req-1", request_body='{"q":1}',
                                               response_body="", duration_ms=42))
        fetched = (await db.query_call_logs(limit=1))[0]
        assert fetched.api_key_id == k.id
        assert fetched.error_type == "timeout"
        assert fetched.traceback == "tb"
        assert fetched.request_id == "req-1"
        assert fetched.duration_ms == 42


class TestQueryCallLogsFilters:
    async def test_filters(self, db):
        pid, mid = await _provider_model(db)
        k = await db.create_api_key("k", label="x")
        await db.create_call_log(CallLog(model_id=mid, provider_id=pid, group_id=1, status="success", api_key_id=k.id))
        await db.create_call_log(CallLog(model_id=mid, provider_id=pid, group_id=1, status="error",
                                         error_type="timeout", api_key_id=k.id))
        assert len(await db.query_call_logs(model_id=mid)) == 2
        assert len(await db.query_call_logs(provider_id=pid)) == 2
        assert len(await db.query_call_logs(api_key_id=k.id)) == 2
        assert len(await db.query_call_logs(error_type="timeout")) == 1
        assert len(await db.query_call_logs(status="error")) == 1
        assert len(await db.query_call_logs(status="success", model_id=mid)) == 1
        # limit
        assert len(await db.query_call_logs(limit=1)) == 1


class TestModelStatsApiKeyFilter:
    async def test_filter(self, db):
        _, mid = await _provider_model(db)
        k1 = (await db.create_api_key("a", label="a")).id
        k2 = (await db.create_api_key("b", label="b")).id
        await db.create_call_log(CallLog(model_id=mid, status="success", api_key_id=k1))
        await db.create_call_log(CallLog(model_id=mid, status="success", api_key_id=k2))
        assert (await db.get_model_stats(mid)).total_calls == 2
        assert (await db.get_model_stats(mid, api_key_id=k1)).total_calls == 1

    async def test_zero_success(self, db):
        _, mid = await _provider_model(db)
        await db.create_call_log(CallLog(model_id=mid, status="error"))
        s = await db.get_model_stats(mid)
        assert s.success_calls == 0 and s.total_calls == 1

    async def test_nonexistent(self, db):
        assert await db.get_model_stats(9999) is None


class TestGroupStatsApiKeyFilter:
    async def test_filter_and_name(self, db):
        pid, mid = await _provider_model(db)
        gid = await db.create_group(__import__("botflow.storage.models", fromlist=["ModelGroup"]).ModelGroup(name="g"))
        await db.add_model_to_group(gid, mid, 1.0)
        k1 = (await db.create_api_key("a", label="a")).id
        await db.create_call_log(CallLog(model_id=mid, group_id=gid, status="success", api_key_id=k1, cost=0.1))
        s = await db.get_group_stats(gid)
        assert s.group_name == "g" and s.total_calls == 1
        assert (await db.get_group_stats(gid, api_key_id=k1)).total_calls == 1
        # group with no models -> group_name default unknown
        gid2 = await db.create_group(__import__("botflow.storage.models", fromlist=["ModelGroup"]).ModelGroup(name="empty"))
        s2 = await db.get_group_stats(gid2)
        assert s2 is None  # no logs yet -> stats not generated

    async def test_nonexistent(self, db):
        assert await db.get_group_stats(9999) is None


class TestCostSummaryApiKeyFilter:
    async def test_filter(self, db):
        _, mid = await _provider_model(db)
        k1 = (await db.create_api_key("a", label="a")).id
        k2 = (await db.create_api_key("b", label="b")).id
        await db.create_call_log(CallLog(model_id=mid, status="success", cost=0.1, api_key_id=k1))
        await db.create_call_log(CallLog(model_id=mid, status="success", cost=0.2, api_key_id=k2))
        all_c = await db.get_cost_summary(days=30)
        assert sum(r["total_cost"] for r in all_c) == pytest.approx(0.3)
        f = await db.get_cost_summary(days=30, api_key_id=k1)
        assert sum(r["total_cost"] for r in f) == pytest.approx(0.1)

    async def test_empty(self, db):
        assert isinstance(await db.get_cost_summary(days=30), list)


class TestDailySummaries:
    async def test_upsert_and_get(self, db):
        day = "2026-01-01"
        await db.upsert_daily_summary(day, "# Summary", json.dumps({"n": 10}))
        got = await db.get_daily_summary(day)
        assert got is not None and got.summary_md == "# Summary"
        await db.upsert_daily_summary(day, "# Updated", json.dumps({"n": 20}))
        got2 = await db.get_daily_summary(day)
        assert got2.summary_md == "# Updated"

    async def test_missing(self, db):
        assert await db.get_daily_summary("1999-01-01") is None

    async def test_delete_old(self, db):
        old = "2000-01-01"
        await db.upsert_daily_summary(old, "x", "{}")
        assert await db.delete_old_daily_summaries("2001-01-01") >= 1
        assert await db.get_daily_summary(old) is None


class TestRawSessions:
    async def test_store_and_query(self, db):
        import gzip, json
        day = datetime.now(timezone.utc).date().isoformat()
        blob = gzip.compress(json.dumps([{"a": 1}]).encode("utf-8"))
        await db.save_raw_session(day, blob)
        got = await db.get_raw_session(day)
        assert got is not None and got == blob
        assert await db.get_raw_session("1999-01-01") is None
        # cutoff strictly after today -> today's session is older and gets deleted
        future = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
        assert await db.delete_old_raw_sessions(future) >= 1
        assert await db.get_raw_session(day) is None
        # older session
        old = (datetime.now(timezone.utc) - timedelta(days=5)).date().isoformat()
        await db.save_raw_session(old, b"x")
        assert await db.delete_old_raw_sessions(future) >= 1
        assert await db.get_raw_session(old) is None


class TestCleanupWithKeys:
    async def test_cleanup_old_call_logs(self, db):
        _, mid = await _provider_model(db)
        k = (await db.create_api_key("k", label="x")).id
        await db.create_call_log(CallLog(model_id=mid, status="success", api_key_id=k))
        assert await db.delete_old_call_logs("2099-01-01") == 1
        assert await db.query_call_logs(api_key_id=k) == []

    async def test_purge_old_detail(self, db):
        from botflow.config import BotflowSettings, set_config
        from botflow.storage import daily_summary as ds
        _, mid = await _provider_model(db)
        await db.create_call_log(CallLog(model_id=mid, status="success", request_body="{}"))
        # insert an old log outside retention window
        await db._conn.execute(
            "INSERT INTO call_logs (model_id, status, request_body, created_at) VALUES (?, 'success', '{}', '2000-01-01 00:00:00')",
            (mid,),
        )
        await db._conn.commit()
        set_config(BotflowSettings(call_log_detail_days=0))
        try:
            assert await ds.purge_old_detail(db) >= 1
        finally:
            set_config(None)


class TestGetCallLogsForDay:
    async def test_returns_today(self, db):
        _, mid = await _provider_model(db)
        await db.create_call_log(CallLog(model_id=mid, status="success"))
        day = datetime.now(timezone.utc).date().isoformat()
        logs = await db.get_call_logs_for_day(day)
        assert len(logs) >= 1
        # future -> empty
        assert await db.get_call_logs_for_day("2999-01-01") == []
