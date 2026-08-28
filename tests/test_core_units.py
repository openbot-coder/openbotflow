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


# ---------------------------------------------------------------------------
# RateLimitMiddleware (unit, no ASGI stack)
# ---------------------------------------------------------------------------


def test_rate_limit_key_bearer():
    rm = core.RateLimitMiddleware(core.app)
    class Req:
        headers = {"authorization": "Bearer abc"}
        query_params = {}
        client = None
    assert rm._get_rate_limit_key(Req()) == "abc"


def test_rate_limit_key_query():
    rm = core.RateLimitMiddleware(core.app)
    class Req:
        headers = {}
        query_params = {"api_key": "qk"}
        client = None
    assert rm._get_rate_limit_key(Req()) == "qk"


def test_rate_limit_key_anonymous():
    rm = core.RateLimitMiddleware(core.app)
    class Req:
        headers = {}
        query_params = {}
        client = type("C", (), {"host": "1.2.3.4"})()
    assert rm._get_rate_limit_key(Req()) == "1.2.3.4"


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
    from botflow.storage.models import CallLog
    return CallLog(api_key_id=1, group_id=1, model_id=1, status="success",
                   request_body="{}", response_body="{}")


class _FakeDB:
    def __init__(self):
        self.written = []
        self.closed = False
    async def create_call_log(self, entry):
        self.written.append(entry)
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
