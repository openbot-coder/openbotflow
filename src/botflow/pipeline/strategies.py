"""内建路由策略：RandomWeights / RoundRobin / Sequential。"""

from __future__ import annotations

from typing import ClassVar

from botflow.common.exceptions import AllModelsCooldownError, NoAvailableModelError
from botflow.pipeline.base import BaseStrategy, RouteResult, register_strategy
from botflow.router import CooldownManager, ModelEndpoint, weighted_random_select, weighted_random_order
from botflow.storage.db import Database


class RandomWeightsStrategy(BaseStrategy):
    """加权随机策略 — 行为与原 GroupRouter 完全一致。"""

    async def select_endpoints(
        self,
        messages: list[dict],
        db: Database,
        cooldown: CooldownManager,
        group_id: int,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs,
    ) -> RouteResult:
        from botflow.pipeline._shared import load_endpoints, filter_available, truncate_messages

        endpoints = await load_endpoints(group_id, db)
        if not endpoints:
            raise NoAvailableModelError(f"Group {group_id} has no enabled models")

        available = filter_available(endpoints, cooldown, group_id)
        if not available:
            raise AllModelsCooldownError(f"Group {group_id}: all models on cooldown")

        selected = weighted_random_select([ep.detail for ep in available])
        primary = next(ep for ep in available if ep.model_id == selected.model_id)

        remaining = [ep for ep in available if ep.model_id != selected.model_id]
        if remaining:
            ordered_rest = weighted_random_order([ep.detail for ep in remaining])
            fallback = [next(ep for ep in available if ep.model_id == d.model_id) for d in ordered_rest]
        else:
            fallback = []

        truncated = truncate_messages(messages, available, max_tokens)

        return RouteResult(
            endpoints=[primary] + fallback,
            messages=truncated,
            temperature=temperature,
            max_tokens=max_tokens,
            extra_kwargs=kwargs,
        )


class RoundRobinStrategy(BaseStrategy):
    """轮询策略 — 按固定顺序依次选择。"""

    _counters: ClassVar[dict[int, int]] = {}

    async def select_endpoints(
        self,
        messages: list[dict],
        db: Database,
        cooldown: CooldownManager,
        group_id: int,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs,
    ) -> RouteResult:
        from botflow.pipeline._shared import load_endpoints, filter_available, truncate_messages

        endpoints = await load_endpoints(group_id, db)
        if not endpoints:
            raise NoAvailableModelError(f"Group {group_id} has no enabled models")

        available = filter_available(endpoints, cooldown, group_id)
        if not available:
            raise AllModelsCooldownError(f"Group {group_id}: all models on cooldown")

        available.sort(key=lambda ep: ep.detail.weight, reverse=True)

        idx = self._counters.get(group_id, 0)
        selected_idx = idx % len(available)

        next_idx = idx + 1
        self._counters[group_id] = next_idx

        primary = available[selected_idx]
        fallback = [available[i] for i in range(len(available)) if i != selected_idx]

        truncated = truncate_messages(messages, available, max_tokens)

        return RouteResult(
            endpoints=[primary] + fallback,
            messages=truncated,
            temperature=temperature,
            max_tokens=max_tokens,
            extra_kwargs=kwargs,
        )


class SequentialStrategy(BaseStrategy):
    """顺序降级策略 — 按固定顺序依次尝试，第一个成功即返回。"""

    async def select_endpoints(
        self,
        messages: list[dict],
        db: Database,
        cooldown: CooldownManager,
        group_id: int,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs,
    ) -> RouteResult:
        from botflow.pipeline._shared import load_endpoints, filter_available, truncate_messages

        endpoints = await load_endpoints(group_id, db)
        if not endpoints:
            raise NoAvailableModelError(f"Group {group_id} has no enabled models")

        available = filter_available(endpoints, cooldown, group_id)
        if not available:
            raise AllModelsCooldownError(f"Group {group_id}: all models on cooldown")

        available.sort(key=lambda ep: ep.detail.weight, reverse=True)

        truncated = truncate_messages(messages, available, max_tokens)

        return RouteResult(
            endpoints=available,
            messages=truncated,
            temperature=temperature,
            max_tokens=max_tokens,
            extra_kwargs=kwargs,
        )


# --- 注册内建策略 ---
register_strategy("random_weights", RandomWeightsStrategy)
register_strategy("round_robin", RoundRobinStrategy)
register_strategy("sequential", SequentialStrategy)
