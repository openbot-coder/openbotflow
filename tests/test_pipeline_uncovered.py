"""补充覆盖：pipeline 层的分支缺口。

- ``langgraph_engine``：节点级分支（首轮 visited 追加、fallback 组缺失、
  fatal_error 短路、langgraph 策略拒绝、``finalize_error`` 的 Exception/dict 形态、
  ``_route_after_call`` 的 fatal_error 分支、``route()`` 在 graph 未产出 result 时的兜底）
- ``strategies``：RoundRobin / Sequential 的「无模型」「全部冷却」抛出
- ``_shared``：``_apply_model_extra_config`` 从 kwargs 剥离 reasoning 参数

节点级分支通过把真实节点函数装进最小的单节点 StateGraph 来触发——这样
``get_config()["configurable"]["ctx"]`` 仍由 LangGraph 运行时注入，测的是
节点自身的真实行为，而不是打桩绕过它。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from langgraph.graph import END, StateGraph

from botflow.common.exceptions import (
    AllModelsCooldownError,
    ConfigurationError,
    NoAvailableModelError,
    ProviderError,
)
from botflow.pipeline._shared import _apply_model_extra_config
from botflow.pipeline.langgraph_engine import (
    GraphContext,
    LangGraphEngine,
    RouteState,
    finalize_error,
)
from botflow.pipeline.strategies import (
    RandomWeightsStrategy,
    RoundRobinStrategy,
    SequentialStrategy,
)
from botflow.router import CooldownManager, ModelEndpoint
from botflow.storage.db import Database
from botflow.storage.models import GroupModelWithDetails, ModelGroup


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _group(gid: int = 1, type_: str = "random_weights", fallback_gid=None) -> ModelGroup:
    return ModelGroup(
        id=gid, name=f"g{gid}", type=type_, fallback_group_id=fallback_gid, params={}
    )


def _ep(model_id: int = 1) -> ModelEndpoint:
    detail = GroupModelWithDetails(
        id=model_id,
        group_id=1,
        model_id=model_id,
        weight=1.0,
        is_enabled=True,
        model_name=f"m{model_id}",
        display_name=f"m{model_id}",
        api_format="openai",
        provider_id=1,
        provider_name="p",
        provider_type="openai",
        max_retries=1,
        cooldown_seconds=30,
        cooldown_failure_threshold=3,
        context_window=8192,
    )
    return ModelEndpoint(model_detail=detail, provider_instance=MagicMock())


def _ctx(db=None) -> GraphContext:
    return GraphContext(
        db=db or MagicMock(spec=Database),
        cooldown=MagicMock(spec=CooldownManager),
    )


async def _invoke_node(node, state: dict, ctx: GraphContext) -> dict:
    """Run a single graph node through a minimal StateGraph (real ctx injection)."""
    graph = StateGraph(RouteState)
    graph.add_node("node", node)
    graph.set_entry_point("node")
    graph.add_edge("node", END)
    return await graph.compile().ainvoke(state, config={"configurable": {"ctx": ctx}})


# ---------------------------------------------------------------------------
# NOTE (SG-1 §3.2): 以下节点级用例已由图内组级降级迁到驱动 core._drive：
#   test_resolve_group_first_pass_appends_unvisited_group → T1.4（visited 由驱动维护）
#   test_resolve_group_fallback_group_not_found_is_fatal  → T1.7（备份组缺失 → ConfigurationError）
# 同时 §3.1 删除 fatal_error 短路用例（fatal_error 退出历史，配置错误改由
# recoverable=False 表达）：
#   test_load_and_select_short_circuits_when_fatal_error_present
#   test_load_and_select_rejects_langgraph_strategy → T6.4（拒绝迁到驱动步骤②）
#   test_route_after_call_fatal_error_routes_to_error（_route_after_call 的 fatal_error 分支消失）
# SG-1 后 _resolve_group / _load_and_select / _route_after_call 不再存在，
# 上述旧用例已删除，等价语义由 T1.x / T6.x 在驱动层与图出口覆盖。
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# finalize_error
# ---------------------------------------------------------------------------


async def testfinalize_error_uses_exception_message():
    out = await _invoke_node(finalize_error, {"error": RuntimeError("kaboom")}, _ctx())
    assert out["result"]["error"]["message"] == "kaboom"
    assert out["result"]["error"]["type"] == "server_error"


async def testfinalize_error_uses_dict_message():
    out = await _invoke_node(finalize_error, {"error": {"message": "dict msg"}}, _ctx())
    assert out["result"]["error"]["message"] == "dict msg"


# ---------------------------------------------------------------------------
# LangGraphEngine.route() — graph produced no "result"
# ---------------------------------------------------------------------------


class _StubGraph:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def ainvoke(self, state, config=None):  # noqa: ARG002
        return self._payload


def _engine() -> LangGraphEngine:
    return LangGraphEngine(
        db_factory=lambda: MagicMock(spec=Database),
        cooldown=MagicMock(spec=CooldownManager),
    )


async def test_route_without_result_raises_error_dict_message():
    engine = _engine()
    engine._graph = _StubGraph({"error": {"message": "graph failed"}})
    with pytest.raises(ProviderError, match="graph failed"):
        await engine.route(group=_group(), messages=[{"role": "user", "content": "hi"}])


async def test_route_without_result_raises_raw_exception_message():
    engine = _engine()
    engine._graph = _StubGraph({"error": RuntimeError("raw exc")})
    with pytest.raises(ProviderError, match="raw exc"):
        await engine.route(group=_group(), messages=[{"role": "user", "content": "hi"}])


async def test_route_without_result_and_without_error_raises_placeholder():
    engine = _engine()
    engine._graph = _StubGraph({})
    with pytest.raises(ProviderError, match="no result"):
        await engine.route(group=_group(), messages=[{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# strategies — empty / all-cooldown raise paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strategy_cls", [RoundRobinStrategy, SequentialStrategy])
async def test_sequential_family_raises_when_group_has_no_endpoints(monkeypatch, strategy_cls):
    monkeypatch.setattr(
        "botflow.pipeline._shared.load_endpoints", AsyncMock(return_value=[])
    )
    with pytest.raises(NoAvailableModelError, match="has no enabled models"):
        await strategy_cls({}).select_endpoints(
            messages=[],
            db=MagicMock(spec=Database),
            cooldown=MagicMock(spec=CooldownManager),
            group_id=3,
        )


@pytest.mark.parametrize("strategy_cls", [RoundRobinStrategy, SequentialStrategy])
async def test_sequential_family_raises_when_all_on_cooldown(monkeypatch, strategy_cls):
    monkeypatch.setattr(
        "botflow.pipeline._shared.load_endpoints", AsyncMock(return_value=[_ep(1)])
    )
    monkeypatch.setattr(
        "botflow.pipeline._shared.filter_available", lambda eps, cd, gid: []
    )
    with pytest.raises(AllModelsCooldownError, match="all models on cooldown"):
        await strategy_cls({}).select_endpoints(
            messages=[],
            db=MagicMock(spec=Database),
            cooldown=MagicMock(spec=CooldownManager),
            group_id=3,
        )


async def test_random_weights_returns_primary_plus_fallbacks(monkeypatch):
    """加权随机的正常路径（保证 strategies 模块整体仍为 100%）。"""
    eps = [_ep(1), _ep(2)]
    monkeypatch.setattr(
        "botflow.pipeline._shared.load_endpoints", AsyncMock(return_value=eps)
    )
    monkeypatch.setattr(
        "botflow.pipeline._shared.filter_available", lambda e, cd, gid: list(e)
    )
    result = await RandomWeightsStrategy({}).select_endpoints(
        messages=[{"role": "user", "content": "hi"}],
        db=MagicMock(spec=Database),
        cooldown=MagicMock(spec=CooldownManager),
        group_id=1,
    )
    assert len(result.endpoints) == 2
    assert {ep.model_id for ep in result.endpoints} == {1, 2}


# ---------------------------------------------------------------------------
# _shared._apply_model_extra_config
# ---------------------------------------------------------------------------


def test_apply_model_extra_config_strips_reasoning_from_kwargs():
    """kwargs 自带 reasoning_mode=off 时也要剥离，而不只是看 model 的 extra_config。"""
    out = _apply_model_extra_config(
        {"reasoning_mode": "off", "reasoning_effort": "high", "keep": 1},
        {"timeout": 5},
    )
    assert "reasoning_effort" not in out
    assert "reasoning_content" not in out
    assert out["keep"] == 1
    assert out["reasoning_mode"] == "off"


def test_apply_model_extra_config_strips_from_config_and_explicit_list():
    out = _apply_model_extra_config(
        {"reasoning_effort": "high", "logprobs": True, "keep": 2},
        {"reasoning_mode": "off", "strip_params": ["logprobs"]},
    )
    assert "reasoning_effort" not in out
    assert "logprobs" not in out
    assert out["keep"] == 2
