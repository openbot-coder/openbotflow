"""PipelineEngine — 统一路由入口（LangGraph 实现）。

对外保持 ``route()`` / ``route_stream()`` 接口不变，
内部路由、重试、降级全部由 ``LangGraphEngine`` 的 StateGraph 驱动。
"""

from __future__ import annotations

from typing import Any, Callable

from botflow.common.exceptions import ConfigurationError
from botflow.common.logger import get_logger
from botflow.pipeline.langgraph_engine import LangGraphEngine
from botflow.router import CooldownManager
from botflow.storage.db import Database
from botflow.storage.models import ModelGroup

log = get_logger("pipeline.engine")


class PipelineEngine:
    """统一路由入口 — 代理到 LangGraphEngine。

    ``cooldown`` 属性对外暴露，供 ``core._stream_common()`` 调用
    ``cooldown.record_success()`` 时使用。
    """

    def __init__(self, db_factory: Callable[[], Database], cooldown: CooldownManager):
        self._inner = LangGraphEngine(db_factory, cooldown)
        self.cooldown = cooldown  # public — used by core.py stream path
        # Backward-compatible internal attributes (tests access these directly)
        self._db_factory = db_factory
        self._group_cache = self._inner._group_cache
        self._GROUP_CACHE_TTL = self._inner._GROUP_CACHE_TTL

    @property
    def db(self) -> Database:
        return self._inner.db

    def _create_strategy(self, group: ModelGroup):
        """Backward-compatible: create a strategy instance from the registry."""
        from botflow.pipeline.base import STRATEGY_REGISTRY, BaseStrategy
        strategy_cls = STRATEGY_REGISTRY.get(group.type)
        if strategy_cls is None:
            raise ConfigurationError(
                f"Unknown strategy type '{group.type}' for group '{group.name}'. "
                f"Available: {', '.join(sorted(STRATEGY_REGISTRY))}"
            )
        return strategy_cls(params=group.params)

    async def _load_group(self, group_id: int) -> ModelGroup:
        return await self._inner._load_group(group_id)

    async def route(
        self,
        group: ModelGroup,
        messages: list[dict],
        stream: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs,
    ) -> dict:
        """非流式路由 — 图内单次执行 + 驱动层组级降级（兼容 R-09~R-14）。"""
        return await self._inner.route(
            group, messages,
            stream=stream,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )

    async def run(
        self,
        strategy=None,
        group: ModelGroup | None = None,
        *,
        messages: list[dict] | None = None,
        mode: str = "chat",
        temperature: float | None = None,
        max_tokens: int | None = None,
        request: Any | None = None,
        **kwargs: Any,
    ):
        """SG-1 单组执行入口 — 委托内层 LangGraphEngine。

        ``mode="chat"`` 返回最终 result dict；``mode="stream"`` 返回事件异步生成器。
        """
        return await self._inner.run(
            strategy, group,
            messages=messages, mode=mode,
            temperature=temperature, max_tokens=max_tokens,
            request=request, **kwargs,
        )

    async def stream_events(self, strategy=None, group: ModelGroup | None = None, **kwargs: Any):
        """SG-1 流式事件入口 — 委托内层 LangGraphEngine。"""
        return await self._inner.stream_events(strategy, group, **kwargs)
