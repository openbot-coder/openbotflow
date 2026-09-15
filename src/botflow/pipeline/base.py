"""Pipeline strategy base classes and registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import NamedTuple

from botflow.common.exceptions import ProviderError
from botflow.router import CooldownManager, ModelEndpoint
from botflow.storage.db import Database


class RouteResult(NamedTuple):
    """策略选择结果：候选 endpoints + 准备好的 messages。"""

    endpoints: list[ModelEndpoint]  # 按优先级排序的候选列表
    messages: list[dict]  # 截断后的 messages
    temperature: float | None
    max_tokens: int | None
    extra_kwargs: dict


class StrategyError(Exception):
    """策略执行过程中发生的错误。"""

    pass


class BaseStrategy(ABC):
    """所有路由策略的基类。

    策略只做「选择」，不做「调用」。
    _shared.py 提供 call_llm() 等工具函数。
    """

    def __init__(self, params: dict):
        self.params = params

    @abstractmethod
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
        """选择候选 endpoints 并准备调用参数。

        返回 RouteResult，包含按优先级排序的 endpoints 列表和截断后的 messages。
        strategy 不调用 LLM，只做选择。
        """
        ...

    async def execute(
        self,
        messages: list[dict],
        db: Database,
        cooldown: CooldownManager,
        group_id: int,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs,
    ) -> dict:
        """非流式便捷方法：select + 逐个尝试 call_llm，返回第一个成功的结果。"""
        from botflow.pipeline._shared import call_llm

        result = await self.select_endpoints(
            messages, db, cooldown, group_id, temperature, max_tokens, **kwargs
        )
        for ep in result.endpoints:
            resp = await call_llm(
                ep,
                result.messages,
                group_id,
                cooldown,
                result.temperature,
                result.max_tokens,
                **result.extra_kwargs,
            )
            if resp is not None:
                return resp
        raise ProviderError(f"All endpoints failed in strategy for group {group_id}")


STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {}


def register_strategy(name: str, cls: type[BaseStrategy]) -> None:
    if name in STRATEGY_REGISTRY:
        raise ValueError(f"Strategy '{name}' already registered")
    STRATEGY_REGISTRY[name] = cls
