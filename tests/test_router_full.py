"""Full coverage tests for the routing engine (PipelineEngine + helpers)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from botflow.common.exceptions import (
    AllModelsCooldownError,
    NoAvailableModelError,
    ProviderError,
)
from botflow.pipeline.strategies import RandomWeightsStrategy
from botflow.providers.base import BaseProvider
from botflow.router import (
    CooldownManager,
    ModelEndpoint,
    _get_cached_provider,
    weighted_random_order,
    weighted_random_select,
)
from botflow.storage.models import GroupModelWithDetails, ModelGroup, Provider


def _make_detail(model_id: int, name: str = "m", weight: float = 1.0, group_id: int = 1, **kw) -> GroupModelWithDetails:
    return GroupModelWithDetails(
        id=model_id,
        group_id=group_id,
        model_id=model_id,
        weight=weight,
        is_enabled=True,
        model_name=name,
        display_name=name,
        provider_id=10 + model_id,
        provider_name="p",
        provider_type="openai",
        max_retries=kw.get("max_retries", 1),
        cooldown_seconds=kw.get("cooldown_seconds", 60),
        cooldown_failure_threshold=kw.get("cooldown_failure_threshold", 3),
        context_window=kw.get("context_window", 0),
    )


def _make_provider(pid: int = 20, ptype: str = "openai") -> Provider:
    return Provider(id=pid, name="prov", provider_type=ptype, api_key="k", base_url="http://x")


def _make_group(group_id: int = 1, name: str = "default", type_: str = "random_weights",
                params: dict | None = None, fallback_group_id: int | None = None) -> ModelGroup:
    return ModelGroup(
        id=group_id, name=name, description="test", is_enabled=True,
        type=type_, params=params or {}, fallback_group_id=fallback_group_id,
        created_at="2026-01-01T00:00:00", updated_at="2026-01-01T00:00:00",
    )


import botflow.router as _rt


@pytest.fixture(autouse=True)
def _clear_endpoint_cache():
    _rt._endpoint_cache.clear()
    yield
    _rt._endpoint_cache.clear()


class FakeProvider(BaseProvider):
    def __init__(self, response=None, exc=None, **kwargs):
        self._response = response or {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        self._exc = exc
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        return self._response

    async def chat_stream(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        yield {"choices": [{"delta": {"content": "ok"}}]}

    async def list_models(self):
        return ["fake-model"]


# ---------------------------------------------------------------------------
# Module-level init + global cooldown manager
# ---------------------------------------------------------------------------


def test_cooldown_manager_constructible():
    cm = CooldownManager()
    assert cm is not None


def test_get_cached_provider_creates_and_caches():
    p = _get_cached_provider(1, "openai", "k", "http://x")
    p2 = _get_cached_provider(1, "openai", "k", "http://x")
    assert p is p2  # cached


def test_get_cached_provider_unsupported():
    with pytest.raises(ValueError):
        _get_cached_provider(2, "nonsense", "k", "http://x")


def test_get_cached_provider_refreshes_on_ttl(monkeypatch):
    import botflow.router as r
    monkeypatch.setattr(r, "_PROVIDER_CACHE_TTL", -1)
    p1 = _get_cached_provider(3, "openai", "k", "http://x")
    p2 = _get_cached_provider(3, "openai", "k", "http://x")
    assert p1 is not p2


# ---------------------------------------------------------------------------
# CooldownManager
# ---------------------------------------------------------------------------


def test_cooldown_record_success_no_prior_state():
    cm = CooldownManager()
    cm.record_success(1, 1)
    assert cm.get_failure_count(1, 1) == 0


def test_cooldown_record_failure_under_threshold():
    cm = CooldownManager()
    cm.record_failure(1, 1, cooldown_failure_threshold=3, cooldown_seconds=60)
    assert cm.get_failure_count(1, 1) == 1
    assert not cm.is_on_cooldown(1, 1)


def test_cooldown_record_failure_over_threshold():
    cm = CooldownManager()
    for _ in range(3):
        cm.record_failure(1, 1, cooldown_failure_threshold=3, cooldown_seconds=60)
    assert cm.is_on_cooldown(1, 1)


def test_cooldown_expiry_reset():
    cm = CooldownManager()
    cm.record_failure(1, 1, cooldown_failure_threshold=1, cooldown_seconds=-10)  # already past
    assert not cm.is_on_cooldown(1, 1)
    assert cm.get_failure_count(1, 1) == 0


def test_cooldown_get_all_active_none():
    cm = CooldownManager()
    assert cm.get_all_active_cooldowns() == []


def test_cooldown_get_all_active_some():
    cm = CooldownManager()
    cm.record_failure(1, 1, cooldown_failure_threshold=1, cooldown_seconds=1000)
    out = cm.get_all_active_cooldowns()
    assert len(out) == 1
    assert out[0]["group_id"] == 1 and out[0]["model_id"] == 1


def test_cooldown_restore_expired():
    cm = CooldownManager()
    import time
    cm.restore_state(2, 2, failures=5, cooldown_until=time.time() - 1)  # already expired
    assert cm.get_failure_count(2, 2) == 0


def test_cooldown_restore_active():
    cm = CooldownManager()
    import time
    cm.restore_state(2, 2, failures=5, cooldown_until=time.time() + 1000)
    assert cm.is_on_cooldown(2, 2)
    assert cm.get_failure_count(2, 2) == 5


# ---------------------------------------------------------------------------
# Weighted selection
# ---------------------------------------------------------------------------


def test_weighted_select_empty_raises():
    with pytest.raises(NoAvailableModelError):
        weighted_random_select([])


def test_weighted_select_all_zero_raises():
    with pytest.raises(NoAvailableModelError):
        weighted_random_select([_make_detail(1, weight=0), _make_detail(2, weight=0)])


def test_weighted_select_skips_zero_weight():
    models = [_make_detail(1, weight=0), _make_detail(2, weight=1)]
    sel = weighted_random_select(models)
    assert sel.model_id == 2


def test_weighted_order_empty_raises():
    with pytest.raises(NoAvailableModelError):
        weighted_random_order([])


def test_weighted_order_full():
    models = [_make_detail(1, weight=1), _make_detail(2, weight=1), _make_detail(3, weight=1)]
    ordered = weighted_random_order(models)
    assert {m.model_id for m in ordered} == {1, 2, 3}
    assert len(ordered) == 3


# ---------------------------------------------------------------------------
# load_endpoints (standalone function in router.py)
# ---------------------------------------------------------------------------


class _FakeDb:
    def __init__(self, models, providers, groups=None):
        self._models = models
        self._providers = providers
        self._groups = groups or {}
        self._group_models_cache = None

    async def get_group(self, group_id):
        return self._groups.get(group_id)

    async def get_group_models(self, group_id, enabled_only=True):
        return [m for m in self._models if m.group_id == group_id]

    async def get_provider(self, provider_id):
        return self._providers.get(provider_id)


async def test_load_endpoints_filters_disabled_provider():
    from botflow.router import load_endpoints
    models = [_make_detail(1)]
    providers = {11: _make_provider(11, "openai")}
    providers[11].is_enabled = False
    db = _FakeDb(models, providers)
    eps = await load_endpoints(1, db)
    assert eps == []


async def test_load_endpoints_builds():
    from botflow.router import load_endpoints
    models = [_make_detail(1), _make_detail(2)]
    providers = {11: _make_provider(11), 12: _make_provider(12)}
    db = _FakeDb(models, providers)
    eps = await load_endpoints(1, db)
    assert len(eps) == 2
    assert all(isinstance(e, ModelEndpoint) for e in eps)


async def test_filter_available_excludes_cooldown():
    from botflow.pipeline._shared import filter_available
    from botflow.router import load_endpoints
    models = [_make_detail(1)]
    providers = {11: _make_provider(11)}
    db = _FakeDb(models, providers)
    cm = CooldownManager()
    eps = await load_endpoints(1, db)
    cm.record_failure(1, 1, cooldown_failure_threshold=1, cooldown_seconds=1000)
    avail = filter_available(eps, cm, 1)
    assert avail == []


# ---------------------------------------------------------------------------
# PipelineEngine route non-stream success / failure / fallback
# ---------------------------------------------------------------------------


async def test_route_no_models():
    from botflow.pipeline.engine import PipelineEngine
    db = _FakeDb([], {})
    engine = PipelineEngine(db_factory=lambda: db, cooldown=CooldownManager())
    group = _make_group()
    with pytest.raises(NoAvailableModelError):
        await engine.route(group=group, messages=[{"role": "user", "content": "hi"}])


async def test_route_non_stream_success():
    from botflow.pipeline.engine import PipelineEngine
    provider = FakeProvider()
    models = [_make_detail(1)]
    providers = {11: _make_provider(11)}
    db = _FakeDb(models, providers)
    engine = PipelineEngine(db_factory=lambda: db, cooldown=CooldownManager())
    group = _make_group()
    # Inject fake provider
    from botflow.router import _endpoint_cache, load_endpoints
    eps = await load_endpoints(1, db)
    eps[0].provider = provider
    _endpoint_cache[1] = (eps, time.time())  # force cache
    res = await engine.route(group=group, messages=[{"role": "user", "content": "hi"}])
    assert res["choices"][0]["message"]["content"] == "ok"
    assert res["_routing"]["model_id"] == 1


async def test_route_non_stream_all_cooldown_raises():
    from botflow.pipeline.engine import PipelineEngine
    models = [_make_detail(1)]
    providers = {11: _make_provider(11)}
    db = _FakeDb(models, providers)
    cm = CooldownManager()
    engine = PipelineEngine(db_factory=lambda: db, cooldown=cm)
    group = _make_group()
    cm.record_failure(1, 1, cooldown_failure_threshold=1, cooldown_seconds=1000)
    with pytest.raises(AllModelsCooldownError):
        await engine.route(group=group, messages=[{"role": "user", "content": "hi"}])


async def test_route_non_stream_cooldown_fallback_to_group():
    from botflow.pipeline.engine import PipelineEngine
    cm = CooldownManager()

    models_primary = [_make_detail(1)]
    models_fallback = [_make_detail(2, group_id=2)]
    providers = {11: _make_provider(11), 12: _make_provider(12)}

    primary_db = _FakeDb(models_primary, providers)
    fallback_db = _FakeDb(models_fallback, providers)

    primary_group = _make_group(group_id=1, fallback_group_id=2)
    fallback_group = _make_group(group_id=2, name="fallback")

    # Force primary model onto cooldown
    cm.record_failure(1, 1, cooldown_failure_threshold=1, cooldown_seconds=1000)

    # Prepare fallback provider
    from botflow.router import _endpoint_cache, load_endpoints
    fallback_eps = await load_endpoints(2, fallback_db)
    fallback_eps[0].provider = FakeProvider()
    _endpoint_cache[2] = (fallback_eps, time.time())

    # Use primary DB but patch get_group for fallback
    async def patched_get_group(gid):
        return fallback_group if gid == 2 else primary_group

    primary_db.get_group = patched_get_group

    engine = PipelineEngine(db_factory=lambda: primary_db, cooldown=cm)
    res = await engine.route(group=primary_group, messages=[{"role": "user", "content": "hi"}])
    assert res["choices"][0]["message"]["content"] == "ok"


async def test_route_non_stream_fallback_after_exhaustion():
    from botflow.pipeline.engine import PipelineEngine
    cm = CooldownManager()

    models_primary = [_make_detail(1, max_retries=1)]
    models_fallback = [_make_detail(2, max_retries=1, group_id=2)]
    providers = {11: _make_provider(11), 12: _make_provider(12)}

    primary_db = _FakeDb(models_primary, providers)
    fallback_db = _FakeDb(models_fallback, providers)

    primary_group = _make_group(group_id=1, fallback_group_id=2)
    fallback_group = _make_group(group_id=2, name="fallback")

    # Primary model always fails
    from botflow.router import _endpoint_cache, load_endpoints
    primary_eps = await load_endpoints(1, primary_db)
    primary_eps[0].provider = FakeProvider(exc=ProviderError("HTTP 500 boom"))
    _endpoint_cache[1] = (primary_eps, time.time())

    # Fallback model succeeds
    fallback_eps = await load_endpoints(2, fallback_db)
    fallback_eps[0].provider = FakeProvider()
    _endpoint_cache[2] = (fallback_eps, time.time())

    async def patched_get_group(gid):
        return fallback_group if gid == 2 else primary_group

    primary_db.get_group = patched_get_group

    engine = PipelineEngine(db_factory=lambda: primary_db, cooldown=cm)
    res = await engine.route(group=primary_group, messages=[{"role": "user", "content": "hi"}])
    assert res["choices"][0]["message"]["content"] == "ok"


async def test_route_non_stream_all_exhausted_no_fallback():
    from botflow.pipeline.engine import PipelineEngine
    models = [_make_detail(1, max_retries=1)]
    providers = {11: _make_provider(11)}
    db = _FakeDb(models, providers)
    engine = PipelineEngine(db_factory=lambda: db, cooldown=CooldownManager())
    group = _make_group()
    from botflow.router import _endpoint_cache, load_endpoints
    eps = await load_endpoints(1, db)
    eps[0].provider = FakeProvider(exc=ProviderError("HTTP 500 boom"))
    _endpoint_cache[1] = (eps, 0.0)
    with pytest.raises(ProviderError):
        await engine.route(group=group, messages=[{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# Stream routing via PipelineEngine.route_stream
# ---------------------------------------------------------------------------


async def test_route_stream_returns_ordered_endpoints(monkeypatch):
    """SG-1 §3.3：route_stream 合并进 run(mode="stream")，选端点语义不变（经 spy 捕获断言）。"""
    from botflow.pipeline.engine import PipelineEngine

    models = [_make_detail(1), _make_detail(2)]
    providers = {11: _make_provider(11), 12: _make_provider(12)}
    db = _FakeDb(models, providers)
    engine = PipelineEngine(db_factory=lambda: db, cooldown=CooldownManager())
    group = _make_group()

    captured: dict = {}
    orig = RandomWeightsStrategy.select_endpoints

    async def _spy(self_, *a, **k):
        res = await orig(self_, *a, **k)
        captured["endpoints"] = res.endpoints
        return res

    monkeypatch.setattr(RandomWeightsStrategy, "select_endpoints", _spy)

    class _Done(Exception):
        pass

    def _abort_writer():
        raise _Done()

    # SG-1: 流式图不再调用 call_llm（端点级流式调用在图内直接用 provider.chat_stream），
    # 中止点改到 try_stream 最早期执行的 get_stream_writer()。
    monkeypatch.setattr(
        "botflow.pipeline.langgraph_engine.get_stream_writer", _abort_writer,
    )
    with pytest.raises(_Done):
        # run(mode="stream") 为 async def，需先 await 取事件异步生成器（与 core._drive 一致）
        async for _ in await engine.run(group=group, mode="stream", messages=[{"role": "user", "content": "hi"}]):
            pass
    assert len(captured["endpoints"]) == 2


# NOTE (SG-1 §3.2 第 7 组): 原 test_route_stream_all_cooldown_fallback 依赖 route_stream
# 在图内解析 fallback 组（fallback 组无模型 → NoAvailableModelError）。SG-1 把组级 fallback 移出
# 图外，改由驱动 core._drive 负责；engine.run(mode="stream") 本身不再做跨组 fallback。
# 该语义已迁到驱动层 T1.2 / T1.6（tests/test_core_runtime.py），此处不再保留冗余用例。


async def test_route_stream_all_cooldown_no_fallback_raises(monkeypatch):
    """SG-1 §3.3：route_stream 合并进 run(mode="stream")；无 fallback → AllModelsCooldownError。"""
    from botflow.pipeline.engine import PipelineEngine

    cm = CooldownManager()
    models = [_make_detail(1)]
    providers = {11: _make_provider(11)}
    db = _FakeDb(models, providers)
    group = _make_group()
    cm.record_failure(1, 1, cooldown_failure_threshold=1, cooldown_seconds=1000)
    engine = PipelineEngine(db_factory=lambda: db, cooldown=cm)
    with pytest.raises(AllModelsCooldownError):
        # run(mode="stream") 为 async def，先 await 取事件生成器（与 core._drive 契约一致）
        async for _ in await engine.run(group=group, mode="stream", messages=[{"role": "user", "content": "hi"}]):
            pass


async def test_route_stream_context_window_truncation(monkeypatch):
    """SG-1 §3.3：route_stream 合并进 run(mode="stream")；上下文窗口截断语义不变（经 spy 捕获断言）。"""
    from botflow.pipeline.engine import PipelineEngine

    models = [_make_detail(1, context_window=10), _make_detail(2)]
    providers = {11: _make_provider(11), 12: _make_provider(12)}
    db = _FakeDb(models, providers)
    engine = PipelineEngine(db_factory=lambda: db, cooldown=CooldownManager())
    group = _make_group()
    big = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "x" * 5000},
        {"role": "user", "content": "y" * 5000},
        {"role": "user", "content": "z" * 5000},
    ]

    captured: dict = {}
    orig = RandomWeightsStrategy.select_endpoints

    async def _spy(self_, *a, **k):
        res = await orig(self_, *a, **k)
        captured["messages"] = res.messages
        return res

    monkeypatch.setattr(RandomWeightsStrategy, "select_endpoints", _spy)

    class _Done(Exception):
        pass

    def _abort_writer():
        raise _Done()

    # 见 test_route_stream_returns_ordered_endpoints：中止点迁到 get_stream_writer()。
    monkeypatch.setattr(
        "botflow.pipeline.langgraph_engine.get_stream_writer", _abort_writer,
    )
    with pytest.raises(_Done):
        async for _ in await engine.run(group=group, mode="stream", messages=big):
            pass
    assert captured["messages"] == [{"role": "system", "content": "s"}]
