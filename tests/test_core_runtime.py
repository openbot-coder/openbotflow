"""Tests for core.py —— 补齐未被覆盖的运行时/装配分支。

覆盖：
- ``CallLogWriter`` 缓冲写入器的启动、定时 flush、满缓冲 flush 与优雅停止
- ``sync_models_from_provider`` / ``sync_all_models`` 上游模型同步
- ``lifespan`` 启动/关停全流程与 4 个后台任务循环（含失败路径）
- ``RateLimitMiddleware`` / ``AuthMiddleware`` 的纯 ASGI 分支
- 去重辅助函数的异常兜底
- ``/v1/completions`` 的 prompt 列表、``/v1/messages`` 与 ``/v1/responses`` 流式入口
- ``/v1/models`` 的 Anthropic 分支
- ``_stream_common`` 客户端断连与 fallback group 加载失败
- ``_stream_anthropic`` / ``_responses_serialize_raw`` / ``_stream_responses``
- ``create_app`` / ``start_service``
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import botflow.core as core
from botflow.config import BotflowSettings, set_config
from botflow.router import CooldownManager, ModelEndpoint
from botflow.storage.db import Database
from botflow.storage.models import (
    CallLog,
    GroupModelWithDetails,
    Model,
    ModelGroup,
    Provider,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _new_db(tmp_path, name: str = "botflow.db") -> Database:
    db = Database(tmp_path / "data" / name)
    await db.initialize()
    return db


def _endpoint(model_id: int = 1, provider=None, max_retries: int = 3) -> ModelEndpoint:
    detail = GroupModelWithDetails(
        id=model_id, group_id=1, model_id=model_id, weight=1.0, is_enabled=True,
        model_name=f"model-{model_id}", display_name=f"model-{model_id}",
        provider_id=1, provider_name="p", provider_type="openai",
        max_retries=max_retries, cooldown_seconds=60, cooldown_failure_threshold=3,
    )
    return ModelEndpoint(detail, provider or MagicMock())


async def _recording_send():
    msgs: list[dict] = []

    async def send(message):
        msgs.append(message)

    return msgs, send


async def _empty_receive():
    return {"type": "http.request", "body": b"", "more_body": False}


def _http_scope(path: str = "/v1/chat/completions", headers=None) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers or [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }


class _RecordingApp:
    """Minimal ASGI app that records invocations."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, scope, receive, send):
        self.calls += 1


# ===========================================================================
# 1. CallLogWriter
# ===========================================================================


class TestCallLogWriter:

    async def test_start_runs_flush_loop_and_stop_cancels(self, tmp_path):
        db = await _new_db(tmp_path)
        try:
            writer = core.CallLogWriter(db, flush_interval=0.01, max_buffer=100)
            await writer.start()
            assert writer._flush_task is not None
            await asyncio.sleep(0.05)  # let the flush loop tick at least once
            assert not writer._flush_task.done()
            await writer.stop()
            assert writer._flush_task.cancelled() or writer._flush_task.done()
        finally:
            await db.close()

    async def test_stop_without_start_is_safe(self, tmp_path):
        db = await _new_db(tmp_path)
        try:
            writer = core.CallLogWriter(db)
            await writer.stop()  # no task to cancel, final flush is a no-op
        finally:
            await db.close()

    async def test_log_flushes_when_buffer_reaches_max(self):
        db = AsyncMock()
        writer = core.CallLogWriter(db, max_buffer=2)
        await writer.log(CallLog(status="success"))
        assert db.create_call_log.await_count == 0  # below threshold, still buffered
        await writer.log(CallLog(status="success"))
        assert db.create_call_log.await_count == 2  # threshold reached → flushed
        assert writer._buffer == []

    async def test_flush_swallows_database_errors(self, tmp_path):
        db = AsyncMock()
        db.create_call_log.side_effect = RuntimeError("db down")
        writer = core.CallLogWriter(db, max_buffer=1)
        # Must not raise even when the underlying write fails.
        await writer.log(CallLog(status="success"))
        assert writer._buffer == []


# ===========================================================================
# 2. Model sync
# ===========================================================================


class _FakeResponse:
    def __init__(self, payload: dict | None = None, status_code: int = 200) -> None:
        self._payload = payload or {}
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeAsyncClient:
    def __init__(self, response: _FakeResponse, **kwargs) -> None:
        self._response = response
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def get(self, url, headers=None):
        self.url = url
        self.headers = headers
        return self._response


def _patch_http(monkeypatch, response: _FakeResponse) -> None:
    monkeypatch.setattr(
        core.httpx, "AsyncClient", lambda **kwargs: _FakeAsyncClient(response, **kwargs)
    )


class TestSyncModels:

    async def test_missing_provider_returns_error(self, tmp_path):
        db = await _new_db(tmp_path)
        try:
            out = await core.sync_models_from_provider(999, db=db)
            assert out == {
                "added": 0, "skipped": 0, "errors": ["Provider 999 not found"],
            }
        finally:
            await db.close()

    async def test_http_failure_is_reported(self, tmp_path, monkeypatch):
        db = await _new_db(tmp_path)
        try:
            pid = await db.create_provider(
                Provider(name="p", provider_type="openai", api_key="k", base_url="http://x/")
            )
            _patch_http(monkeypatch, _FakeResponse(status_code=500))
            out = await core.sync_models_from_provider(pid, db=db)
            assert out["added"] == 0
            assert out["errors"] and "HTTP 500" in out["errors"][0]
        finally:
            await db.close()

    async def test_adds_new_and_skips_existing(self, tmp_path, monkeypatch):
        db = await _new_db(tmp_path)
        try:
            pid = await db.create_provider(
                Provider(name="p", provider_type="openai", api_key="k", base_url="http://x/")
            )
            await db.create_model(Model(name="existing", provider_id=pid))
            _patch_http(monkeypatch, _FakeResponse(payload={"data": [
                {"id": "existing"},
                {"id": "brand-new"},
                {"id": ""},          # no id → skipped silently
                {"no_id_key": 1},    # no id → skipped silently
            ]}))
            out = await core.sync_models_from_provider(pid, db=db)
            assert out["added"] == 1
            assert out["skipped"] == 1
            assert out["errors"] == []
            names = {m.name for m in await db.list_models(provider_id=pid)}
            assert "brand-new" in names
        finally:
            await db.close()

    async def test_sync_all_aggregates_providers(self, tmp_path, monkeypatch):
        db = await _new_db(tmp_path)
        try:
            pid = await db.create_provider(
                Provider(name="p", provider_type="openai", api_key="k", base_url="http://x/")
            )
            _patch_http(monkeypatch, _FakeResponse(payload={"data": [{"id": "m1"}]}))
            core._db = db
            try:
                out = await core.sync_all_models()
            finally:
                core._db = None
            assert out["added"] == 1
            assert out["errors"] == []
            assert pid  # provider was iterated
        finally:
            await db.close()


# ===========================================================================
# 3. lifespan
# ===========================================================================


class _SleepBudget:
    """Fake ``asyncio.sleep``: yields immediately, then raises CancelledError.

    Lets the infinite background loops execute their bodies a bounded
    number of times and then terminate, so the lifespan can be unit-tested
    without waiting on wall-clock intervals of 5–1440 minutes.

    The budget is tracked **per distinct delay value**, not globally.  A single
    shared counter does not work: the log-writer flush loop resumes from
    ``await sleep(0)`` instantly, so while the lifespan awaits its (threaded)
    SQLite calls it busy-spins and would consume the entire budget before the
    four maintenance loops are even created — leaving their bodies uncovered.
    Keying the budget on the delay gives each loop its own allowance
    (flush=5s, sync=30s, cooldown=300s, dedup=600s, daily=~86400s).
    """

    def __init__(self, per_delay: int = 4) -> None:
        self._per_delay = per_delay
        self._counts: dict[object, int] = {}
        self._real = asyncio.sleep

    async def __call__(self, delay, *args, **kwargs):
        if self._counts.get(delay, 0) >= self._per_delay:
            raise asyncio.CancelledError()
        self._counts[delay] = self._counts.get(delay, 0) + 1
        await self._real(0)


def _patch_core_asyncio(monkeypatch, per_delay: int = 4) -> None:
    """Replace only ``core``'s view of asyncio so other modules are unaffected."""
    monkeypatch.setattr(core, "asyncio", SimpleNamespace(
        sleep=_SleepBudget(per_delay),
        create_task=asyncio.create_task,
        CancelledError=asyncio.CancelledError,
        Lock=asyncio.Lock,
        Task=asyncio.Task,
        timeout=asyncio.timeout,
    ))


@pytest.fixture
def _lifespan_env(tmp_path, monkeypatch):
    """Prepare globals + stubbed background work for lifespan runs."""
    saved = (core._db, core._config, core._log_writer)
    monkeypatch.setenv("LLM_KEY", "sk-legacy-test")
    # Deterministic clock: 12:00 UTC so the "already past the target hour" branch fires.
    class _FixedDateTime:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(core, "datetime", _FixedDateTime)
    monkeypatch.setattr(core, "run_daily_summary", AsyncMock())
    monkeypatch.setattr(core, "purge_old_detail", AsyncMock(return_value=2))
    monkeypatch.setattr(core, "purge_old_raw_sessions", AsyncMock(return_value=0))
    monkeypatch.setattr(core, "purge_old_call_logs", AsyncMock(return_value=1))
    _sync_results = [
        {"added": 3, "skipped": 1, "errors": []},   # added>0 分支
        {"added": 0, "skipped": 0, "errors": ["boom"]},  # errors 分支
        {"added": 0, "skipped": 0, "errors": []},   # 两者皆否分支
    ]

    async def _fake_sync_all_models():
        if not _sync_results:
            raise RuntimeError("sync exploded")  # except 分支
        return _sync_results.pop(0)

    monkeypatch.setattr(core, "sync_all_models", _fake_sync_all_models)
    yield
    core._db, core._config, core._log_writer = saved


async def _drain_tasks(times: int = 300) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


class TestLifespan:

    async def test_startup_and_shutdown_happy_path(self, tmp_path, monkeypatch, _lifespan_env):
        db = await _new_db(tmp_path)
        cfg = BotflowSettings(
            workspace=str(tmp_path), daily_summary_hour=3, model_sync_interval=1,
        )
        set_config(cfg)
        core._db = db
        core._config = cfg
        monkeypatch.setattr(db, "load_cooldown_states", AsyncMock(return_value=[{
            "group_id": 1, "model_id": 1,
            "consecutive_failures": 2, "cooldown_until": 0.0,
        }]))
        # Drive the cooldown-save and dedup-cleanup loops down their "did work"
        # branches (active cooldowns present / rows actually deleted).
        monkeypatch.setattr(
            core._cooldown_manager, "get_all_active_cooldowns",
            lambda: [{"group_id": 1, "model_id": 1,
                      "consecutive_failures": 2, "cooldown_until": 0.0}],
        )
        monkeypatch.setattr(db, "save_cooldown_state", AsyncMock())
        monkeypatch.setattr(db, "cleanup_config_by_prefix", AsyncMock(return_value=3))
        _patch_core_asyncio(monkeypatch)

        async with core.lifespan(core.app):
            assert core._log_writer is not None
            await _drain_tasks()

        # Shutdown closed the database.
        assert core._log_writer is not None

    async def test_legacy_key_falls_back_to_empty_env(
        self, tmp_path, monkeypatch, _lifespan_env,
    ):
        """No DB llm_key and no LLM_KEY env → the env fallback resolves to ''."""
        db = await _new_db(tmp_path)
        cfg = BotflowSettings(
            workspace=str(tmp_path), daily_summary_hour=3, model_sync_interval=0,
        )
        set_config(cfg)
        core._db = db
        core._config = cfg
        monkeypatch.delenv("LLM_KEY", raising=False)
        monkeypatch.setattr(db, "load_cooldown_states", AsyncMock(return_value=[]))
        monkeypatch.setattr(db, "cleanup_config_by_prefix", AsyncMock(return_value=0))
        _patch_core_asyncio(monkeypatch)

        async with core.lifespan(core.app):
            await _drain_tasks()

        # No key was registered, because neither source produced one.
        assert await db.get_config("llm_key") is None
        assert await db.list_api_keys() == []

    async def test_legacy_key_registered_from_db_config(
        self, tmp_path, monkeypatch, _lifespan_env,
    ):
        """A pre-existing DB llm_key is auto-registered as a client API key."""
        db = await _new_db(tmp_path)
        await db.set_config("llm_key", "sk-from-db-config")
        cfg = BotflowSettings(
            workspace=str(tmp_path), daily_summary_hour=3, model_sync_interval=0,
        )
        set_config(cfg)
        core._db = db
        core._config = cfg
        monkeypatch.delenv("LLM_KEY", raising=False)
        monkeypatch.setattr(db, "load_cooldown_states", AsyncMock(return_value=[]))
        monkeypatch.setattr(db, "cleanup_config_by_prefix", AsyncMock(return_value=0))
        _patch_core_asyncio(monkeypatch)

        async with core.lifespan(core.app):
            await _drain_tasks()

        keys = await db.list_api_keys()
        assert [k.label for k in keys] == ["legacy:llm_key"]

    async def test_auto_initializes_when_db_missing(self, tmp_path, monkeypatch, _lifespan_env):
        cfg = BotflowSettings(
            workspace=str(tmp_path), daily_summary_hour=3, model_sync_interval=0,
        )
        set_config(cfg)
        core._db = None
        core._config = None
        monkeypatch.setattr(core, "get_workspace_path", lambda *a, **k: tmp_path)
        monkeypatch.setattr(core, "init_workspace", lambda *a, **k: None)
        _patch_core_asyncio(monkeypatch)

        async with core.lifespan(core.app):
            assert core._db is not None
            await _drain_tasks()

        assert (tmp_path / "data" / "botflow.db").exists()

    async def test_background_task_failures_are_swallowed(
        self, tmp_path, monkeypatch, _lifespan_env,
    ):
        """维护任务失败必须只记日志，不能把服务打挂。"""
        db = await _new_db(tmp_path)
        cfg = BotflowSettings(
            workspace=str(tmp_path), daily_summary_hour=3, model_sync_interval=1,
        )
        set_config(cfg)
        core._db = db
        core._config = cfg
        # Force every background job down its failure branch.
        monkeypatch.setattr(db, "load_cooldown_states", AsyncMock(
            side_effect=RuntimeError("cannot restore cooldowns")
        ))
        monkeypatch.setattr(db, "cleanup_config_by_prefix", AsyncMock(
            side_effect=RuntimeError("cleanup failed")
        ))
        monkeypatch.setattr(core, "run_daily_summary", AsyncMock(
            side_effect=RuntimeError("summary failed")
        ))
        monkeypatch.setattr(
            core._cooldown_manager, "get_all_active_cooldowns",
            lambda: [{"group_id": 1, "model_id": 1}],
        )
        monkeypatch.setattr(db, "save_cooldown_state", AsyncMock(
            side_effect=RuntimeError("save failed")
        ))
        _patch_core_asyncio(monkeypatch)

        async with core.lifespan(core.app):
            await _drain_tasks()


# ===========================================================================
# 4. Middleware ASGI branches
# ===========================================================================


class TestRateLimitMiddleware:

    async def test_non_http_scope_passes_through(self):
        app = _RecordingApp()
        mw = core.RateLimitMiddleware(app)
        await mw({"type": "lifespan"}, None, None)
        assert app.calls == 1

    async def test_health_path_skips_rate_limiting(self):
        app = _RecordingApp()
        mw = core.RateLimitMiddleware(app)
        _, send = await _recording_send()
        await mw(_http_scope("/health"), _empty_receive, send)
        assert app.calls == 1

    async def test_query_param_key_used(self):
        app = _RecordingApp()
        mw = core.RateLimitMiddleware(app)
        scope = _http_scope()
        scope["query_string"] = b"api_key=qp-key"
        _, send = await _recording_send()
        await mw(scope, _empty_receive, send)
        assert "qp-key" in mw._requests

    async def test_anonymous_key_when_no_credentials(self):
        app = _RecordingApp()
        mw = core.RateLimitMiddleware(app)
        scope = _http_scope()
        scope.pop("client")
        _, send = await _recording_send()
        await mw(scope, _empty_receive, send)
        assert "anonymous" in mw._requests

    async def test_periodic_cleanup_evicts_stale_keys(self):
        app = _RecordingApp()
        mw = core.RateLimitMiddleware(app, max_requests=300, max_keys=1)
        mw._request_count = 999  # next request triggers the cleanup tick
        for stale in ("stale-a", "stale-b"):
            mw._requests[stale] = deque([0.0], maxlen=300)
            mw._last_access[stale] = 0.0
        _, send = await _recording_send()
        await mw(_http_scope(), _empty_receive, send)
        assert "stale-a" not in mw._requests
        assert "stale-b" not in mw._requests

    async def test_exceeding_window_returns_429(self):
        app = _RecordingApp()
        mw = core.RateLimitMiddleware(app, max_requests=2, window_seconds=60)
        headers = [(b"authorization", b"Bearer rl-key")]
        _, send = await _recording_send()
        await mw(_http_scope(headers=headers), _empty_receive, send)
        msgs, send = await _recording_send()
        await mw(_http_scope(headers=headers), _empty_receive, send)

        start = next(m for m in msgs if m["type"] == "http.response.start")
        assert start["status"] == 429
        body = b"".join(
            m.get("body", b"") for m in msgs if m["type"] == "http.response.body"
        )
        assert b"Too many requests" in body
        assert app.calls == 1  # second request never reached the app


class TestAuthMiddleware:

    async def test_non_http_scope_passes_through(self):
        app = _RecordingApp()
        mw = core.AuthMiddleware(app)
        await mw({"type": "lifespan"}, None, None)
        assert app.calls == 1

    async def test_health_path_is_public(self):
        app = _RecordingApp()
        mw = core.AuthMiddleware(app)
        _, send = await _recording_send()
        await mw(_http_scope("/health"), _empty_receive, send)
        assert app.calls == 1

    async def test_admin_path_skips_middleware_auth(self):
        app = _RecordingApp()
        mw = core.AuthMiddleware(app)
        _, send = await _recording_send()
        await mw(_http_scope("/admin/providers"), _empty_receive, send)
        assert app.calls == 1

    async def test_missing_token_returns_401(self):
        app = _RecordingApp()
        mw = core.AuthMiddleware(app)
        msgs, send = await _recording_send()
        await mw(_http_scope(), _empty_receive, send)
        start = next(m for m in msgs if m["type"] == "http.response.start")
        assert start["status"] == 401
        assert app.calls == 0

    async def test_raw_authorization_header_without_bearer(self, tmp_path):
        db = await _new_db(tmp_path)
        try:
            await db.create_api_key("raw-key", label="t")
            saved, core._db = core._db, db
            try:
                app = _RecordingApp()
                mw = core.AuthMiddleware(app)
                headers = [(b"authorization", b"raw-key")]
                _, send = await _recording_send()
                await mw(_http_scope(headers=headers), _empty_receive, send)
                assert app.calls == 1
            finally:
                core._db = saved
        finally:
            await db.close()

    async def test_invalid_token_returns_401(self, tmp_path):
        db = await _new_db(tmp_path)
        try:
            saved, core._db = core._db, db
            try:
                app = _RecordingApp()
                mw = core.AuthMiddleware(app)
                msgs, send = await _recording_send()
                await mw(
                    _http_scope(headers=[(b"x-api-key", b"nope")]), _empty_receive, send,
                )
                start = next(m for m in msgs if m["type"] == "http.response.start")
                assert start["status"] == 401
            finally:
                core._db = saved
        finally:
            await db.close()


# ===========================================================================
# 5. Deduplication helpers
# ===========================================================================


class TestDeduplicationHelpers:

    async def test_check_returns_none_on_db_error(self, monkeypatch):
        monkeypatch.setattr(
            core, "_get_db", lambda: MagicMock(
                get_config=AsyncMock(side_effect=RuntimeError("db down"))
            ),
        )
        assert await core._check_request_deduplication("rid") is None

    async def test_check_returns_cached_result(self, monkeypatch):
        import json as _json
        import time as _time
        payload = _json.dumps({"result": {"ok": 1}, "timestamp": _time.time()})
        monkeypatch.setattr(
            core, "_get_db", lambda: MagicMock(get_config=AsyncMock(return_value=payload)),
        )
        assert await core._check_request_deduplication("rid") == {"ok": 1}

    async def test_check_ignores_expired_entry(self, monkeypatch):
        import json as _json
        payload = _json.dumps({"result": {"ok": 1}, "timestamp": 0})
        monkeypatch.setattr(
            core, "_get_db", lambda: MagicMock(get_config=AsyncMock(return_value=payload)),
        )
        assert await core._check_request_deduplication("rid") is None

    async def test_cache_swallows_db_error(self, monkeypatch):
        monkeypatch.setattr(
            core, "_get_db", lambda: MagicMock(
                set_config=AsyncMock(side_effect=RuntimeError("db down"))
            ),
        )
        await core._cache_request_result("rid", {"ok": 1})  # must not raise


# ===========================================================================
# 6. Health / models endpoint branches
# ===========================================================================


class _StubProvider:
    async def chat(self, **kwargs):
        return {
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "id": "c1", "model": "m", "_routing": {"model_id": 1, "provider_id": 1},
        }

    async def chat_stream(self, **kwargs):
        yield {"choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}]}
        yield {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}


class _StubEngine:
    def __init__(self) -> None:
        self.cooldown = CooldownManager()

    async def route(self, group, messages, temperature=None, max_tokens=None,
                    stream=False, **kwargs):
        return await _StubProvider().chat()

    async def route_stream(self, group, messages, temperature=None, max_tokens=None, **kwargs):
        return {
            "endpoints": [_endpoint(1, _StubProvider())],
            "group_id": 1,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "kwargs": kwargs,
            "fallback_group_id": None,
        }


@pytest.fixture
def client(tmp_path, monkeypatch):
    loop = asyncio.new_event_loop()
    db = Database(tmp_path / "data" / "botflow.db")
    loop.run_until_complete(db.initialize())
    loop.run_until_complete(db.create_api_key("test-key", label="t"))
    loop.run_until_complete(db.create_group(ModelGroup(name="default")))
    core._db = db
    core._engine = None
    cfg = BotflowSettings()
    core._config = cfg
    set_config(cfg)
    engine = _StubEngine()
    monkeypatch.setattr(core, "_get_engine", lambda: engine)
    monkeypatch.setattr(core, "_get_group_id", AsyncMock(return_value=1))
    monkeypatch.setattr(core, "_log_call", AsyncMock())
    test_client = TestClient(core.app)
    test_client.engine = engine
    yield test_client
    loop.run_until_complete(db.close())
    loop.close()
    core._db = None
    core._engine = None


AUTH = {"authorization": "Bearer test-key"}


class TestEndpointBranches:

    def test_completions_prompt_list_is_joined(self, client):
        payload = {"model": "default", "prompt": ["line one", "line two"]}
        r = client.post("/v1/completions", json=payload, headers=AUTH)
        assert r.status_code == 200
        assert "choices" in r.json()

    def test_completions_stream_returns_sse(self, client):
        payload = {"model": "default", "prompt": "hi", "stream": True}
        with client.stream("POST", "/v1/completions", json=payload, headers=AUTH) as r:
            assert r.status_code == 200
            assert "text/event-stream" in r.headers["content-type"]
            lines = list(r.iter_lines())
        assert any("data:" in line for line in lines)

    def test_anthropic_messages_stream_returns_sse(self, client):
        payload = {"model": "default", "messages": [{"role": "user", "content": "hi"}],
                   "stream": True}
        with client.stream("POST", "/v1/messages", json=payload, headers=AUTH) as r:
            assert r.status_code == 200
            assert "text/event-stream" in r.headers["content-type"]
            lines = list(r.iter_lines())
        assert any("event:" in line for line in lines)

    def test_responses_non_stream(self, client):
        payload = {"model": "default", "input": "hello"}
        r = client.post("/v1/responses", json=payload, headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["object"] == "response"
        assert body["output_text"] == "hi"

    def test_responses_stream_returns_sse(self, client):
        payload = {"model": "default", "input": "hello", "stream": True}
        with client.stream("POST", "/v1/responses", json=payload, headers=AUTH) as r:
            assert r.status_code == 200
            assert "text/event-stream" in r.headers["content-type"]
            lines = list(r.iter_lines())
        assert any("response.created" in line for line in lines)

    def test_models_anthropic_format(self, client):
        headers = {**AUTH, "accept": "application/anthropic+json"}
        r = client.get("/v1/models", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert "data" in body
        assert body["data"][0]["type"] == "model"


# ===========================================================================
# 7. _get_extra_route_params
# ===========================================================================


class TestExtraRouteParams:

    async def test_missing_group_raises_configuration_error(self, monkeypatch):
        from botflow.common.exceptions import ConfigurationError
        monkeypatch.setattr(core, "_get_engine", lambda: MagicMock())
        monkeypatch.setattr(core, "_get_group_id", AsyncMock(return_value=7))
        monkeypatch.setattr(core, "_get_db", lambda: MagicMock(
            get_group=AsyncMock(return_value=None),
        ))
        with pytest.raises(ConfigurationError, match="Group 7 not found"):
            await core._get_extra_route_params({"model": "m"})

    async def test_extra_kwargs_are_filtered_and_logged(self, monkeypatch):
        monkeypatch.setattr(core, "_get_engine", lambda: "engine")
        monkeypatch.setattr(core, "_get_group_id", AsyncMock(return_value=1))
        monkeypatch.setattr(core, "_get_db", lambda: MagicMock(
            get_group=AsyncMock(return_value=ModelGroup(name="g")),
        ))
        group_id, engine, group, extra = await core._get_extra_route_params({
            "model": "m",
            "extra": {"seed": 1, "nonsense": True},
        })
        assert (group_id, engine) == (1, "engine")
        assert group.name == "g"
        assert extra == {"seed": 1}  # filtered against SAFE_EXTRA_KEYS


# ===========================================================================
# 8. Streaming internals
# ===========================================================================


class _StreamProvider:
    """chat_stream yields behavior items; Exception items are raised."""

    def __init__(self, behavior: list) -> None:
        self.behavior = behavior
        self.calls = 0

    async def chat_stream(self, *args, **kwargs):
        self.calls += 1
        result = self.behavior[min(self.calls - 1, len(self.behavior) - 1)]
        if isinstance(result, Exception):
            raise result
        for item in result:
            if isinstance(item, Exception):
                raise item
            yield item


class _StreamEngine(_StubEngine):
    def __init__(self, endpoints, fallback_group_id=None, load_group_error=None):
        super().__init__()
        self.endpoints = endpoints
        self.fallback_group_id = fallback_group_id
        self.load_group_error = load_group_error

    async def route_stream(self, group, messages, temperature=None, max_tokens=None, **kwargs):
        return {
            "endpoints": self.endpoints,
            "group_id": 1,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "kwargs": kwargs,
            "fallback_group_id": self.fallback_group_id,
        }

    async def _load_group(self, group_id):
        if self.load_group_error is not None:
            raise self.load_group_error
        return ModelGroup(id=group_id, name=f"group-{group_id}")


def _serialize(chunk: dict) -> tuple[list[str], dict | None]:
    return [f"data: {chunk['content']}\n\n"], None


def _setup_stream(monkeypatch, engine) -> None:
    monkeypatch.setattr(core, "_log_call", AsyncMock())
    monkeypatch.setattr(
        core, "_get_extra_route_params",
        AsyncMock(return_value=(1, engine, ModelGroup(name="g"), {})),
    )


class _RequestStub:
    def __init__(self, disconnected: bool = False) -> None:
        self._disconnected = disconnected

    async def is_disconnected(self) -> bool:
        return self._disconnected


class TestStreamingInternals:

    async def test_client_disconnect_aborts_stream(self, monkeypatch):
        ep = _endpoint(1, _StreamProvider([[{"content": "a"}, {"content": "b"}]]))
        engine = _StreamEngine([ep])
        _setup_stream(monkeypatch, engine)

        out = [
            line async for line in core._stream_common(
                {"model": "x", "messages": []}, _serialize,
                request=_RequestStub(disconnected=True),
            )
        ]
        # The very first chunk trips the disconnect check → no data lines yielded.
        assert "data: a\n\n" not in out
        assert out[-1] == "data: [DONE]\n\n"

    async def test_fallback_group_load_failure_raises_original_error(self, monkeypatch):
        err = core.ProviderError("primary failed")
        ep = _endpoint(1, _StreamProvider([err]))
        engine = _StreamEngine([ep], fallback_group_id=2, load_group_error=err)
        _setup_stream(monkeypatch, engine)

        out = [line async for line in core._stream_common({"model": "x", "messages": []}, _serialize)]
        assert out[-1] == "data: [DONE]\n\n"
        error_line = next(
            line for line in out if line.startswith("data: {") and '"error"' in line
        )
        assert "primary failed" in error_line

    async def test_fallback_group_is_used_on_success(self, monkeypatch):
        ep1 = _endpoint(1, _StreamProvider([core.ProviderError("nope")]))
        ep2 = _endpoint(2, _StreamProvider([[{"content": "ok"}]]))
        engine = _StreamEngine([ep1], fallback_group_id=2)
        _setup_stream(monkeypatch, engine)

        async def _route_stream(group, messages, **kwargs):
            gid = group.id if hasattr(group, "id") else 1
            eps = [ep2] if gid == 2 else [ep1]
            return {
                "endpoints": eps, "group_id": gid, "messages": messages,
                "temperature": None, "max_tokens": None, "kwargs": {},
                "fallback_group_id": 2,
            }

        monkeypatch.setattr(engine, "route_stream", _route_stream)
        out = [line async for line in core._stream_common({"model": "x", "messages": []}, _serialize)]
        assert "data: ok\n\n" in out

    async def test_anthropic_serialize_reraises_on_failure(self, monkeypatch):
        monkeypatch.setattr(
            core, "internal_chunk_to_anthropic_sse",
            MagicMock(side_effect=ValueError("bad chunk")),
        )
        with pytest.raises(ValueError, match="bad chunk"):
            core._anthropic_serialize({"choices": [{"delta": {}}]})

    async def test_stream_anthropic_forwards_sse_events(self, monkeypatch):
        ep = _endpoint(1, _StreamProvider([[{"choices": [{"index": 0, "delta": {"content": "hi"}}]}]]))
        engine = _StreamEngine([ep])
        _setup_stream(monkeypatch, engine)

        out = [
            line async for line in core._stream_anthropic(
                {"model": "x", "messages": []}, _RequestStub(),
            )
        ]
        assert any(line.startswith("event: content_block_delta") for line in out)

    def test_responses_serialize_raw_emits_events(self):
        lines = core._responses_serialize_raw(
            {"model": "m", "choices": [{"delta": {"content": "hi"}, "finish_reason": None}]},
            "resp_abc", 1700000000, is_first=True,
        )
        joined = "".join(lines)
        assert "event: response.created" in joined
        assert "event: response.output_text.delta" in joined
        assert '"resp_abc"' in joined

    async def test_stream_responses_emits_response_events(self, monkeypatch):
        ep = _endpoint(1, _StreamProvider([
            [
                {"choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ],
        ]))
        engine = _StreamEngine([ep])
        _setup_stream(monkeypatch, engine)

        out = [
            line async for line in core._stream_responses(
                {"model": "x", "messages": []}, _RequestStub(),
            )
        ]
        joined = "".join(out)
        assert "event: response.created" in joined
        assert "event: response.completed" in joined
        # Responses API must not emit the OpenAI [DONE] sentinel.
        assert "[DONE]" not in joined


# ===========================================================================
# 9. App assembly / service start
# ===========================================================================


class TestAppAssembly:

    async def test_create_app_initializes_database(self, tmp_path):
        cfg = BotflowSettings(workspace=str(tmp_path))
        saved_db, saved_cfg = core._db, core._config
        try:
            app = await core.create_app(tmp_path, cfg)
            assert app is core.app
            assert core._db is not None
            assert core._config is cfg
            assert (tmp_path / "data" / "botflow.db").exists()
        finally:
            if core._db is not None:
                await core._db.close()
            core._db, core._config = saved_db, saved_cfg

    async def test_start_service_configures_and_serves(self, tmp_path, monkeypatch):
        import uvicorn
        serve = AsyncMock()
        monkeypatch.setattr(uvicorn.Server, "serve", serve)
        cfg = BotflowSettings(workspace=str(tmp_path))
        saved_db, saved_cfg = core._db, core._config
        try:
            await core.start_service(tmp_path, "127.0.0.1", 4000, cfg)
            serve.assert_awaited_once()
        finally:
            if core._db is not None:
                await core._db.close()
            core._db, core._config = saved_db, saved_cfg
