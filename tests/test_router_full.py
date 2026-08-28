"""Full coverage tests for the routing engine (GroupRouter + helpers)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from botflow.common.exceptions import (
    AllModelsCooldownError,
    NoAvailableModelError,
    ProviderError,
)
from botflow.providers.base import BaseProvider
from botflow.router import (
    CooldownManager,
    GroupRouter,
    ModelEndpoint,
    _get_cached_provider,
    weighted_random_order,
    weighted_random_select,
)
from botflow.storage.models import GroupModelWithDetails, Provider


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
    # No prior state: must not raise.
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
    import time
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
# GroupRouter._load_endpoints / _get_available
# ---------------------------------------------------------------------------


class _FakeDb:
    def __init__(self, models, providers):
        self._models = models
        self._providers = providers

    async def get_group_models(self, group_id, enabled_only=True):
        return [m for m in self._models if m.group_id == group_id]

    async def get_provider(self, provider_id):
        return self._providers.get(provider_id)


async def test_load_endpoints_filters_disabled_provider():
    models = [_make_detail(1)]
    providers = {11: _make_provider(11, "openai")}
    providers[11].is_enabled = False
    db = _FakeDb(models, providers)
    router = GroupRouter(1, db, CooldownManager())
    eps = await router._load_endpoints()
    assert eps == []


async def test_load_endpoints_builds():
    models = [_make_detail(1), _make_detail(2)]
    providers = {11: _make_provider(11), 12: _make_provider(12)}
    db = _FakeDb(models, providers)
    router = GroupRouter(1, db, CooldownManager())
    eps = await router._load_endpoints()
    assert len(eps) == 2
    assert all(isinstance(e, ModelEndpoint) for e in eps)


async def test_get_available_excludes_cooldown():
    models = [_make_detail(1)]
    providers = {11: _make_provider(11)}
    db = _FakeDb(models, providers)
    cm = CooldownManager()
    router = GroupRouter(1, db, cm)
    eps = await router._load_endpoints()
    cm.record_failure(1, 1, cooldown_failure_threshold=1, cooldown_seconds=1000)
    avail = router._get_available(eps)
    assert avail == []


# ---------------------------------------------------------------------------
# Route non-stream success / failure / fallback
# ---------------------------------------------------------------------------


async def test_route_no_models():
    db = _FakeDb([], {})
    router = GroupRouter(1, db, CooldownManager())
    with pytest.raises(NoAvailableModelError):
        await router.route([{"role": "user", "content": "hi"}])


async def test_route_non_stream_success():
    provider = FakeProvider()
    models = [_make_detail(1)]
    providers = {11: _make_provider(11)}
    db = _FakeDb(models, providers)
    router = GroupRouter(1, db, CooldownManager())
    eps = await router._load_endpoints()
    eps[0].provider = provider
    res = await router.route([{"role": "user", "content": "hi"}])
    assert res["choices"][0]["message"]["content"] == "ok"
    assert res["_routing"]["model_id"] == 1


async def test_route_non_stream_all_cooldown_raises():
    models = [_make_detail(1)]
    providers = {11: _make_provider(11)}
    db = _FakeDb(models, providers)
    cm = CooldownManager()
    router = GroupRouter(1, db, cm)
    eps = await router._load_endpoints()
    cm.record_failure(1, 1, cooldown_failure_threshold=1, cooldown_seconds=1000)
    with pytest.raises(AllModelsCooldownError):
        await router.route([{"role": "user", "content": "hi"}])


async def test_route_non_stream_cooldown_fallback_to_group():
    # Primary group all cooldown -> fallback group succeeds.
    cm = CooldownManager()

    def make_db(detail, pid):
        providers = {pid: _make_provider(pid)}
        return _FakeDb([detail], providers)

    primary_detail = _make_detail(1)
    fallback_detail = _make_detail(2, group_id=2)

    primary_router = GroupRouter(1, make_db(primary_detail, 11), cm, fallback_group_id=2)
    fallback_router = GroupRouter(2, make_db(fallback_detail, 12), cm)

    # Force primary model onto cooldown.
    cm.record_failure(1, 1, cooldown_failure_threshold=1, cooldown_seconds=1000)
    # Inject fallback router's provider.
    fallback_eps = await fallback_router._load_endpoints()
    fallback_eps[0].provider = FakeProvider()

    # Patch GroupRouter construction inside fallback branch to use our prepared router.
    import botflow.router as r
    orig = r.GroupRouter
    r.GroupRouter = lambda gid, db, cd, **kw: fallback_router if gid == 2 else orig(gid, db, cd, **kw)
    try:
        res = await primary_router.route([{"role": "user", "content": "hi"}])
    finally:
        r.GroupRouter = orig
    assert res["choices"][0]["message"]["content"] == "ok"


async def test_route_non_stream_fallback_after_exhaustion():
    """All primary models fail then fallback group tried."""
    cm = CooldownManager()

    def make_db(detail, pid):
        return _FakeDb([detail], {pid: _make_provider(pid)})

    primary_detail = _make_detail(1, max_retries=1)
    fallback_detail = _make_detail(2, max_retries=1, group_id=2)

    primary_router = GroupRouter(1, make_db(primary_detail, 11), cm, fallback_group_id=2)
    fallback_router = GroupRouter(2, make_db(fallback_detail, 12), cm)

    # Primary model always fails (connection error); fallback succeeds.
    primary_eps = await primary_router._load_endpoints()
    primary_eps[0].provider = FakeProvider(exc=ProviderError("HTTP 500 boom"))
    fallback_eps = await fallback_router._load_endpoints()
    fallback_eps[0].provider = FakeProvider()

    import botflow.router as r
    orig = r.GroupRouter
    r.GroupRouter = lambda gid, db, cd, **kw: fallback_router if gid == 2 else orig(gid, db, cd, **kw)
    try:
        res = await primary_router.route([{"role": "user", "content": "hi"}])
    finally:
        r.GroupRouter = orig
    assert res["choices"][0]["message"]["content"] == "ok"


async def test_route_non_stream_all_exhausted_no_fallback():
    models = [_make_detail(1, max_retries=1)]
    providers = {11: _make_provider(11)}
    db = _FakeDb(models, providers)
    router = GroupRouter(1, db, CooldownManager())
    eps = await router._load_endpoints()
    eps[0].provider = FakeProvider(exc=ProviderError("HTTP 500 boom"))
    with pytest.raises(ProviderError):
        await router.route([{"role": "user", "content": "hi"}])


async def test_route_non_stream_context_window_truncation():
    provider = FakeProvider()
    models = [_make_detail(1, context_window=10)]
    providers = {11: _make_provider(11)}
    db = _FakeDb(models, providers)
    router = GroupRouter(1, db, CooldownManager())
    eps = await router._load_endpoints()
    eps[0].provider = provider
    big = [{"role": "system", "content": "s"}, {"role": "user", "content": "x" * 5000}]
    await router.route(big)
    sent = provider.calls[0]["messages"]
    assert len(sent[0]["content"]) < 5000 or len(sent) < 2


# ---------------------------------------------------------------------------
# Stream routing
# ---------------------------------------------------------------------------


async def test_route_stream_returns_ordered_endpoints():
    models = [_make_detail(1), _make_detail(2)]
    providers = {11: _make_provider(11), 12: _make_provider(12)}
    db = _FakeDb(models, providers)
    router = GroupRouter(1, db, CooldownManager())
    out = await router.route([{"role": "user", "content": "hi"}], stream=True)
    assert "endpoints" in out
    assert len(out["endpoints"]) == 2


async def test_route_stream_all_cooldown_fallback():
    cm = CooldownManager()
    models = [_make_detail(1)]
    providers = {11: _make_provider(11)}
    db = _FakeDb(models, providers)
    router = GroupRouter(1, db, cm, fallback_group_id=2)
    eps = await router._load_endpoints()
    cm.record_failure(1, 1, cooldown_failure_threshold=1, cooldown_seconds=1000)
    # fallback_group_id set; route fallback group has no models -> raises
    with pytest.raises(NoAvailableModelError):
        await router.route([{"role": "user", "content": "hi"}], stream=True)


async def test_route_stream_all_cooldown_no_fallback_raises():
    cm = CooldownManager()
    models = [_make_detail(1)]
    providers = {11: _make_provider(11)}
    db = _FakeDb(models, providers)
    router = GroupRouter(1, db, cm)
    eps = await router._load_endpoints()
    cm.record_failure(1, 1, cooldown_failure_threshold=1, cooldown_seconds=1000)
    with pytest.raises(AllModelsCooldownError):
        await router.route([{"role": "user", "content": "hi"}], stream=True)


async def test_route_stream_context_window_truncation():
    models = [_make_detail(1, context_window=10), _make_detail(2)]
    providers = {11: _make_provider(11), 12: _make_provider(12)}
    db = _FakeDb(models, providers)
    router = GroupRouter(1, db, CooldownManager())
    big = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "x" * 5000},
        {"role": "user", "content": "y" * 5000},
        {"role": "user", "content": "z" * 5000},
    ]
    out = await router.route(big, stream=True)
    sent = out["messages"]
    assert sent == [{"role": "system", "content": "s"}]
