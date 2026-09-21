"""补充覆盖：router 与 storage.daily_summary 的零散分支。

- ``router._noop_asynccontext``：未配置信号量时返回的空上下文
- ``router._get_cached_provider``：per-model proxy 合并进 extra_config
- ``router.weighted_random_select``：浮点兜底（返回最后一个模型）
- ``daily_summary._generate_wiki``：未知策略 → ""、策略无 choices → ""
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from botflow import router
from botflow.pipeline.strategies import RandomWeightsStrategy
from botflow.router import (
    _get_cached_provider,
    _noop_asynccontext,
    weighted_random_select,
)
from botflow.storage.daily_summary import _generate_wiki
from botflow.storage.db import Database
from botflow.storage.models import GroupModelWithDetails, ModelGroup


def _detail(model_id: int, weight: float) -> GroupModelWithDetails:
    return GroupModelWithDetails(
        id=model_id,
        group_id=1,
        model_id=model_id,
        weight=weight,
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


# ---------------------------------------------------------------------------
# router
# ---------------------------------------------------------------------------


def test_noop_asynccontext_is_usable_context_manager():
    with _noop_asynccontext() as value:
        assert value is None


def test_get_cached_provider_merges_proxy_into_extra_config():
    router._provider_cache.clear()
    provider = _get_cached_provider(
        provider_id=101,
        provider_type="openai",
        api_key="k",
        base_url="http://upstream.local",
        extra_config={"timeout": 7},
        api_format="",
        proxy="http://proxy.local:7890",
    )
    assert provider.extra_config["proxy"] == "http://proxy.local:7890"
    assert provider.extra_config["timeout"] == 7
    # The proxy participates in the cache key, so a different proxy -> new instance.
    other = _get_cached_provider(
        provider_id=101,
        provider_type="openai",
        api_key="k",
        base_url="http://upstream.local",
        extra_config={"timeout": 7},
        api_format="",
        proxy="",
    )
    assert other is not provider
    router._provider_cache.clear()


def test_weighted_random_select_defensive_fallback_returns_last_model(monkeypatch):
    """r 落在累计权重之外（浮点边界）时兜底返回最后一个模型。"""
    monkeypatch.setattr(router.random, "uniform", lambda a, b: b)
    models = [_detail(1, 1.0), _detail(2, 1.0)]
    assert weighted_random_select(models).model_id == 2


def test_weighted_random_select_skips_zero_weight_models(monkeypatch):
    monkeypatch.setattr(router.random, "uniform", lambda a, b: 0.0)
    models = [_detail(1, 0.0), _detail(2, 2.0)]
    assert weighted_random_select(models).model_id == 2


def test_weighted_random_select_raises_when_total_weight_zero():
    from botflow.common.exceptions import NoAvailableModelError

    with pytest.raises(NoAvailableModelError):
        weighted_random_select([_detail(1, 0.0)])


# ---------------------------------------------------------------------------
# daily_summary._generate_wiki
# ---------------------------------------------------------------------------


def _group(type_: str) -> ModelGroup:
    return ModelGroup(id=1, name="default", type=type_, params={})


async def test_generate_wiki_returns_empty_for_unknown_strategy():
    db = MagicMock(spec=Database)
    db.list_groups = AsyncMock(return_value=[_group("not-a-strategy")])
    cfg = SimpleNamespace(summary_group="default")
    assert await _generate_wiki(db, "prompt", cfg) == ""


async def test_generate_wiki_returns_empty_when_strategy_yields_no_choices(monkeypatch):
    db = MagicMock(spec=Database)
    db.list_groups = AsyncMock(return_value=[_group("random_weights")])
    monkeypatch.setattr(
        RandomWeightsStrategy, "execute", AsyncMock(return_value={"foo": "bar"})
    )
    cfg = SimpleNamespace(summary_group="default")
    assert await _generate_wiki(db, "prompt", cfg) == ""


async def test_generate_wiki_returns_content_when_strategy_yields_choices(monkeypatch):
    db = MagicMock(spec=Database)
    db.list_groups = AsyncMock(return_value=[_group("random_weights")])
    monkeypatch.setattr(
        RandomWeightsStrategy,
        "execute",
        AsyncMock(return_value={"choices": [{"message": {"content": "# wiki"}}]}),
    )
    cfg = SimpleNamespace(summary_group="default")
    assert await _generate_wiki(db, "prompt", cfg) == "# wiki"


async def test_generate_wiki_returns_empty_when_no_groups():
    db = MagicMock(spec=Database)
    db.list_groups = AsyncMock(return_value=[])
    cfg = SimpleNamespace(summary_group="default")
    assert await _generate_wiki(db, "prompt", cfg) == ""
