"""Tests for new botflow.storage.db methods (api keys, summaries, filters)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from botflow.storage.db import Database
from botflow.storage.models import CallAttempt, CallLog, Provider


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


# ===========================================================================
# SG-0 F1 / F2：call_attempts 建表、索引、批量写入
# ===========================================================================


class TestCallAttempts:
    # T1.1 正例：初始化后 call_attempts 表存在且列齐全
    async def test_call_attempts_table_created(self, db):
        rows = await db.execute_read(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='call_attempts'"
        )
        assert len(rows) == 1
        cols = await db.execute_read("PRAGMA table_info(call_attempts)")
        col_names = {r["name"] for r in cols}
        # `id INTEGER PRIMARY KEY AUTOINCREMENT` is the house convention (same
        # as call_logs), so assert the required columns are a subset instead of
        # exact set equality.
        required = {
            "request_id", "group_id", "model_id", "provider_id", "stage",
            "endpoint_idx", "attempt_no", "error_type", "error_message",
            "duration_ms", "created_at",
        }
        assert required <= col_names
        assert "id" in col_names

    # T1.2 正例：索引 idx_call_attempts_request 存在
    async def test_call_attempts_index_created(self, db):
        rows = await db.execute_read(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_call_attempts_request'"
        )
        assert len(rows) == 1

    # T1.3 边界：旧库（只有 call_logs，没有 call_attempts）升级后建表且不破坏既有数据
    async def test_legacy_db_upgrade_creates_table_and_keeps_data(self, tmp_path):
        p = tmp_path / "legacy.db"
        legacy = Database(str(p))
        await legacy.initialize()
        # Simulate a pre-SG-0 database: drop the new table, insert a call_logs row.
        await legacy.execute_write("DROP TABLE call_attempts")
        await legacy.execute_write("INSERT INTO call_logs (status) VALUES ('success')")
        await legacy.close()

        upgraded = Database(str(p))
        await upgraded.initialize()
        logs = await upgraded.execute_read("SELECT * FROM call_logs")
        assert len(logs) == 1 and logs[0]["status"] == "success"
        tbl = await upgraded.execute_read(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='call_attempts'"
        )
        assert len(tbl) == 1
        await upgraded.close()

    # T2.1 正例：批量写入 3 行（一次 executemany，而非逐行 round-trip）
    async def test_create_call_attempts_batch_insert(self, db, monkeypatch):
        a1 = CallAttempt(request_id="r1", group_id=1, model_id=10, provider_id=1,
                         stage="select", endpoint_idx=0, attempt_no=1,
                         error_type="ProviderError", error_message="boom", duration_ms=5)
        a2 = CallAttempt(request_id="r1", group_id=1, model_id=11, provider_id=1,
                         stage="stream", endpoint_idx=0, attempt_no=1,
                         error_type="TimeoutError", error_message="timeout", duration_ms=7)
        a3 = CallAttempt(request_id="r1", group_id=2, model_id=20, provider_id=2,
                         stage="select", endpoint_idx=1, attempt_no=2,
                         error_type="ValueError", error_message="bad", duration_ms=3)

        # create_call_attempts writes via `conn.executemany` directly (not
        # `db.execute_write`), so spy on the connection to prove batching.
        class _ConnSpy:
            def __init__(self, real):
                self._real = real
                self.executemany_calls = 0
                self.rows_per_call = []

            async def executemany(self, sql, rows):
                self.executemany_calls += 1
                self.rows_per_call.append(len(list(rows)))
                return await self._real.executemany(sql, rows)

            def __getattr__(self, name):
                return getattr(self._real, name)

        spy = _ConnSpy(await db._ensure_connection())

        async def _ensure():
            return spy

        monkeypatch.setattr(db, "_ensure_connection", _ensure)
        await db.create_call_attempts([a1, a2, a3])

        rows = await db.execute_read("SELECT * FROM call_attempts ORDER BY id")
        assert len(rows) == 3
        assert {r["model_id"] for r in rows} == {10, 11, 20}
        assert {r["group_id"] for r in rows} == {1, 2}
        # 一次批量调用，证明走 executemany（不是逐行 INSERT）。
        assert spy.executemany_calls == 1
        assert spy.rows_per_call == [3]

    # T2.2 边界：空列表不报错、不写行、连连接都不开
    async def test_create_call_attempts_empty_list(self, db, monkeypatch):
        opened = []
        real_ensure = db._ensure_connection

        async def _ensure():
            opened.append(1)
            return await real_ensure()

        monkeypatch.setattr(db, "_ensure_connection", _ensure)
        written = await db.create_call_attempts([])
        # 空列表在开连接之前就短路返回（快照必须取在此刻 —— 下面读表本身也会开连接）。
        assert written == 0
        assert opened == []
        rows = await db.execute_read("SELECT * FROM call_attempts")
        assert rows == []

    # T2.4 正例（覆盖率）：query_call_attempts 的 provider_id / group_id /
    # error_type 三个过滤分支（T8.x 只有 request_id / model_id 走过）。
    async def test_query_call_attempts_extra_filters(self, db):
        await db.create_call_attempts([
            CallAttempt(request_id="r1", group_id=1, model_id=10, provider_id=1,
                        stage="select", endpoint_idx=0, attempt_no=1,
                        error_type="ProviderError", error_message="a", duration_ms=1),
            CallAttempt(request_id="r2", group_id=2, model_id=20, provider_id=2,
                        stage="stream", endpoint_idx=1, attempt_no=1,
                        error_type="TimeoutError", error_message="b", duration_ms=2),
        ])
        by_provider = await db.query_call_attempts(provider_id=2)
        assert [r.request_id for r in by_provider] == ["r2"]
        by_group = await db.query_call_attempts(group_id=1)
        assert [r.request_id for r in by_group] == ["r1"]
        by_err = await db.query_call_attempts(error_type="TimeoutError")
        assert [r.request_id for r in by_err] == ["r2"]
        # 组合过滤 + 分页参数仍然可用。
        both = await db.query_call_attempts(provider_id=1, group_id=1, limit=10, offset=0)
        assert [r.request_id for r in both] == ["r1"]

    # T2.3 反例：select 阶段失败（model_id/provider_id 为 None）也可写入
    async def test_create_call_attempts_allows_null_attribution(self, db):
        a = CallAttempt(request_id="r", group_id=1, model_id=None, provider_id=None,
                        stage="select", endpoint_idx=0, attempt_no=1,
                        error_type="AllModelsCooldownError", error_message="all cooldown")
        await db.create_call_attempts([a])
        rows = await db.execute_read("SELECT model_id, provider_id FROM call_attempts")
        assert len(rows) == 1
        assert rows[0]["model_id"] is None
        assert rows[0]["provider_id"] is None
