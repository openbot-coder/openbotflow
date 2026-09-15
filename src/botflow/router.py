"""Model routing engine with weighted selection, cooldown, retry and fallback."""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from contextlib import nullcontext
from typing import Any


from botflow.common.exceptions import (
    AllModelsCooldownError,
    NoAvailableModelError,
    ProviderError,
)
from botflow.common.context import truncate_to_context_window
from botflow.common.logger import get_logger
from botflow.config import get_config
from botflow.providers.base import BaseProvider
from botflow.providers.anthropic_provider import AnthropicProvider
from botflow.providers.google_provider import GoogleProvider
from botflow.providers.openai_compat import OpenAICompatProvider
from botflow.providers.deepseek_provider import DeepSeekProvider
from botflow.storage.db import Database
from botflow.storage.models import GroupModelWithDetails

log = get_logger("router")

# ---------------------------------------------------------------------------
# Per-provider concurrency semaphore (shared across all groups)
# ---------------------------------------------------------------------------

_provider_semaphores: dict[int, asyncio.Semaphore | None] = {}


def _ensure_provider_semaphore(provider_id: int, size: int) -> asyncio.Semaphore | None:
    """Return the cached semaphore for ``provider_id``, creating it if needed.

    If ``size <= 0`` the provider is considered unlimited-concurrency and the
    function returns ``None`` (callers should skip the ``async with``).
    """
    entry = _provider_semaphores.get(provider_id)
    if entry is not None:
        return entry
    if size <= 0:
        _provider_semaphores[provider_id] = None
        return None
    sem = asyncio.Semaphore(size)
    _provider_semaphores[provider_id] = sem
    return sem


def _noop_asynccontext() -> Any:
    """Context manager that does nothing — used when no semaphore is configured."""
    return nullcontext()


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

# Cache: (provider_id, resolved_type) -> (BaseProvider, create_time)
_provider_cache: dict[tuple[int, str], tuple[BaseProvider, float]] = {}
_PROVIDER_CACHE_TTL = 300  # 5 minutes

# Cache: group_id -> (list[ModelEndpoint], create_time)
_endpoint_cache: dict[int, tuple[list["ModelEndpoint"], float]] = {}
_ENDPOINT_CACHE_TTL = 60  # 1 minute


def _get_cached_provider(provider_id: int, provider_type: str, api_key: str, base_url: str,
                         extra_config: dict[str, Any] | None = None,
                         api_format: str = "",
                         proxy: str = "") -> BaseProvider:
    """Get or create a cached provider instance.

    ``api_format`` allows per-model SDK override: when non-empty it replaces
    ``provider_type`` for the provider class lookup while the connection
    details (base_url, api_key, extra_config) still come from the provider.
    This enables relay/aggregator scenarios where one provider connection
    serves multiple vendor SDKs.

    ``proxy`` is per-model: when non-empty, it overrides any proxy in
    extra_config and the cache key includes it so models with different
    proxies get separate provider instances.
    """
    resolved_type = api_format if api_format else provider_type
    cache_key = (provider_id, resolved_type, proxy)
    now = time.time()
    cached = _provider_cache.get(cache_key)
    if cached:
        instance, create_time = cached
        if now - create_time < _PROVIDER_CACHE_TTL:
            return instance
    # Merge proxy into extra_config if provided
    merged_config = dict(extra_config) if extra_config else {}
    if proxy:
        merged_config["proxy"] = proxy
    # Create new instance
    cls = PROVIDER_TYPE_MAP.get(resolved_type)
    if cls is None:
        raise ValueError(f"Unsupported provider type: {resolved_type}")
    instance = cls(api_key=api_key, base_url=base_url, extra_config=merged_config)
    _provider_cache[cache_key] = (instance, now)
    return instance


async def load_endpoints(group_id: int, db: Database) -> list[ModelEndpoint]:
    """从 DB 加载 group 的所有 enabled endpoints（带缓存）。

    1:1 复用 _endpoint_cache 逻辑和 _get_cached_provider。
    """
    now = time.time()
    cached = _endpoint_cache.get(group_id)
    if cached:
        endpoints, create_time = cached
        if now - create_time < _ENDPOINT_CACHE_TTL:
            return endpoints

    # Cache miss or expired — reload from DB
    models = await db.get_group_models(group_id, enabled_only=True)
    endpoints: list[ModelEndpoint] = []
    for m in models:
        provider = await db.get_provider(m.provider_id)
        if provider is None or not provider.is_enabled:
            continue
        provider_instance = _get_cached_provider(
            provider_id=provider.id,
            provider_type=provider.provider_type,
            api_key=provider.api_key,
            base_url=provider.base_url,
            extra_config=provider.extra_config,
            api_format=m.api_format,
            proxy=m.proxy,
        )
        endpoints.append(ModelEndpoint(m, provider_instance))

    _endpoint_cache[group_id] = (endpoints, now)
    return endpoints


def invalidate_endpoint_cache(group_id: int) -> None:
    """Admin 修改 group_models 后调用，清除缓存。"""
    _endpoint_cache.pop(group_id, None)


def invalidate_provider_cache(provider_id: int | None = None) -> None:
    """清除 provider 实例缓存。

    provider_id=None 时清空整个 _provider_cache；否则只清掉
    key[0] == provider_id 的所有条目（同 provider 跨 type/proxy 的所有实例）。
    """
    if provider_id is None:
        _provider_cache.clear()
        return
    for key in list(_provider_cache.keys()):
        if key[0] == provider_id:
            _provider_cache.pop(key, None)


def invalidate_all_caches() -> None:
    """Admin 改动 provider/model（波及多 group）后调用。

    清空 _endpoint_cache 与 _provider_cache，但**不**清空 _provider_semaphores
    （信号量对象身份不能打断，否则并发上限被绕过）。
    """
    _endpoint_cache.clear()
    _provider_cache.clear()


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
    """Determine if an error is worth retrying.

    1. Prefer structured ``status_code`` attribute (httpx/openai errors).
    2. Fall back to regex on ``str(error)`` for ProviderError / plain Exception.
    """
    # Structured attribute check (httpx.Response, openai, etc.)
    status = getattr(error, "status_code", None)
    if isinstance(status, int) and status in RETRYABLE_STATUS_CODES:
        return True

    # String-based fallback for ProviderError / plain exceptions
    msg = str(error)
    import re
    match = re.search(r"HTTP\s+(\d{3})\b", msg)
    if match and int(match.group(1)) in RETRYABLE_STATUS_CODES:
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


# GroupRouter 已迁移至 PipelineEngine + Strategy 系统，本模块保留基础设施函数。
