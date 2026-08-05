"""Model routing engine with weighted selection, cooldown, retry and fallback."""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any


from botflow.common.exceptions import (
    AllModelsCooldownError,
    NoAvailableModelError,
    ProviderError,
)
from botflow.common.context import truncate_to_context_window
from botflow.common.logger import get_logger
from botflow.providers.base import BaseProvider
from botflow.providers.anthropic_provider import AnthropicProvider
from botflow.providers.google_provider import GoogleProvider
from botflow.providers.openai_compat import OpenAICompatProvider
from botflow.providers.deepseek_provider import DeepSeekProvider
from botflow.storage.db import Database
from botflow.storage.models import GroupModelWithDetails

log = get_logger("router")

# ---------------------------------------------------------------------------
# Cooldown state
# ---------------------------------------------------------------------------


@dataclass
class CooldownState:
    """Tracks cooldown state for a single model."""

    consecutive_failures: int = 0
    cooldown_until: float = 0.0  # monotonic time


class CooldownManager:
    """Manages cooldown states for all models across groups.

    Supports persistence to survive service restarts.
    """

    def __init__(self) -> None:
        self._states: dict[tuple[int, int], CooldownState] = {}  # (group_id, model_id) -> state
        self._monotonic_start = time.monotonic()  # Track startup time for relative->absolute conversion

    def record_success(self, group_id: int, model_id: int) -> None:
        """Reset failure count on success."""
        key = (group_id, model_id)
        state = self._states.get(key)
        if state is not None:
            state.consecutive_failures = 0
            state.cooldown_until = 0.0

    def record_failure(
        self,
        group_id: int,
        model_id: int,
        cooldown_failure_threshold: int,
        cooldown_seconds: int,
    ) -> None:
        """Increment failure count and optionally enter cooldown."""
        key = (group_id, model_id)
        state = self._states.setdefault(key, CooldownState())
        state.consecutive_failures += 1

        if state.consecutive_failures >= cooldown_failure_threshold:
            state.cooldown_until = time.monotonic() + cooldown_seconds
            log.warning(
                "Model {} entered cooldown for {}s (failures: {})",
                model_id,
                cooldown_seconds,
                state.consecutive_failures,
            )

    def is_on_cooldown(self, group_id: int, model_id: int) -> bool:
        """Check if model is currently cooling down."""
        key = (group_id, model_id)
        state = self._states.get(key)
        if state is None:
            return False
        if state.cooldown_until == 0:
            return False
        if time.monotonic() >= state.cooldown_until:
            # Cooldown expired, reset
            state.consecutive_failures = 0
            state.cooldown_until = 0.0
            return False
        return True

    def get_failure_count(self, group_id: int, model_id: int) -> int:
        key = (group_id, model_id)
        state = self._states.get(key)
        return state.consecutive_failures if state else 0

    def get_all_active_cooldowns(self) -> list[dict]:
        """Get all models currently in cooldown.

        Returns wall-clock ``cooldown_until`` values so they survive restarts.
        """
        result = []
        now_mono = time.monotonic()
        wall_now = time.time()
        for (group_id, model_id), state in self._states.items():
            if state.cooldown_until > now_mono:
                remaining = state.cooldown_until - now_mono
                result.append({
                    "group_id": group_id,
                    "model_id": model_id,
                    "consecutive_failures": state.consecutive_failures,
                    "cooldown_until": wall_now + remaining,  # wall clock for persistence
                })
        return result

    def restore_state(self, group_id: int, model_id: int, failures: int, cooldown_until: float) -> None:
        """Restore a cooldown state from persistence.

        ``cooldown_until`` is wall-clock time; convert back to monotonic.
        """
        remaining = cooldown_until - time.time()
        if remaining <= 0:
            return  # already expired, skip restore
        mono_until = time.monotonic() + remaining
        key = (group_id, model_id)
        state = CooldownState(consecutive_failures=failures, cooldown_until=mono_until)
        self._states[key] = state
        log.info(
            "Restored cooldown for model {} (group {}): {}s remaining",
            model_id, group_id, int(remaining),
        )


# ---------------------------------------------------------------------------
# Provider factory + caching
# ---------------------------------------------------------------------------

PROVIDER_TYPE_MAP: dict[str, type[BaseProvider]] = {
    "openai": OpenAICompatProvider,
    "azure": OpenAICompatProvider,
    "ollama": OpenAICompatProvider,
    "vllm": OpenAICompatProvider,
    "deepseek": DeepSeekProvider,
    "anthropic": AnthropicProvider,
    "google": GoogleProvider,
}

# Cache: provider_id -> (BaseProvider, create_time)
_provider_cache: dict[int, tuple[BaseProvider, float]] = {}
_PROVIDER_CACHE_TTL = 300  # 5 minutes

# Cache: group_id -> (list[ModelEndpoint], create_time)
_endpoint_cache: dict[int, tuple[list["ModelEndpoint"], float]] = {}
_ENDPOINT_CACHE_TTL = 60  # 1 minute


def _get_cached_provider(provider_id: int, provider_type: str, api_key: str, base_url: str, extra_config: dict[str, Any] | None = None) -> BaseProvider:
    """Get or create a cached provider instance."""
    now = time.time()
    cached = _provider_cache.get(provider_id)
    if cached:
        instance, create_time = cached
        if now - create_time < _PROVIDER_CACHE_TTL:
            return instance
    # Create new instance
    cls = PROVIDER_TYPE_MAP.get(provider_type)
    if cls is None:
        raise ValueError(f"Unsupported provider type: {provider_type}")
    instance = cls(api_key=api_key, base_url=base_url, extra_config=extra_config)
    _provider_cache[provider_id] = (instance, now)
    return instance


# ---------------------------------------------------------------------------
# Weighted random selection
# ---------------------------------------------------------------------------


def weighted_random_select(models: list[GroupModelWithDetails]) -> GroupModelWithDetails:
    """Select a model using weighted random selection.

    Args:
        models: List of models with weights.

    Returns:
        Selected model.

    Raises:
        NoAvailableModelError: If total weight is 0 or all weights are <= 0.
    """
    total_weight = sum(m.weight for m in models if m.weight > 0)
    if total_weight <= 0:
        raise NoAvailableModelError("No available models (total weight is 0 or negative)")

    r = random.uniform(0, total_weight)
    cumulative = 0.0
    for model in models:
        if model.weight <= 0:
            continue
        cumulative += model.weight
        if r < cumulative:
            return model

    # Fallback (shouldn't reach here due to floating point, but defensive)
    return models[-1]


def weighted_random_order(models: list[GroupModelWithDetails]) -> list[GroupModelWithDetails]:
    """Order models by weighted random sampling without replacement.

    The first element follows the same distribution as weighted_random_select,
    so fallback order respects model weights. Zero-weight models are excluded.
    """
    remaining = [m for m in models if m.weight > 0]
    if not remaining:
        raise NoAvailableModelError("No available models (total weight is 0 or negative)")
    ordered: list[GroupModelWithDetails] = []
    while remaining:
        selected = weighted_random_select(remaining)
        remaining.remove(selected)
        ordered.append(selected)
    return ordered


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def is_retryable_error(error: Exception) -> bool:
    """Determine if an error is worth retrying."""
    import re
    if isinstance(error, ProviderError):
        msg = str(error)
        match = re.search(r"HTTP\s+(\d{3})\b", msg)
        if match:
            status_code = int(match.group(1))
            if status_code in RETRYABLE_STATUS_CODES:
                return True
        if "timeout" in msg.lower() or "timed out" in msg.lower():
            return True
    return False


async def exponential_backoff(attempt: int, base: float = 1.0, max_delay: float = 30.0) -> None:
    """Sleep with exponential backoff + jitter."""
    delay = min(base * (2**attempt), max_delay)
    jitter = random.uniform(0, delay * 0.1)
    await asyncio.sleep(delay + jitter)


# ---------------------------------------------------------------------------
# Provider wrapper (holds state for a single model within a group)
# ---------------------------------------------------------------------------


class ModelEndpoint:
    """Wraps a model instance with its cooldown-aware calling logic."""

    def __init__(self, model_detail: GroupModelWithDetails, provider_instance: BaseProvider) -> None:
        self.detail = model_detail
        self.provider = provider_instance

    @property
    def model_id(self) -> int:
        return self.detail.model_id

    @property
    def cooldown_threshold(self) -> int:
        return self.detail.cooldown_failure_threshold

    @property
    def cooldown_seconds(self) -> int:
        return self.detail.cooldown_seconds

    @property
    def max_retries(self) -> int:
        return self.detail.max_retries


# ---------------------------------------------------------------------------
# Group Router
# ---------------------------------------------------------------------------


class GroupRouter:
    """Routes requests through models in a group with retry and fallback."""

    def __init__(self, group_id: int, db: Database, cooldown_manager: CooldownManager, fallback_group_id: int | None = None) -> None:
        self.group_id = group_id
        self.db = db
        self.cooldown = cooldown_manager
        self.fallback_group_id = fallback_group_id

    async def _load_endpoints(self) -> list[ModelEndpoint]:
        """Load and build endpoints for all enabled models in the group (with caching)."""
        now = time.time()
        cached = _endpoint_cache.get(self.group_id)
        if cached:
            endpoints, create_time = cached
            if now - create_time < _ENDPOINT_CACHE_TTL:
                return endpoints
        
        # Cache miss or expired - reload from DB
        models = await self.db.get_group_models(self.group_id, enabled_only=True)
        endpoints: list[ModelEndpoint] = []
        for m in models:
            provider = await self.db.get_provider(m.provider_id)
            if provider is None or not provider.is_enabled:
                continue
            provider_instance = _get_cached_provider(
                provider_id=provider.id,
                provider_type=provider.provider_type,
                api_key=provider.api_key,
                base_url=provider.base_url,
                extra_config=provider.extra_config,
            )
            endpoints.append(ModelEndpoint(m, provider_instance))
        
        _endpoint_cache[self.group_id] = (endpoints, now)
        return endpoints

    def _get_available(self, endpoints: list[ModelEndpoint]) -> list[ModelEndpoint]:
        """Filter out models currently on cooldown."""
        return [
            ep
            for ep in endpoints
            if not self.cooldown.is_on_cooldown(self.group_id, ep.model_id)
        ]

    async def route(
        self,
        messages: list[dict[str, Any]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Route a chat completion request through the group.

        Process:
            1. Load endpoints from DB
            2. Filter available (not cooling)
            3. Weighted random select
            4. Attempt with retry
            5. On failure: record cooldown, fallback to next model
            6. Log to call_logs
        """
        endpoints = await self._load_endpoints()
        if not endpoints:
            raise NoAvailableModelError(f"Group {self.group_id} has no enabled models")

        if stream:
            return await self._route_stream(endpoints, messages, temperature, max_tokens, **kwargs)
        return await self._route_non_stream(endpoints, messages, temperature, max_tokens, **kwargs)

    async def _route_non_stream(
        self,
        endpoints: list[ModelEndpoint],
        messages: list[dict[str, Any]],
        temperature: float | None,
        max_tokens: int | None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Non-streaming routing with retry + fallback."""
        used_endpoints: list[ModelEndpoint] = []

        while True:
            available = self._get_available(endpoints)
            if not available:
                # All models on cooldown — try fallback group before raising
                if self.fallback_group_id is not None:
                    log.warning("Group {} all models on cooldown, falling back to group {}", self.group_id, self.fallback_group_id)
                    fallback_router = GroupRouter(self.fallback_group_id, self.db, self.cooldown)
                    return await fallback_router.route(
                        messages=messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        stream=False,
                        **kwargs,
                    )
                raise AllModelsCooldownError(f"Group {self.group_id}: all models are on cooldown")

            selected = weighted_random_select([ep.detail for ep in available])
            matching_ep = next(ep for ep in available if ep.model_id == selected.model_id)
            used_endpoints.append(matching_ep)

            context_windows = [ep.detail.context_window for ep in available if ep.detail.context_window > 0]
            context_window = min(context_windows) if context_windows else 0
            if context_window > 0:
                messages = truncate_to_context_window(messages, context_window, max_tokens)

            result = await self._attempt_call(matching_ep, messages, temperature, max_tokens, **kwargs)
            if result is not None:
                result["_routing"] = {
                    "model_id": matching_ep.model_id,
                    "provider_id": matching_ep.detail.provider_id,
                }
                return result

            # If all endpoints have been tried and all failed
            if len(used_endpoints) >= len(endpoints):
                if self.fallback_group_id is not None:
                    log.warning("Group {} exhausted, falling back to group {}", self.group_id, self.fallback_group_id)
                    fallback_router = GroupRouter(self.fallback_group_id, self.db, self.cooldown)
                    return await fallback_router.route(
                        messages=messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        stream=False,
                        **kwargs,
                    )
                raise ProviderError(f"Group {self.group_id}: all models exhausted")

    async def _route_stream(
        self,
        endpoints: list[ModelEndpoint],
        messages: list[dict[str, Any]],
        temperature: float | None,
        max_tokens: int | None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Streaming routing — returns candidate endpoints in weighted order.

        The caller tries each endpoint in order until one starts streaming,
        then falls back to the next on pre-stream failure (mirrors
        ``_route_non_stream`` fallback). Falls back to ``fallback_group_id``
        if all models in this group are on cooldown.
        """
        available = self._get_available(endpoints)
        if not available:
            if self.fallback_group_id is not None:
                log.warning("Group {} all models on cooldown, falling back to group {}", self.group_id, self.fallback_group_id)
                fallback_router = GroupRouter(self.fallback_group_id, self.db, self.cooldown)
                return await fallback_router.route(
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=True,
                    **kwargs,
                )
            raise AllModelsCooldownError(f"Group {self.group_id}: all models are on cooldown")

        ordered = weighted_random_order([ep.detail for ep in available])
        endpoints_ordered = [next(ep for ep in available if ep.model_id == d.model_id) for d in ordered]

        context_windows = [ep.detail.context_window for ep in available if ep.detail.context_window > 0]
        if context_windows:
            messages = truncate_to_context_window(messages, min(context_windows), max_tokens)

        return {
            "endpoints": endpoints_ordered,
            "group_id": self.group_id,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "kwargs": kwargs,
        }

    async def _attempt_call(
        self,
        ep: ModelEndpoint,
        messages: list[dict[str, Any]],
        temperature: float | None,
        max_tokens: int | None,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        """Try calling a single endpoint with retry logic.

        Returns the response dict on success, or None on failure (for fallback).
        """
        last_error: Exception | None = None

        for attempt in range(ep.max_retries):
            try:
                result = await ep.provider.chat(
                    messages=messages,
                    model=ep.detail.model_name,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **kwargs,
                )
                self.cooldown.record_success(self.group_id, ep.model_id)
                return result

            except Exception as e:
                last_error = e
                log.warning(
                    "Model {} (attempt {}/{}) failed: {}",
                    ep.detail.model_name,
                    attempt + 1,
                    ep.max_retries,
                    e,
                )

                if is_retryable_error(e) and attempt < ep.max_retries - 1:
                    await exponential_backoff(attempt)
                    continue
                else:
                    break

        # All retries exhausted
        self.cooldown.record_failure(
            self.group_id,
            ep.model_id,
            ep.cooldown_threshold,
            ep.cooldown_seconds,
        )
        log.error(
            "Model {} exhausted after {} retries: {}",
            ep.detail.model_name,
            ep.max_retries,
            last_error,
        )
        return None
