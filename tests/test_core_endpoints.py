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
    """SG-1 §3.3：轻量 PipelineEngine 替身，单入口 ``run`` / ``stream_events``。
    非流式返回最终 result dict；流式返回 ("chunk", c)/("state", s) 事件序列。"""

    def __init__(self):
        self.kwargs = None
        self.exc = None
        self.cooldown = CooldownManager()

    async def route(self, group, messages, temperature=None, max_tokens=None, stream=False, **kwargs):
        # 保留以兼容非流式端点路径
        self.kwargs = kwargs
        if self.exc is not None:
            raise self.exc
        return dict(NON_STREAM_RESPONSE)

    async def run(self, strategy, group, mode, **kwargs):
        """SG-1 单入口：非流式返回 result dict；流式返回事件 async gen。"""
        self.kwargs = kwargs
        if self.exc is not None:
            raise self.exc
        if mode == "stream":
            return self._stream_events(kwargs.get("messages"))
        return dict(NON_STREAM_RESPONSE)

    async def stream_events(self, strategy, group, **kwargs):
        return self._stream_events(kwargs.get("messages"))

    def _stream_events(self, messages=None):
        async def _gen():
            # 首个（也是唯一的）delta 带 role 且 finish_reason="stop" —— 真实 provider
            # 的「完整短流」就是这个形状：
            #   * role 让 anthropic 序列化产出 message_start（其中含被传输层覆盖后的
            #     model 名，T5.1 断言它出现）；
            #   * finish_reason 让 responses 序列化产出 response.completed 终态事件。
            yield ("chunk", {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}]})
            # 已推过 chunk 的流是「已提交」的，recoverable 必须为 False，
            # 否则 _drive_stream 会当成「组级失败」去降级（把成功流改写成 error SSE）。
            yield ("state", {"recoverable": False, "used_model_id": 1, "provider_id": 2})
        return _gen()


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


# ---------------------------------------------------------------------------
# SG-1 F5 (T5.1) + F7 (T7.1)：传输层分离 / 归一
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint, payload_extra, terminator", [
    ("/v1/chat/completions", {}, "data: [DONE]"),
    ("/v1/completions", {"prompt": "hi"}, "data: [DONE]"),
    ("/v1/messages", {}, "data: [DONE]"),
    # Responses API 的终态是 response.completed，且**不发** OpenAI 的 [DONE]。
    ("/v1/responses", {}, "event: response.completed"),
])
def test_stream_events_serialize_all_four_protocols(client, endpoint, payload_extra, terminator):
    """T5.1 正例(F5)：4 种协议各一条流式 → ① 响应含请求 model 名（传输层覆盖为请求 model）；
    ② 终态信号在最末且仅一次；③ final_state 被消费（驱动读到 recoverable/used_model_id）。

    ⚠️ `/v1/responses` 是唯一例外：其终态事件为 `response.completed`，**不发** `[DONE]`
    （OpenAI Responses 语义）。原任务单把 4 条协议都写成「[DONE] 在最末」属笔误 ——
    以既有守卫 `test_core_runtime.test_stream_responses_emits_response_events`
    （断言 `"[DONE]" not in joined`）为准，本用例按协议区分终态信号。
    """
    payload = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    payload.update(payload_extra)
    headers = {"authorization": "Bearer test-key"}
    with client.stream("POST", endpoint, json=payload, headers=headers) as r:
        assert r.status_code == 200
        lines = [ln for ln in r.iter_lines() if ln.strip()]
    joined = "\n".join(lines)
    # 终态信号出现且只出现一次
    assert terminator in lines
    assert sum(1 for ln in lines if ln == terminator) == 1
    if terminator == "data: [DONE]":
        assert lines[-1] == "data: [DONE]"
    else:
        # SSE 每事件两行（`event: X` + `data: {...}`）→ 终态 event 行后紧跟它自己的 data 行
        assert "[DONE]" not in joined
        assert lines[-1].startswith("data: ")
        assert lines[-2] == terminator
    # 传输层把 model 覆盖为请求的 model 名（至少响应文本含请求 model）
    assert "gpt-4" in joined


def test_non_stream_response_byte_identical(client):
    """T7.1 正例(F7)：固定请求 + 固定 stub 上游 → 非流式响应与改动前的「黄金响应」逐字段一致。

    golden 按 spec 的两处豁免构造（不做「关键字段抽查」，仍是整 dict 比对）：
      * `created`：时间戳类字段（每次请求不同），两侧同时剔除后再比；
      * `model`：驱动层按 T5.1 语义覆盖为**请求的 model 名**（`gpt-4`），
        而非 stub 上游自称的 `m`；`object` 由响应格式化器补写。
    """
    golden = {k: v for k, v in NON_STREAM_RESPONSE.items() if k != "_routing"}
    golden.pop("created", None)   # 时间戳类字段，spec 明确豁免
    golden["model"] = "gpt-4"     # 传输层覆盖为请求 model（与 T5.1 同款语义）
    golden["object"] = "chat.completion"
    payload = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}
    r = client.post("/v1/chat/completions", json=payload, headers={"authorization": "Bearer test-key"})
    assert r.status_code == 200
    body = r.json()
    body.pop("created", None)
    assert body == golden, f"response drift vs golden:\n{body}\n!=\n{golden}"
