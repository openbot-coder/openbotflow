"""Pipeline 共享基础设施。

所有函数为独立的 async 函数（不是 BaseStrategy 的方法），策略通过参数调用。
运行态缓存/信号量/相关可调用符号的单一事实源在 router.py，本模块仅 re-export。
"""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any

from botflow.common.logger import get_logger
from botflow.common.context import truncate_to_context_window
from botflow.config import get_config
from botflow.router import (
    CooldownManager,
    ModelEndpoint,
    is_retryable_error,
    exponential_backoff,
    _endpoint_cache,
    _provider_semaphores,
    _provider_cache,
    _ENDPOINT_CACHE_TTL,
    _PROVIDER_CACHE_TTL,
    PROVIDER_TYPE_MAP,
    _ensure_provider_semaphore,
    _get_cached_provider,
    load_endpoints,
    invalidate_endpoint_cache,
    invalidate_provider_cache,
    invalidate_all_caches,
)

log = get_logger("pipeline.shared")


# ---------------------------------------------------------------------------
# Cooldown filtering
# ---------------------------------------------------------------------------


def filter_available(
    endpoints: list[ModelEndpoint],
    cooldown: CooldownManager,
    group_id: int,
) -> list[ModelEndpoint]:
    """过滤掉 cooldown 中的 endpoints。"""
    return [
        ep
        for ep in endpoints
        if not cooldown.is_on_cooldown(group_id, ep.model_id)
    ]


# ---------------------------------------------------------------------------
# Semaphore management (from router.py, re-exported)
# ---------------------------------------------------------------------------


def _noop_asynccontext() -> Any:
    """Context manager that does nothing — used when no semaphore is configured."""
    return nullcontext()


# ---------------------------------------------------------------------------
# Model extra config filtering (from router.py)
# ---------------------------------------------------------------------------


def _apply_model_extra_config(
    kwargs: dict[str, Any],
    model_extra_config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Strip model-unsupported kwargs based on extra_config."""
    cfg = model_extra_config or {}
    strip_keys: set[str] = set()

    explicit_strip = cfg.get("strip_params")
    if isinstance(explicit_strip, list):
        strip_keys.update(explicit_strip)

    if cfg.get("reasoning_mode") == "off":
        strip_keys.update({"reasoning_effort", "reasoning_content"})

    if kwargs.get("reasoning_mode") == "off":
        strip_keys.update({"reasoning_effort", "reasoning_content"})

    if strip_keys:
        kwargs = {k: v for k, v in kwargs.items() if k not in strip_keys}
    return kwargs


# ---------------------------------------------------------------------------
# LLM call with retry + cooldown + semaphore
# ---------------------------------------------------------------------------


async def call_llm(
    ep: ModelEndpoint,
    messages: list[dict],
    group_id: int,
    cooldown: CooldownManager,
    temperature: float | None = None,
    max_tokens: int | None = None,
    **kwargs,
) -> tuple[dict | None, Exception | None]:
    """调用单个 endpoint，带 retry + cooldown + 信号量管理。

    从 GroupRouter._attempt_call 1:1 搬过来。
    信号量是全局跨 group 共享的——同一个 provider 跨 group 限流。

    返回 ``(result, error)``：成功时 ``(result, None)``，全部重试失败后
    ``(None, last_error)``。错误不再被静默吞掉——调用方（图节点 / 策略 /
    SG-0 留痕）需要 ``error_type`` 做白名单判定与失败留痕。
    """
    kwargs = _apply_model_extra_config(kwargs, ep.detail.extra_config)
    sem = _ensure_provider_semaphore(
        ep.detail.provider_id, get_config().upstream_semaphore_size
    )

    last_error = None
    for attempt in range(ep.max_retries):
        try:
            async with sem or _noop_asynccontext():
                result = await ep.provider.chat(
                    messages=messages,
                    model=ep.detail.model_name,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **kwargs,
                )
            cooldown.record_success(group_id, ep.model_id)
            result["_routing"] = {
                "model_id": ep.model_id,
                "provider_id": ep.detail.provider_id,
            }
            return result, None
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
            break

    cooldown.record_failure(
        group_id,
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
    return None, last_error


# ---------------------------------------------------------------------------
# Context window truncation
# ---------------------------------------------------------------------------


def truncate_messages(
    messages: list[dict],
    endpoints: list[ModelEndpoint],
    max_tokens: int | None,
) -> list[dict]:
    """Context window 截断（取 available endpoints 最小 context_window）。"""
    context_windows = [
        ep.detail.context_window
        for ep in endpoints
        if ep.detail.context_window > 0
    ]
    if not context_windows:
        return messages
    return truncate_to_context_window(messages, min(context_windows), max_tokens)
