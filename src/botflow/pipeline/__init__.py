"""Pipeline Router — 可扩展的路由策略引擎。"""

from botflow.pipeline.base import (
    BaseStrategy,
    RouteResult,
    STRATEGY_REGISTRY,
    StrategyError,
    register_strategy,
)
from botflow.pipeline.engine import PipelineEngine
from botflow.pipeline.strategies import (
    RandomWeightsStrategy,
    RoundRobinStrategy,
    SequentialStrategy,
)
from botflow.pipeline.langgraph_strategy import LangGraphStrategy  # noqa: F401 — 注册策略

__all__ = [
    "BaseStrategy",
    "RouteResult",
    "STRATEGY_REGISTRY",
    "StrategyError",
    "register_strategy",
    "PipelineEngine",
    "RandomWeightsStrategy",
    "RoundRobinStrategy",
    "SequentialStrategy",
    "LangGraphStrategy",
]
