# P1-3 功能点文档：3 个内建策略

> 任务：RandomWeightsStrategy + RoundRobinStrategy + SequentialStrategy
> 文件：`src/botflow/pipeline/strategies.py` + `src/botflow/pipeline/__init__.py`

---

## 功能点 1：`RandomWeightsStrategy.select_endpoints()`

**文件**：`pipeline/strategies.py`

**行为**：与 `GroupRouter._route_non_stream` 中的加权随机选择完全一致。

```python
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
            raise NoAvailableModelError(f"Group {group_id}: all models on cooldown")

        # 加权随机选一个作为首选
        selected = weighted_random_select([ep.detail for ep in available])
        primary = next(ep for ep in available if ep.model_id == selected.model_id)

        # 其余作为 fallback 候选（也按权重随机排序）
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
```

---

## 功能点 2：`RandomWeightsStrategy.execute()` 覆盖

**文件**：`pipeline/strategies.py`

**行为**：不覆盖 `execute()`。`BaseStrategy.execute()` 的默认实现（select + 逐个 call_llm 循环）已满足需求——`select_endpoints()` 返回的 endpoint 列表以加权随机选中的为首选，后续为 fallback。

**无需额外代码**。继承 `BaseStrategy.execute()` 即可。

---

## 功能点 3：`RoundRobinStrategy.select_endpoints()`

**文件**：`pipeline/strategies.py`

**行为**：按 `group_models` 中的固定顺序（weight 降序）轮询。用内存计数器 + `group_id` 做 key。

```python
class RoundRobinStrategy(BaseStrategy):
    """轮询策略 — 按固定顺序依次选择。"""

    _counters: ClassVar[dict[int, int]] = {}  # group_id -> counter

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
            raise NoAvailableModelError(f"Group {group_id}: all models on cooldown")

        # 按 weight 降序排列（weight 相同时保持原始顺序）
        available.sort(key=lambda ep: ep.detail.weight, reverse=True)

        idx = self._counters.get(group_id, 0)
        selected_idx = idx % len(available)

        # 计数器递增 + 溢出保护
        next_idx = idx + 1
        self._counters[group_id] = next_idx % (len(available) * 1000)

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
```

---

## 功能点 4：`RoundRobinStrategy` 计数器 + 溢出保护

**文件**：`pipeline/strategies.py`

**行为**：`ClassVar[dict[int, int]]` 作为类级内存计数器。每次 select 递增后做 `(idx + 1) % (len * 1000)` 防溢出。

**已在功能点 3 中实现**。关键代码行：

```python
_counters: ClassVar[dict[int, int]] = {}

# ...
idx = self._counters.get(group_id, 0)
selected_idx = idx % len(available)

next_idx = idx + 1
self._counters[group_id] = next_idx % (len(available) * 1000)
```

**边界情况**：
- 重启后计数器归零（ClassVar 不持久化）——可接受
- `len(available) == 1` 时，`1 * 1000 = 1000`，计数器在 0-999 循环——安全
- `len(available) == 0` 已被前面的 `if not available` 拦截——不会除零

---

## 功能点 5：`SequentialStrategy.select_endpoints()`

**文件**：`pipeline/strategies.py`

**行为**：按 `group_models` 中的顺序（weight 降序）依次尝试。endpoint 列表的顺序即优先级。

```python
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
            raise NoAvailableModelError(f"Group {group_id}: all models on cooldown")

        # 按 weight 降序排列（weight 相同时保持原始顺序）
        available.sort(key=lambda ep: ep.detail.weight, reverse=True)

        truncated = truncate_messages(messages, available, max_tokens)

        return RouteResult(
            endpoints=available,  # 顺序即优先级
            messages=truncated,
            temperature=temperature,
            max_tokens=max_tokens,
            extra_kwargs=kwargs,
        )
```

---

## 功能点 6：`SequentialStrategy` 按 weight 降序排列

**文件**：`pipeline/strategies.py`

**行为**：`available.sort(key=lambda ep: ep.detail.weight, reverse=True)`。

**已在功能点 5 中实现**。注意 `list.sort()` 是 stable sort，weight 相同时保持 `load_endpoints` 返回的原始顺序（即 `group_models` 表中的插入顺序）。

---

## 功能点 7：3 个策略注册到 STRATEGY_REGISTRY

**文件**：`pipeline/strategies.py`（文件底部）

```python
# --- 注册内建策略 ---
register_strategy("random_weights", RandomWeightsStrategy)
register_strategy("round_robin", RoundRobinStrategy)
register_strategy("sequential", SequentialStrategy)
```

**注意**：`register_strategy` 从 `botflow.pipeline.base` 导入。若重复注册同一 name 会抛 `ValueError`。由于 `strategies.py` 只在模块加载时执行一次注册，正常不会冲突。

---

## 功能点 8：`__init__.py` 导出更新

**文件**：`pipeline/__init__.py`

**改动**：新增导出 3 个策略类。

```python
"""Pipeline Router — 可扩展的路由策略引擎。"""

from botflow.pipeline.base import (
    BaseStrategy,
    RouteResult,
    STRATEGY_REGISTRY,
    StrategyError,
    register_strategy,
)
from botflow.pipeline.strategies import (
    RandomWeightsStrategy,
    RoundRobinStrategy,
    SequentialStrategy,
)

__all__ = [
    "BaseStrategy",
    "RouteResult",
    "STRATEGY_REGISTRY",
    "StrategyError",
    "register_strategy",
    "RandomWeightsStrategy",
    "RoundRobinStrategy",
    "SequentialStrategy",
]
```

---

## 完整 `strategies.py` 文件

```python
"""内建路由策略：RandomWeights / RoundRobin / Sequential。"""

from __future__ import annotations

from typing import ClassVar

from botflow.common.exceptions import NoAvailableModelError
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
            raise NoAvailableModelError(f"Group {group_id}: all models on cooldown")

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
            raise NoAvailableModelError(f"Group {group_id}: all models on cooldown")

        available.sort(key=lambda ep: ep.detail.weight, reverse=True)

        idx = self._counters.get(group_id, 0)
        selected_idx = idx % len(available)

        next_idx = idx + 1
        self._counters[group_id] = next_idx % (len(available) * 1000)

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
            raise NoAvailableModelError(f"Group {group_id}: all models on cooldown")

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
```
