"""Trivial 快路径（perf §七#2）— 100% 覆盖回归测试。

验证：
  * run() 一旦选择成功即走快路径（跳过 pregel 调度）——**任意端点数**都如此，
    包括生产策略返回的 [primary] + fallback 多端点队列。
  * 快路径产出与图路径逐字节一致（chat 结果 dict / stream 事件序列 / sg-0 留痕 /
    错误语义）；单端点（T6/T6b/T7）与多端点（T18~T23）均做等价对照。
  * 选 endpoint 失败（NoAvailableModelError 等）按原样上抛。
  * 流式快路径覆盖：成功 / 首块超时重试后成功 / 超时全败 / 空流 /
    可重试异常 / 不可重试异常 / 流式中断 / 客户端断连。

注意：本机 Windows 因 loopback 被禁无法创建 asyncio 事件循环（见 AGENTS.md），
这些 async 测试需在 mq3 复跑；测试全部用 AsyncMock，无真实网络。
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from botflow.common.exceptions import NoAvailableModelError, ProviderError
from botflow.pipeline._shared import semaphore_guarded_stream
from botflow.pipeline.langgraph_engine import LangGraphEngine, _select_endpoints
from botflow.router import CooldownManager, ModelEndpoint, _provider_semaphores
from botflow.storage.db import Database
from botflow.storage.models import GroupModelWithDetails, ModelGroup


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _engine() -> LangGraphEngine:
    cooldown = MagicMock(spec=CooldownManager)
    cooldown.is_on_cooldown.return_value = False
    return LangGraphEngine(db_factory=lambda: MagicMock(spec=Database), cooldown=cooldown)


def _group(gid: int = 1, type_: str = "random_weights") -> ModelGroup:
    return ModelGroup(id=gid, name=f"g{gid}", type=type_, fallback_group_id=None, params={})


def _ep(model_name: str = "m1", model_id: int = 1, provider_id: int = 10,
        extra_config: dict | None = None, max_retries: int = 1) -> ModelEndpoint:
    detail = GroupModelWithDetails(
        id=model_id, group_id=1, model_id=model_id, weight=1.0, is_enabled=True,
        model_name=model_name, display_name=model_name, api_format="openai",
        provider_id=provider_id, provider_name="p", provider_type="openai",
        max_retries=max_retries, cooldown_seconds=30, cooldown_failure_threshold=3,
        context_window=8192, extra_config=extra_config or {},
    )
    return ModelEndpoint(model_detail=detail, provider_instance=MagicMock())


def _async_gen(chunks: list):
    async def gen():
        for c in chunks:
            yield c
    return gen()


def _failing_gen(exc):
    """异步生成器：首个 ``__anext__`` 即抛 ``exc``。

    ⚠️ 函数体内必须含 ``yield`` —— 否则 ``async def gen`` 是**协程**函数，
    ``gen()`` 返回 coroutine（无 ``__anext__`` / ``aclose``），图路径与快路径
    都会崩。这里用「空循环里的 yield」保证它是 async generator，又不引入不可达语句。
    """
    async def gen():
        for _ in ():
            yield None
        raise exc
    return gen()


def _empty_gen():
    """空异步流：首个 ``__anext__`` 自然抛 ``StopAsyncIteration``（真正的空流）。

    注意不能用 ``_failing_gen(StopAsyncIteration)`` 模拟 —— PEP 479 会把 async
    generator 内部抛出的 ``StopAsyncIteration`` 改写成 ``RuntimeError``。
    """
    async def gen():
        for _ in ():
            yield None
    return gen()


def _failing_after_first(exc):
    async def gen():
        yield {"choices": [{"delta": {"content": "first"}}]}
        raise exc
    return gen()


class _StatusError(Exception):
    pass


def _status_error(code: int) -> Exception:
    e = _StatusError(f"HTTP {code}")
    e.status_code = code  # is_retryable_error() 读取此属性
    return e


@contextmanager
def _patch_select(endpoints):
    """Patch 选 endpoint 的两个依赖（load_endpoints / filter_available 均为本地导入）。

    返回**单个**上下文管理器，调用点写成
    ``with _patch_select([...]), patch(...):``（可再叠加本文件的其它 patch）。
    """
    with patch("botflow.pipeline._shared.load_endpoints", new_callable=AsyncMock, return_value=endpoints), \
         patch("botflow.pipeline._shared.filter_available", new=lambda eps, cd, gid: eps):
        yield


def _make_call_llm_side(decide):
    """构造 ``botflow.pipeline.langgraph_engine.call_llm`` 的 side_effect。

    ``decide(ep) -> (resp, err)``：err 非 None 表示失败；否则 resp 成功并自动注入
    ``_routing``（与真实 call_llm 一致，便于快/图路径等价对照）。
    """
    async def side(ep, messages, group_id, cooldown, temperature=None, max_tokens=None, **kw):
        resp, err = decide(ep)
        if err is not None:
            return (None, err)
        r = dict(resp) if resp is not None else resp
        if r is not None:
            r["_routing"] = {"model_id": ep.model_id, "provider_id": ep.detail.provider_id}
        return (r, None)
    return side


async def _collect(gen):
    """Collect (kind, payload) from a run* generator, projecting the state to the
    keys ``_drive_stream`` actually consumes (无关 RouteState bookkeeping 被忽略)。"""
    events = []
    async for kind, payload in gen:
        if kind == "chunk":
            events.append(("chunk", payload))
        else:
            events.append(("state", {
                k: payload.get(k) for k in (
                    "attempts", "recoverable", "used_model_id",
                    "used_provider_id", "result", "stream_started",
                )
            }))
    return events


# ---------------------------------------------------------------------------
# 安全判定 / 回退（run 分发）
# ---------------------------------------------------------------------------


async def test_run_chat_single_endpoint_uses_fast_path():
    """T1：单 endpoint → run() 走快路径（_run_one_fast）。"""
    engine = _engine()
    group = _group()
    ep = _ep()
    real_fast = engine._run_one_fast
    calls = []
    async def spy(*a, **k):
        calls.append(1)
        return await real_fast(*a, **k)
    engine._run_one_fast = spy
    with _patch_select([ep]), \
         patch("botflow.pipeline.langgraph_engine.call_llm",
               new=_make_call_llm_side(lambda ep: ({"choices": [{"message": {"content": "x"}}]}, None))):
        result = await engine.run(group=group, messages=[{"role": "user", "content": "hi"}], mode="chat")
    assert calls == [1]
    assert result["choices"][0]["message"]["content"] == "x"
    assert result["_attempts"] == []


async def test_run_chat_multi_endpoint_uses_fast_path():
    """T2：多 endpoint（组内 fallback）→ run() **走快路径**（_run_one_fast），
    不再回退图路径（§七#2 推广：任意端点数都绕 pregel 调度）。"""
    engine = _engine()
    group = _group()  # random_weights：生产默认，返回 [primary] + fallback
    ep_a = _ep("a", model_id=1)
    ep_b = _ep("b", model_id=2)

    calls_fast, calls_graph = [], []
    real_fast = engine._run_one_fast

    async def fast_spy(*a, **k):
        calls_fast.append(1)
        return await real_fast(*a, **k)

    async def graph_spy(*a, **k):
        calls_graph.append(1)
        return None
    engine._run_one_fast = fast_spy
    engine._run_one = graph_spy

    with _patch_select([ep_a, ep_b]), \
         patch("botflow.pipeline.langgraph_engine.call_llm", new=_make_call_llm_side(
             lambda ep: (None, ProviderError("a down")) if ep.model_id == 1
             else ({"choices": [{"message": {"content": "ok"}}]}, None))):
        result = await engine.run(group=group, messages=[{"role": "user", "content": "hi"}], mode="chat")
    assert calls_fast == [1]
    assert calls_graph == []
    assert result["choices"][0]["message"]["content"] == "ok"


async def test_run_stream_single_endpoint_uses_fast_path():
    """T3：单 endpoint 流式 → run() 走快路径（_run_stream_fast）。"""
    engine = _engine()
    group = _group()
    ep = _ep()
    ep.provider.chat_stream = MagicMock(return_value=_async_gen(
        [{"choices": [{"delta": {"content": "a"}}]},
         {"choices": [{"delta": {"content": "b"}}]}]))
    real_fast = engine._run_stream_fast
    calls = []
    async def spy(*a, **k):
        calls.append(1)
        async for ev in real_fast(*a, **k):
            yield ev
    engine._run_stream_fast = spy
    with _patch_select([ep]):
        gen = await engine.run(group=group, messages=[{"role": "user", "content": "hi"}], mode="stream")
        events = await _collect(gen)
    assert calls == [1]
    assert [k for k, _ in events] == ["chunk", "chunk", "state"]


async def test_run_stream_multi_endpoint_uses_fast_path():
    """T4：多 endpoint 流式 → run() **走快路径**（_run_stream_fast），不再回退图路径。"""
    engine = _engine()
    group = _group()  # random_weights：生产默认，返回 [primary] + fallback
    ep_a = _ep("a", model_id=1)
    ep_b = _ep("b", model_id=2)
    ep_a.provider.chat_stream = MagicMock(return_value=_failing_gen(ProviderError("a down")))
    ep_b.provider.chat_stream = MagicMock(return_value=_async_gen(
        [{"choices": [{"delta": {"content": "from b"}}]}]))
    calls_fast, calls_graph = [], []
    real_fast = engine._run_stream_fast

    async def fast_spy(*a, **k):
        calls_fast.append(1)
        async for ev in real_fast(*a, **k):
            yield ev

    async def graph_spy(*a, **k):
        calls_graph.append(1)
        for ev in ():
            yield ev
    engine._run_stream_fast = fast_spy
    engine._run_stream = graph_spy
    with _patch_select([ep_a, ep_b]):
        gen = await engine.run(group=group, messages=[{"role": "user", "content": "hi"}], mode="stream")
        events = await _collect(gen)
    assert calls_fast == [1]
    assert calls_graph == []
    contents = [p["choices"][0]["delta"]["content"] for k, p in events if k == "chunk"]
    assert contents == ["from b"]


async def test_run_select_failure_propagates():
    """T5：选 endpoint 失败（无模型）→ 原样上抛 NoAvailableModelError。"""
    engine = _engine()
    group = _group()
    with _patch_select([]):
        with pytest.raises(NoAvailableModelError):
            await engine.run(group=group, messages=[], mode="chat")


async def test_run_select_failure_propagates_stream():
    """T5b：流式同样：选 endpoint 失败上抛。"""
    engine = _engine()
    group = _group()
    with _patch_select([]):
        with pytest.raises(NoAvailableModelError):
            await engine.run(group=group, messages=[], mode="stream")


# ---------------------------------------------------------------------------
# 等价对照：快路径 vs 图路径
# ---------------------------------------------------------------------------


async def test_fast_vs_graph_chat_identical():
    """T6：单 endpoint chat —— 快路径与图路径结果 dict 逐字节一致。"""
    engine = _engine()
    group = _group()
    ep = _ep()
    messages = [{"role": "user", "content": "hi"}]
    with _patch_select([ep]), \
         patch("botflow.pipeline.langgraph_engine.call_llm",
               new=_make_call_llm_side(lambda ep: ({"choices": [{"message": {"content": "hi"}}]}, None))):
        preselected = await _select_endpoints(group, messages, None, None, {}, engine.db, engine.cooldown)
        fast = await engine._run_one_fast(group, messages, None, None, None, {}, preselected)
        graph = await engine._run_one(None, group, messages, "chat", None, None, None, {}, preselected)
    assert fast == graph
    assert fast["choices"][0]["message"]["content"] == "hi"
    assert fast["_attempts"] == []
    assert fast["_routing"]["model_id"] == ep.model_id


async def test_fast_vs_graph_chat_failure_identical_shape():
    """T6b：单 endpoint chat 失败 —— 快路径与图路径都抛 ProviderError 且留痕结构一致。"""
    engine = _engine()
    group = _group()
    ep = _ep(max_retries=1)
    messages = [{"role": "user", "content": "hi"}]

    async def run_fast():
        return await engine._run_one_fast(group, messages, None, None, None, {}, preselected)

    async def run_graph():
        return await engine._run_one(None, group, messages, "chat", None, None, None, {}, preselected)

    with _patch_select([ep]), \
         patch("botflow.pipeline.langgraph_engine.call_llm",
               new=_make_call_llm_side(lambda ep: (None, ProviderError("boom")))):
        preselected = await _select_endpoints(group, messages, None, None, {}, engine.db, engine.cooldown)
        for run in (run_fast, run_graph):
            with pytest.raises(ProviderError) as ei:
                await run()
            attempts = ei.value.attempts
            assert len(attempts) == 1
            assert attempts[0]["stage"] == "call"
            assert attempts[0]["endpoint_idx"] == 0
            assert attempts[0]["attempt_no"] == ep.max_retries
            assert attempts[0]["error_type"] == "ProviderError"
            assert ei.value.used_model_id == ep.model_id
            assert ei.value.used_provider_id == ep.detail.provider_id


async def test_fast_vs_graph_stream_identical():
    """T7：单 endpoint stream —— 快路径与图路径事件序列（块 + 相关 state）一致。"""
    engine = _engine()
    group = _group()
    ep = _ep()
    chunks = [{"choices": [{"delta": {"content": "a"}}]},
              {"choices": [{"delta": {"content": "b"}}]}]
    # side_effect（不是 return_value）：快/图两条路径各调一次 chat_stream，
    # 必须是各自**新建**的流对象，不能共用同一个已被消费的生成器。
    ep.provider.chat_stream = MagicMock(side_effect=lambda **kw: _async_gen(chunks))
    messages = [{"role": "user", "content": "hi"}]
    with _patch_select([ep]):
        preselected = await _select_endpoints(group, messages, None, None, {}, engine.db, engine.cooldown)
        fast_gen = engine._run_stream_fast(group, messages, None, None, None, {}, preselected)
        graph_gen = engine._run_stream(None, group, messages, None, None, None, {}, preselected)
        fast_events = await _collect(fast_gen)
        graph_events = await _collect(graph_gen)
    assert [k for k, _ in fast_events] == [k for k, _ in graph_events]
    assert fast_events == graph_events


# ---------------------------------------------------------------------------
# 快路径分支覆盖
# ---------------------------------------------------------------------------


async def test_run_one_fast_success():
    """T8：快路径 chat 成功 → 返回 dict + _attempts=[]。"""
    engine = _engine()
    group = _group()
    ep = _ep()
    with _patch_select([ep]), \
         patch("botflow.pipeline.langgraph_engine.call_llm",
               new=_make_call_llm_side(lambda ep: ({"choices": [{"message": {"content": "ok"}}]}, None))):
        result = await engine.run(group=group, messages=[{"role": "user", "content": "hi"}], mode="chat")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert result["_attempts"] == []
    assert result["_routing"]["model_id"] == ep.model_id


async def test_run_one_fast_failure_raises_with_attempts():
    """T9：快路径 chat 失败 → 抛 ProviderError，并 smuggle 留痕（sg-0）。"""
    engine = _engine()
    group = _group()
    ep = _ep(max_retries=1)
    with _patch_select([ep]), \
         patch("botflow.pipeline.langgraph_engine.call_llm",
               new=_make_call_llm_side(lambda ep: (None, ProviderError("boom")))):
        with pytest.raises(ProviderError) as ei:
            await engine.run(group=group, messages=[{"role": "user", "content": "hi"}], mode="chat")
    assert ei.value.attempts
    assert ei.value.attempts[0]["error_type"] == "ProviderError"
    assert ei.value.used_model_id == ep.model_id


async def test_run_stream_fast_success():
    """T10：快路径 stream 成功 → chunk + 末态（recoverable=False）。"""
    engine = _engine()
    group = _group()
    ep = _ep()
    ep.provider.chat_stream = MagicMock(return_value=_async_gen(
        [{"choices": [{"delta": {"content": "a"}}]},
         {"choices": [{"delta": {"content": "b"}}]}]))
    with _patch_select([ep]):
        gen = await engine.run(group=group, messages=[{"role": "user", "content": "hi"}], mode="stream")
        events = await _collect(gen)
    assert [k for k, _ in events] == ["chunk", "chunk", "state"]
    assert events[-1][1]["recoverable"] is False
    assert events[-1][1]["used_model_id"] == ep.model_id


async def test_run_stream_fast_timeout_then_success():
    """T11：首块超时重试（backoff）→ 第二次成功。"""
    engine = _engine()
    group = _group()
    ep = _ep(max_retries=2)
    chunk = {"choices": [{"delta": {"content": "a"}}]}
    fail_gen = _failing_gen(asyncio.TimeoutError())
    ok_gen = _async_gen([chunk])
    ep.provider.chat_stream = MagicMock(side_effect=[fail_gen, ok_gen])
    with _patch_select([ep]), \
         patch("botflow.pipeline.langgraph_engine.exponential_backoff", new=AsyncMock()):
        gen = await engine.run(group=group, messages=[{"role": "user", "content": "hi"}], mode="stream")
        events = await _collect(gen)
    assert [k for k, _ in events] == ["chunk", "state"]
    assert events[-1][1]["recoverable"] is False


async def test_run_stream_fast_timeout_all_fail():
    """T12：首块超时（max_retries=1）全败 → 末态 recoverable=True（驱动回退）。"""
    engine = _engine()
    group = _group()
    ep = _ep(max_retries=1)
    ep.provider.chat_stream = MagicMock(return_value=_failing_gen(asyncio.TimeoutError()))
    with _patch_select([ep]):
        gen = await engine.run(group=group, messages=[{"role": "user", "content": "hi"}], mode="stream")
        events = await _collect(gen)
    assert events == [("state", {
        "attempts": [{"group_id": group.id, "model_id": ep.model_id,
                      "provider_id": ep.detail.provider_id, "stage": "stream",
                      "endpoint_idx": 0, "attempt_no": 1, "error_type": "TimeoutError",
                      "error_message": f"{ep.detail.model_name} timed out waiting for first chunk",
                      "duration_ms": 0}],
        "recoverable": True, "used_model_id": ep.model_id,
        "used_provider_id": ep.detail.provider_id, "result": None, "stream_started": None,
    })]


async def test_run_stream_fast_empty_stream():
    """T13：空流（StopAsyncIteration）→ record_failure + 全败 recoverable=True。"""
    engine = _engine()
    group = _group()
    ep = _ep(max_retries=1)
    ep.provider.chat_stream = MagicMock(return_value=_empty_gen())
    with _patch_select([ep]):
        gen = await engine.run(group=group, messages=[{"role": "user", "content": "hi"}], mode="stream")
        events = await _collect(gen)
    assert events[-1][1]["recoverable"] is True
    engine.cooldown.record_failure.assert_called_once_with(
        group.id, ep.model_id, ep.cooldown_threshold, ep.cooldown_seconds)


async def test_run_stream_fast_retryable_exception_fails():
    """T14：可重试异常（503）重试后仍在末次失败 → 全败。"""
    err = _status_error(503)
    engine = _engine()
    group = _group()
    ep = _ep(max_retries=2)
    g1 = _failing_gen(err)
    g2 = _failing_gen(err)
    ep.provider.chat_stream = MagicMock(side_effect=[g1, g2])
    with _patch_select([ep]), \
         patch("botflow.pipeline.langgraph_engine.exponential_backoff", new=AsyncMock()):
        gen = await engine.run(group=group, messages=[{"role": "user", "content": "hi"}], mode="stream")
        events = await _collect(gen)
    assert events[-1][1]["recoverable"] is True
    attempts = events[-1][1]["attempts"]
    assert len(attempts) == 2
    assert attempts[0]["error_type"] == "_StatusError"


async def test_run_stream_fast_non_retryable_exception_fails():
    """T15：不可重试异常 → 立即全败（不重试）。"""
    engine = _engine()
    group = _group()
    ep = _ep(max_retries=2)
    ep.provider.chat_stream = MagicMock(return_value=_failing_gen(RuntimeError("nope")))
    with _patch_select([ep]):
        gen = await engine.run(group=group, messages=[{"role": "user", "content": "hi"}], mode="stream")
        events = await _collect(gen)
    assert events[-1][1]["recoverable"] is True
    assert len(events[-1][1]["attempts"]) == 1  # 未重试
    assert events[-1][1]["attempts"][0]["error_type"] == "RuntimeError"


async def test_run_stream_fast_mid_stream_failure():
    """T16：首块已发后中断 → recoverable=False（已提交，不可回退）。"""
    engine = _engine()
    group = _group()
    ep = _ep(max_retries=1)
    ep.provider.chat_stream = MagicMock(return_value=_failing_after_first(RuntimeError("mid")))
    with _patch_select([ep]):
        gen = await engine.run(group=group, messages=[{"role": "user", "content": "hi"}], mode="stream")
        events = await _collect(gen)
    assert events[0][0] == "chunk"  # 首块已发
    assert events[-1][1]["recoverable"] is False  # 已提交


async def test_run_stream_fast_disconnect():
    """T17：流式中断连（客户端离开）→ 不发后续块，末态 recoverable=False。"""
    engine = _engine()
    group = _group()
    ep = _ep(max_retries=1)
    ep.provider.chat_stream = MagicMock(return_value=_async_gen(
        [{"choices": [{"delta": {"content": "a"}}]}]))
    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=True)
    with _patch_select([ep]):
        gen = await engine.run(group=group, messages=[{"role": "user", "content": "hi"}], mode="stream", request=request)
        events = await _collect(gen)
    # 首个块在断连判定前已被拉取但未下发；只产出已提交的末态。
    assert all(k == "state" for k, _ in events)
    assert events[-1][1]["recoverable"] is False


# ---------------------------------------------------------------------------
# 多端点等价对照：快路径 vs 图路径（§七#2 推广到任意端点数后新增）
#
# 用 SequentialStrategy 拿到**确定性**队列 [ep_a, ep_b]（RandomWeights 会随机
# 选 primary），再对同一 preselected 分别驱动快路径与图路径，断言逐字节一致。
# ---------------------------------------------------------------------------


async def _preselect(engine, group, messages):
    """跑一次选择，返回 RouteResult（供快/图两条路径共用同一 preselected）。"""
    return await _select_endpoints(group, messages, None, None, {}, engine.db, engine.cooldown)


async def _capture_provider_error(coro):
    with pytest.raises(ProviderError) as ei:
        await coro
    return ei.value


def _strip_durations(attempts):
    """比较留痕时剔除 wall-clock 的 ``duration_ms``（快/图两次计时不可能完全相等）。"""
    return [{k: v for k, v in a.items() if k != "duration_ms"} for a in attempts]


async def test_fast_vs_graph_chat_multi_primary_succeeds():
    """T18：多端点 chat 正例（primary 直接成功）—— 快/图结果 dict 逐字节一致，
    且 fallback 端点未被调用（组内降级不提前发生）。"""
    engine = _engine()
    group = _group(type_="sequential")
    ep_a = _ep("a", model_id=1)
    ep_b = _ep("b", model_id=2)
    messages = [{"role": "user", "content": "hi"}]
    called: list[int] = []

    async def side(ep, messages, group_id, cooldown, temperature=None, max_tokens=None, **kw):
        called.append(ep.model_id)
        return ({"choices": [{"message": {"content": f"from {ep.model_id}"}}],
                 "_routing": {"model_id": ep.model_id, "provider_id": ep.detail.provider_id}}, None)

    with _patch_select([ep_a, ep_b]), \
         patch("botflow.pipeline.langgraph_engine.call_llm", new=side):
        preselected = await _preselect(engine, group, messages)
        assert [e.model_id for e in preselected.endpoints] == [1, 2]
        fast = await engine._run_one_fast(group, messages, None, None, None, {}, preselected)
        graph = await engine._run_one(None, group, messages, "chat", None, None, None, {}, preselected)

    assert fast == graph
    assert fast["choices"][0]["message"]["content"] == "from 1"
    assert fast["_attempts"] == []
    assert called == [1, 1]  # 每条路径只调 primary；fallback 未被触碰


async def test_fast_vs_graph_chat_multi_fallback_success():
    """T19：多端点 chat 失败回退 —— primary 失败后 fallback 成功；快/图结果 dict
    与 `_attempts`（endpoint_idx=真实队列索引）逐字节一致。"""
    engine = _engine()
    group = _group(type_="sequential")
    ep_a = _ep("a", model_id=1, max_retries=2)
    ep_b = _ep("b", model_id=2)
    messages = [{"role": "user", "content": "hi"}]

    async def side(ep, messages, group_id, cooldown, temperature=None, max_tokens=None, **kw):
        if ep.model_id == 1:
            return (None, ProviderError("a down"))
        return ({"choices": [{"message": {"content": "from 2"}}],
                 "_routing": {"model_id": ep.model_id, "provider_id": ep.detail.provider_id}}, None)

    with _patch_select([ep_a, ep_b]), \
         patch("botflow.pipeline.langgraph_engine.call_llm", new=side):
        preselected = await _preselect(engine, group, messages)
        fast = await engine._run_one_fast(group, messages, None, None, None, {}, preselected)
        graph = await engine._run_one(None, group, messages, "chat", None, None, None, {}, preselected)

    assert {k: v for k, v in fast.items() if k != "_attempts"} == \
           {k: v for k, v in graph.items() if k != "_attempts"}
    assert _strip_durations(fast["_attempts"]) == _strip_durations(graph["_attempts"])
    assert fast["choices"][0]["message"]["content"] == "from 2"
    assert [a["endpoint_idx"] for a in fast["_attempts"]] == [0]
    assert [a["model_id"] for a in fast["_attempts"]] == [1]
    assert fast["_attempts"][0]["attempt_no"] == 2


async def test_fast_vs_graph_chat_multi_all_fail():
    """T20：多端点 chat 全败 —— 快/图都抛 ProviderError("All endpoints in group failed")，
    attempts 覆盖每个端点（endpoint_idx 0/1）、used_* = 最后端点，全部逐字段一致。"""
    engine = _engine()
    group = _group(type_="sequential")
    ep_a = _ep("a", model_id=1, max_retries=1)
    ep_b = _ep("b", model_id=2, max_retries=3)
    messages = [{"role": "user", "content": "hi"}]

    with _patch_select([ep_a, ep_b]), \
         patch("botflow.pipeline.langgraph_engine.call_llm",
               new=_make_call_llm_side(lambda ep: (None, ProviderError(f"{ep.detail.model_name} down")))):
        preselected = await _preselect(engine, group, messages)
        fast_exc = await _capture_provider_error(
            engine._run_one_fast(group, messages, None, None, None, {}, preselected))
        graph_exc = await _capture_provider_error(
            engine._run_one(None, group, messages, "chat", None, None, None, {}, preselected))

    assert type(fast_exc) is type(graph_exc) is ProviderError
    assert str(fast_exc) == str(graph_exc) == "All endpoints in group failed"
    assert _strip_durations(fast_exc.attempts) == _strip_durations(graph_exc.attempts)
    assert [a["endpoint_idx"] for a in fast_exc.attempts] == [0, 1]
    assert [a["attempt_no"] for a in fast_exc.attempts] == [1, 3]
    assert [a["error_type"] for a in fast_exc.attempts] == ["ProviderError", "ProviderError"]
    assert fast_exc.used_model_id == graph_exc.used_model_id == 2
    assert fast_exc.used_provider_id == graph_exc.used_provider_id == ep_b.detail.provider_id


async def test_fast_vs_graph_stream_multi_primary_succeeds():
    """T21：多端点流式正例（primary 直接成功）—— 快/图事件序列一致，fallback 未被触碰。"""
    engine = _engine()
    group = _group(type_="sequential")
    ep_a = _ep("a", model_id=1)
    ep_b = _ep("b", model_id=2)
    # side_effect（不是 return_value）：两条路径各调一次，需各自新建流对象。
    ep_a.provider.chat_stream = MagicMock(side_effect=lambda **kw: _async_gen(
        [{"choices": [{"delta": {"content": "a1"}}]},
         {"choices": [{"delta": {"content": "a2"}}]}]))
    messages = [{"role": "user", "content": "hi"}]

    with _patch_select([ep_a, ep_b]):
        preselected = await _preselect(engine, group, messages)
        fast_events = await _collect(
            engine._run_stream_fast(group, messages, None, None, None, {}, preselected))
        graph_events = await _collect(
            engine._run_stream(None, group, messages, None, None, None, {}, preselected))

    assert fast_events == graph_events
    assert [k for k, _ in fast_events] == ["chunk", "chunk", "state"]
    assert fast_events[-1][1]["recoverable"] is False
    assert ep_b.provider.chat_stream.call_count == 0


async def test_fast_vs_graph_stream_multi_fallback():
    """T22：多端点流式失败回退 —— 端点 A 空流 → 换端点 B 成功；快/图事件序列与
    `attempts`（A 的 EmptyStream 留痕）逐字节一致。"""
    engine = _engine()
    group = _group(type_="sequential")
    ep_a = _ep("a", model_id=1, max_retries=1)
    ep_b = _ep("b", model_id=2)
    ep_a.provider.chat_stream = MagicMock(side_effect=lambda **kw: _empty_gen())
    ep_b.provider.chat_stream = MagicMock(side_effect=lambda **kw: _async_gen(
        [{"choices": [{"delta": {"content": "from b"}}]}]))
    messages = [{"role": "user", "content": "hi"}]

    with _patch_select([ep_a, ep_b]):
        preselected = await _preselect(engine, group, messages)
        fast_events = await _collect(
            engine._run_stream_fast(group, messages, None, None, None, {}, preselected))
        graph_events = await _collect(
            engine._run_stream(None, group, messages, None, None, None, {}, preselected))

    assert fast_events == graph_events
    contents = [p["choices"][0]["delta"]["content"] for k, p in fast_events if k == "chunk"]
    assert contents == ["from b"]
    assert [a["error_type"] for a in fast_events[-1][1]["attempts"]] == ["EmptyStream"]
    assert fast_events[-1][1]["used_model_id"] == 2
    assert fast_events[-1][1]["recoverable"] is False


async def test_fast_vs_graph_stream_multi_all_fail():
    """T23：多端点流式全败（首块前）—— 快/图都产出单个 recoverable=True 末态，
    attempts 覆盖每个端点、used_* = 最后端点。

    注：`endpoint_idx` 恒为 0 是**镜像图节点 `try_stream`** 的既有行为（它
    `for ep in endpoints` 未 enumerate）；此处固化该等价性，不引入差异。
    """
    engine = _engine()
    group = _group(type_="sequential")
    ep_a = _ep("a", model_id=1, max_retries=1)
    ep_b = _ep("b", model_id=2, max_retries=1)
    ep_a.provider.chat_stream = MagicMock(side_effect=lambda **kw: _failing_gen(asyncio.TimeoutError()))
    ep_b.provider.chat_stream = MagicMock(side_effect=lambda **kw: _failing_gen(RuntimeError("b down")))
    messages = [{"role": "user", "content": "hi"}]

    with _patch_select([ep_a, ep_b]):
        preselected = await _preselect(engine, group, messages)
        fast_events = await _collect(
            engine._run_stream_fast(group, messages, None, None, None, {}, preselected))
        graph_events = await _collect(
            engine._run_stream(None, group, messages, None, None, None, {}, preselected))

    assert fast_events == graph_events
    assert len(fast_events) == 1 and fast_events[0][0] == "state"
    state = fast_events[0][1]
    assert state["recoverable"] is True
    assert [a["error_type"] for a in state["attempts"]] == ["TimeoutError", "RuntimeError"]
    assert [a["endpoint_idx"] for a in state["attempts"]] == [0, 0]
    assert state["used_model_id"] == 2
    assert state["used_provider_id"] == ep_b.detail.provider_id


# ---------------------------------------------------------------------------
# 上游并发信号量（perf 报告 §七#6）：流式 chat_stream 接入 per-provider 信号量
# ---------------------------------------------------------------------------


@pytest.fixture
def _clear_provider_semaphores():
    _provider_semaphores.clear()
    yield
    _provider_semaphores.clear()


async def test_stream_semaphore_passthrough_when_disabled():
    """size=0（默认）→ 原样返回同一生成器，无任何包装（行为不变）。"""
    gen = _async_gen([1, 2])
    with patch("botflow.pipeline._shared.get_config") as cfg:
        cfg.return_value = MagicMock(upstream_semaphore_size=0)
        assert semaphore_guarded_stream(gen, 9000) is gen


async def test_stream_semaphore_gates_concurrent_streams():
    """size=2 → 5 个并发流最多 2 个同时在上游产出（真实门控，非仅身份注入）。"""
    state = {"inflight": 0, "max": 0}

    def make_gen():
        async def gen():
            state["inflight"] += 1
            state["max"] = max(state["max"], state["inflight"])
            try:
                yield 1
                await asyncio.sleep(0.01)
                yield 2
            finally:
                state["inflight"] -= 1
        return gen()

    async def drain(g):
        return [c async for c in g]

    with patch("botflow.pipeline._shared.get_config") as cfg:
        cfg.return_value = MagicMock(upstream_semaphore_size=2)
        streams = [semaphore_guarded_stream(make_gen(), 9001) for _ in range(5)]
        results = await asyncio.wait_for(
            asyncio.gather(*[drain(g) for g in streams]), timeout=5)

    assert results == [[1, 2]] * 5
    assert state["max"] == 2


async def test_stream_semaphore_released_on_early_close():
    """消费方提前 aclose → 信号量立即归还（引擎所有退出路径都会 aclose）。"""
    state = {"inflight": 0}

    async def gen():
        state["inflight"] += 1
        try:
            yield 1
            yield 2  # 不可达
        finally:
            state["inflight"] -= 1

    with patch("botflow.pipeline._shared.get_config") as cfg:
        cfg.return_value = MagicMock(upstream_semaphore_size=1)
        guarded = semaphore_guarded_stream(gen(), 9002)
        assert await asyncio.wait_for(guarded.__anext__(), timeout=2) == 1
        assert state["inflight"] == 1
        await guarded.aclose()
        assert state["inflight"] == 0

        # 额度已归还：新流可立即拿到首块（若泄漏，这里会 TimeoutError）
        g2 = semaphore_guarded_stream(_async_gen([9]), 9002)
        assert await asyncio.wait_for(g2.__anext__(), timeout=2) == 9


async def test_stream_semaphore_released_on_upstream_error():
    """上游中途抛错 → 信号量在异常传播前归还，额度可被后续流立即复用。"""
    async def gen():
        yield 1
        raise RuntimeError("mid-stream")

    with patch("botflow.pipeline._shared.get_config") as cfg:
        cfg.return_value = MagicMock(upstream_semaphore_size=1)
        guarded = semaphore_guarded_stream(gen(), 9003)
        assert await asyncio.wait_for(guarded.__anext__(), timeout=2) == 1
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(guarded.__anext__(), timeout=2)

        g2 = semaphore_guarded_stream(_async_gen([9]), 9003)
        assert await asyncio.wait_for(g2.__anext__(), timeout=2) == 9


async def test_run_stream_respects_provider_semaphore(_clear_provider_semaphores):
    """集成：size=1 时两条并发 _run_stream_fast 流在上游侧完全串行
    （修复前 chat_stream 完全绕过信号量，max 会是 2）。"""
    engine = _engine()
    group = _group()
    ep = _ep()
    state = {"inflight": 0, "max": 0}

    def make_gen():
        async def gen():
            state["inflight"] += 1
            state["max"] = max(state["max"], state["inflight"])
            try:
                yield {"choices": [{"delta": {"content": "a"}}]}
                await asyncio.sleep(0.02)
                yield {"choices": [{"delta": {"content": "b"}}]}
            finally:
                state["inflight"] -= 1
        return gen()

    ep.provider.chat_stream = MagicMock(side_effect=lambda **kw: make_gen())

    async def one():
        g = await engine.run(
            group=group, messages=[{"role": "user", "content": "hi"}], mode="stream")
        return await _collect(g)

    # patch 必须在 gather 外层只进一次：并发任务各自进 patch 会把
    # _shared.load_endpoints 的 AsyncMock 泄漏到模块外（patch 栈错乱）。
    with _patch_select([ep]), \
         patch("botflow.pipeline._shared.get_config") as cfg:
        cfg.return_value = MagicMock(upstream_semaphore_size=1)
        await asyncio.wait_for(asyncio.gather(one(), one()), timeout=5)

    assert state["max"] == 1
