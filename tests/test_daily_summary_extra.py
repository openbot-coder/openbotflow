"""Coverage for daily_summary helper branches not exercised elsewhere."""

from __future__ import annotations

import types
from datetime import datetime
from unittest.mock import AsyncMock

from botflow.config import BotflowSettings, get_config, set_config
from botflow.storage import daily_summary as ds


def _log(**kw):
    d = dict(
        status="success",
        model_id=1,
        api_key_id=None,
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        cost=0.0,
        error_type=None,
        request_body=None,
        response_body=None,
        traceback=None,
        created_at="2026-01-01 00:00:00",
    )
    d.update(kw)
    return types.SimpleNamespace(**d)


def _group(name, gid):
    return types.SimpleNamespace(id=gid, name=name, is_enabled=True)


def test_yesterday_day_default():
    assert ds._yesterday_day()  # returns a string like YYYY-MM-DD


def test_yesterday_day_explicit():
    assert ds._yesterday_day(datetime(2026, 1, 2)) == "2026-01-01"


def test_build_stats_empty():
    s = ds.build_stats([])
    assert s["total_calls"] == 0
    assert s["error_rate"] == 0.0
    assert s["by_model"] == {}
    assert s["by_api_key"] == {}


def test_build_stats_with_key_and_error():
    logs = [
        _log(api_key_id=5, status="error", error_type="timeout", model_id=2,
             request_body='{"messages":[{"role":"user","content":"hi"}]}',
             prompt_tokens=10, completion_tokens=20, total_tokens=30, cost=0.5),
        _log(api_key_id=5, model_id=2, request_body='{"messages":[{"role":"user","content":"bye"}]}'),
    ]
    s = ds.build_stats(logs)
    assert s["by_api_key"] == {"5": 2}
    assert s["by_model"] == {"2": 2}
    assert s["error_calls"] == 1
    assert s["tokens"]["total"] == 30 + 2
    assert s["cost"] == 0.5 + 0.0
    assert s["error_types"] == {"timeout": 1}


def test_build_summary_prompt_bad_json_falls_back():
    # request_body is not valid JSON -> except branch (line 78-79) -> req={}
    logs = [_log(request_body="{not valid json")]
    prompt = ds._build_summary_prompt(logs, ds.build_stats(logs))
    assert "(no user messages captured)" in prompt


def test_build_summary_prompt_with_samples():
    logs = [_log(request_body='{"messages":[{"role":"user","content":"What is 1+1?"}]}')]
    prompt = ds._build_summary_prompt(logs, ds.build_stats(logs))
    assert "What is 1+1?" in prompt


async def test_generate_wiki_no_groups_returns_empty():
    class DB:
        async def list_groups(self, enabled_only=True):
            return []
    set_config(BotflowSettings(summary_group="missing"))
    try:
        assert await ds._generate_wiki(DB(), None, get_config()) == ""
    finally:
        set_config(None)


async def test_generate_wiki_falls_back_to_first_group(monkeypatch):
    # summary_group does not match any group name -> use groups[0] (line 146).

    class DB:
        async def list_groups(self, enabled_only=True):
            return [_group("actual", 7)]

    captured = {}

    class FakeRouter:
        def __init__(self, group_id, db):
            captured["group_id"] = group_id
            captured["db"] = db

        async def route(self, **kwargs):
            return {"content": "wiki entry"}

    monkeypatch.setattr(ds, "GroupRouter", FakeRouter)
    set_config(BotflowSettings(summary_group="does-not-exist"))
    try:
        out = await ds._generate_wiki(DB(), None, get_config())
    finally:
        set_config(None)
    assert captured["group_id"] == 7
    assert out == "wiki entry"
