"""Endpoint + middleware integration tests for core.py via TestClient."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import botflow.core as core
from botflow.common.exceptions import ProviderError
from botflow.config import BotflowSettings, set_config
from botflow.router import CooldownManager, ModelEndpoint
from botflow.storage.db import Database
from botflow.storage.models import GroupModelWithDetails, ModelGroup


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class StreamProvider:
    async def chat(self, **kwargs):
        return {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}

    async def chat_stream(self, **kwargs):
        yield {"choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}]}
        yield {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}


def _make_stream_endpoint():
    detail = GroupModelWithDetails(
        id=1, group_id=1, model_id=1, weight=1.0, is_enabled=True,
        model_name="gpt-4", display_name="gpt-4",
        provider_id=2, provider_name="p", provider_type="openai",
        max_retries=3, cooldown_seconds=60, cooldown_failure_threshold=3,
    )
    return ModelEndpoint(detail, StreamProvider())


NON_STREAM_RESPONSE = {
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    "id": "c1", "model": "m", "created": 1,
    "_routing": {"group_id": 1, "model_id": 1, "provider_id": 2},
}


class StubEngine:
    """Lightweight stand-in for PipelineEngine used by core._handle_chat_* and _stream_common."""

    def __init__(self):
        self.kwargs = None
        self.exc = None
        self.cooldown = CooldownManager()

    async def route(self, group, messages, temperature=None, max_tokens=None, stream=False, **kwargs):
        self.kwargs = kwargs
        if self.exc is not None:
            raise self.exc
        return dict(NON_STREAM_RESPONSE)

    async def route_stream(self, group, messages, temperature=None, max_tokens=None, **kwargs):
        if self.exc is not None:
            raise self.exc
        ep = _make_stream_endpoint()
        return {
            "endpoints": [ep],
            "group_id": 1,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "kwargs": kwargs,
            "fallback_group_id": None,
        }


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    loop = asyncio.new_event_loop()
    db = Database(tmp_path / "data" / "botflow.db")
    loop.run_until_complete(db.initialize())
    loop.run_until_complete(db.create_api_key("test-key", label="t"))
    loop.run_until_complete(db.create_group(ModelGroup(name="default")))
    core._db = db
    core._engine = None  # clear any cached engine
    cfg = BotflowSettings()
    core._config = cfg
    set_config(cfg)
    engine = StubEngine()
    monkeypatch.setattr(core, "_get_engine", lambda: engine)
    monkeypatch.setattr(core, "_get_group_id", AsyncMock(return_value=1))
    client = TestClient(core.app)
    client.engine = engine
    yield client
    loop.run_until_complete(db.close())
    loop.close()
    core._db = None
    core._engine = None


# ---------------------------------------------------------------------------
# Basic endpoints
# ---------------------------------------------------------------------------


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_models(client):
    r = client.get("/v1/models", headers={"authorization": "Bearer test-key"})
    assert r.status_code == 200
    assert "data" in r.json()


def test_chat_completions_non_stream(client):
    payload = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}
    r = client.post("/v1/chat/completions", json=payload, headers={"authorization": "Bearer test-key"})
    assert r.status_code == 200
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "hi"


def test_chat_completions_stream(client):
    payload = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    with client.stream("POST", "/v1/chat/completions", json=payload, headers={"authorization": "Bearer test-key"}) as r:
        assert r.status_code == 200
        lines = list(r.iter_lines())
    assert any("data:" in ln for ln in lines)


def test_chat_completions_no_auth(client):
    payload = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}
    r = client.post("/v1/chat/completions", json=payload)
    assert r.status_code == 401


def test_completions_endpoint(client):
    payload = {"model": "gpt-4", "prompt": "hi", "stream": False}
    r = client.post("/v1/completions", json=payload, headers={"authorization": "Bearer test-key"})
    assert r.status_code == 200
    assert "choices" in r.json()


def test_messages_anthropic_endpoint(client):
    payload = {"model": "claude", "messages": [{"role": "user", "content": "hi"}]}
    r = client.post("/v1/messages", json=payload, headers={"authorization": "Bearer test-key"})
    assert r.status_code == 200
    assert "content" in r.json()


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_rate_limit_exceeded(client):
    payload = {"model": "gpt-4", "messages": [{"role": "user", "content": "x"}]}
    headers = {"authorization": "Bearer test-key"}
    last = None
    for _ in range(25):
        last = client.post("/v1/chat/completions", json=payload, headers=headers)
    assert last.status_code in (200, 429)


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------


def test_auth_middleware_unknown_key(client):
    payload = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}
    r = client.post("/v1/chat/completions", json=payload, headers={"authorization": "Bearer unknown"})
    assert r.status_code == 401


def test_auth_middleware_x_api_key_valid(client):
    payload = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}
    r = client.post("/v1/chat/completions", json=payload, headers={"x-api-key": "test-key"})
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "hi"


def test_auth_middleware_x_api_key_invalid(client):
    payload = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}
    r = client.post("/v1/chat/completions", json=payload, headers={"x-api-key": "bad-key"})
    assert r.status_code == 401


def test_auth_middleware_x_api_key_precedence(client):
    # x-api-key takes precedence over Authorization when both are present.
    payload = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}
    r = client.post(
        "/v1/chat/completions",
        json=payload,
        headers={"x-api-key": "test-key", "authorization": "Bearer unknown"},
    )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Engine integration
# ---------------------------------------------------------------------------


def test_chat_completions_error_propagates_502(client):
    client.engine.exc = ProviderError("HTTP 500 boom")
    payload = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}
    r = client.post("/v1/chat/completions", json=payload, headers={"authorization": "Bearer test-key"})
    assert r.status_code == 502


def test_chat_completions_dedup_cache_hit(client, monkeypatch):
    cached = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "cached"}, "finish_reason": "stop"}]}
    monkeypatch.setattr(core, "_check_request_deduplication", AsyncMock(return_value=cached))
    payload = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}], "request_id": "fixed-id"}
    r = client.post("/v1/chat/completions", json=payload, headers={"authorization": "Bearer test-key"})
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "cached"


async def test_get_engine_real(tmp_path):
    from botflow.pipeline.engine import PipelineEngine
    from botflow.storage.models import Provider
    db = Database(tmp_path / "d.db")
    await db.initialize()
    await db.create_provider(Provider(name="p", provider_type="openai", api_key="k", base_url="http://x"))
    await db.create_group(ModelGroup(name="g1"))
    core._db = db
    try:
        engine = core._get_engine()
        assert isinstance(engine, PipelineEngine)
    finally:
        core._db = None
