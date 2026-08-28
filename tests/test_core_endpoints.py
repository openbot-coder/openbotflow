"""Endpoint + middleware integration tests for core.py via TestClient."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import botflow.core as core
from botflow.config import BotflowSettings, set_config
from botflow.storage.db import Database
from botflow.router import ModelEndpoint


class StreamProvider:
    async def chat(self, **kwargs):
        return {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}

    async def chat_stream(self, **kwargs):
        yield {"choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}]}
        yield {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}


class FakeRouter:
    def __init__(self, *a, **k):
        self.kwargs = None
        self.exc = None

    async def route(self, **kwargs):
        self.kwargs = kwargs
        if self.exc is not None:
            raise self.exc
        if kwargs.get("stream"):
            ep = ModelEndpoint(group_id=1, model_id=1, provider_id=2, provider=StreamProvider())
            return {
                "group_id": 1,
                "endpoints": [ep],
                "messages": kwargs.get("messages"),
                "kwargs": {},
            }
        return {
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "id": "c1", "model": "m", "created": 1,
            "_routing": {"group_id": 1, "model_id": 1, "provider_id": 2},
        }


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = Database(tmp_path / "data" / "botflow.db")
    asyncio.get_event_loop().run_until_complete(db.initialize())
    asyncio.get_event_loop().run_until_complete(db.create_api_key("test-key", label="t"))
    core._db = db
    cfg = BotflowSettings()
    core._config = cfg
    set_config(cfg)
    router = FakeRouter()
    monkeypatch.setattr(core, "_get_router", AsyncMock(return_value=router))
    monkeypatch.setattr(core, "_get_group_id", AsyncMock(return_value=1))
    client = TestClient(core.app)
    client.router = router
    yield client
    asyncio.get_event_loop().run_until_complete(db.close())
    core._db = None


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


def test_rate_limit_exceeded(client):
    payload = {"model": "gpt-4", "messages": [{"role": "user", "content": "x"}]}
    headers = {"authorization": "Bearer test-key"}
    last = None
    for _ in range(25):
        last = client.post("/v1/chat/completions", json=payload, headers=headers)
    assert last.status_code in (200, 429)


def test_auth_middleware_unknown_key(client):
    payload = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}
    r = client.post("/v1/chat/completions", json=payload, headers={"authorization": "Bearer unknown"})
    assert r.status_code == 401


async def test_get_router_real(tmp_path, monkeypatch):
    from botflow.storage.db import Database
    from botflow.storage.models import Provider, ModelGroup
    from botflow.router import GroupRouter
    db = Database(tmp_path / "d.db")
    await db.initialize()
    await db.create_provider(Provider(name="p", provider_type="openai", api_key="k", base_url="http://x"))
    gid = await db.create_group(ModelGroup(name="g1"))
    core._db = db
    try:
        r = await core._get_router(gid)
        assert isinstance(r, GroupRouter)
        assert r.group_id == gid
    finally:
        core._db = None


def test_chat_completions_error_propagates_502(client):
    from botflow.common.exceptions import ProviderError
    client.router.exc = ProviderError("HTTP 500 boom")
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
