"""Unit tests for core.py pure helpers (no network)."""

from __future__ import annotations

import asyncio
import time
from collections import deque

import pytest

import botflow.core as core
from botflow.config import BotflowSettings, set_config
from botflow.common.exceptions import ProviderError
from botflow.router import ModelEndpoint
from botflow.storage.models import CallAttempt, CallLog


def test_filter_safe_extra():
    extra = {"temperature": 0.5, "evil": "x", "tools": [], "stop": ["\n"]}
    out = core._filter_safe_extra(extra)
    assert "temperature" in out and "tools" in out and "stop" in out
    assert "evil" not in out


def test_request_summary_short():
    assert core._request_summary({"a": 1}) is not None


def test_request_summary_truncated():
    big = {"x": "y" * 5000}
    s = core._request_summary(big)
    assert s.endswith("…[truncated]")


def test_request_summary_unserializable():
    assert core._request_summary({"f": object()}) is None


def test_limit_traceback():
    assert core._limit_traceback("") is None
    assert core._limit_traceback("short") == "short"
    long = "z" * 9000
    assert core._limit_traceback(long).endswith("…[truncated]")


def test_generate_request_id_deterministic():
    a = core._generate_request_id({"model": "x", "messages": []})
    b = core._generate_request_id({"messages": [], "model": "x"})
    assert a == b and len(a) == 16


def test_extract_model_route_info():
    g, m, p = core._extract_model_route_info({}, {"_group_id": 7})
    assert g == 7 and m is None and p is None


def test_set_request_ctx_and_request_id():
    class Req:
        headers = {}
    tok = core._set_request_ctx(5, "rid")
    assert core._request_ctx.get()["api_key_id"] == 5
    core._request_ctx.reset(tok)
    assert core._request_id(Req())  # generated uuid


def test_get_request_ctx_default():
    assert core._request_ctx.get() is None


def test_rate_limit_middleware_wired_from_settings():
    # core.py registers RateLimitMiddleware at import time from get_config()
    # values (core._cfg). Assert the registered kwargs match that settings
    # object so the middleware can never silently drift back to hardcoded
    # limits.
    mw = next(m for m in core.app.user_middleware if m.cls is core.RateLimitMiddleware)
    cfg = core._cfg
    assert isinstance(cfg, BotflowSettings)
    assert mw.kwargs == {
        "max_requests": cfg.rate_limit_max_requests,
        "window_seconds": cfg.rate_limit_window_seconds,
        "max_keys": cfg.rate_limit_max_keys,
    }


# ---------------------------------------------------------------------------
# RateLimitMiddleware (unit, no ASGI stack)
# ---------------------------------------------------------------------------


def test_rate_limit_key_bearer():
    rm = core.RateLimitMiddleware(core.app)
    scope = {
        "headers": [(b"authorization", b"Bearer abc")],
        "query_string": b"",
        "client": None,
    }
    assert rm._get_rate_limit_key(scope) == "abc"


def test_rate_limit_key_query():
    rm = core.RateLimitMiddleware(core.app)
    scope = {
        "headers": [],
        "query_string": b"api_key=qk",
        "client": None,
    }
    assert rm._get_rate_limit_key(scope) == "qk"


def test_rate_limit_key_query_duplicate_last_wins():
    # Mirrors starlette QueryParams.get semantics (dict: last duplicate wins).
    rm = core.RateLimitMiddleware(core.app)
    scope = {
        "headers": [],
        "query_string": b"api_key=first&api_key=qk",
        "client": ("1.2.3.4", 5),
    }
    assert rm._get_rate_limit_key(scope) == "qk"


def test_rate_limit_key_query_blank_falls_through_to_client():
    rm = core.RateLimitMiddleware(core.app)
    scope = {
        "headers": [],
        "query_string": b"api_key=&x=1",
        "client": ("1.2.3.4", 5),
    }
    assert rm._get_rate_limit_key(scope) == "1.2.3.4"


def test_rate_limit_key_anonymous():
    rm = core.RateLimitMiddleware(core.app)
    scope = {
        "headers": [],
        "query_string": b"",
        "client": ("1.2.3.4", 12345),
    }
    assert rm._get_rate_limit_key(scope) == "1.2.3.4"


def test_asgi_header_first_match_and_missing_default():
    scope = {
        "headers": [(b"x-a", b"1"), (b"x-a", b"2")],
        "query_string": b"",
        "client": None,
    }
    assert core._asgi_header(scope, "x-a") == "1"
    assert core._asgi_header(scope, "x-missing") == ""


def test_cleanup_old_keys():
    rm = core.RateLimitMiddleware(core.app)
    rm._requests["k"] = deque([time.time() - 1000], maxlen=10)
    rm._last_access["k"] = time.time() - 1000
    rm._cleanup_old_keys(time.time())
    assert "k" not in rm._requests


# ---------------------------------------------------------------------------
# CallLogWriter
# ---------------------------------------------------------------------------


def _fake_call_log_entry():
    return CallLog(api_key_id=1, group_id=1, model_id=1, status="success",
                   request_body="{}", response_body="{}")


class _FakeDB:
    def __init__(self):
        self.written = []
        self.attempts_written = []
        self.closed = False
    async def create_call_log(self, entry):
        self.written.append(entry)
    async def create_call_attempts(self, entries):
        self.attempts_written.extend(entries)
        return len(entries)
    async def close(self):
        self.closed = True


async def test_call_log_writer_empty_flush_noop():
    db = _FakeDB()
    w = core.CallLogWriter(db, flush_interval=0.01)
    await w._flush()  # empty buffer -> returns immediately
    assert db.written == []


async def test_call_log_writer_flush_error_logged(caplog):
    db = _FakeDB()
    db.create_call_log = lambda e: (_ for _ in ()).throw(RuntimeError("boom"))
    w = core.CallLogWriter(db, flush_interval=0.01)
    w._buffer.append(_fake_call_log_entry())
    await w._flush()  # swallow exception


def _fake_call_attempt():
    return CallAttempt(request_id="req-1", stage="non_stream",
                       error_type="ProviderError", error_message="boom")


async def test_call_log_writer_log_attempt_buffers_then_flushes():
    # SG-0：`CallLogWriter.log_attempt` 的**真实**生产路径。
    # 此前无人覆盖 —— 守卫用例与 T5.5 都把它整个打桩掉了，这正是"漏 await"
    # 这类缺陷能溜过去的那一层。
    db = _FakeDB()
    w = core.CallLogWriter(db, flush_interval=0.01)
    await w.log_attempt(_fake_call_attempt())
    assert len(w._attempts_buffer) == 1
    await w._flush()
    assert len(db.attempts_written) == 1
    assert db.attempts_written[0].stage == "non_stream"
    assert w._attempts_buffer == []


async def test_call_log_writer_log_attempt_threshold_triggers_flush():
    # 满缓冲自动刷盘（与 `log()` 同构，且必须在锁外 flush 以免死锁）。
    db = _FakeDB()
    w = core.CallLogWriter(db, flush_interval=0.01, max_buffer=1)
    await w.log_attempt(_fake_call_attempt())
    assert len(db.attempts_written) == 1
    assert w._attempts_buffer == []


async def test_call_log_writer_flush_attempts_error_logged():
    # 留痕刷盘失败只记日志、不上抛（SG-0 硬约束 3）。
    db = _FakeDB()

    async def _boom(entries):
        raise RuntimeError("attempts boom")

    db.create_call_attempts = _boom
    w = core.CallLogWriter(db, flush_interval=0.01)
    w._attempts_buffer.append(_fake_call_attempt())
    await w._flush()  # 必须吞掉异常
    assert w._attempts_buffer == []


# ---------------------------------------------------------------------------
# _log_call buffering / fallback
# ---------------------------------------------------------------------------


async def test_log_call_uses_writer(monkeypatch):
    db = _FakeDB()
    core._log_writer = core.CallLogWriter(db, flush_interval=0.01)
    core._db = db
    core._config = BotflowSettings()
    set_config(core._config)
    try:
        await core._log_call(group_id=1, model_id=1, provider_id=1,
                             request_body="{}", response_body="{}", status="success",
                             duration_ms=1, usage={"total_tokens": 5})
        await core._log_writer._flush()
        assert len(db.written) == 1
    finally:
        await core._log_writer.stop()
        core._log_writer = None


async def test_log_call_fallback_direct_write(monkeypatch):
    db = _FakeDB()
    core._log_writer = None
    core._db = db
    core._config = BotflowSettings()
    set_config(core._config)
    await core._log_call(group_id=1, model_id=1, provider_id=1,
                         request_body="{}", response_body="{}", status="success",
                         duration_ms=1, usage=None)
    assert len(db.written) == 1


# ---------------------------------------------------------------------------
# _log_attempts (SG-0) — 失败尝试批量留痕
# ---------------------------------------------------------------------------


class _FakeWriter:
    """Minimal CallLogWriter stand-in for _log_attempts tests (no real flush)."""

    def __init__(self):
        self.entries = []
        self.attempts = []

    async def log(self, entry):
        self.entries.append(entry)

    async def log_attempt(self, attempt):
        self.attempts.append(attempt)


def _sample_attempt() -> dict:
    return {
        "group_id": 1,
        "model_id": 2,
        "provider_id": 3,
        "stage": "call",
        "error_type": "ProviderError",
        "error_message": "boom",
        "duration_ms": 9,
    }


async def test_log_attempts_uses_writer(monkeypatch):
    # 每请求失败尝试经 CallAttempt.model_construct 构造后送入 writer 缓冲。
    writer = _FakeWriter()
    old_writer = core._log_writer
    try:
        core._log_writer = writer
        await core._log_attempts([_sample_attempt()])
        assert len(writer.attempts) == 1
        a = writer.attempts[0]
        assert isinstance(a, CallAttempt)
        assert a.stage == "call" and a.error_type == "ProviderError" and a.duration_ms == 9
        # model_construct 跳过校验：缺失字段由默认值补齐（created_at 由 default_factory 生成）。
        assert a.created_at is not None
    finally:
        core._log_writer = old_writer


async def test_log_attempts_fallback_direct_write(monkeypatch):
    # writer 未初始化时回退到直接落库（与 _log_call 同构）。
    db = _FakeDB()
    old_writer = core._log_writer
    old_db = core._db
    try:
        core._log_writer = None
        core._db = db
        await core._log_attempts([_sample_attempt()])
        assert len(db.attempts_written) == 1
        assert isinstance(db.attempts_written[0], CallAttempt)
    finally:
        core._log_writer = old_writer
        core._db = old_db


async def test_log_attempts_empty_list_is_noop(monkeypatch):
    # 空列表是 no-op：不触 writer、不落库。
    writer = _FakeWriter()
    old_writer = core._log_writer
    try:
        core._log_writer = writer
        await core._log_attempts([])
        assert writer.attempts == []
    finally:
        core._log_writer = old_writer


async def test_log_attempts_swallows_writer_failure(monkeypatch):
    # 留痕失败只记日志、绝不上抛（SG-0 硬约束 3）。
    class BoomWriter:
        async def log_attempt(self, attempt):
            raise RuntimeError("boom")

    old_writer = core._log_writer
    try:
        core._log_writer = BoomWriter()
        await core._log_attempts([_sample_attempt()])  # 不得抛异常
    finally:
        core._log_writer = old_writer

