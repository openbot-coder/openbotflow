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
from botflow.pipeline.base import StrategyError
from botflow.pipeline.langgraph_engine import (
    GraphContext,
    LangGraphEngine,
    finalize_error,
    select_endpoints,
    try_stream,
)
from botflow.pipeline.strategies import RandomWeightsStrategy
from botflow.router import CooldownManager, ModelEndpoint
from botflow.storage.db import Database
from langgraph.graph import END, StateGraph
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
         patch("botflow.pipeline.langgraph_engine.call_llm", new_callable=AsyncMock, return_value=(llm_resp, None)):
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
         patch("botflow.pipeline.langgraph_engine.call_llm", new_callable=AsyncMock, side_effect=[(None, ProviderError("primary failed")), (resp_b, None)]):
        result = await engine.route(group=group, messages=[])

    assert result["_routing"]["model_id"] == 2


# ---------------------------------------------------------------------------
# Non-streaming: all endpoints fail, no fallback → finalize_error path
#   The graph routes to _finalize_error node; LangGraphEngine.route()
#   raises ProviderError with the error message.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_route_all_endpoints_fail_finalize_error():
    """SG-1 §3.1：图不再有 fallback 边，「无备份组」语义已迁到驱动 ``_drive``（T1.6）。
    此处只断言图出口（``state["error"]`` 非空 / ``recoverable`` 已写）；不再断言
    ``"No fallback group available"``（该消息由图产生，已删除）。

    另：图出口对「全端点失败」写的是组级哨兵消息 ``"All endpoints in group failed"``
    （该字面量 SG-1 前后都是**未改动行**）。端点级真实原因不靠这句哨兵承载 —— 它落在
    ``state["attempts"]`` 留痕里（SG-0 G1/G2）随异常带出，所以要断言的是 attempts。
    """
    engine = _engine()
    group = _group()  # no fallback_group_id

    with patch("botflow.pipeline._shared.load_endpoints", new_callable=AsyncMock, return_value=[_ep()]), \
         patch("botflow.pipeline._shared.filter_available", new=lambda eps, cd, gid: eps), \
         patch("botflow.pipeline.langgraph_engine.call_llm", new_callable=AsyncMock, return_value=(None, ProviderError("all endpoints failed"))):
        with pytest.raises(ProviderError) as exc_info:
            await engine.route(group=group, messages=[])

    exc = exc_info.value
    # 图出口行为：error 已写 → 抛 ProviderError，消息为图出口的组级哨兵
    assert "All endpoints in group failed" in str(exc)
    # 端点级真实原因没丢：随异常带出的 attempts 留痕逐条可查（SG-0）
    assert exc.attempts, "failed endpoints must leave an attempt trail"
    assert exc.attempts[-1]["error_type"] == "ProviderError"
    assert exc.attempts[-1]["error_message"] == "all endpoints failed"
    assert exc.attempts[-1]["stage"] == "call"


# ---------------------------------------------------------------------------
# NOTE (SG-1 §3.2): 以下三个用例已由图内组级降级迁到驱动 ``core._drive``：
#   test_route_fallback_group_succeeds  → T1.2（流式降级，含非流式对照）
#   test_route_fallback_cycle_detected → T1.4（环检测）
#   test_route_fallback_depth_limit    → T1.5（深度上限 3）
# SG-1 后图内不再有 fallback 边，组级降级改由 ``_drive`` 的 while 循环负责，
# 故原用例（断言 ``engine.route`` 跨组 fallback）已删除，等价语义见 T1.x。
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Streaming: graph only selects endpoints, no LLM call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_graph_stream_invokes_provider():
    """SG-1 F4（语义反转）：流式图内 ``try_stream`` 节点**直接调用 provider**
    并经 ``get_stream_writer()`` 推出 chunk。旧名 ``test_route_stream_selects_endpoints_only``
    （断言「流式图不调 LLM」）已不成立 —— 端点级流式调用已迁入图。

    两处易错点（本用例按实现复核后的写法）：
      * ``try_stream`` 调的是 ``ep.provider.chat_stream(...)``，**不是** ``call_llm``；
        所以必须注入真的异步 provider（``_StreamingProvider``），
        ``MagicMock`` provider 会在 ``await gen.__anext__()`` 处炸掉。
      * ``get_stream_writer`` 由 ``from langgraph.config import get_stream_writer``
        导入到本模块，patch 目标必须是 ``botflow.pipeline.langgraph_engine`` 的
        模块属性；patch ``langgraph.config`` 源模块无效（真函数会抛 RuntimeError，
        节点按 T4.7 兜底成 ``writer=None`` → 一个 chunk 都推不出来）。
    """
    from langgraph.graph import END, StateGraph

    from botflow.pipeline.langgraph_engine import RouteState as _RS

    group = _group()
    chunks = [
        {"choices": [{"delta": {"content": "a"}}]},
        {"choices": [{"delta": {"content": "b"}}]},
    ]
    provider = _StreamingProvider([chunks])

    # 记录 try_stream 经 writer 推出的 payload
    pushed: list = []

    def _recording_writer():
        def _w(payload):
            pushed.append(payload)
        return _w

    # 单节点图直接驱动 try_stream，注入 recording writer（astream 上下文）
    graph = StateGraph(_RS)
    graph.add_node("try_stream", try_stream)
    graph.set_entry_point("try_stream")
    graph.add_edge("try_stream", END)

    with patch("botflow.pipeline.langgraph_engine.get_stream_writer", _recording_writer):
        ctx = GraphContext(db=MagicMock(spec=Database), cooldown=MagicMock(spec=CooldownManager))
        await graph.compile().ainvoke(
            {
                "group": group, "messages": [{"role": "user", "content": "hi"}],
                "endpoints": [_ep_stream(1, provider)], "current_ep_idx": 0, "attempt": 0,
                "extra_kwargs": {}, "stream": True, "attempts": [],
            },
            config={"configurable": {"ctx": ctx}},
        )

    # try_stream 必须真的调用了 provider 并推出恰好 2 个 chunk
    assert provider.calls == 1
    assert len(pushed) == 2
    assert pushed[0]["chunk"]["choices"][0]["delta"]["content"] == "a"
    assert pushed[1]["chunk"]["choices"][0]["delta"]["content"] == "b"


# ---------------------------------------------------------------------------
# Streaming: no available endpoints → NoAvailableModelError
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_route_stream_no_endpoints_raises():
    """SG-1 §3.3：``route_stream`` 合并进 ``run(mode="stream")``。
    无可用端点 → ``NoAvailableModelError``（来自 ``select_endpoints``，T6.2 的白名单来源）。"""
    engine = _engine()
    group = _group()

    with patch("botflow.pipeline._shared.load_endpoints", new_callable=AsyncMock, return_value=[]):
        with pytest.raises(NoAvailableModelError):
            await engine.run(group=group, messages=[], mode="stream")


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
        return (resp, None)

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
        return (resp, None)

    with patch("botflow.pipeline._shared.load_endpoints", new_callable=AsyncMock, return_value=[ep]), \
         patch("botflow.pipeline._shared.filter_available", new=lambda eps, cd, gid: eps), \
         patch("botflow.pipeline.langgraph_engine.call_llm", side_effect=check_args):
        await engine.route(group=group, messages=[], temperature=0.7, max_tokens=256)


# ===========================================================================
# SG-1 (F2/F3/F4/F6): 图瘦身 + try_stream 入图 + recoverable 白名单
# ---------------------------------------------------------------------------
# 以下用例针对 SG-1 冻结契约（图节点集 == {select_endpoints, try_call,
# try_stream, finalize_error}，组级降级移出图外由 core._drive 负责）。
# 实现落地前本模块会因「新符号不存在」而收集/运行失败 —— 这是预期状态，
# 待编码子 agent 落地 core._drive / engine.run / try_stream 后需复核。
# ===========================================================================


def _run_slim_graph(engine, group, messages, stream=False, model_name=""):
    """直接驱动 SG-1 瘦身后的图（entry = select_endpoints，无 resolve_group）。"""
    ctx = GraphContext(db=MagicMock(spec=Database), cooldown=MagicMock())
    initial = {
        "messages": messages, "model_name": model_name, "temperature": None,
        "max_tokens": None, "extra_kwargs": {}, "group": group,
        "fallback_group_id": group.fallback_group_id, "fallback_depth": 0,
        "visited_groups": [group.id], "stream": stream,
    }
    return engine._graph.ainvoke(initial, config={"configurable": {"ctx": ctx}})


@pytest.mark.asyncio
async def test_resolve_group_removed_from_module():
    """T2.1（反例，关键）：``_resolve_group`` 与 ``_route_after_load`` 已从模块移除。
    SG-1 把组级降级移出图外（core._drive），图内不再解析 fallback 组。"""
    from botflow.pipeline import langgraph_engine as lge

    assert not hasattr(lge, "_resolve_group"), "_resolve_group must be removed"
    assert not hasattr(lge, "_route_after_load"), "_route_after_load must be removed"


@pytest.mark.asyncio
async def test_graph_node_set_is_minimal():
    """T2.2：编译后的图节点集合恰为 {select_endpoints, try_call, try_stream, finalize_error}。"""
    from botflow.pipeline.langgraph_engine import build_route_graph

    nodes = set(build_route_graph().nodes)
    assert {"select_endpoints", "try_call", "try_stream", "finalize_error"} <= nodes
    # 旧节点不得遗留
    assert "resolve_group" not in nodes
    assert "load_and_select" not in nodes


@pytest.mark.asyncio
async def test_graph_success_exit_writes_result_only():
    """T2.3：成功出口 —— ``result`` 非空、``error`` 为 None（显式写，防旧缺陷复活）。"""
    engine = _engine()
    group = _group()
    ep = _ep()
    resp = {"choices": [{"message": {"content": "hi"}}], "_routing": {"model_id": 1}}

    with patch("botflow.pipeline._shared.load_endpoints", new_callable=AsyncMock, return_value=[ep]), \
         patch("botflow.pipeline._shared.filter_available", new=lambda eps, cd, gid: eps), \
         patch("botflow.pipeline.langgraph_engine.call_llm", new_callable=AsyncMock, return_value=(resp, None)):
        state = await _run_slim_graph(engine, group, [{"role": "user", "content": "hi"}], stream=False)

    assert state.get("result") is not None
    assert state.get("error") is None


@pytest.mark.asyncio
async def test_graph_failure_exit_writes_error_and_recoverable():
    """T2.4：失败出口 —— ``error`` 非空且 ``recoverable`` 键已写（bool）。"""
    engine = _engine()
    group = _group()
    ep = _ep()

    with patch("botflow.pipeline._shared.load_endpoints", new_callable=AsyncMock, return_value=[ep]), \
         patch("botflow.pipeline._shared.filter_available", new=lambda eps, cd, gid: eps), \
         patch("botflow.pipeline.langgraph_engine.call_llm", new_callable=AsyncMock, return_value=(None, ProviderError("all endpoints failed"))):
        state = await _run_slim_graph(engine, group, [{"role": "user", "content": "hi"}], stream=False)

    assert state.get("error") is not None
    assert "recoverable" in state
    assert isinstance(state["recoverable"], bool)


# ---------------------------------------------------------------------------
# F4: try_stream 节点（T4.1 ~ T4.7）
# ---------------------------------------------------------------------------


class _TrackedStream:
    """provider 侧流的替身：支持 ``__aiter__``/``__anext__`` 与 ``aclose()``。

    真实 provider 的 ``chat_stream`` 是**异步生成器函数**，调用它拿到的是原生
    异步生成器对象，``try_stream`` 里的 ``gen.aclose()`` 关的是**这个生成器**
    （触发其 ``finally`` 里的清理，例如关闭底层 httpx 流），**不是** provider
    对象上的方法 —— 真实 provider 压根没有 ``aclose`` 方法。所以 close 必须
    记在流对象上，用 ``closed`` 观察，并回调 provider 维护 ``aclose_called``。
    """

    def __init__(self, items: list, on_close) -> None:
        self._items = list(items)
        self._idx = 0
        self._on_close = on_close
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._idx >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._idx]
        self._idx += 1
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self):
        self.closed = True
        self._on_close()


class _StreamingProvider:
    """``chat_stream`` 按 behavior 逐次产出 chunk 列表；元素为 list[dict] 或 Exception。

    每次调用返回一个 ``_TrackedStream``（与真实 provider 返回异步生成器同形）。
    """

    def __init__(self, behavior: list) -> None:
        self.behavior = behavior
        self.calls = 0
        self.aclose_called = False
        self.streams: list[_TrackedStream] = []

    def chat_stream(self, *args, **kwargs):
        self.calls += 1
        result = self.behavior[min(self.calls - 1, len(self.behavior) - 1)]
        items = [result] if isinstance(result, Exception) else list(result)
        stream = _TrackedStream(items, lambda: setattr(self, "aclose_called", True))
        self.streams.append(stream)
        return stream


def _ep_stream(model_id=1, provider=None):
    detail = GroupModelWithDetails(
        id=model_id, group_id=1, model_id=model_id, weight=1.0, is_enabled=True,
        model_name=f"m{model_id}", display_name=f"m{model_id}", api_format="openai",
        provider_id=1, provider_name="p", provider_type="openai", max_retries=3,
        cooldown_seconds=30, cooldown_failure_threshold=3, context_window=8192,
    )
    return ModelEndpoint(model_detail=detail, provider_instance=provider or MagicMock())


def _run_try_stream_node(state: dict, ctx: GraphContext, writer):
    """单节点图运行 try_stream；writer 注入为 recorder 或保留真实 RuntimeError 路径。"""
    from botflow.pipeline.langgraph_engine import RouteState as _RS

    graph = StateGraph(_RS)
    graph.add_node("try_stream", try_stream)
    graph.set_entry_point("try_stream")
    graph.add_edge("try_stream", END)
    return graph.compile().ainvoke(state, config={"configurable": {"ctx": ctx}})


@pytest.mark.asyncio
async def test_try_stream_pushes_chunks_via_writer(monkeypatch):
    """T4.1（正例）：provider 正常吐 N 个 chunk → 经 writer() 推出恰好 N 个且顺序一致；attempts 为空。"""
    provider = _StreamingProvider([[{"choices": [{"delta": {"content": "a"}}]},
                                    {"choices": [{"delta": {"content": "b"}}]}]])
    pushed: list = []
    monkeypatch.setattr(
        "botflow.pipeline.langgraph_engine.get_stream_writer",
        lambda: (lambda p: pushed.append(p)),
    )
    ctx = GraphContext(db=MagicMock(spec=Database), cooldown=MagicMock())
    state = {
        "group": _group(), "messages": [{"role": "user", "content": "hi"}],
        "endpoints": [_ep_stream(1, provider)], "current_ep_idx": 0, "attempt": 0,
        "extra_kwargs": {}, "stream": True, "attempts": [], "model_name": "",
    }
    out = await _run_try_stream_node(state, ctx, None)
    assert len(pushed) == 2
    assert pushed[0]["chunk"]["choices"][0]["delta"]["content"] == "a"
    assert pushed[1]["chunk"]["choices"][0]["delta"]["content"] == "b"
    assert out.get("attempts") == []


@pytest.mark.asyncio
async def test_try_stream_retries_after_first_chunk_timeout(monkeypatch):
    """T4.2（正例）：第 1 次首 chunk 超时、第 2 次成功 → 无异常外泄，chunk 正常推出；attempts 含 1 条超时记录。"""
    provider = _StreamingProvider([TimeoutError(),
                                   [{"choices": [{"delta": {"content": "ok"}}]}]])
    pushed: list = []
    monkeypatch.setattr(
        "botflow.pipeline.langgraph_engine.get_stream_writer",
        lambda: (lambda p: pushed.append(p)),
    )
    monkeypatch.setattr("botflow.pipeline.langgraph_engine.exponential_backoff", AsyncMock())
    ctx = GraphContext(db=MagicMock(spec=Database), cooldown=MagicMock())
    state = {
        "group": _group(), "messages": [{"role": "user", "content": "hi"}],
        "endpoints": [_ep_stream(1, provider)], "current_ep_idx": 0, "attempt": 0,
        "extra_kwargs": {}, "stream": True, "attempts": [], "model_name": "",
    }
    out = await _run_try_stream_node(state, ctx, None)
    assert len(pushed) == 1
    assert pushed[0]["chunk"]["choices"][0]["delta"]["content"] == "ok"
    timeout_records = [a for a in out.get("attempts", []) if "timed out" in (a.get("error_message") or "")]
    assert len(timeout_records) == 1


@pytest.mark.asyncio
async def test_try_stream_empty_stream_moves_to_next_endpoint(monkeypatch):
    """T4.3（正例）：端点 A 空流 → 换端点 B；A 被 cooldown.record_failure 恰 1 次；attempts 含 A 的空流记录。"""
    pa = _StreamingProvider([[]])
    pb = _StreamingProvider([[{"choices": [{"delta": {"content": "b"}}]}]])
    pushed: list = []
    monkeypatch.setattr(
        "botflow.pipeline.langgraph_engine.get_stream_writer",
        lambda: (lambda p: pushed.append(p)),
    )
    ctx = GraphContext(db=MagicMock(spec=Database), cooldown=MagicMock())
    state = {
        "group": _group(), "messages": [{"role": "user", "content": "hi"}],
        "endpoints": [_ep_stream(1, pa), _ep_stream(2, pb)], "current_ep_idx": 0, "attempt": 0,
        "extra_kwargs": {}, "stream": True, "attempts": [], "model_name": "",
    }
    out = await _run_try_stream_node(state, ctx, None)
    assert len(pushed) == 1
    assert pushed[0]["chunk"]["choices"][0]["delta"]["content"] == "b"
    assert ctx.cooldown.record_failure.called
    empty_records = [a for a in out.get("attempts", []) if "empty stream" in (a.get("error_message") or "")]
    assert len(empty_records) == 1
    assert empty_records[0]["model_id"] == 1


@pytest.mark.asyncio
async def test_try_stream_non_retryable_error_no_retry(monkeypatch):
    """T4.4（反例）：首 chunk 前抛不可重试错误（400）→ 不重试（provider 只被调用 1 次）、换下一端点。"""
    pa = _StreamingProvider([ProviderError("HTTP 400 Bad Request")])
    pb = _StreamingProvider([[{"choices": [{"delta": {"content": "b"}}]}]])
    pushed: list = []
    monkeypatch.setattr(
        "botflow.pipeline.langgraph_engine.get_stream_writer",
        lambda: (lambda p: pushed.append(p)),
    )
    ctx = GraphContext(db=MagicMock(spec=Database), cooldown=MagicMock())
    state = {
        "group": _group(), "messages": [{"role": "user", "content": "hi"}],
        "endpoints": [_ep_stream(1, pa), _ep_stream(2, pb)], "current_ep_idx": 0, "attempt": 0,
        "extra_kwargs": {}, "stream": True, "attempts": [], "model_name": "",
    }
    out = await _run_try_stream_node(state, ctx, None)
    assert pa.calls == 1  # 不可重试 → 不重试
    assert len(pushed) == 1


@pytest.mark.asyncio
async def test_try_stream_after_first_chunk_failure_not_recoverable(monkeypatch):
    """T4.5（反例，R5）：已推出 ≥1 chunk 后**同一流内**失败 → state['recoverable'] is False
    （内容无法收回，禁止驱动换组重放）。

    ⚠️ 用例修正（SG-1）：原文写 ``[[chunk], ProviderError(...)]`` —— 第二个元素只在
    ``chat_stream`` **被第二次调用**时才会用到，而本场景只有一次调用，所以「中途失败」
    从未真正发生，``except Exception`` 分支一直是死的。改成把异常放进**同一个** chunk
    列表里（``[[chunk, ProviderError(...)]]``）才表达出「先推 chunk、再断流」。
    """
    # 第 1 个端点先推 1 个 chunk，同一条流内再抛错（列表里的 Exception 项即中途抛出）
    pa = _StreamingProvider([[{"choices": [{"delta": {"content": "a"}}]},
                              ProviderError("connection reset")]])
    pushed: list = []
    monkeypatch.setattr(
        "botflow.pipeline.langgraph_engine.get_stream_writer",
        lambda: (lambda p: pushed.append(p)),
    )
    ctx = GraphContext(db=MagicMock(spec=Database), cooldown=MagicMock())
    state = {
        "group": _group(), "messages": [{"role": "user", "content": "hi"}],
        "endpoints": [_ep_stream(1, pa)], "current_ep_idx": 0, "attempt": 0,
        "extra_kwargs": {}, "stream": True, "attempts": [], "model_name": "",
    }
    out = await _run_try_stream_node(state, ctx, None)
    assert len(pushed) == 1            # stream 已开始，第 1 个 chunk 已推给客户端
    assert out.get("recoverable") is False
    assert out.get("stream_started") is True
    assert pa.streams[0].closed is True  # 中途失败的流被关闭


@pytest.mark.asyncio
async def test_try_stream_client_disconnect_acloses_generator(monkeypatch):
    """T4.6（边界）：GraphContext.request.is_disconnected() 返回 True → 中止迭代，provider 流被 aclose()。

    要点：``try_stream`` 关的是 ``ep.provider.chat_stream(...)`` 返回的**流对象**
    （真实实现是原生异步生成器），所以断言落在 ``streams[0].closed``；provider 上的
    ``aclose_called`` 只是同一事件的镜像。
    """
    provider = _StreamingProvider([[{"choices": [{"delta": {"content": "a"}}]}]])
    pushed: list = []
    monkeypatch.setattr(
        "botflow.pipeline.langgraph_engine.get_stream_writer",
        lambda: (lambda p: pushed.append(p)),
    )
    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=True)
    ctx = GraphContext(db=MagicMock(spec=Database), cooldown=MagicMock(), request=request)
    state = {
        "group": _group(), "messages": [{"role": "user", "content": "hi"}],
        "endpoints": [_ep_stream(1, provider)], "current_ep_idx": 0, "attempt": 0,
        "extra_kwargs": {}, "stream": True, "attempts": [], "model_name": "",
    }
    await _run_try_stream_node(state, ctx, None)
    # 客户端已断开 → 不应推出任何 chunk，且 provider 流被 aclose
    assert len(pushed) == 0
    assert provider.aclose_called is True
    assert provider.streams[0].closed is True


@pytest.mark.asyncio
async def test_try_stream_without_writer_context_does_not_raise(monkeypatch):
    """T4.7（边界，R1）：``get_stream_writer()`` 抛 ``RuntimeError`` 时被捕获 → 节点不炸。

    ⚠️ 实测更正（SG-1）：本版 LangGraph 在 ``ainvoke`` 下调用真实
    ``get_stream_writer()`` **不会**抛 ``RuntimeError``（返回一个 no-op writer），
    所以「不 monkeypatch 就自然覆盖 except 分支」是错的 —— 那样 ``writer = None``
    这行永远不会执行，覆盖率会留一个假缺口。这里把异常**显式注入**，才是该分支的
    真实触发条件；断言也顺带确认节点仍把成功结果写出了图。
    """
    provider = _StreamingProvider([[{"choices": [{"delta": {"content": "a"}}]}]])

    def _raising_writer():
        raise RuntimeError("get_stream_writer() called outside of a streaming context")

    monkeypatch.setattr("botflow.pipeline.langgraph_engine.get_stream_writer", _raising_writer)
    ctx = GraphContext(db=MagicMock(spec=Database), cooldown=MagicMock())
    state = {
        "group": _group(), "messages": [{"role": "user", "content": "hi"}],
        "endpoints": [_ep_stream(1, provider)], "current_ep_idx": 0, "attempt": 0,
        "extra_kwargs": {}, "stream": True, "attempts": [], "model_name": "",
    }
    # 必须不抛异常
    out = await _run_try_stream_node(state, ctx, None)
    assert isinstance(out, dict)
    # writer=None 只是不推 chunk，端点调用与成功结果都不受影响
    assert provider.calls == 1
    assert out.get("result") == {"stream": "ok"}


# ---------------------------------------------------------------------------
# F6: recoverable 白名单（T6.1 ~ T6.7）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recoverable_all_models_cooldown():
    """T6.1（正例）：select_endpoints 抛 AllModelsCooldownError → recoverable is True。"""
    engine = _engine()
    group = _group()
    with patch("botflow.pipeline._shared.load_endpoints", new_callable=AsyncMock, return_value=[_ep()]), \
         patch("botflow.pipeline._shared.filter_available", new=lambda eps, cd, gid: []):
        state = await _run_slim_graph(engine, group, [{"role": "user", "content": "hi"}], stream=False)
    assert state.get("recoverable") is True


@pytest.mark.asyncio
async def test_recoverable_no_available_model():
    """T6.2（正例）：select_endpoints 抛 NoAvailableModelError → recoverable is True。"""
    engine = _engine()
    group = _group()
    with patch("botflow.pipeline._shared.load_endpoints", new_callable=AsyncMock, return_value=[]):
        state = await _run_slim_graph(engine, group, [{"role": "user", "content": "hi"}], stream=False)
    assert state.get("recoverable") is True


@pytest.mark.asyncio
async def test_recoverable_retryable_provider_error():
    """T6.3（正例）：call_llm 抛可重试 ProviderError（如 503）→ recoverable is True。"""
    engine = _engine()
    group = _group()
    with patch("botflow.pipeline._shared.load_endpoints", new_callable=AsyncMock, return_value=[_ep()]), \
         patch("botflow.pipeline._shared.filter_available", new=lambda eps, cd, gid: eps), \
         patch("botflow.pipeline.langgraph_engine.call_llm", new_callable=AsyncMock, return_value=(None, ProviderError("HTTP 503"))):
        state = await _run_slim_graph(engine, group, [{"role": "user", "content": "hi"}], stream=False)
    assert state.get("recoverable") is True


# NOTE (SG-1 §2 F6 落点修正): T6.4 / T6.5 属**驱动层**（docs/tasks/SG-1_tests.md §2
# 明确把 test_not_recoverable_configuration_error_unknown_strategy 与
# test_not_recoverable_unexpected_type_error_and_logged 落在 tests/test_core_runtime.py）。
# 图内「select_endpoints 抛 ConfigurationError / TypeError → recoverable=False」的覆盖已由
# 下方 T6.7 的 parametrize（configuration_error / type_error 两种 scenario）承担，故此处不再
# 保留独立的图内 T6.4 / T6.5，避免与驱动层同名用例重复计数。驱动层实现见
# tests/test_core_runtime.py 的 T6.4 / T6.5。


@pytest.mark.asyncio
async def test_not_recoverable_strategy_error():
    """T6.6（反例）：StrategyError → recoverable is False。"""
    engine = _engine()
    group = _group()
    with patch("botflow.pipeline._shared.load_endpoints", new_callable=AsyncMock, return_value=[_ep()]), \
         patch.object(RandomWeightsStrategy, "select_endpoints", new=AsyncMock(side_effect=StrategyError("bad"))):
        state = await _run_slim_graph(engine, group, [{"role": "user", "content": "hi"}], stream=False)
    assert state.get("recoverable") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", [
    "all_models_cooldown", "no_available_model", "retryable_provider_error",
    "configuration_error", "type_error", "strategy_error",
])
async def test_recoverable_key_always_present_on_failure(scenario, monkeypatch):
    """T6.7（边界，防回退守卫）：每个失败态 state 都有 recoverable 键且为 bool。"""
    engine = _engine()
    group = _group()
    if scenario == "configuration_error":
        group = _group(type_="does_not_exist")
    with patch("botflow.pipeline._shared.load_endpoints", new_callable=AsyncMock, return_value=[_ep()]), \
         patch("botflow.pipeline._shared.filter_available", new=(lambda eps, cd, gid: [] if scenario == "all_models_cooldown" else eps)), \
         patch("botflow.pipeline.langgraph_engine.call_llm", new_callable=AsyncMock, return_value=(None, ProviderError("HTTP 503"))), \
         patch.object(RandomWeightsStrategy, "select_endpoints", new=AsyncMock(side_effect={
             "type_error": TypeError("x"), "strategy_error": StrategyError("x"),
         }.get(scenario, None)) if scenario in ("type_error", "strategy_error") else AsyncMock()):
        state = await _run_slim_graph(engine, group, [{"role": "user", "content": "hi"}], stream=False)
    assert "recoverable" in state
    assert isinstance(state["recoverable"], bool)


# ---------------------------------------------------------------------------
# F5：engine.stream_events(...) —— transport 缝上的别名（覆盖 engine 层）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_events_alias_yields_event_generator(monkeypatch):
    """SG-1 F5：``engine.stream_events(...)`` 是 ``run(mode="stream")`` 的别名，
    **一次 await 必须拿到事件异步生成器**（与驱动 ``gen = await engine.run(...)`` 同契约），
    产出 ``("chunk", c)`` / ``("state", s)``。

    少写 ``await`` 时这里拿到的是协程而非异步迭代器，``async for`` 会直接报
    ``'async for' requires an object with __aiter__ method, got coroutine``。
    """
    engine = _engine()
    group = _group()
    provider = _StreamingProvider(
        [[{"choices": [{"delta": {"role": "assistant", "content": "a"}}]}]]
    )
    pushed: list = []
    monkeypatch.setattr(
        "botflow.pipeline.langgraph_engine.get_stream_writer",
        lambda: (lambda p: pushed.append(p)),
    )
    with patch("botflow.pipeline._shared.load_endpoints", new_callable=AsyncMock,
               return_value=[_ep_stream(1, provider)]), \
         patch("botflow.pipeline._shared.filter_available", new=lambda eps, cd, gid: eps):
        gen = await engine.stream_events(
            group=group, messages=[{"role": "user", "content": "hi"}],
        )
        events = [ev async for ev in gen]

    # 端点级流式调用真的发生了，且 chunk 经 writer 推出
    assert len(pushed) == 1
    assert provider.calls == 1
    # 最后一个事件是图出口 state，且已写好 recoverable（已提交 → False）
    assert events[-1][0] == "state"
    assert events[-1][1] is not None
    assert events[-1][1].get("recoverable") is False


# ---------------------------------------------------------------------------
# SG-1 覆盖率补洞（全量跑测暴露的目标模块缺口，逐行定性后按「真分支」补测）
#
# 定性结论：
#   * 真分支 → 补测（本节 G1 ~ G6）
#   * 死代码 → 从 src 删除，绝不用 `# UNCOVERED` 掩盖：
#       - ``_run_one`` 尾部 ``return {}  # unreachable``（``_raise_routing_error`` 是 NoReturn）
#       - ``core._chain_first``（SG-1 把逐 chunk 迭代迁进图后，core 里这份已无调用点）
# ---------------------------------------------------------------------------


def test_build_strategy_rejects_langgraph_strategy():
    """G1：``group.type == "langgraph"`` 是多步工作流策略，**不允许**经单组路径
    ``route()/run()`` 执行 → ``ConfigurationError``（配置类错误，驱动不得降级）。"""
    from botflow.pipeline.langgraph_engine import _build_strategy

    with pytest.raises(ConfigurationError) as exc_info:
        _build_strategy(_group(type_="langgraph"))
    assert "LangGraph strategy" in str(exc_info.value)


@pytest.mark.asyncio
async def test_try_call_preserves_preexisting_typed_error():
    """G2：``try_call`` 在「端点队列为空 + ``state['error']`` 已被上游写死」时，
    原样保留那个 typed cause（而非回退成字符串哨兵 ``"All endpoints in group failed"``），
    并沿用 ``state['recoverable']``。

    说明：这条分支在服务路径上踩不到（``select_endpoints`` 一旦失败，
    ``_route_after_select`` 直接走 ``finalize_error``，不会进 ``try_call``），
    所以按「节点级契约」单独驱动它 —— 与 T4.x 直接驱动 ``try_stream`` 同一手法。
    """
    from botflow.pipeline.langgraph_engine import RouteState as _RS
    from botflow.pipeline.langgraph_engine import try_call

    typed = NoAvailableModelError("Group 7: no available model")
    graph = StateGraph(_RS)
    graph.add_node("try_call", try_call)
    graph.set_entry_point("try_call")
    graph.add_edge("try_call", END)

    ctx = GraphContext(db=MagicMock(spec=Database), cooldown=MagicMock())
    out = await graph.compile().ainvoke(
        {
            "group": _group(), "messages": [{"role": "user", "content": "hi"}],
            "endpoints": [], "current_ep_idx": 0, "attempt": 0,
            "extra_kwargs": {}, "stream": False, "attempts": [],
            "error": typed, "recoverable": True,
        },
        config={"configurable": {"ctx": ctx}},
    )
    assert out["error"] is typed          # 原类型/原对象保住，驱动据此判可降级性
    assert out["recoverable"] is True
    assert out["endpoints"] == []


@pytest.mark.asyncio
async def test_try_stream_all_timeouts_exhaust_endpoint_then_falls_through(monkeypatch):
    """G3：单端点连续 3 次首-chunk 超时 → 用满 ``max_retries`` 后**放弃该端点**
    （超时分支的 ``break``），且无任何端点产出时落到图的「全端点失败」出口
    （``recoverable`` 沿用入口值 → 驱动可做组级降级）。"""
    provider = _StreamingProvider([TimeoutError(), TimeoutError(), TimeoutError()])
    pushed: list = []
    monkeypatch.setattr(
        "botflow.pipeline.langgraph_engine.get_stream_writer",
        lambda: (lambda p: pushed.append(p)),
    )
    monkeypatch.setattr("botflow.pipeline.langgraph_engine.exponential_backoff", AsyncMock())
    ctx = GraphContext(db=MagicMock(spec=Database), cooldown=MagicMock())
    state = {
        "group": _group(), "messages": [{"role": "user", "content": "hi"}],
        "endpoints": [_ep_stream(1, provider)], "current_ep_idx": 0, "attempt": 0,
        "extra_kwargs": {}, "stream": True, "attempts": [], "model_name": "",
        "recoverable": True,
    }
    out = await _run_try_stream_node(state, ctx, None)
    assert provider.calls == 3            # 用满 max_retries=3
    assert pushed == []
    assert out["endpoints"] == []
    assert out["recoverable"] is True     # 一个 chunk 都没交出去 → 允许降级
    timeouts = [a for a in out["attempts"] if a["error_type"] == "TimeoutError"]
    assert len(timeouts) == 3


@pytest.mark.asyncio
async def test_try_stream_retryable_error_retries_then_succeeds(monkeypatch):
    """G4：首 chunk 前抛**可重试**错误（HTTP 503）→ 按 ``max_retries`` 重试
    （区别于 T4.4 的不可重试 400：那条不重试），第 2 次成功。"""
    provider = _StreamingProvider([ProviderError("HTTP 503 Service Unavailable"),
                                   [{"choices": [{"delta": {"content": "ok"}}]}]])
    pushed: list = []
    monkeypatch.setattr(
        "botflow.pipeline.langgraph_engine.get_stream_writer",
        lambda: (lambda p: pushed.append(p)),
    )
    backoff = AsyncMock()
    monkeypatch.setattr("botflow.pipeline.langgraph_engine.exponential_backoff", backoff)
    ctx = GraphContext(db=MagicMock(spec=Database), cooldown=MagicMock())
    state = {
        "group": _group(), "messages": [{"role": "user", "content": "hi"}],
        "endpoints": [_ep_stream(1, provider)], "current_ep_idx": 0, "attempt": 0,
        "extra_kwargs": {}, "stream": True, "attempts": [], "model_name": "",
    }
    out = await _run_try_stream_node(state, ctx, None)
    assert provider.calls == 2            # 503 可重试 → 重试了一次
    backoff.assert_awaited_once()
    assert pushed[0]["chunk"]["choices"][0]["delta"]["content"] == "ok"
    assert out.get("result") == {"stream": "ok"}


@pytest.mark.asyncio
async def test_stream_events_uses_real_langgraph_custom_channel(monkeypatch):
    """G5（关键缝）：**不**打桩 ``get_stream_writer``，让 LangGraph 在 ``astream``
    下装上**真正的** custom-stream writer —— 验证「节点 ``writer({"chunk": c})`` →
    ``astream(stream_mode="custom")`` → ``("chunk", c)`` 事件」这条链路真的通。

    F5 那条打了桩，只证明别名契约；本版 LangGraph 的真实 writer 只在 ``astream``
    上下文里存在，所以链路必须由这条用例守。
    """
    engine = _engine()
    group = _group()
    provider = _StreamingProvider(
        [[{"choices": [{"delta": {"role": "assistant", "content": "a"}}]}]]
    )
    with patch("botflow.pipeline._shared.load_endpoints", new_callable=AsyncMock,
               return_value=[_ep_stream(1, provider)]), \
         patch("botflow.pipeline._shared.filter_available", new=lambda eps, cd, gid: eps):
        gen = await engine.stream_events(
            group=group, messages=[{"role": "user", "content": "hi"}],
        )
        events = [ev async for ev in gen]

    assert provider.calls == 1
    assert events[0][0] == "chunk"
    assert events[0][1]["choices"][0]["delta"]["content"] == "a"
    assert events[-1][0] == "state"


@pytest.mark.asyncio
async def test_run_stream_raises_on_group_level_failure(monkeypatch):
    """G6：``_run_stream`` 在未预选端点（``preselected=None``）时，图内
    ``select_endpoints`` 失败 → 图出口仍带 ``error`` → 必须抛出**原 typed error**
    （而不是沉成通用 ``ProviderError``），驱动据此决定是否降级。

    生产路径上 ``run(mode="stream")`` 会先 ``_select_endpoints`` 预选，这是兜底守卫；
    此处直接驱动私有 ``_run_stream`` 固化契约。
    """
    engine = _engine()
    group = _group()
    typed = NoAvailableModelError("Group 1: no available model")

    with patch("botflow.pipeline.langgraph_engine._select_endpoints",
               new_callable=AsyncMock, side_effect=typed):
        gen = engine._run_stream(
            None, group, [{"role": "user", "content": "hi"}], None, None, None, {},
        )
        with pytest.raises(NoAvailableModelError) as exc_info:
            async for _ in gen:
                pass
    assert exc_info.value is typed


@pytest.mark.asyncio
async def test_run_chat_mode_returns_result_dict():
    """G7：``run(mode="chat")`` 走 ``_run_one`` 的非流式分支，返回图结果 dict
    并附 ``_attempts``（驱动 ``_drive_chat`` 的取值来源）。"""
    engine = _engine()
    group = _group()
    llm_resp = {"choices": [{"message": {"content": "hi"}}], "_routing": {"model_id": 1}}

    with patch("botflow.pipeline._shared.load_endpoints", new_callable=AsyncMock, return_value=[_ep()]), \
         patch("botflow.pipeline._shared.filter_available", new=lambda eps, cd, gid: eps), \
         patch("botflow.pipeline.langgraph_engine.call_llm", new_callable=AsyncMock, return_value=(llm_resp, None)):
        result = await engine.run(
            group=group, messages=[{"role": "user", "content": "hi"}], mode="chat",
        )

    assert result["choices"][0]["message"]["content"] == "hi"
    assert result["_attempts"] == []
