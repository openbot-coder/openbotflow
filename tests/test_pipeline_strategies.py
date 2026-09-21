"""Tests for P1-3: 3 built-in pipeline strategies.

Covers 20 test scenarios from docs/tasks/P1-3_tests.md:
- RandomWeightsStrategy (7)
- RoundRobinStrategy (6)
- SequentialStrategy (5)
- Registry / exports (2)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from botflow.common.exceptions import NoAvailableModelError, ProviderError
from botflow.pipeline.base import STRATEGY_REGISTRY, RouteResult
from botflow.router import CooldownManager, ModelEndpoint
from botflow.storage.models import GroupModelWithDetails


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_endpoint(
    model_id: int = 1,
    weight: float = 1.0,
    context_window: int = 8192,
    max_retries: int = 3,
    cooldown_failure_threshold: int = 3,
    cooldown_seconds: int = 60,
    provider_id: int = 1,
    model_name: str = "test-model",
) -> ModelEndpoint:
    """Create a ModelEndpoint for testing."""
    detail = GroupModelWithDetails(
        id=model_id,
        group_id=1,
        model_id=model_id,
        weight=weight,
        is_enabled=True,
        model_name=model_name,
        display_name=model_name,
        provider_id=provider_id,
        provider_name="test-provider",
        provider_type="openai",
        max_retries=max_retries,
        cooldown_seconds=cooldown_seconds,
        cooldown_failure_threshold=cooldown_failure_threshold,
        context_window=context_window,
    )
    mock_provider = MagicMock()
    return ModelEndpoint(detail, mock_provider)


def make_mock_db(endpoints: list[ModelEndpoint]) -> MagicMock:
    """Create a mock Database whose get_group_models returns endpoint details."""
    db = AsyncMock()
    db.get_group_models.return_value = [ep.detail for ep in endpoints]
    db.get_provider.return_value = MagicMock(id=1, is_enabled=True)
    return db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_round_robin_counters():
    """Clear RoundRobin class counters before and after each test."""
    from botflow.pipeline.strategies import RoundRobinStrategy
    RoundRobinStrategy._counters.clear()
    yield
    RoundRobinStrategy._counters.clear()


@pytest.fixture
def cooldown():
    return CooldownManager()


# Convenience patch targets — all _shared imports are patched at the
# ``botflow.pipeline.strategies`` namespace (where they are imported).
_PATCH_LOAD = "botflow.pipeline._shared.load_endpoints"
_PATCH_FILTER = "botflow.pipeline._shared.filter_available"
_PATCH_TRUNCATE = "botflow.pipeline._shared.truncate_messages"
_PATCH_CALL_LLM = "botflow.pipeline._shared.call_llm"


# ===========================================================================
# 1. RandomWeightsStrategy (7 tests)
# ===========================================================================


class TestRandomWeightsStrategy:

    def _make_strategy(self):
        from botflow.pipeline.strategies import RandomWeightsStrategy
        return RandomWeightsStrategy(params={})

    # -- 1.1 select_endpoints returns RouteResult ---------------------------

    async def test_select_returns_route_result(self):
        ep1 = make_endpoint(model_id=1, weight=1.0)
        ep2 = make_endpoint(model_id=2, weight=1.0)
        strategy = self._make_strategy()
        db = make_mock_db([ep1, ep2])
        cooldown = CooldownManager()
        messages = [{"role": "user", "content": "hello"}]

        with patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[ep1, ep2]), \
             patch(_PATCH_FILTER, return_value=[ep1, ep2]), \
             patch(_PATCH_TRUNCATE, return_value=messages):
            result = await strategy.select_endpoints(messages, db, cooldown, group_id=1)

        assert isinstance(result, RouteResult)
        assert len(result.endpoints) == 2
        assert result.messages == messages
        assert result.temperature is None
        assert result.max_tokens is None
        assert result.extra_kwargs == {}

    # -- 1.2 weighted random distribution -----------------------------------

    async def test_distribution(self):
        ep_heavy = make_endpoint(model_id=1, weight=3.0, model_name="heavy")
        ep_light = make_endpoint(model_id=2, weight=1.0, model_name="light")
        strategy = self._make_strategy()
        db = make_mock_db([ep_heavy, ep_light])
        cooldown = CooldownManager()
        messages = [{"role": "user", "content": "hello"}]

        counts = {1: 0, 2: 0}
        N = 1000
        with patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[ep_heavy, ep_light]), \
             patch(_PATCH_FILTER, return_value=[ep_heavy, ep_light]), \
             patch(_PATCH_TRUNCATE, return_value=messages):
            for _ in range(N):
                result = await strategy.select_endpoints(messages, db, cooldown, group_id=1)
                counts[result.endpoints[0].model_id] += 1

        ratio = counts[1] / N
        assert 0.65 < ratio < 0.85, f"Heavy model ratio {ratio:.2f} not in expected range"

    # -- 1.3 cooldown model skipped -----------------------------------------

    async def test_skips_cooldown(self):
        ep1 = make_endpoint(model_id=1, weight=1.0)
        ep2 = make_endpoint(model_id=2, weight=1.0)
        ep3 = make_endpoint(model_id=3, weight=1.0)
        strategy = self._make_strategy()
        db = make_mock_db([ep1, ep2, ep3])
        cooldown = CooldownManager()
        messages = [{"role": "user", "content": "hello"}]

        available = [ep1, ep3]  # ep2 in cooldown

        with patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[ep1, ep2, ep3]), \
             patch(_PATCH_FILTER, return_value=available), \
             patch(_PATCH_TRUNCATE, return_value=messages):
            result = await strategy.select_endpoints(messages, db, cooldown, group_id=1)

        returned_ids = {ep.model_id for ep in result.endpoints}
        assert 2 not in returned_ids
        assert len(result.endpoints) == 2

    # -- 1.4 all cooldown → NoAvailableModelError ---------------------------

    async def test_all_cooldown_raises(self):
        ep1 = make_endpoint(model_id=1, weight=1.0)
        strategy = self._make_strategy()
        db = make_mock_db([ep1])
        cooldown = CooldownManager()
        messages = [{"role": "user", "content": "hello"}]

        with patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[ep1]), \
             patch(_PATCH_FILTER, return_value=[]):
            with pytest.raises(NoAvailableModelError):
                await strategy.select_endpoints(messages, db, cooldown, group_id=1)

    # -- 1.5 context window truncation --------------------------------------

    async def test_truncation(self):
        ep1 = make_endpoint(model_id=1, weight=1.0, context_window=4096)
        strategy = self._make_strategy()
        db = make_mock_db([ep1])
        cooldown = CooldownManager()
        messages = [{"role": "user", "content": "x" * 10000}]
        truncated = [{"role": "user", "content": "x" * 4000}]

        with patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[ep1]), \
             patch(_PATCH_FILTER, return_value=[ep1]), \
             patch(_PATCH_TRUNCATE, return_value=truncated) as mock_truncate:
            result = await strategy.select_endpoints(
                messages, db, cooldown, group_id=1, max_tokens=2000,
            )

        mock_truncate.assert_called_once_with(messages, [ep1], 2000)
        assert result.messages == truncated

    # -- 1.6 execute success ------------------------------------------------

    async def test_execute_success(self):
        ep1 = make_endpoint(model_id=1, weight=1.0)
        strategy = self._make_strategy()
        db = make_mock_db([ep1])
        cooldown = CooldownManager()
        messages = [{"role": "user", "content": "hello"}]
        llm_response = {"choices": [{"message": {"content": "hi"}}]}

        with patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[ep1]), \
             patch(_PATCH_FILTER, return_value=[ep1]), \
             patch(_PATCH_TRUNCATE, return_value=messages), \
             patch(_PATCH_CALL_LLM, new_callable=AsyncMock, return_value=(llm_response, None)) as mock_call:
            result = await strategy.execute(messages, db, cooldown, group_id=1)

        assert result == llm_response
        mock_call.assert_called_once()

    # -- 1.7 execute all fail → ProviderError -------------------------------

    async def test_execute_all_fail(self):
        ep1 = make_endpoint(model_id=1, weight=1.0)
        ep2 = make_endpoint(model_id=2, weight=1.0)
        strategy = self._make_strategy()
        db = make_mock_db([ep1, ep2])
        cooldown = CooldownManager()
        messages = [{"role": "user", "content": "hello"}]

        with patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[ep1, ep2]), \
             patch(_PATCH_FILTER, return_value=[ep1, ep2]), \
             patch(_PATCH_TRUNCATE, return_value=messages), \
             patch(_PATCH_CALL_LLM, new_callable=AsyncMock, return_value=(None, ProviderError("call_llm failed"))):
            with pytest.raises(ProviderError, match="All endpoints failed"):
                await strategy.execute(messages, db, cooldown, group_id=1)


# ===========================================================================
# 2. RoundRobinStrategy (6 tests)
# ===========================================================================


class TestRoundRobinStrategy:

    def _make_strategy(self):
        from botflow.pipeline.strategies import RoundRobinStrategy
        return RoundRobinStrategy(params={})

    # -- 2.1 first selection → index 0 --------------------------------------

    async def test_first_selection(self):
        ep1 = make_endpoint(model_id=1, weight=1.0, model_name="model-A")
        ep2 = make_endpoint(model_id=2, weight=1.0, model_name="model-B")
        ep3 = make_endpoint(model_id=3, weight=1.0, model_name="model-C")
        strategy = self._make_strategy()
        db = make_mock_db([ep1, ep2, ep3])
        cooldown = CooldownManager()
        messages = [{"role": "user", "content": "hello"}]

        with patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[ep1, ep2, ep3]), \
             patch(_PATCH_FILTER, return_value=[ep1, ep2, ep3]), \
             patch(_PATCH_TRUNCATE, return_value=messages):
            result = await strategy.select_endpoints(messages, db, cooldown, group_id=1)

        assert result.endpoints[0].model_id == 1

    # -- 2.2 second request → next ------------------------------------------

    async def test_second_selection(self):
        ep1 = make_endpoint(model_id=1, weight=1.0)
        ep2 = make_endpoint(model_id=2, weight=1.0)
        ep3 = make_endpoint(model_id=3, weight=1.0)
        strategy = self._make_strategy()
        db = make_mock_db([ep1, ep2, ep3])
        cooldown = CooldownManager()
        messages = [{"role": "user", "content": "hello"}]

        with patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[ep1, ep2, ep3]), \
             patch(_PATCH_FILTER, return_value=[ep1, ep2, ep3]), \
             patch(_PATCH_TRUNCATE, return_value=messages):
            await strategy.select_endpoints(messages, db, cooldown, group_id=1)
            result2 = await strategy.select_endpoints(messages, db, cooldown, group_id=1)

        assert result2.endpoints[0].model_id == 2

    # -- 2.3 counter overflow protection ------------------------------------

    async def test_counter_overflow_protection(self):
        ep1 = make_endpoint(model_id=1, weight=1.0)
        ep2 = make_endpoint(model_id=2, weight=1.0)
        strategy = self._make_strategy()
        db = make_mock_db([ep1, ep2])
        cooldown = CooldownManager()
        messages = [{"role": "user", "content": "hello"}]

        from botflow.pipeline.strategies import RoundRobinStrategy
        RoundRobinStrategy._counters[1] = 1999

        with patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[ep1, ep2]), \
             patch(_PATCH_FILTER, return_value=[ep1, ep2]), \
             patch(_PATCH_TRUNCATE, return_value=messages):
            result = await strategy.select_endpoints(messages, db, cooldown, group_id=1)

        # 1999 % 2 = 1 → ep2 (index 1)
        assert result.endpoints[0].model_id == 2
        # Counter wraps: (1999 + 1) % 2000 = 0
        assert RoundRobinStrategy._counters[1] == 0

    # -- 2.4 single model group ---------------------------------------------

    async def test_single_model(self):
        ep1 = make_endpoint(model_id=1, weight=1.0)
        strategy = self._make_strategy()
        db = make_mock_db([ep1])
        cooldown = CooldownManager()
        messages = [{"role": "user", "content": "hello"}]

        with patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[ep1]), \
             patch(_PATCH_FILTER, return_value=[ep1]), \
             patch(_PATCH_TRUNCATE, return_value=messages):
            for _ in range(5):
                result = await strategy.select_endpoints(messages, db, cooldown, group_id=1)
                assert result.endpoints[0].model_id == 1
                assert len(result.endpoints) == 1

    # -- 2.5 cooldown model skipped, round-robin among remaining ------------

    async def test_skips_cooldown(self):
        ep1 = make_endpoint(model_id=1, weight=1.0)
        ep2 = make_endpoint(model_id=2, weight=1.0)
        ep3 = make_endpoint(model_id=3, weight=1.0)
        strategy = self._make_strategy()
        db = make_mock_db([ep1, ep2, ep3])
        cooldown = CooldownManager()
        messages = [{"role": "user", "content": "hello"}]

        available = [ep1, ep3]  # ep2 filtered

        with patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[ep1, ep2, ep3]), \
             patch(_PATCH_FILTER, return_value=available), \
             patch(_PATCH_TRUNCATE, return_value=messages):
            r1 = await strategy.select_endpoints(messages, db, cooldown, group_id=1)
            r2 = await strategy.select_endpoints(messages, db, cooldown, group_id=1)

        assert r1.endpoints[0].model_id == 1
        assert r2.endpoints[0].model_id == 3

        # Third request wraps back to ep1
        with patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[ep1, ep2, ep3]), \
             patch(_PATCH_FILTER, return_value=available), \
             patch(_PATCH_TRUNCATE, return_value=messages):
            r3 = await strategy.select_endpoints(messages, db, cooldown, group_id=1)
        assert r3.endpoints[0].model_id == 1

    # -- 2.6 execute success ------------------------------------------------

    async def test_execute_success(self):
        ep1 = make_endpoint(model_id=1, weight=1.0)
        strategy = self._make_strategy()
        db = make_mock_db([ep1])
        cooldown = CooldownManager()
        messages = [{"role": "user", "content": "hello"}]
        llm_response = {"choices": [{"message": {"content": "hi"}}]}

        with patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[ep1]), \
             patch(_PATCH_FILTER, return_value=[ep1]), \
             patch(_PATCH_TRUNCATE, return_value=messages), \
             patch(_PATCH_CALL_LLM, new_callable=AsyncMock, return_value=(llm_response, None)) as mock_call:
            result = await strategy.execute(messages, db, cooldown, group_id=1)

        assert result == llm_response
        mock_call.assert_called_once()


# ===========================================================================
# 3. SequentialStrategy (5 tests)
# ===========================================================================


class TestSequentialStrategy:

    def _make_strategy(self):
        from botflow.pipeline.strategies import SequentialStrategy
        return SequentialStrategy(params={})

    # -- 3.1 endpoints ordered by weight descending -------------------------

    async def test_weight_descending_order(self):
        ep_low = make_endpoint(model_id=1, weight=1.0, model_name="low")
        ep_high = make_endpoint(model_id=2, weight=3.0, model_name="high")
        ep_mid = make_endpoint(model_id=3, weight=2.0, model_name="mid")
        strategy = self._make_strategy()
        db = make_mock_db([ep_low, ep_high, ep_mid])
        cooldown = CooldownManager()
        messages = [{"role": "user", "content": "hello"}]

        with patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[ep_low, ep_high, ep_mid]), \
             patch(_PATCH_FILTER, return_value=[ep_low, ep_high, ep_mid]), \
             patch(_PATCH_TRUNCATE, return_value=messages):
            result = await strategy.select_endpoints(messages, db, cooldown, group_id=1)

        ids = [ep.model_id for ep in result.endpoints]
        assert ids == [2, 3, 1], f"Expected [2, 3, 1] (weight desc), got {ids}"

    # -- 3.2 highest weight endpoint is first -------------------------------

    async def test_highest_weight_first(self):
        ep1 = make_endpoint(model_id=1, weight=5.0)
        ep2 = make_endpoint(model_id=2, weight=1.0)
        strategy = self._make_strategy()
        db = make_mock_db([ep1, ep2])
        cooldown = CooldownManager()
        messages = [{"role": "user", "content": "hello"}]

        with patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[ep1, ep2]), \
             patch(_PATCH_FILTER, return_value=[ep1, ep2]), \
             patch(_PATCH_TRUNCATE, return_value=messages):
            result = await strategy.select_endpoints(messages, db, cooldown, group_id=1)

        assert result.endpoints[0].model_id == 1
        assert result.endpoints[0].detail.weight == 5.0

    # -- 3.3 execute: first endpoint succeeds -------------------------------

    async def test_execute_first_success(self):
        ep1 = make_endpoint(model_id=1, weight=3.0)
        ep2 = make_endpoint(model_id=2, weight=1.0)
        strategy = self._make_strategy()
        db = make_mock_db([ep1, ep2])
        cooldown = CooldownManager()
        messages = [{"role": "user", "content": "hello"}]
        llm_response = {"choices": [{"message": {"content": "hi"}}]}

        with patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[ep1, ep2]), \
             patch(_PATCH_FILTER, return_value=[ep1, ep2]), \
             patch(_PATCH_TRUNCATE, return_value=messages), \
             patch(_PATCH_CALL_LLM, new_callable=AsyncMock, return_value=(llm_response, None)) as mock_call:
            result = await strategy.execute(messages, db, cooldown, group_id=1)

        assert result == llm_response
        assert mock_call.call_count == 1

    # -- 3.4 execute: first fails, falls back to second ---------------------

    async def test_execute_fallback_to_second(self):
        ep1 = make_endpoint(model_id=1, weight=3.0)
        ep2 = make_endpoint(model_id=2, weight=1.0)
        strategy = self._make_strategy()
        db = make_mock_db([ep1, ep2])
        cooldown = CooldownManager()
        messages = [{"role": "user", "content": "hello"}]
        llm_response = {"choices": [{"message": {"content": "hi"}}]}

        call_count = 0

        async def fake_call_llm(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return (None, ProviderError("call_llm failed"))
            return (llm_response, None)

        with patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[ep1, ep2]), \
             patch(_PATCH_FILTER, return_value=[ep1, ep2]), \
             patch(_PATCH_TRUNCATE, return_value=messages), \
             patch(_PATCH_CALL_LLM, side_effect=fake_call_llm) as mock_call:
            result = await strategy.execute(messages, db, cooldown, group_id=1)

        assert result == llm_response
        assert mock_call.call_count == 2

    # -- 3.5 execute: all fail → ProviderError -------------------------------

    async def test_execute_all_fail(self):
        ep1 = make_endpoint(model_id=1, weight=2.0)
        ep2 = make_endpoint(model_id=2, weight=1.0)
        strategy = self._make_strategy()
        db = make_mock_db([ep1, ep2])
        cooldown = CooldownManager()
        messages = [{"role": "user", "content": "hello"}]

        with patch(_PATCH_LOAD, new_callable=AsyncMock, return_value=[ep1, ep2]), \
             patch(_PATCH_FILTER, return_value=[ep1, ep2]), \
             patch(_PATCH_TRUNCATE, return_value=messages), \
             patch(_PATCH_CALL_LLM, new_callable=AsyncMock, return_value=(None, ProviderError("call_llm failed"))):
            with pytest.raises(ProviderError, match="All endpoints failed"):
                await strategy.execute(messages, db, cooldown, group_id=1)


# ===========================================================================
# 4. Registry / exports (2 tests)
# ===========================================================================


class TestRegistryAndExports:

    def test_all_strategies_registered(self):
        from botflow.pipeline.strategies import (
            RandomWeightsStrategy,
            RoundRobinStrategy,
            SequentialStrategy,
        )

        assert STRATEGY_REGISTRY["random_weights"] is RandomWeightsStrategy
        assert STRATEGY_REGISTRY["round_robin"] is RoundRobinStrategy
        assert STRATEGY_REGISTRY["sequential"] is SequentialStrategy

    def test_pipeline_init_exports(self):
        from botflow.pipeline import (
            RandomWeightsStrategy,
            RoundRobinStrategy,
            SequentialStrategy,
        )

        assert RandomWeightsStrategy is not None
        assert RoundRobinStrategy is not None
        assert SequentialStrategy is not None
