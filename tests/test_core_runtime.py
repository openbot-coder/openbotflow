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
from fastapi import HTTPException
from fastapi.testclient import TestClient

import botflow.core as core
from botflow.config import BotflowSettings, set_config
from botflow.common.exceptions import (
    AllModelsCooldownError,
    ConfigurationError,
    NoAvailableModelError,
    ProviderError,
)
from botflow.pipeline.base import StrategyError
from botflow.router import CooldownManager, ModelEndpoint
from botflow.storage.db import Database
from botflow.storage.models import (
    CallAttempt,
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
        # 保留以兼容非流式端点路径；SG-1 后非流式改走 run(mode="chat")
        return await _StubProvider().chat()

    async def run(self, strategy, group, mode, **kwargs):
        """SG-1 §3.3：单入口。非流式返回最终 result dict；流式返回事件 async gen。"""
        if mode == "stream":
            return self._stream_events(strategy, group, **kwargs)
        return await _StubProvider().chat()

    async def stream_events(self, strategy, group, **kwargs):
        """SG-1 F5：产出 ("chunk", c) / ("state", s) 事件序列。"""
        return self._stream_events(strategy, group, **kwargs)

    def _stream_events(self, strategy, group, **kwargs):
        async def _gen():
            yield ("chunk", {"choices": [{"delta": {"content": "hi"}}]})
            yield ("state", {"recoverable": True, "used_model_id": 1, "provider_id": 1})
        return _gen()


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
# 6b. SG-0 F5/F6 非流式驱动：失败留痕 + 错误行归属修正
# ===========================================================================


def _make_attempt(ep, error_type: str, error_message: str, *, endpoint_idx: int = 0,
                  attempt_no: int = 1, stage: str = "non_stream",
                  model_id=None, provider_id=None, group_id=None) -> dict:
    """Build a RouteState.attempts row (shape per docs/pipeline-single-graph-design.md §3.6)."""
    return {
        "request_id": "req-sg0",
        "group_id": ep.detail.group_id if group_id is None else group_id,
        "model_id": ep.model_id if model_id is None else model_id,
        "provider_id": ep.detail.provider_id if provider_id is None else provider_id,
        "stage": stage,
        "endpoint_idx": endpoint_idx,
        "attempt_no": attempt_no,
        "error_type": error_type,
        "error_message": error_message,
        "duration_ms": 12,
        "created_at": "2026-01-01T00:00:00Z",
    }


class _AttemptEngine:
    """Stub engine whose route() returns a response plus collected attempts.

    Mirrors SG-0_features.md F4/F5: the graph collects ``attempts`` into the
    RouteState and the driver forwards them to ``core._log_attempts``. The real
    engine populates these from failed ``call_llm`` attempts; here we inject
    them directly so the driver-wiring (not the graph) is what's under test.
    """

    def __init__(self, response, attempts, status="success"):
        self.cooldown = CooldownManager()
        self._response = response
        self._attempts = attempts
        self._status = status

    async def route(self, group, messages, temperature=None, max_tokens=None, **kwargs):
        if self._status == "error":
            # Mirror LangGraphEngine: a routing failure *raises*, smuggling the
            # attempt trail + last attempted endpoint onto the exception (see
            # langgraph_engine._raise_routing_error). The driver reads them via
            # getattr(e, "attempts" / "used_model_id" / "used_provider_id").
            exc = ProviderError("all endpoints failed")
            exc.attempts = self._attempts
            exc.used_model_id = self._attempts[-1]["model_id"] if self._attempts else None
            exc.used_provider_id = self._attempts[-1]["provider_id"] if self._attempts else None
            raise exc
        result = dict(self._response)
        # Attempts ride out under "_attempts" (internal, "_"-prefixed like
        # `_routing`) and are popped by the driver before serialisation.
        result["_attempts"] = self._attempts
        return result

    async def run(self, strategy, group, mode, **kwargs):
        """SG-1 单入口（_AttemptEngine 主要服务 SG-0 非流式留痕测试；此处补齐 run/stream_events）。"""
        if mode == "stream":
            async def _gen():
                yield ("chunk", {"choices": [{"delta": {"content": "hi"}}]})
            return _gen()
        return await self.route(group, messages=kwargs.get("messages", []))

    async def stream_events(self, strategy, group, **kwargs):
        return await self.run(strategy, group, mode="stream", **kwargs)


class TestAttemptLogging:
    """F5 (驱动落库) + F6 (归属修正) —— 非流式路径。

    守卫点：T5.1+T5.5 组合、T6.1 反例守卫（归属不为 None）。
    """

    def _drive(self, client, monkeypatch, engine, captured):
        async def _capture(rows):
            captured.extend(rows)

        # Must be async: the driver does `await _log_attempts(...)`, so a sync
        # stub would raise TypeError and be swallowed by the error path.
        monkeypatch.setattr(core, "_log_attempts", _capture, raising=False)
        monkeypatch.setattr(core, "_get_engine", lambda: engine)
        return client.post(
            "/v1/chat/completions",
            json={"model": "default", "messages": [{"role": "user", "content": "hi"}]},
            headers=AUTH,
        )

    def _logged_statuses(self):
        # `_log_call` is invoked with keyword arguments (core.py:1053 / :1077),
        # so `call.args` is always empty -- read `call.kwargs` instead.
        return [
            call.kwargs["status"]
            for call in core._log_call.call_args_list
            if call.kwargs.get("status")
        ]

    def test_retry_success_writes_attempts_row(self, client, monkeypatch):
        # T5.1 (G1 关键)：首端点失败 → 次端点成功，仍记录失败尝试
        captured = []
        ep1 = _endpoint(1)
        attempts = [_make_attempt(ep1, "ProviderError", "HTTP 500")]
        engine = _AttemptEngine(
            response={"choices": [{"message": {"content": "ok"}}],
                      "id": "c1", "_routing": {"model_id": 1, "provider_id": 1}},
            attempts=attempts, status="success",
        )
        r = self._drive(client, monkeypatch, engine, captured)
        assert r.status_code == 200
        assert "success" in self._logged_statuses()  # 一次请求一行 success 语义不变
        assert len(captured) >= 1
        row = captured[0]
        assert row["model_id"] == ep1.model_id
        assert row["provider_id"] == ep1.detail.provider_id
        assert row["error_type"]

    def test_backup_group_attempts_recorded(self, client, monkeypatch):
        # T5.2 (G3 关键)：主组 + backup 组都尝试过，attempts 跨组累加
        captured = []
        ep_primary = _endpoint(1)
        ep_backup = _endpoint(2)
        attempts = [
            _make_attempt(ep_primary, "ProviderError", "primary down", group_id=10),
            _make_attempt(ep_backup, "TimeoutError", "backup down", group_id=20),
        ]
        engine = _AttemptEngine(
            response={"choices": [{"message": {"content": "ok"}}],
                      "id": "c1", "_routing": {"model_id": 1, "provider_id": 1}},
            attempts=attempts, status="success",
        )
        r = self._drive(client, monkeypatch, engine, captured)
        assert r.status_code == 200
        assert {a["group_id"] for a in captured} == {10, 20}

    def test_all_fail_records_every_attempt(self, client, monkeypatch):
        # T5.3：全失败，attempts 覆盖每个端点的每次实际尝试
        captured = []
        ep1 = _endpoint(1, max_retries=2)
        attempts = [
            _make_attempt(ep1, "ProviderError", "try 1", endpoint_idx=0, attempt_no=1),
            _make_attempt(ep1, "ProviderError", "try 2", endpoint_idx=0, attempt_no=2),
        ]
        engine = _AttemptEngine(
            response={"choices": [{"message": {"content": "x"}}],
                      "id": "c1", "_routing": {"model_id": 1, "provider_id": 1}},
            attempts=attempts, status="error",
        )
        r = self._drive(client, monkeypatch, engine, captured)
        assert r.status_code == 502  # 全失败 -> HTTPException(502)
        assert "error" in self._logged_statuses()
        assert {(a["endpoint_idx"], a["attempt_no"]) for a in captured} == {(0, 1), (0, 2)}

    def test_no_failed_attempt_writes_no_rows(self, client, monkeypatch):
        # T5.4 (边界)：首端点一次成功、全程无失败 → 不写噪音行
        captured = []
        engine = _AttemptEngine(
            response={"choices": [{"message": {"content": "ok"}}],
                      "id": "c1", "_routing": {"model_id": 1, "provider_id": 1}},
            attempts=[], status="success",
        )
        r = self._drive(client, monkeypatch, engine, captured)
        assert r.status_code == 200
        assert captured == []

    def test_attempts_write_failure_does_not_break_request(self, client, monkeypatch):
        # T5.5 (反例)：留痕的**底层写入**抛错 → 主链路仍正常返回，只 log.error。
        # 打桩在底层 writer（而不是 `_log_attempts` 本身）：按 SG-0_tests.md §2，
        # 吞错必须发生在 `_log_attempts` 内部，驱动不再包第二层 try。
        class _BoomWriter:
            async def log_attempt(self, entry):
                raise RuntimeError("writer down")

        fake_log = MagicMock()
        monkeypatch.setattr(core, "_log_writer", _BoomWriter(), raising=False)
        # loguru 不向 stdlib logging 传播，caplog 看不到 —— 直接替换 core.log。
        monkeypatch.setattr(core, "log", fake_log, raising=False)
        monkeypatch.setattr(core, "_get_engine", lambda: _AttemptEngine(
            response={"choices": [{"message": {"content": "ok"}}],
                      "id": "c1", "_routing": {"model_id": 1, "provider_id": 1}},
            attempts=[_make_attempt(_endpoint(1), "ProviderError", "x")], status="success",
        ))
        r = client.post(
            "/v1/chat/completions",
            json={"model": "default", "messages": [{"role": "user", "content": "hi"}]},
            headers=AUTH,
        )
        assert r.status_code == 200
        assert fake_log.error.called

    def test_non_stream_error_row_has_attribution(self, client, monkeypatch):
        # T6.1 (G2 关键)：非流式全失败 → 错误行 model_id/provider_id 非 None，= 最后尝试端点
        captured = []
        ep_last = _endpoint(3)
        attempts = [
            _make_attempt(_endpoint(1), "ProviderError", "first down", endpoint_idx=0),
            _make_attempt(ep_last, "ProviderError", "last down", endpoint_idx=1),
        ]
        engine = _AttemptEngine(
            response={"choices": [{"message": {"content": "x"}}],
                      "id": "c1", "_routing": {"model_id": 3, "provider_id": 1}},
            attempts=attempts, status="error",
        )
        r = self._drive(client, monkeypatch, engine, captured)
        assert r.status_code == 502
        # `_log_call` receives keyword arguments (not a CallLog object), so the
        # row is the kwargs dict itself.
        error_rows = [
            c.kwargs for c in core._log_call.call_args_list
            if c.kwargs.get("status") == "error"
        ]
        assert error_rows, "must log an error row"
        err_row = error_rows[-1]
        assert err_row["model_id"] == ep_last.model_id
        assert err_row["provider_id"] == ep_last.detail.provider_id

    # ---- T5.1 加固：真实执行 `_log_attempts`（不是打桩它）----------------
    async def test_log_attempts_actually_reaches_writer(self, monkeypatch):
        # 回归守卫：只断言「`_log_attempts` 被调用过」抓不到漏 `await` ——
        # 漏了的话协程永不执行，主链路照常返回，缺陷静默溜过（本轮真发生过）。
        seen = []

        class _RecordingWriter:
            async def log_attempt(self, entry):
                seen.append(entry)

        monkeypatch.setattr(core, "_log_writer", _RecordingWriter(), raising=False)
        token = core._request_ctx.set({"request_id": "req-guard"})
        try:
            await core._log_attempts([
                {
                    "group_id": 1, "model_id": 7, "provider_id": 2,
                    "stage": "non_stream", "endpoint_idx": 0, "attempt_no": 1,
                    "error_type": "ProviderError", "error_message": "boom",
                    "duration_ms": 11,
                }
            ])
        finally:
            core._request_ctx.reset(token)

        assert len(seen) == 1
        assert isinstance(seen[0], CallAttempt)
        assert seen[0].request_id == "req-guard"
        assert seen[0].model_id == 7
        assert seen[0].provider_id == 2
        assert seen[0].error_type == "ProviderError"
        assert seen[0].stage == "non_stream"

    async def test_log_attempts_falls_back_to_direct_db_write(self, monkeypatch):
        # `_log_writer` 未初始化（如 CLI 路径）时的直写兜底分支。
        written = []

        class _FakeDB:
            async def create_call_attempts(self, entries):
                written.extend(entries)
                return len(entries)

        monkeypatch.setattr(core, "_log_writer", None, raising=False)
        monkeypatch.setattr(core, "_get_db", lambda: _FakeDB(), raising=False)
        token = core._request_ctx.set({"request_id": "req-fallback"})
        try:
            await core._log_attempts([
                {
                    "group_id": None, "model_id": None, "provider_id": None,
                    "stage": "select", "endpoint_idx": 0, "attempt_no": 1,
                    "error_type": "AllModelsCooldownError", "error_message": "cold",
                    "duration_ms": 1,
                }
            ])
        finally:
            core._request_ctx.reset(token)

        assert len(written) == 1
        assert isinstance(written[0], CallAttempt)
        assert written[0].request_id == "req-fallback"
        assert written[0].model_id is None

    async def test_log_attempts_empty_list_is_noop(self, monkeypatch):
        # 边界：空列表直接短路，不碰 writer、不碰 DB。
        touched = []

        class _RecordingWriter:
            async def log_attempt(self, entry):
                touched.append(entry)

        monkeypatch.setattr(core, "_log_writer", _RecordingWriter(), raising=False)
        await core._log_attempts([])
        assert touched == []

    async def test_log_attempts_swallows_writer_failure(self, monkeypatch):
        # T5.5 的可观测补充：留痕失败绝不上抛（已在此断言直接抛错被吞）。
        class _BoomWriter:
            async def log_attempt(self, entry):
                raise RuntimeError("writer down")

        fake_log = MagicMock()
        monkeypatch.setattr(core, "_log_writer", _BoomWriter(), raising=False)
        monkeypatch.setattr(core, "log", fake_log, raising=False)
        await core._log_attempts([
            {"stage": "non_stream", "error_type": "ProviderError",
             "error_message": "x", "duration_ms": 1},
        ])
        assert fake_log.error.called


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

    def _stream_events(self, strategy, group, **kwargs):
        # 由端点桩逐 chunk 产出（兼容 _StreamProvider 行为），供 _stream_common 消费
        async def _gen():
            for ep in self.endpoints:
                prov = ep.provider
                async for item in prov.chat_stream():
                    yield ("chunk", item)
        return _gen()

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
        """SG-1 §3.3：_stream_common 现在消费 engine.stream_events 产出的
        ("chunk", c)/("state", s)；客户端断连则中止产出（仍收 [DONE]）。"""
        async def _events(strategy, group, **kwargs):
            yield ("chunk", {"content": "a"})
            yield ("chunk", {"content": "b"})
        engine = _StubEngine()
        engine.stream_events = _events
        _setup_stream(monkeypatch, engine)

        out = [
            line async for line in core._stream_common(
                {"model": "x", "messages": []}, _serialize,
                request=_RequestStub(disconnected=True),
            )
        ]
        # 首 chunk 即触发断连检查 → 不产出任何 data 行（仍收 [DONE]）。
        assert "data: a\n\n" not in out
        assert out[-1] == "data: [DONE]\n\n"

    async def test_fallback_group_load_failure_raises_original_error(self, monkeypatch):
        """SG-1 §3.3：组级 fallback 已迁到驱动 core._drive。主组全冷却 → 驱动调
        _load_group(backup) → 加载失败 → 原错（AllModelsCooldownError）原样上抛。"""
        err = AllModelsCooldownError("primary failed")
        engine = _DriverEngine({1: lambda s, g, m: (_ for _ in ()).throw(err)})
        primary = ModelGroup(id=1, name="g1", type="random_weights", fallback_group_id=2)
        monkeypatch.setattr(core, "_get_extra_route_params", AsyncMock(return_value=(1, engine, primary, {})))
        monkeypatch.setattr(core, "_load_group", AsyncMock(side_effect=err))
        monkeypatch.setattr(core, "_log_call", AsyncMock())
        monkeypatch.setattr(core, "_log_attempts", AsyncMock())
        with pytest.raises(AllModelsCooldownError):
            await core._drive({"model": "x", "messages": []}, mode="chat")

    async def test_fallback_group_is_used_on_success(self, monkeypatch):
        """SG-1 §3.3：组级降级迁到驱动。主组全冷却 → 备份组成功 → 降级返回备份结果
        （与 T1.2 流式降级等价，此处走非流式路径验证 orchestration）。"""
        err = AllModelsCooldownError("primary failed")

        # ``_DriverEngine.run`` awaits the behavior, so both must be async
        # coroutine factories (same contract as the ``_ok`` / ``_cool`` helpers).
        async def _primary_fail(s, g, m):
            raise err

        async def _backup_ok(s, g, m):
            return {"choices": [{"message": {"content": "ok"}}], "_routing": {"model_id": 2}}

        engine = _DriverEngine({1: _primary_fail, 2: _backup_ok})
        primary = ModelGroup(id=1, name="g1", type="random_weights", fallback_group_id=2)
        backup = ModelGroup(id=2, name="g2", type="random_weights")
        monkeypatch.setattr(core, "_get_extra_route_params", AsyncMock(return_value=(1, engine, primary, {})))
        monkeypatch.setattr(core, "_load_group", AsyncMock(return_value=backup))
        monkeypatch.setattr(core, "_log_call", AsyncMock())
        monkeypatch.setattr(core, "_log_attempts", AsyncMock())
        result = await core._drive({"model": "x", "messages": []}, mode="chat")
        assert result["choices"][0]["message"]["content"] == "ok"

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


# ===========================================================================
# 11. SG-1 F1（驱动四步骨架）+ F6（降级白名单，驱动层）+ F7/F8（call_logs 字段）
#     以下用例直接驱动 core._drive(internal, mode)，对齐 docs/tasks/SG-1_tests.md §2 F1/F6/F7。
#     实现未落地时 core._drive / engine.run 不存在 → 收集/运行失败属预期，待编码子 agent 落地后复核。
# ===========================================================================


class _DriverEngine:
    """SG-1 驱动层桩：单入口 ``run(strategy, group, mode, **kw)``。

    ``_run_behavior[gid]`` 为 coroutine 工厂（async fn(s, g, m) -> result | raise）。
    - 成功返回 result dict；失败抛 typed 异常（驱动据异常类型判断是否可降级）。
    - AllModelsCooldownError / NoAvailableModelError → 可降级（recoverable）；
      ProviderError(非重试) / ConfigurationError / TypeError / StrategyError → 不可降级。
    """

    def __init__(self, run_behavior, cooldown=None):
        self.cooldown = cooldown or CooldownManager()
        self._run_behavior = run_behavior  # gid -> async fn(strategy, group, mode)
        self.run_calls: list = []

    async def run(self, strategy, group, mode, **kw):
        self.run_calls.append((group.id, mode))
        return await self._run_behavior[group.id](strategy, group, mode)


def _ok(gid, content=None):
    async def _fn(s, g, m):
        return _ok_result(gid, content)
    return _fn


def _cool():
    async def _fn(s, g, m):
        raise AllModelsCooldownError("all models on cooldown")
    return _fn


def _err(exc):
    async def _fn(s, g, m):
        raise exc
    return _fn


def _ok_result(gid, content=None):
    return {
        "choices": [{"message": {"content": content or f"ok-{gid}"}}],
        "id": "c1", "model": f"m{gid}", "created": 1,
        "_routing": {"group_id": gid, "model_id": gid, "provider_id": 1},
    }


def _make_group(gid, type_="random_weights", fallback_gid=None):
    return ModelGroup(id=gid, name=f"g{gid}", type=type_, fallback_group_id=fallback_gid, params={})


def _drive_setup(monkeypatch, engine, primary, backup=None, attempts=None):
    """接好 core._drive 的依赖桩，避免触碰 DB / 真实引擎。"""
    monkeypatch.setattr(
        core, "_get_extra_route_params", AsyncMock(return_value=(1, engine, primary, {})),
    )
    if backup is not None:
        monkeypatch.setattr(core, "_load_group", AsyncMock(return_value=backup))
    else:
        monkeypatch.setattr(core, "_load_group", AsyncMock())
    monkeypatch.setattr(core, "_log_call", AsyncMock())
    if attempts is not None:
        monkeypatch.setattr(core, "_log_attempts", AsyncMock(side_effect=lambda rows: attempts.extend(rows)))
    else:
        monkeypatch.setattr(core, "_log_attempts", AsyncMock())


async def test_driver_primary_group_success_no_fallback(monkeypatch):
    """T1.1 正例：主组首次成功 → engine.run 恰好 1 次；_load_group 未被调用（没走备份组）。"""
    engine = _DriverEngine({1: _ok(1)})
    monkeypatch.setattr(core, "_get_extra_route_params", AsyncMock(return_value=(1, engine, _make_group(1), {})))
    monkeypatch.setattr(core, "_load_group", AsyncMock())
    monkeypatch.setattr(core, "_log_call", AsyncMock())
    monkeypatch.setattr(core, "_log_attempts", AsyncMock())
    result = await core._drive({"model": "x", "messages": []}, mode="chat")
    assert result["choices"][0]["message"]["content"] == "ok-1"
    assert engine.run_calls == [(1, "chat")]
    core._load_group.assert_not_called()


async def test_driver_backup_also_cooldown_raises_original_error(monkeypatch):
    """T1.3 正例(R4 守卫)：主组与备份组全冷却 → raise 的是原始 AllModelsCooldownError，
    type(e).__name__ == "AllModelsCooldownError"（不是 ProviderError）。保住 call_logs.error_type 语义。"""
    engine = _DriverEngine({1: _cool(), 2: _cool()})
    _drive_setup(monkeypatch, engine, _make_group(1, fallback_gid=2), _make_group(2))
    with pytest.raises(AllModelsCooldownError):
        await core._drive({"model": "x", "messages": []}, mode="chat")


async def test_driver_cycle_guard_stops_loop(monkeypatch):
    """T1.4 反例：组链 A→B→A。run 最多被调用 2 次（A、B），第 3 次因 A∈visited 被拦后终止。"""
    engine = _DriverEngine({1: _cool(), 2: _cool()})
    gA = _make_group(1, fallback_gid=2)
    gB = _make_group(2, fallback_gid=1)  # 指回 A → 环
    _drive_setup(monkeypatch, engine, gA, gB)
    with pytest.raises(AllModelsCooldownError):
        await core._drive({"model": "x", "messages": []}, mode="chat")
    assert len(engine.run_calls) == 2
    assert set(g for (g, m) in engine.run_calls) == {1, 2}


async def test_driver_depth_limit_three(monkeypatch):
    """T1.5 边界：4 级链 A→B→C→D。第 3 跳仍执行（run 调用 3 次），第 4 跳因深度上限终止。"""
    engine = _DriverEngine({i: _cool() for i in (1, 2, 3, 4)})
    groups = {i: _make_group(i, fallback_gid=(i + 1 if i < 4 else None)) for i in (1, 2, 3, 4)}
    monkeypatch.setattr(core, "_get_extra_route_params", AsyncMock(return_value=(1, engine, groups[1], {})))
    monkeypatch.setattr(core, "_load_group", AsyncMock(side_effect=lambda gid: groups[gid]))
    monkeypatch.setattr(core, "_log_call", AsyncMock())
    monkeypatch.setattr(core, "_log_attempts", AsyncMock())
    with pytest.raises(AllModelsCooldownError):
        await core._drive({"model": "x", "messages": []}, mode="chat")
    # 第 1/2/3 跳执行（run 3 次）；第 4 跳因 depth>=3 终止
    assert len(engine.run_calls) == 3


async def test_driver_no_backup_group_terminates_immediately(monkeypatch):
    """T1.6 反例：fallback_group_id is None → 不尝试降级（_load_group 未被调用）、立即上抛。"""
    engine = _DriverEngine({1: _cool()})
    g = _make_group(1, fallback_gid=None)
    monkeypatch.setattr(core, "_get_extra_route_params", AsyncMock(return_value=(1, engine, g, {})))
    monkeypatch.setattr(core, "_load_group", AsyncMock())
    monkeypatch.setattr(core, "_log_call", AsyncMock())
    monkeypatch.setattr(core, "_log_attempts", AsyncMock())
    with pytest.raises(AllModelsCooldownError):
        await core._drive({"model": "x", "messages": []}, mode="chat")
    assert engine.run_calls == [(1, "chat")]
    core._load_group.assert_not_called()


async def test_driver_backup_group_missing_raises_configuration_error(monkeypatch):
    """T1.7 边界：备份组 id 指向不存在的组（_load_group 抛 ConfigurationError）→
    上抛且不再继续降级（黑名单语义，非白名单）。"""
    engine = _DriverEngine({1: _cool()})
    gA = _make_group(1, fallback_gid=99)
    monkeypatch.setattr(core, "_get_extra_route_params", AsyncMock(return_value=(1, engine, gA, {})))
    monkeypatch.setattr(core, "_load_group", AsyncMock(side_effect=ConfigurationError("group 99 not found")))
    monkeypatch.setattr(core, "_log_call", AsyncMock())
    monkeypatch.setattr(core, "_log_attempts", AsyncMock())
    with pytest.raises(ConfigurationError):
        await core._drive({"model": "x", "messages": []}, mode="chat")
    assert engine.run_calls == [(1, "chat")]  # 不继续降级


async def test_not_recoverable_configuration_error_unknown_strategy(monkeypatch):
    """T6.4 反例(R9，驱动层)：未知 group.type / 图抛 ConfigurationError → 不降级，
    run 仅 1 次，原类型上抛 → HTTP 502（不是 404）。"""
    engine = _DriverEngine({1: _err(ConfigurationError("unknown strategy type"))})
    g = _make_group(1, type_="does_not_exist", fallback_gid=2)
    monkeypatch.setattr(core, "_get_extra_route_params", AsyncMock(return_value=(1, engine, g, {})))
    monkeypatch.setattr(core, "_load_group", AsyncMock())
    monkeypatch.setattr(core, "_log_call", AsyncMock())
    monkeypatch.setattr(core, "_log_attempts", AsyncMock())
    with pytest.raises(ConfigurationError):
        await core._drive({"model": "x", "messages": []}, mode="chat")
    assert engine.run_calls == [(1, "chat")]
    core._load_group.assert_not_called()


async def test_not_recoverable_unexpected_type_error_and_logged(monkeypatch):
    """T6.5 反例(R9，驱动层)：select_endpoints 抛未预期 TypeError → recoverable is False → 502；
    同时断言 call_attempts 有留痕行（SG-0）—— 黑名单失败必须留痕，否则从「掩盖 bug」变「静默失败」。"""
    engine = _DriverEngine({1: _err(TypeError("unexpected"))})
    attempts: list = []
    _drive_setup(monkeypatch, engine, _make_group(1), attempts=attempts)
    with pytest.raises(TypeError):
        await core._drive({"model": "x", "messages": []}, mode="chat")
    assert attempts, "non-recoverable failure must leave a call_attempts row (SG-0)"


async def test_call_logs_fields_unchanged(monkeypatch):
    """T7.4 正例：成功/失败两条路径的 call_logs 关键字段（status/model_id/provider_id/
    duration_ms/prompt_tokens/completion_tokens/total_tokens）与改动前一致（配合 SG-0 留痕逐条一致）。"""
    seen: list = []

    async def _capture(**kwargs):
        seen.append(kwargs)

    monkeypatch.setattr(core, "_log_call", _capture)
    await core._log_call(
        status="success", model_id=1, provider_id=2, duration_ms=12,
        prompt_tokens=3, completion_tokens=4, total_tokens=7, model_name="gpt-4",
    )
    await core._log_call(
        status="error", model_id=1, provider_id=2, duration_ms=15,
        prompt_tokens=3, completion_tokens=0, total_tokens=3,
        error_type="ProviderError", model_name="gpt-4",
    )
    assert len(seen) == 2
    required = ("status", "model_id", "provider_id", "duration_ms",
                "prompt_tokens", "completion_tokens", "total_tokens")
    for row in seen:
        for key in required:
            assert key in row, f"missing field {key} in {row}"
    assert seen[0]["status"] == "success"
    assert seen[1]["error_type"] == "ProviderError"


# ---------------------------------------------------------------------------
# SG-1 覆盖率补洞 + 回归修复（core 驱动层）
#
# 定性：
#   * G8  真分支 → 补测（``_load_group`` 委派缝，其它用例全把它整只打桩）
#   * G9  真分支 → 补测（``_drive_stream`` 未预期异常分支）
#   * G10 真分支 → 补测（``_drive_stream`` 降级耗尽分支）
#   * G11 真分支 → 补测（``_handle_chat_non_stream`` 的 HTTPException 透传）
#   * 死代码 → 删除 ``core._chain_first``（逐 chunk 迭代已迁进图 ``try_stream``，
#     core 里这份再无调用点），不用 `# UNCOVERED` 掩盖
# ---------------------------------------------------------------------------


async def test_load_group_delegates_to_active_engine(monkeypatch):
    """G8：``core._load_group`` 是驱动加载备份组的模块级缝，必须真的委派给当前引擎
    的 ``_load_group``（60s 缓存那层）。其它用例都把它整只打桩，这 2 行因此从未执行。
    """
    engine = MagicMock()
    engine._load_group = AsyncMock(return_value=ModelGroup(id=2, name="g2"))
    monkeypatch.setattr(core, "_get_engine", lambda: engine)

    group = await core._load_group(2)
    assert group.id == 2
    engine._load_group.assert_awaited_once_with(2)


async def test_drive_stream_unexpected_error_leaves_trace_and_closes(monkeypatch):
    """G9（**SG-1 回归修复**）：流式驱动遇到**非** BotflowError 的未预期异常（如
    ``ValueError``）→ design §3.6「黑名单失败必须留痕」：写 ``call_attempts``
    （``stage="route"``）+ 一条 ``status="error"`` 的 ``call_logs``，再推 error SSE +
    done 收流。

    修复前这条分支只把留痕 append 到内存 list 就 return，生产上这类流式故障在
    ``call_logs`` 里查无此行（非流式 ``_drive_chat`` 一直是有留痕的）。
    """
    engine = _DriverEngine({1: _err(ValueError("unexpected boom"))})
    calls: list = []
    attempt_rows: list = []

    async def _cap_call(**kwargs):
        calls.append(kwargs)

    async def _cap_attempts(rows):
        attempt_rows.extend(rows)

    monkeypatch.setattr(
        core, "_get_extra_route_params",
        AsyncMock(return_value=(1, engine, ModelGroup(id=1, name="g1"), {})),
    )
    monkeypatch.setattr(core, "_log_call", _cap_call)
    monkeypatch.setattr(core, "_log_attempts", _cap_attempts)

    out = [
        line async for line in core._drive({"model": "x", "messages": []}, mode="stream")
    ]
    assert out[-1] == "data: [DONE]\n\n"
    assert any("unexpected boom" in line for line in out)
    assert [r["stage"] for r in attempt_rows] == ["route"]
    assert len(calls) == 1
    assert calls[0]["status"] == "error"
    assert calls[0]["error_type"] == "ValueError"
    assert calls[0]["group_id"] == 1


async def test_drive_stream_exhausted_fallback_leaves_trace(monkeypatch):
    """G10（**SG-1 回归修复**）：主组全冷却且**无备份组** → 直接走「降级耗尽」出口，
    同样必须留痕（error 行 + done）。``used_model_id`` 取自图出口 state（此处为 None）。
    """
    engine = _DriverEngine({1: _cool()})
    calls: list = []

    async def _cap_call(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(
        core, "_get_extra_route_params",
        AsyncMock(return_value=(1, engine, ModelGroup(id=1, name="g1"), {})),
    )
    monkeypatch.setattr(core, "_log_call", _cap_call)
    monkeypatch.setattr(core, "_log_attempts", AsyncMock())

    out = [
        line async for line in core._drive({"model": "x", "messages": []}, mode="stream")
    ]
    assert out[-1] == "data: [DONE]\n\n"
    assert any("all models on cooldown" in line for line in out)
    assert len(calls) == 1
    assert calls[0]["status"] == "error"
    assert calls[0]["error_type"] == "AllModelsCooldownError"
    assert calls[0]["model_id"] is None


async def test_handle_chat_non_stream_reraises_http_exception(monkeypatch):
    """G11：``_handle_chat_non_stream`` 对驱动抛出的 ``HTTPException`` 必须**原样
    透传**（保留原 status_code/detail），不得被下面的兜底再包一层 502。
    """
    async def _raise(*args, **kwargs):
        raise HTTPException(status_code=409, detail="conflict")

    monkeypatch.setattr(core, "_drive", _raise)

    with pytest.raises(HTTPException) as exc_info:
        await core._handle_chat_non_stream(
            {"model": "x", "messages": []}, MagicMock(), lambda r: r,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "conflict"


async def test_handle_chat_non_stream_wraps_unexpected_error_as_502(monkeypatch):
    """G12：驱动抛出**非** ``HTTPException`` 的异常 → 兜底包成 ``HTTPException(502)``。

    这行原先挂着 SG-1 新增的 ``# pragma: no cover - defensive``（HEAD 版 core.py 里
    这类标记数量为 0）—— 但该分支**可达**：``_drive_chat`` 耗尽备份后会原样上抛 typed
    异常。按红线「不得用标记掩盖缺口」去掉标记并真测它。
    """
    async def _raise(*args, **kwargs):
        raise ProviderError("upstream down")

    monkeypatch.setattr(core, "_drive", _raise)

    with pytest.raises(HTTPException) as exc_info:
        await core._handle_chat_non_stream(
            {"model": "x", "messages": []}, MagicMock(), lambda r: r,
        )

    assert exc_info.value.status_code == 502
    assert "upstream down" in exc_info.value.detail
