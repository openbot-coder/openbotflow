"""P0-1 provider 泄漏专项：被替换实例的延迟关闭（retire → aclose）。

覆盖面：
- BaseProvider.aclose 幂等默认实现 + 各 provider 的 _close_client；
- _get_cached_provider 过期重建 → 旧实例被延迟关闭；
- invalidate_provider_cache（含 provider_id 过滤）/ invalidate_all_caches → 旧实例被关闭；
- 仍在用的实例不会被立即关闭（grace 期内）；
- 无事件循环的同步调用方：入队等待，下一次有 loop 的 retire 时 flush；
- 关闭异常被吞掉、任务可取消。

技巧：把 _STALE_CLOSE_GRACE monkeypatch 成 0/极小值，让 grace 期立即到期。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock

import pytest

import botflow.router as router
from botflow.providers.anthropic_provider import AnthropicProvider
from botflow.providers.base import BaseProvider
from botflow.providers.deepseek_provider import DeepSeekProvider
from botflow.providers.google_provider import GoogleProvider
from botflow.providers.openai_compat import OpenAICompatProvider
from botflow.router import (
    _get_cached_provider,
    _pending_provider_closes,
    _provider_cache,
    _STALE_CLOSE_GRACE,
    invalidate_all_caches,
    invalidate_provider_cache,
)


class FakeProvider(BaseProvider):
    """Minimal concrete provider that counts close calls."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(
            api_key=kwargs.get("api_key", "k"),
            base_url=kwargs.get("base_url", ""),
            extra_config=kwargs.get("extra_config"),
        )
        self.close_count = 0

    async def chat(self, messages, model, temperature=None, max_tokens=None, stream=False, **kwargs):
        return {}

    async def chat_stream(self, messages, model, temperature=None, max_tokens=None, **kwargs):
        yield {}

    async def list_models(self):
        return []

    async def _close_client(self) -> None:
        self.close_count += 1


class BrokenCloser(FakeProvider):
    async def _close_client(self) -> None:
        raise RuntimeError("close boom")


class NakedProvider(BaseProvider):
    """实现抽象方法但不覆写 _close_client —— 用于测基类默认 no-op。"""

    async def chat(self, messages, model, temperature=None, max_tokens=None, stream=False, **kwargs):
        return {}

    async def chat_stream(self, messages, model, temperature=None, max_tokens=None, **kwargs):
        yield {}

    async def list_models(self):
        return []


@pytest.fixture(autouse=True)
def _clean_router_state(monkeypatch):
    """隔离共享缓存/待关闭队列；grace 默认清零以便同步观察关闭结果。"""
    monkeypatch.setattr(router, "_STALE_CLOSE_GRACE", 0.0)
    _provider_cache.clear()
    _pending_provider_closes.clear()
    yield
    _provider_cache.clear()
    _pending_provider_closes.clear()


async def _drain_tasks() -> None:
    """让 grace=0 的 _delayed_close 任务跑完。"""
    for _ in range(3):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# BaseProvider.aclose 默认实现 / 幂等
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_base_aclose_default_is_noop_and_idempotent():
    """默认 _close_client 为 no-op；aclose 两次不炸且只走一次 teardown。"""
    p = NakedProvider(api_key="k")
    await p.aclose()
    await p.aclose()
    assert p._closed is True


@pytest.mark.asyncio
async def test_retire_already_closed_instance_skips():
    """已关闭的实例再被 retire：直接跳过，不产生重复关闭任务。"""
    p = FakeProvider()
    await p.aclose()
    router._retire_provider(p)
    await _drain_tasks()
    assert p.close_count == 1
    assert _pending_provider_closes == []


# ---------------------------------------------------------------------------
# 各 provider 的 _close_client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_compat_aclose_closes_both_clients():
    p = OpenAICompatProvider(api_key="k", base_url="https://x/v1")
    client, azure = AsyncMock(), AsyncMock()
    p._client = client
    p._azure_client = azure
    await p.aclose()
    client.close.assert_awaited_once()
    azure.close.assert_awaited_once()
    assert p._client is None
    assert p._azure_client is None


@pytest.mark.asyncio
async def test_openai_compat_aclose_uninitialized_ok():
    """client 懒初始化、从未使用时 aclose 也不炸。"""
    p = OpenAICompatProvider(api_key="k")
    await p.aclose()
    assert p._closed is True


@pytest.mark.asyncio
async def test_anthropic_aclose_closes_client():
    p = AnthropicProvider(api_key="k")
    client = AsyncMock()
    p._client = client
    await p.aclose()
    client.close.assert_awaited_once()
    assert p._client is None


@pytest.mark.asyncio
async def test_anthropic_aclose_uninitialized_ok():
    p = AnthropicProvider(api_key="k")
    await p.aclose()
    assert p._closed is True


@pytest.mark.asyncio
async def test_deepseek_aclose_closes_client():
    p = DeepSeekProvider(api_key="k")  # 构造即建 AsyncOpenAI
    client = p._client
    await p.aclose()
    assert p._client is None
    # 真实 SDK close：幂等，重复调用安全
    await client.close()


@pytest.mark.asyncio
async def test_google_aclose_closes_async_client():
    p = GoogleProvider(api_key="k")
    client = AsyncMock()
    p._client = client
    await p.aclose()
    client.aio.aclose.assert_awaited_once()
    assert p._client is None


@pytest.mark.asyncio
async def test_google_aclose_uninitialized_ok():
    p = GoogleProvider(api_key="k")
    await p.aclose()
    assert p._closed is True


# ---------------------------------------------------------------------------
# _get_cached_provider：过期重建 → 旧实例被关闭
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_rebuild_closes_old_instance(monkeypatch):
    monkeypatch.setitem(router.PROVIDER_TYPE_MAP, "fake", FakeProvider)
    old = _get_cached_provider(1, "fake", "k", "http://x")
    monkeypatch.setattr(router, "_PROVIDER_CACHE_TTL", -1)
    new = _get_cached_provider(1, "fake", "k", "http://x")
    assert new is not old
    await _drain_tasks()
    assert old.close_count == 1  # 旧实例最终被关闭
    assert new.close_count == 0  # 新实例不受影响


@pytest.mark.asyncio
async def test_cache_hit_returns_same_instance_without_close(monkeypatch):
    """热路径回归契约：命中缓存返回同一实例、不触发任何关闭。"""
    monkeypatch.setitem(router.PROVIDER_TYPE_MAP, "fake", FakeProvider)
    p1 = _get_cached_provider(7, "fake", "k", "http://x")
    p2 = _get_cached_provider(7, "fake", "k", "http://x")
    assert p1 is p2
    await _drain_tasks()
    assert p1.close_count == 0


# ---------------------------------------------------------------------------
# invalidate_provider_cache / invalidate_all_caches → 关闭旧实例
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalidate_provider_cache_by_id_closes_matching_only():
    p1a, p1b, p2 = FakeProvider(), FakeProvider(), FakeProvider()
    now = time.time()
    _provider_cache[(1, "openai", "")] = (p1a, now)
    _provider_cache[(1, "anthropic", "")] = (p1b, now)
    _provider_cache[(2, "openai", "")] = (p2, now)

    invalidate_provider_cache(1)
    await _drain_tasks()

    assert (1, "openai", "") not in _provider_cache
    assert (1, "anthropic", "") not in _provider_cache
    assert (2, "openai", "") in _provider_cache
    assert p1a.close_count == 1
    assert p1b.close_count == 1
    assert p2.close_count == 0


@pytest.mark.asyncio
async def test_invalidate_provider_cache_none_closes_all():
    p1, p2 = FakeProvider(), FakeProvider()
    now = time.time()
    _provider_cache[(1, "openai", "")] = (p1, now)
    _provider_cache[(2, "openai", "")] = (p2, now)

    invalidate_provider_cache()
    await _drain_tasks()

    assert _provider_cache == {}
    assert p1.close_count == 1
    assert p2.close_count == 1


@pytest.mark.asyncio
async def test_invalidate_all_caches_closes_providers():
    p1, p2 = FakeProvider(), FakeProvider()
    now = time.time()
    _provider_cache[(1, "openai", "")] = (p1, now)
    _provider_cache[(2, "openai", "")] = (p2, now)

    invalidate_all_caches()
    await _drain_tasks()

    assert _provider_cache == {}
    assert p1.close_count == 1
    assert p2.close_count == 1


# ---------------------------------------------------------------------------
# 时序保证：grace 期内不关（in-flight 安全）；grace 过后必关
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retired_instance_not_closed_during_grace(monkeypatch):
    """替换后新请求立即用新实例；旧实例在 grace 期内保持可用。"""
    monkeypatch.setattr(router, "_STALE_CLOSE_GRACE", 0.5)
    old = FakeProvider()
    now = time.time()
    _provider_cache[(1, "openai", "")] = (old, now)
    monkeypatch.setattr(router, "_PROVIDER_CACHE_TTL", -1)
    monkeypatch.setitem(router.PROVIDER_TYPE_MAP, "openai", FakeProvider)

    new = _get_cached_provider(1, "openai", "k", "http://x")
    assert new is not old
    await asyncio.sleep(0.05)  # grace 未到
    assert old.close_count == 0

    await asyncio.sleep(0.7)  # grace 到期，后台任务完成关闭
    assert old.close_count == 1


# ---------------------------------------------------------------------------
# 无事件循环的同步调用方：入队，下次有 loop 的 retire 时 flush
# ---------------------------------------------------------------------------


def test_retire_without_running_loop_queues_instance(monkeypatch):
    def _no_loop():
        raise RuntimeError("no running event loop")

    monkeypatch.setattr(router.asyncio, "get_running_loop", _no_loop)
    p = FakeProvider()
    router._retire_provider(p)
    assert _pending_provider_closes == [p]
    assert p.close_count == 0


@pytest.mark.asyncio
async def test_sync_queued_entries_flushed_on_next_retire():
    """同步上下文入队的旧实例，由下一次有 loop 的 retire 统一 flush 关闭。"""
    queued = FakeProvider()
    _pending_provider_closes.append(queued)  # 模拟同步调用方留下的队列

    fresh = FakeProvider()
    router._retire_provider(fresh)  # loop 内 → flush 队列 + 本实例
    await _drain_tasks()

    assert queued.close_count == 1
    assert fresh.close_count == 1
    assert _pending_provider_closes == []


# ---------------------------------------------------------------------------
# 关闭健壮性：异常吞掉 / 任务可取消
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delayed_close_swallows_close_errors():
    bad = BrokenCloser()
    await router._delayed_close(bad)  # 不抛异常
    assert bad._closed is True


@pytest.mark.asyncio
async def test_delayed_close_task_is_cancellable(monkeypatch):
    monkeypatch.setattr(router, "_STALE_CLOSE_GRACE", 100.0)
    p = FakeProvider()
    task = asyncio.create_task(router._delayed_close(p))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert p.close_count == 0


def test_grace_is_bounded_not_zero():
    """契约：grace 必须为正（in-flight 保护），且有上界（fd 最终释放）。"""
    assert 0 < _STALE_CLOSE_GRACE <= 600
