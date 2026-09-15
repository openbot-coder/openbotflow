"""P0-1 缓存/限流单一事实源收敛测试：对象同一性 + 3 个 invalidate 行为。

覆盖 TC-01~TC-20、TC-33、TC-34（见 docs/tasks/P0-1_tests.md）。
只新建测试文件，不改动任何 src/ 源码。
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import botflow.pipeline._shared as shared
import botflow.router as router
from botflow.pipeline._shared import call_llm
from botflow.router import CooldownManager, ModelEndpoint
from botflow.storage.models import GroupModelWithDetails


# ---------------------------------------------------------------------------
# 自动清理共享全局缓存，避免用例间互相污染
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_caches():
    shared._endpoint_cache.clear()
    shared._provider_cache.clear()
    shared._provider_semaphores.clear()
    yield
    shared._endpoint_cache.clear()
    shared._provider_cache.clear()
    shared._provider_semaphores.clear()


@pytest.fixture
def cooldown() -> CooldownManager:
    return CooldownManager()


@pytest.fixture
def sample_model_detail() -> GroupModelWithDetails:
    return GroupModelWithDetails(
        id=1,
        group_id=1,
        model_id=100,
        weight=1.0,
        is_enabled=True,
        model_name="gpt-4o",
        display_name="GPT-4o",
        api_format="",
        provider_id=1,
        provider_name="openai-main",
        provider_type="openai",
        max_retries=2,
        cooldown_seconds=60,
        cooldown_failure_threshold=3,
        context_window=128000,
        proxy="",
        extra_config={},
    )


@pytest.fixture
def sample_endpoint(sample_model_detail) -> ModelEndpoint:
    mock_provider = MagicMock()
    return ModelEndpoint(sample_model_detail, mock_provider)


# ---------------------------------------------------------------------------
# 一、对象同一性 & re-export（F1/F2/F3）
# ---------------------------------------------------------------------------


# TC-01
def test_endpoint_cache_identity():
    assert shared._endpoint_cache is router._endpoint_cache


# TC-02
def test_provider_semaphores_identity():
    assert shared._provider_semaphores is router._provider_semaphores


# TC-03
def test_provider_cache_identity():
    assert shared._provider_cache is router._provider_cache


# TC-04
def test_reexport_symbols_are_same_objects():
    assert shared._get_cached_provider is router._get_cached_provider
    assert shared._ensure_provider_semaphore is router._ensure_provider_semaphore
    assert shared.load_endpoints is router.load_endpoints
    assert shared.invalidate_endpoint_cache is router.invalidate_endpoint_cache
    assert shared.invalidate_provider_cache is router.invalidate_provider_cache
    assert shared.invalidate_all_caches is router.invalidate_all_caches
    assert shared.PROVIDER_TYPE_MAP is router.PROVIDER_TYPE_MAP
    assert shared._ENDPOINT_CACHE_TTL == router._ENDPOINT_CACHE_TTL
    assert shared._PROVIDER_CACHE_TTL == router._PROVIDER_CACHE_TTL
    assert router._ENDPOINT_CACHE_TTL == 60
    assert router._PROVIDER_CACHE_TTL == 300


# TC-05
@pytest.mark.asyncio
async def test_patch_ensure_provider_semaphore_via_shared(sample_endpoint, cooldown):
    sem = asyncio.Semaphore(2)
    sample_endpoint.provider.chat = AsyncMock(return_value={"ok": True})
    with patch(
        "botflow.pipeline._shared._ensure_provider_semaphore", return_value=sem
    ), patch("botflow.pipeline._shared.get_config") as cfg:
        cfg.return_value = MagicMock(upstream_semaphore_size=0)
        result = await call_llm(
            sample_endpoint, messages=[], group_id=1, cooldown=cooldown
        )
    assert result is not None
    assert result["ok"] is True


# TC-06
def test_clear_on_shared_object_propagates():
    s_ec = shared._endpoint_cache
    s_ec[1] = (["fake"], time.time())
    r_ec = router._endpoint_cache
    r_ec.clear()
    assert 1 not in s_ec
    assert 1 not in r_ec


# ---------------------------------------------------------------------------
# 二、invalidate_endpoint_cache（F4）
# ---------------------------------------------------------------------------


# TC-07
def test_invalidate_endpoint_cache_pops():
    router._endpoint_cache[42] = (["fake"], time.time())
    router.invalidate_endpoint_cache(42)
    assert 42 not in router._endpoint_cache


# TC-08
def test_invalidate_endpoint_cache_missing_no_error():
    # 空缓存调用不存在的 group_id 不应抛异常
    router.invalidate_endpoint_cache(99999)


# TC-09
def test_invalidate_endpoint_cache_shared_via_either_module():
    router._endpoint_cache[7] = (["x"], time.time())
    shared.invalidate_endpoint_cache(7)
    assert 7 not in router._endpoint_cache


# ---------------------------------------------------------------------------
# 三、invalidate_provider_cache（F5）
# ---------------------------------------------------------------------------


# TC-10
def test_invalidate_provider_cache_none_clears_all():
    router._provider_cache[(1, "openai", "")] = ("v1", time.time())
    router._provider_cache[(2, "openai", "")] = ("v2", time.time())
    router.invalidate_provider_cache()
    assert router._provider_cache == {}


# TC-11
def test_invalidate_provider_cache_by_id():
    router._provider_cache[(1, "openai", "")] = ("v1", time.time())
    router._provider_cache[(1, "anthropic", "")] = ("v2", time.time())
    router._provider_cache[(2, "openai", "")] = ("v3", time.time())
    router.invalidate_provider_cache(1)
    assert (2, "openai", "") in router._provider_cache
    assert (1, "openai", "") not in router._provider_cache
    assert (1, "anthropic", "") not in router._provider_cache


# TC-12
def test_invalidate_provider_cache_missing_id_no_error():
    router._provider_cache[(5, "openai", "")] = ("v", time.time())
    router.invalidate_provider_cache(99999)  # 不应抛异常
    assert (5, "openai", "") in router._provider_cache


# TC-13
def test_invalidate_provider_cache_same_provider_multiple_type_proxy():
    pc = router._provider_cache
    pc[(1, "openai", "")] = ("a", time.time())
    pc[(1, "deepseek", "")] = ("b", time.time())
    pc[(1, "openai", "http://p")] = ("c", time.time())
    pc[(2, "openai", "")] = ("d", time.time())
    router.invalidate_provider_cache(1)
    assert all(
        k not in pc
        for k in [(1, "openai", ""), (1, "deepseek", ""), (1, "openai", "http://p")]
    )
    assert (2, "openai", "") in pc


# ---------------------------------------------------------------------------
# 四、invalidate_all_caches & 信号量保护（F6/F7）
# ---------------------------------------------------------------------------


# TC-14
def test_invalidate_all_caches_clears_both():
    router._endpoint_cache[1] = (["x"], time.time())
    router._provider_cache[(1, "openai", "")] = ("v", time.time())
    router.invalidate_all_caches()
    assert router._endpoint_cache == {}
    assert router._provider_cache == {}


# TC-15
def test_invalidate_all_caches_keeps_semaphores():
    sem = router._ensure_provider_semaphore(1, 5)
    router.invalidate_all_caches()
    assert router._provider_semaphores == {1: sem}
    assert router._provider_semaphores[1] is sem


# TC-16
def test_invalidate_provider_cache_keeps_semaphores():
    sem = router._ensure_provider_semaphore(1, 5)
    router.invalidate_provider_cache(1)
    assert router._provider_semaphores[1] is sem


# TC-17
def test_invalidate_endpoint_cache_keeps_semaphores():
    sem = router._ensure_provider_semaphore(1, 5)
    router.invalidate_endpoint_cache(1)
    assert 1 in router._provider_semaphores
    assert router._provider_semaphores[1] is sem


# TC-18
def test_semaphore_identity_after_all_invalidate_calls():
    sem = router._ensure_provider_semaphore(1, 5)
    router.invalidate_endpoint_cache(1)
    router.invalidate_provider_cache(None)
    router.invalidate_all_caches()
    assert router._provider_semaphores[1] is sem


# ---------------------------------------------------------------------------
# 五、load_endpoints 跨路径共享（F3/F1/F10）
# ---------------------------------------------------------------------------


# TC-19
@pytest.mark.asyncio
async def test_load_endpoints_writes_shared_cache_readable_by_router(cooldown):
    model = GroupModelWithDetails(
        id=1,
        group_id=1,
        model_id=100,
        weight=1.0,
        is_enabled=True,
        model_name="gpt-4o",
        display_name="GPT-4o",
        api_format="",
        provider_id=1,
        provider_name="p",
        provider_type="openai",
        max_retries=2,
        cooldown_seconds=60,
        cooldown_failure_threshold=3,
        context_window=128000,
        proxy="",
        extra_config={},
    )
    provider = MagicMock()
    provider.id = 1
    provider.provider_type = "openai"
    provider.api_key = "k"
    provider.base_url = "https://api.openai.com/v1"
    provider.extra_config = {}
    provider.is_enabled = True

    db = AsyncMock()
    db.get_group_models = AsyncMock(return_value=[model])
    db.get_provider = AsyncMock(return_value=provider)

    await shared.load_endpoints(group_id=1, db=db)
    # 第二次调用应命中共享缓存，不再查 DB
    await shared.load_endpoints(group_id=1, db=db)
    # 仅 load_endpoints 查过一次 DB，缓存命中不再查
    assert db.get_group_models.call_count == 1


# TC-20
def test_shared_load_endpoints_is_router_function():
    assert shared.load_endpoints is router.load_endpoints


# ---------------------------------------------------------------------------
# 七、回归契约（F10）
# ---------------------------------------------------------------------------


# TC-33
@pytest.mark.asyncio
async def test_existing_router_suite_contract(sample_endpoint, cooldown):
    # 轻量回归契约：monkeypatch botflow.pipeline._shared._ensure_provider_semaphore
    # 必须命中 call_llm 实际引用的模块属性（与 test_pipeline_base.py:544 一致）。
    sentinel = MagicMock()
    with patch(
        "botflow.pipeline._shared._ensure_provider_semaphore", return_value=None
    ) as m, patch("botflow.pipeline._shared.get_config") as cfg:
        cfg.return_value = MagicMock(upstream_semaphore_size=0)
        sample_endpoint.provider.chat = AsyncMock(return_value={"ok": True})
        result = await call_llm(
            sample_endpoint, messages=[], group_id=1, cooldown=cooldown
        )
        assert m.called
    assert result is not None
    # 对象同一性轻量校验（与 F2 验收标准一致）
    assert shared._endpoint_cache is router._endpoint_cache
    assert shared._provider_semaphores is router._provider_semaphores
    assert shared._provider_cache is router._provider_cache


# ---------------------------------------------------------------------------
# 反例：非 int id 不误删（F5/TC-34）
# ---------------------------------------------------------------------------


# TC-34
def test_invalidate_provider_cache_non_int_id_no_misdelete():
    router._provider_cache[(1, "openai", "")] = ("v", time.time())
    router.invalidate_provider_cache("1")  # 传字符串不应误删 int key
    assert (1, "openai", "") in router._provider_cache
