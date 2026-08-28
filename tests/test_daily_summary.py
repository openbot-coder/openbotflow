"""Tests for botflow.storage.daily_summary (100% coverage target)."""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone

import pytest

from botflow.config import BotflowSettings, set_config
from botflow.storage import daily_summary as ds
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


async def _seed(db, *, with_error=True, n_success=2):
    pid, mid = await _provider_model(db)
    base = dict(model_id=mid, provider_id=pid, group_id=1, request_body="{}",
                response_body="{}", prompt_tokens=10, completion_tokens=5,
                cache_tokens=1, total_tokens=16, cost=0.01, is_stream=False, duration_ms=100)
    for _ in range(n_success):
        await db.create_call_log(CallLog(status="success", **base))
    if with_error:
        await db.create_call_log(CallLog(status="error", error_type="timeout",
                                         traceback="tb", **base))
    return mid


class TestGenerateWiki:
    async def test_no_groups(self, db, monkeypatch):
        set_config(BotflowSettings(summary_group="default"))
        try:
            class StubRouter:
                def __init__(self, group_id, db):
                    pass
                async def route(self, messages, **kw):
                    return {"content": "# W"}
            monkeypatch.setattr(ds, "GroupRouter", StubRouter)
            out = await ds._generate_wiki(db, "prompt", __import__("botflow.config", fromlist=["get_config"]).get_config())
            assert out == ""  # no group -> empty (skipped)
        finally:
            set_config(None)

    async def test_group_resolved(self, db, monkeypatch):
        set_config(BotflowSettings(summary_group="default"))
        try:
            await db.create_group(__import__("botflow.storage.models", fromlist=["ModelGroup"]).ModelGroup(name="default"))
            class StubRouter:
                captured = {}
                def __init__(self, group_id, db):
                    StubRouter.captured["gid"] = group_id
                async def route(self, messages, **kw):
                    return {"content": "intro\n```wiki\n# Title\nbody\n```\ntail"}
            monkeypatch.setattr(ds, "GroupRouter", StubRouter)
            cfg = __import__("botflow.config", fromlist=["get_config"]).get_config()
            out = await ds._generate_wiki(db, "prompt", cfg)
            assert StubRouter.captured["gid"] == 1
            assert "Title" in out  # raw content returned as-is
        finally:
            set_config(None)

    async def test_empty_content(self, db, monkeypatch):
        set_config(BotflowSettings(summary_group="default"))
        try:
            await db.create_group(__import__("botflow.storage.models", fromlist=["ModelGroup"]).ModelGroup(name="default"))
            class StubRouter:
                def __init__(self, group_id, db):
                    pass
                async def route(self, messages, **kw):
                    return {"content": ""}
            monkeypatch.setattr(ds, "GroupRouter", StubRouter)
            out = await ds._generate_wiki(db, "prompt", __import__("botflow.config", fromlist=["get_config"]).get_config())
            assert out == ""
        finally:
            set_config(None)

    async def test_plain_content(self, db, monkeypatch):
        set_config(BotflowSettings(summary_group="default"))
        try:
            await db.create_group(__import__("botflow.storage.models", fromlist=["ModelGroup"]).ModelGroup(name="default"))
            class StubRouter:
                def __init__(self, group_id, db):
                    pass
                async def route(self, messages, **kw):
                    return {"content": "plain wiki"}
            monkeypatch.setattr(ds, "GroupRouter", StubRouter)
            out = await ds._generate_wiki(db, "prompt", __import__("botflow.config", fromlist=["get_config"]).get_config())
            assert out == "plain wiki"
        finally:
            set_config(None)


class TestBuildSummaryPrompt:
    def test_no_logs(self):
        prompt = ds._build_summary_prompt([], {"total_calls": 0})
        assert "LLM Wiki" in prompt

    def test_with_logs(self):
        logs = [CallLog(id=1, model_name="gpt-4", group_name="default", status="success",
                        error_type=None, request_body=json.dumps({"messages": [{"role": "user", "content": "hi"}]}),
                        response_body=json.dumps({"choices": [{"message": {"content": "hello"}}]}))]
        prompt = ds._build_summary_prompt(logs, {"total_calls": 1})
        # only user prompts are sampled (not assistant responses)
        assert "hi" in prompt and "hello" not in prompt

    def test_with_error_log(self):
        logs = [CallLog(id=2, model_name="gpt-4", group_name="default", status="error",
                        error_type="timeout", request_body="{}", response_body="",
                        traceback="some trace")]
        prompt = ds._build_summary_prompt(logs, {"total_calls": 1, "error_types": {"timeout": 1}})
        # error_type appears in stats json; traceback is not included in prompt
        assert "timeout" in prompt and "some trace" not in prompt

    def test_long_prompt_truncated(self):
        long_msg = "y" * 500
        logs = [CallLog(id=3, model_name="gpt-4", group_name="default", status="success",
                        error_type=None, request_body=json.dumps({"messages": [{"role": "user", "content": long_msg}]}),
                        response_body="{}")]
        prompt = ds._build_summary_prompt(logs, {"total_calls": 1})
        # user prompt is truncated to 300 chars
        assert long_msg[:300] in prompt and long_msg not in prompt


class TestBuildStats:
    async def test_full(self, db):
        await _seed(db)
        logs = await db.query_call_logs(limit=100)
        stats = ds.build_stats(logs)
        assert stats["total_calls"] == 3
        assert stats["error_calls"] == 1
        assert stats["error_rate"] == pytest.approx(1 / 3, abs=1e-3)
        assert stats["by_model"]["1"] == 3
        assert stats["by_api_key"] == {}
        assert stats["tokens"]["total"] == 48
        assert stats["cost"] == pytest.approx(0.03)
        assert stats["error_types"]["timeout"] == 1

    async def test_empty(self, db):
        stats = ds.build_stats([])
        assert stats["total_calls"] == 0
        assert stats["error_rate"] == 0.0
        assert stats["by_model"] == {}
        assert stats["by_api_key"] == {}
        assert stats["error_types"] == {}


class TestRunDailySummary:
    async def test_no_logs_skips(self, db, monkeypatch):
        day = datetime.now(timezone.utc).date()
        day_iso = day.isoformat()
        # ensure no logs for today: use a far-future day for seeding? Instead delete all.
        await db.delete_old_call_logs(day_iso)
        gen = {"n": 0}
        async def fake(db, prompt, config):
            gen["n"] += 1
            return ""
        monkeypatch.setattr(ds, "_generate_wiki", fake)
        assert await ds.run_daily_summary(db, day) is None
        assert gen["n"] == 0

    async def test_with_logs_generates_and_stores(self, db, monkeypatch):
        day = datetime.now(timezone.utc).date()
        await _seed(db)
        async def fake(db, prompt, config):
            return "WIKI-OUTPUT"
        monkeypatch.setattr(ds, "_generate_wiki", fake)
        await ds.run_daily_summary(db, day)
        again = await db.get_daily_summary(day.isoformat())
        assert again is not None
        assert again.summary_md == "WIKI-OUTPUT"

    async def test_default_day(self, db, monkeypatch):
        # run_daily_summary(day) seeds logs created around now -> use today
        day = datetime.now(timezone.utc).date()
        pid, mid = await _provider_model(db)
        await db.create_call_log(CallLog(model_id=mid, status="success"))
        async def fake(db, prompt, config):
            return "X"
        monkeypatch.setattr(ds, "_generate_wiki", fake)
        await ds.run_daily_summary(db, day)
        again = await db.get_daily_summary(day.isoformat())
        assert again is not None and again.summary_md == "X"

    async def test_generate_failure_caught(self, db, monkeypatch):
        day = datetime.now(timezone.utc).date()
        await _seed(db)
        async def boom(db, prompt, config):
            raise RuntimeError("llm down")
        monkeypatch.setattr(ds, "_generate_wiki", boom)
        # failure must not propagate; stats still saved with empty summary
        await ds.run_daily_summary(db, day)
        got = await db.get_daily_summary(day.isoformat())
        assert got is not None
        assert got.summary_md == ""


class TestRawSessionStore:
    async def test_roundtrip(self, db):
        day = datetime.now(timezone.utc).date().isoformat()
        payload = [{"role": "user", "content": "hi"}]
        await db.save_raw_session(day, gzip.compress(json.dumps(payload).encode("utf-8")))
        got = await db.get_raw_session(day)
        assert got is not None
        assert gzip.decompress(got) == json.dumps(payload).encode("utf-8")

    async def test_missing(self, db):
        assert await db.get_raw_session("1999-01-01") is None

    async def test_delete_old(self, db):
        old = (datetime.now(timezone.utc) - timedelta(days=10)).date().isoformat()
        recent = datetime.now(timezone.utc).date().isoformat()
        await db.save_raw_session(old, b"x")
        await db.save_raw_session(recent, b"y")
        future = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
        # both old and recent are < future -> deleted
        assert await db.delete_old_raw_sessions(future) >= 1
        assert await db.get_raw_session(old) is None
        assert await db.get_raw_session(recent) is None


class TestPurge:
    async def test_purge_old_detail(self, db):
        await _seed(db)
        # insert an old log directly so it falls outside the retention window
        await db._conn.execute(
            "INSERT INTO call_logs (model_id, status, request_body, created_at) VALUES (?, 'success', '{}', '2000-01-01 00:00:00')",
            (1,),
        )
        await db._conn.commit()
        set_config(BotflowSettings(call_log_detail_days=0))
        try:
            deleted = await ds.purge_old_detail(db)
            assert deleted >= 1
        finally:
            set_config(None)

    async def test_purge_old_raw_sessions(self, db):
        old = (datetime.now(timezone.utc) - timedelta(days=10)).date().isoformat()
        await db.save_raw_session(old, b"x")
        set_config(BotflowSettings(raw_session_retention_days=0))
        try:
            deleted = await ds.purge_old_raw_sessions(db)
            assert deleted >= 1
        finally:
            set_config(None)
        assert await db.get_raw_session(old) is None
