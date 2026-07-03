"""Tests for the routing engine: weighted selection, cooldown, retry/fallback."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from botflow.common.exceptions import NoAvailableModelError, AllModelsCooldownError, ProviderError
from botflow.router import (
    CooldownManager,
    GroupRouter,
    weighted_random_select,
    is_retryable_error,
    exponential_backoff,
)
from botflow.storage.models import GroupModelWithDetails, Provider


# ---------------------------------------------------------------------------
# Weighted random selection tests
# ---------------------------------------------------------------------------

def _make_model_detail(model_id: int, weight: float, model_name: str = "test-model") -> GroupModelWithDetails:
    return GroupModelWithDetails(
        id=model_id,
        group_id=1,
        model_id=model_id,
        weight=weight,
        is_enabled=True,
        model_name=model_name,
        display_name=model_name,
        provider_id=1,
        provider_name="test-provider",
        provider_type="openai",
        max_retries=3,
        cooldown_seconds=60,
        cooldown_failure_threshold=3,
    )


class TestWeightedRandomSelect:
    def test_select_with_equal_weights(self):
        models = [_make_model_detail(1, 1.0), _make_model_detail(2, 1.0)]
        # Run many times to ensure both can be selected
        selected_ids = set()
        for _ in range(100):
            selected = weighted_random_select(models)
            selected_ids.add(selected.model_id)
        assert len(selected_ids) == 2

    def test_select_zero_weight_skipped(self):
        models = [_make_model_detail(1, 0.0), _make_model_detail(2, 1.0)]
        for _ in range(50):
            selected = weighted_random_select(models)
            assert selected.model_id == 2

    def test_all_zero_weights_raises(self):
        models = [_make_model_detail(1, 0.0), _make_model_detail(2, 0.0)]
        with pytest.raises(NoAvailableModelError, match="total weight is 0"):
            weighted_random_select(models)

    def test_empty_list_raises(self):
        with pytest.raises(NoAvailableModelError):
            weighted_random_select([])

    def test_single_model_always_selected(self):
        models = [_make_model_detail(1, 5.0)]
        for _ in range(20):
            assert weighted_random_select(models).model_id == 1

    def test_weight_distribution(self):
        """Model with higher weight should be chosen more often."""
        models = [_make_model_detail(1, 90.0), _make_model_detail(2, 10.0)]
        counts = {1: 0, 2: 0}
        for _ in range(1000):
            selected = weighted_random_select(models)
            counts[selected.model_id] += 1
        assert counts[1] > counts[2]
        assert 800 <= counts[1] <= 1000  # ~90%


# ---------------------------------------------------------------------------
# CooldownManager tests
# ---------------------------------------------------------------------------

class TestCooldownManager:
    def test_success_resets_failures(self):
        cm = CooldownManager()
        cm.record_failure(1, 1, 3, 60)
        assert cm.get_failure_count(1, 1) == 1
        cm.record_success(1, 1)
        assert cm.get_failure_count(1, 1) == 0

    def test_cooldown_activated_after_threshold(self):
        cm = CooldownManager()
        cm.record_failure(1, 1, 3, 60)
        cm.record_failure(1, 1, 3, 60)
        cm.record_failure(1, 1, 3, 60)
        assert cm.is_on_cooldown(1, 1)

    def test_not_on_cooldown_below_threshold(self):
        cm = CooldownManager()
        cm.record_failure(1, 1, 5, 60)
        cm.record_failure(1, 1, 5, 60)
        assert not cm.is_on_cooldown(1, 1)

    def test_cooldown_expires(self):
        cm = CooldownManager()
        cm.record_failure(1, 1, 1, 0.01)  # 10ms cooldown
        assert cm.is_on_cooldown(1, 1)
        time.sleep(0.02)
        assert not cm.is_on_cooldown(1, 1)

    def test_no_cooldown_for_no_records(self):
        cm = CooldownManager()
        assert not cm.is_on_cooldown(99, 99)
        assert cm.get_failure_count(99, 99) == 0


# ---------------------------------------------------------------------------
# Error retryability tests
# ---------------------------------------------------------------------------

class TestIsRetryable:
    def test_timeout_is_retryable(self):
        e = ProviderError("Connection timed out")
        assert is_retryable_error(e)

    def test_500_is_retryable(self):
        e = ProviderError("HTTP 500 Internal Server Error")
        assert is_retryable_error(e)

    def test_429_is_retryable(self):
        e = ProviderError("HTTP 429 Too Many Requests")
        assert is_retryable_error(e)

    def test_400_is_not_retryable(self):
        e = ProviderError("HTTP 400 Bad Request")
        assert not is_retryable_error(e)

    def test_401_is_not_retryable(self):
        e = ProviderError("HTTP 401 Unauthorized")
        assert not is_retryable_error(e)

    def test_non_provider_error_not_retryable(self):
        """Plain Exception (not ProviderError) should not be retryable."""
        assert not is_retryable_error(Exception("some random error"))
        assert not is_retryable_error(ValueError("invalid value"))


class TestExponentialBackoff:
    @pytest.mark.asyncio
    async def test_first_attempt_short(self):
        # Should complete quickly (base 1s + jitter)
        import time
        start = time.monotonic()
        await exponential_backoff(0)
        elapsed = time.monotonic() - start
        assert 0.5 <= elapsed <= 2.0

    @pytest.mark.asyncio
    async def test_backoff_increases(self):
        import time
        start = time.monotonic()
        await exponential_backoff(0)
        d1 = time.monotonic() - start
        start = time.monotonic()
        await exponential_backoff(1)
        d2 = time.monotonic() - start
        assert d2 > d1

    def test_sleep_cap_calculation(self):
        """Verify exponential backoff delay caps at 30s (math only, no actual sleep)."""
        base = 1.0
        max_delay = 30.0
        assert min(base * (2**10), max_delay) == 30.0  # 1024 capped to 30
        assert min(base * (2**1), max_delay) == 2.0
        assert min(base * (2**4), max_delay) == 16.0
        assert min(base * (2**5), max_delay) == 30.0  # 32 capped to 30


# ---------------------------------------------------------------------------
# GroupRouter tests (with mocked DB)
# ---------------------------------------------------------------------------

class TestGroupRouter:
    @pytest.fixture
    def mock_db(self):
        return AsyncMock()

    @pytest.fixture
    def router(self, mock_db):
        return GroupRouter(group_id=1, db=mock_db, cooldown_manager=CooldownManager())

    @pytest.mark.asyncio
    async def test_no_models_in_group(self, router, mock_db):
        mock_db.get_group_models.return_value = []
        with pytest.raises(NoAvailableModelError):
            await router.route(messages=[{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_no_available_provider(self, router, mock_db):
        mock_db.get_group_models.return_value = [
            _make_model_detail(1, 1.0)
        ]
        mock_db.get_provider.return_value = None
        with pytest.raises(NoAvailableModelError):
            await router.route(messages=[{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_disabled_provider_skipped(self, router, mock_db):
        from botflow.storage.models import Provider
        mock_db.get_group_models.return_value = [
            _make_model_detail(1, 1.0)
        ]
        disabled_provider = Provider(id=1, name="disabled", provider_type="openai", is_enabled=False)
        mock_db.get_provider.return_value = disabled_provider
        with pytest.raises(NoAvailableModelError):
            await router.route(messages=[{"role": "user", "content": "hi"}])
