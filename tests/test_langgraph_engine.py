"""Tests for LangGraphEngine (async architecture).

Covers the new LangGraph-based routing engine:
- Non-streaming route via ainvoke (success, retry, fallback, error, finalize_error)
- Streaming route via ainvoke (endpoint selection, no LLM call)
- Fallback group cycle detection
- Kwargs forwarding

Mock strategy: patch ``load_endpoints`` (in ``botflow.pipeline._shared``)
+ ``call_llm`` (in ``botflow.pipeline.langgraph_engine``) — the graph
nodes call them via ainvoke.

Notes on the finalize_error path:
  In the new architecture, all endpoints exhausted → graph routes to
  ``finalize_error`` node, which returns ``{"result": {"error": {...}}}``.
  ``LangGraphEngine.route()`` sees ``result["result"]["error"]`` and
  raises ``ProviderError`` with the original error message.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from botflow.common.exceptions import (
    AllModelsCooldownError,
    ConfigurationError,
    NoAvailableModelError,
    ProviderError,
)
from botflow.pipeline.langgraph_engine import LangGraphEngine
from botflow.router import CooldownManager, ModelEndpoint
from botflow.storage.db import Database
from botflow.storage.models import GroupModelWithDetails, ModelGroup


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _engine() -> LangGraphEngine:
    cooldown = MagicMock(spec=CooldownManager)
    cooldown.is_on_cooldown.return_value = False
    return LangGraphEngine(db_factory=lambda: MagicMock(spec=Database),
                           cooldown=cooldown)


def _group(gid=1, type_="random_weights", fallback_gid=None):
    return ModelGroup(id=gid, name=f"g{gid}", type=type_,
                      fallback_group_id=fallback_gid, params={})


def _ep(model_name="m1", model_id=1, provider_id=10):
    """Create a ModelEndpoint with GroupModelWithDetails (Pydantic model)."""
    detail = GroupModelWithDetails(
        id=model_id,
        group_id=1,
        model_id=model_id,
        weight=1.0,
        is_enabled=True,
        model_name=model_name,
        display_name=model_name,
        api_format="openai",
        provider_id=provider_id,
        provider_name="test_provider",
        provider_type="openai",
        max_retries=1,
        cooldown_seconds=30,
        cooldown_failure_threshold=3,
        context_window=8192,
    )
    mock_provider = MagicMock()
    return ModelEndpoint(model_detail=detail, provider_instance=mock_provider)


# ---------------------------------------------------------------------------
# Non-streaming: success (basic path through the graph)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_route_success():
    engine = _engine()
    group = _group()
    ep = _ep()
    llm_resp = {"choices": [{"message": {"content": "hi"}}], "_routing": {"model_id": 1}}

    with patch("botflow.pipeline._shared.load_endpoints", new_callable=AsyncMock, return_value=[ep]), \
         patch("botflow.pipeline._shared.filter_available", new=lambda eps, cd, gid: eps), \
         patch("botflow.pipeline.langgraph_engine.call_llm", new_callable=AsyncMock, return_value=llm_resp):
        result = await engine.route(group=group, messages=[{"role": "user", "content": "hi"}])

    assert result["choices"][0]["message"]["content"] == "hi"
    assert result["_routing"]["model_id"] == 1


# ---------------------------------------------------------------------------
# Non-streaming: first endpoint fails → next endpoint succeeds (next_ep)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_route_first_fails_second_succeeds():
    engine = _engine()
    group = _group()
    ep_a = _ep("a", model_id=1)
    ep_b = _ep("b", model_id=2)
    resp_b = {"choices": [{"message": {"content": "ok"}}], "_routing": {"model_id": 2}}

    with patch("botflow.pipeline._shared.load_endpoints", new_callable=AsyncMock, return_value=[ep_a, ep_b]), \
         patch("botflow.pipeline._shared.filter_available", new=lambda eps, cd, gid: eps), \
         patch("botflow.pipeline.langgraph_engine.call_llm", new_callable=AsyncMock, side_effect=[None, resp_b]):
        result = await engine.route(group=group, messages=[])

    assert result["_routing"]["model_id"] == 2


# ---------------------------------------------------------------------------
# Non-streaming: all endpoints fail, no fallback → finalize_error path
#   The graph routes to _finalize_error node; LangGraphEngine.route()
#   raises ProviderError with the error message.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_route_all_endpoints_fail_finalize_error():
    engine = _engine()
    group = _group()  # no fallback_group_id

    with patch("botflow.pipeline._shared.load_endpoints", new_callable=AsyncMock, return_value=[_ep()]), \
         patch("botflow.pipeline._shared.filter_available", new=lambda eps, cd, gid: eps), \
         patch("botflow.pipeline.langgraph_engine.call_llm", new_callable=AsyncMock, return_value=None):
        with pytest.raises(ProviderError) as exc_info:
            await engine.route(group=group, messages=[])

    assert "No fallback group available" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Non-streaming: fallback group → second group succeeds
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_route_fallback_group_succeeds():
    engine = _engine()
    primary_group = _group(gid=1, fallback_gid=2)
    fallback_group = _group(gid=2)

    ep_primary = _ep("primary", model_id=1)
    ep_fallback = _ep("fallback", model_id=2)
    resp = {"choices": [{"message": {"content": "fallback ok"}}], "_routing": {"model_id": 2}}

    # Group 1 → ep_primary, Group 2 → ep_fallback
    async def fake_load_endpoints(gid, db):
        return [ep_primary] if gid == 1 else [ep_fallback]

    # db.get_group for the fallback pass (resolve_group loads group 2)
    mock_db = MagicMock(spec=Database)
    mock_db.get_group = AsyncMock(return_value=fallback_group)
    engine._db_factory = lambda: mock_db

    # ep_primary call fails (None); ep_fallback call succeeds
    async def fake_call_llm(ep, messages, group_id, cooldown, temperature=None, max_tokens=None, **kwargs):
        return None if ep.model_id == 1 else resp

    with patch("botflow.pipeline._shared.load_endpoints", new=fake_load_endpoints), \
         patch("botflow.pipeline._shared.filter_available", new=lambda eps, cd, gid: eps), \
         patch("botflow.pipeline.langgraph_engine.call_llm", new=fake_call_llm):
        result = await engine.route(group=primary_group, messages=[{"role": "user", "content": "hi"}])

    assert result["_routing"]["model_id"] == 2


# ---------------------------------------------------------------------------
# Non-streaming: fallback cycle detection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_route_fallback_cycle_detected():
    engine = _engine()
    group_a = _group(gid=1, fallback_gid=2)
    group_b = _group(gid=2, fallback_gid=1)

    mock_db = MagicMock(spec=Database)
    mock_db.get_group = AsyncMock(side_effect=lambda gid: {1: group_a, 2: group_b}[gid])
    engine._db_factory = lambda: mock_db

    with patch("botflow.pipeline._shared.load_endpoints", new_callable=AsyncMock, return_value=[_ep()]), \
         patch("botflow.pipeline._shared.filter_available", new=lambda eps, cd, gid: eps), \
         patch("botflow.pipeline.langgraph_engine.call_llm", new_callable=AsyncMock, return_value=None):
        with pytest.raises(ProviderError) as exc_info:
            await engine.route(group=group_a, messages=[])

    assert "No fallback group available" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Non-streaming: fallback depth limit (max 3)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_route_fallback_depth_limit():
    engine = _engine()

    groups = {
        i: _group(gid=i, fallback_gid=i + 1 if i < 5 else None)
        for i in range(1, 6)
    }

    mock_db = MagicMock(spec=Database)
    mock_db.get_group = AsyncMock(side_effect=lambda gid: groups[gid])
    engine._db_factory = lambda: mock_db

    with patch("botflow.pipeline._shared.load_endpoints", new_callable=AsyncMock, return_value=[_ep()]), \
         patch("botflow.pipeline._shared.filter_available", new=lambda eps, cd, gid: eps), \
         patch("botflow.pipeline.langgraph_engine.call_llm", new_callable=AsyncMock, return_value=None):
        with pytest.raises(ProviderError) as exc_info:
            await engine.route(group=groups[1], messages=[])

    assert "Fallback chain too deep" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Streaming: graph only selects endpoints, no LLM call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_route_stream_selects_endpoints_only():
    engine = _engine()
    group = _group()
    ep = _ep()

    with patch("botflow.pipeline._shared.load_endpoints", new_callable=AsyncMock, return_value=[ep]), \
         patch("botflow.pipeline._shared.filter_available", new=lambda eps, cd, gid: eps), \
         patch("botflow.pipeline.langgraph_engine.call_llm", new_callable=AsyncMock) as mock_llm:
        result = await engine.route_stream(group=group, messages=[{"role": "user", "content": "hi"}])

    # call_llm must NOT have been called — graph short-circuits at load_and_select
    mock_llm.assert_not_called()
    assert "endpoints" in result
    assert result["group_id"] == 1
    assert len(result["endpoints"]) == 1


# ---------------------------------------------------------------------------
# Streaming: no available endpoints → NoAvailableModelError
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_route_stream_no_endpoints_raises():
    engine = _engine()
    group = _group()

    with patch("botflow.pipeline._shared.load_endpoints", new_callable=AsyncMock, return_value=[]):
        with pytest.raises(NoAvailableModelError):
            await engine.route_stream(group=group, messages=[])


# ---------------------------------------------------------------------------
# Non-streaming: extra kwargs forwarded to call_llm
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_route_forwards_extra_kwargs():
    engine = _engine()
    group = _group()
    ep = _ep()
    resp = {"choices": [{"message": {"content": "ok"}}], "_routing": {}}

    async def check_call_llm(ep, messages, group_id, cooldown, temperature=None, max_tokens=None, **kwargs):
        assert kwargs.get("reasoning_effort") == "high"
        assert kwargs.get("top_p") == 0.9
        return resp

    with patch("botflow.pipeline._shared.load_endpoints", new_callable=AsyncMock, return_value=[ep]), \
         patch("botflow.pipeline._shared.filter_available", new=lambda eps, cd, gid: eps), \
         patch("botflow.pipeline.langgraph_engine.call_llm", side_effect=check_call_llm):
        result = await engine.route(group=group, messages=[],
                                    reasoning_effort="high", top_p=0.9)

    assert result["choices"][0]["message"]["content"] == "ok"


# ---------------------------------------------------------------------------
# Non-streaming: temperature/max_tokens forwarded correctly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_route_temperature_max_tokens_forwarded():
    engine = _engine()
    group = _group()
    ep = _ep()
    resp = {"choices": [], "_routing": {}}

    async def check_args(ep, messages, group_id, cooldown, temperature=None, max_tokens=None, **kw):
        assert temperature == 0.7
        assert max_tokens == 256
        return resp

    with patch("botflow.pipeline._shared.load_endpoints", new_callable=AsyncMock, return_value=[ep]), \
         patch("botflow.pipeline._shared.filter_available", new=lambda eps, cd, gid: eps), \
         patch("botflow.pipeline.langgraph_engine.call_llm", side_effect=check_args):
        await engine.route(group=group, messages=[], temperature=0.7, max_tokens=256)
