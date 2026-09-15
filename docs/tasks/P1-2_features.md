# P1-2 功能点文档：Pipeline 骨架 — BaseStrategy + _shared.py

> 对应设计文档：`docs/pipeline_router_design.md` v2.0（重点第 3、5 节）
> 子任务：P1 — Pipeline 骨架
> 日期：2026-09-09
> 依赖：P1-1 已完成（ModelGroup 加了 type/params 字段，db.py CRUD 已支持）

---

## 概述

创建 `src/botflow/pipeline/` 包，包含：
- `base.py`：`RouteResult`、`StrategyError`、`BaseStrategy` ABC、`STRATEGY_REGISTRY`
- `_shared.py`：从 `router.py` 搬过来的共享基础设施（endpoint 缓存、cooldown 过滤、LLM 调用、context truncation、缓存失效）
- `__init__.py`：包导出

---

## 功能点 1：`RouteResult` NamedTuple 定义

### 改动位置

- 新文件：`src/botflow/pipeline/base.py`

### 实现代码

```python
from typing import NamedTuple
from botflow.router import ModelEndpoint


class RouteResult(NamedTuple):
    """策略选择结果：候选 endpoints + 准备好的 messages。"""
    endpoints: list[ModelEndpoint]   # 按优先级排序的候选列表
    messages: list[dict]             # 截断后的 messages
    temperature: float | None
    max_tokens: int | None
    extra_kwargs: dict
```

### 实现原因

策略只负责"选择"，不负责"调用"。`RouteResult` 封装策略的输出，使 `PipelineEngine` 可以用统一接口处理所有策略的结果。`endpoints` 按优先级排序，由 `execute()` 或流式调用者逐个尝试。

### 依赖的现有代码

- `router.py` 第 321-343 行的 `ModelEndpoint` 类：作为 `RouteResult.endpoints` 的元素类型

---

## 功能点 2：`StrategyError` 异常类

### 改动位置

- 新文件：`src/botflow/pipeline/base.py`

### 实现代码

```python
class StrategyError(Exception):
    """策略执行过程中发生的错误。"""
    pass
```

### 实现原因

`PipelineEngine.route()` 需要区分策略错误和其他错误（`ProviderError`、`AllModelsCooldownError`）。策略错误触发 fallback，配置错误（`ConfigurationError`）不触发。参见设计文档 6.3 节的 `except` 分支。

### 依赖的现有代码

- `common/exceptions.py` 中的异常层次：`StrategyError` 继承 `Exception`（不继承 `BotflowError`，因为它是 pipeline 层特有的）

---

## 功能点 3：`BaseStrategy` ABC（`select_endpoints` + `execute`）

### 改动位置

- 新文件：`src/botflow/pipeline/base.py`

### 实现代码

```python
from abc import ABC, abstractmethod
from botflow.storage.db import Database
from botflow.router import CooldownManager


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
                ep, result.messages, group_id, cooldown,
                result.temperature, result.max_tokens, **result.extra_kwargs
            )
            if resp is not None:
                return resp
        raise ProviderError(f"All endpoints failed in strategy for group {group_id}")
```

### 实现原因

- `db` 和 `cooldown` 通过方法参数传入，不在 `__init__` 中存储——避免 db 实例过期问题
- `group_id` 在方法参数中传递——strategy 需要知道 group_id 用于 cooldown key
- `select_endpoints()` 是核心抽象，streaming 和 non-streaming 都用它
- `execute()` 是 default implementation（select + call 循环），简单策略不需要覆盖

### 依赖的现有代码

- `router.py` 第 350-358 行：`GroupRouter.__init__` 的参数设计（group_id, db, cooldown_manager）
- `router.py` 第 423-479 行：`GroupRouter._route_non_stream` 的 select + call 循环逻辑
- `common/exceptions.py` 第 16 行：`ProviderError`

---

## 功能点 4：`STRATEGY_REGISTRY` + `register_strategy()`

### 改动位置

- 新文件：`src/botflow/pipeline/base.py`

### 实现代码

```python
STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {}


def register_strategy(name: str, cls: type[BaseStrategy]) -> None:
    if name in STRATEGY_REGISTRY:
        raise ValueError(f"Strategy '{name}' already registered")
    STRATEGY_REGISTRY[name] = cls
```

### 实现原因

策略注册表让 `PipelineEngine` 可以通过 `group.type` 字符串查找策略类，实现开闭原则。新增策略只需 `register_strategy()`，不改 engine 代码。重复注册同名策略报错，防止意外覆盖。

### 依赖的现有代码

- 设计文档 3.2 节定义的注册表模式
- 无直接依赖现有代码（纯新定义）

---

## 功能点 5：`_shared.py`：`load_endpoints()` + endpoint 缓存

### 改动位置

- 新文件：`src/botflow/pipeline/_shared.py`

### 实现代码

```python
"""Pipeline 共享基础设施。

所有函数为独立的 async 函数（不是 BaseStrategy 的方法），策略通过参数调用。
从 router.py 搬过来的全局状态和工具函数。
"""

from __future__ import annotations

import asyncio
import time
from contextlib import nullcontext
from typing import Any

from botflow.common.logger import get_logger
from botflow.config import get_config
from botflow.providers.base import BaseProvider
from botflow.providers.anthropic_provider import AnthropicProvider
from botflow.providers.google_provider import GoogleProvider
from botflow.providers.openai_compat import OpenAICompatProvider
from botflow.providers.deepseek_provider import DeepSeekProvider
from botflow.storage.db import Database
from botflow.storage.models import GroupModelWithDetails
from botflow.router import (
    CooldownManager,
    ModelEndpoint,
    is_retryable_error,
    exponential_backoff,
)

log = get_logger("pipeline.shared")

# ---------------------------------------------------------------------------
# 从 router.py 搬过来的全局状态
# ---------------------------------------------------------------------------

_provider_semaphores: dict[int, asyncio.Semaphore | None] = {}
_endpoint_cache: dict[int, tuple[list[ModelEndpoint], float]] = {}
_ENDPOINT_CACHE_TTL = 60  # seconds

_provider_cache: dict[tuple[int, str], tuple[BaseProvider, float]] = {}
_PROVIDER_CACHE_TTL = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Provider factory + caching (from router.py)
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


def _get_cached_provider(
    provider_id: int,
    provider_type: str,
    api_key: str,
    base_url: str,
    extra_config: dict[str, Any] | None = None,
    api_format: str = "",
    proxy: str = "",
) -> BaseProvider:
    resolved_type = api_format if api_format else provider_type
    cache_key = (provider_id, resolved_type, proxy)
    now = time.time()
    cached = _provider_cache.get(cache_key)
    if cached:
        instance, create_time = cached
        if now - create_time < _PROVIDER_CACHE_TTL:
            return instance
    merged_config = dict(extra_config) if extra_config else {}
    if proxy:
        merged_config["proxy"] = proxy
    cls = PROVIDER_TYPE_MAP.get(resolved_type)
    if cls is None:
        raise ValueError(f"Unsupported provider type: {resolved_type}")
    instance = cls(api_key=api_key, base_url=base_url, extra_config=merged_config)
    _provider_cache[cache_key] = (instance, now)
    return instance


async def load_endpoints(group_id: int, db: Database) -> list[ModelEndpoint]:
    """从 DB 加载 group 的所有 enabled endpoints（带缓存）。

    1:1 复用现有 _endpoint_cache 逻辑和 _get_cached_provider。
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
```

### 实现原因

`load_endpoints` 从 `GroupRouter._load_endpoints`（router.py 第 359-387 行）1:1 搬过来，改为模块级函数。60 秒 TTL 缓存避免每次请求都查 DB。所有策略共享同一个缓存，避免重复加载。

### 依赖的现有代码

- `router.py` 第 188-189 行：`_endpoint_cache` 和 `_ENDPOINT_CACHE_TTL` 常量
- `router.py` 第 192-226 行：`_get_cached_provider` 函数
- `router.py` 第 359-387 行：`GroupRouter._load_endpoints` 方法
- `storage/db.py` 第 707-728 行：`Database.get_group_models()` 方法
- `storage/db.py` 第 392-396 行：`Database.get_provider()` 方法

---

## 功能点 6：`_shared.py`：`filter_available()` cooldown 过滤

### 改动位置

- 新文件：`src/botflow/pipeline/_shared.py`

### 实现代码

```python
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
```

### 实现原因

从 `GroupRouter._get_available`（router.py 第 389-395 行）1:1 搬过来，改为独立函数。所有策略共享此过滤逻辑，不需要各自实现 cooldown 检查。

### 依赖的现有代码

- `router.py` 第 389-395 行：`GroupRouter._get_available` 方法
- `router.py` 第 112-125 行：`CooldownManager.is_on_cooldown` 方法

---

## 功能点 7：`_shared.py`：`call_llm()` + 信号量 + retry

### 改动位置

- 新文件：`src/botflow/pipeline/_shared.py`

### 实现代码

```python
def _ensure_provider_semaphore(provider_id: int, size: int) -> asyncio.Semaphore | None:
    """Return the cached semaphore for ``provider_id``, creating it if needed."""
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


async def call_llm(
    ep: ModelEndpoint,
    messages: list[dict],
    group_id: int,
    cooldown: CooldownManager,
    temperature: float | None = None,
    max_tokens: int | None = None,
    **kwargs,
) -> dict | None:
    """调用单个 endpoint，带 retry + cooldown + 信号量管理。

    从 GroupRouter._attempt_call 1:1 搬过来。
    信号量是全局跨 group 共享的——同一个 provider 跨 group 限流。
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
    return None
```

### 实现原因

`call_llm` 是 pipeline 层最核心的共享函数，从 `GroupRouter._attempt_call`（router.py 第 556-621 行）1:1 搬过来，改为模块级函数。关键点：
- **信号量全局共享**：同一个 provider 跨 group 限流，必须在 `_shared.py` 而不是 strategy 里
- **retry + backoff**：非可重试错误直接 break，可重试错误指数退避
- **cooldown 记录**：成功重置失败计数，失败达到阈值触发 cooldown

### 依赖的现有代码

- `router.py` 第 35-57 行：`_provider_semaphores`、`_ensure_provider_semaphore`、`_noop_asynccontext`
- `router.py` 第 528-554 行：`GroupRouter._apply_model_extra_config`
- `router.py` 第 556-621 行：`GroupRouter._attempt_call`
- `router.py` 第 284-313 行：`is_retryable_error`、`exponential_backoff`（保留 import，不搬）
- `config.py` 第 47 行：`get_config().upstream_semaphore_size`

---

## 功能点 8：`_shared.py`：`truncate_messages()` context window

### 改动位置

- 新文件：`src/botflow/pipeline/_shared.py`

### 实现代码

```python
from botflow.common.context import truncate_to_context_window


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
```

### 实现原因

从 `GroupRouter._route_non_stream`（router.py 第 454-457 行）和 `_route_stream`（router.py 第 513-515 行）的截断逻辑提取为独立函数。取所有可用 endpoint 的最小 context_window，确保截断后所有 endpoint 都能处理。

### 依赖的现有代码

- `router.py` 第 454-457 行：`_route_non_stream` 中的 context_window 截断逻辑
- `common/context.py` 第 46-95 行：`truncate_to_context_window` 函数

---

## 功能点 9：`_shared.py`：`invalidate_endpoint_cache()`

### 改动位置

- 新文件：`src/botflow/pipeline/_shared.py`

### 实现代码

```python
def invalidate_endpoint_cache(group_id: int) -> None:
    """Admin 修改 group_models 后调用，清除缓存。"""
    _endpoint_cache.pop(group_id, None)
```

### 实现原因

Admin API 修改 group_models 关联后，需要清除缓存使新配置立即生效。否则旧缓存 TTL 内（60 秒）请求仍用旧 endpoint 列表。

### 依赖的现有代码

- `router.py` 第 188 行：`_endpoint_cache` 全局字典

---

## 功能点 10：`pipeline/__init__.py` 导出

### 改动位置

- 新文件：`src/botflow/pipeline/__init__.py`

### 实现代码

```python
"""Pipeline Router — 可扩展的路由策略引擎。"""

from botflow.pipeline.base import (
    BaseStrategy,
    RouteResult,
    STRATEGY_REGISTRY,
    StrategyError,
    register_strategy,
)

__all__ = [
    "BaseStrategy",
    "RouteResult",
    "STRATEGY_REGISTRY",
    "StrategyError",
    "register_strategy",
]
```

### 实现原因

提供干净的包级导入接口。后续 P2 的 `PipelineEngine` 和内建策略也会从这里导出。`__all__` 明确公共 API，防止内部实现泄漏。

### 依赖的现有代码

- 无直接依赖（纯新定义）

---

## 文件变更清单

| 操作 | 文件 | 说明 |
|------|------|------|
| **新增** | `src/botflow/pipeline/__init__.py` | 包入口 + 导出 |
| **新增** | `src/botflow/pipeline/base.py` | RouteResult + StrategyError + BaseStrategy + STRATEGY_REGISTRY |
| **新增** | `src/botflow/pipeline/_shared.py` | load_endpoints + filter_available + call_llm + truncate_messages + invalidate_endpoint_cache |

### 不变文件

| 文件 | 说明 |
|------|------|
| `src/botflow/router.py` | 保留所有现有代码（GroupRouter 标记 deprecated 在 P7） |
| `src/botflow/common/context.py` | 保持不变，`truncate_to_context_window` 被 import 调用 |
| `src/botflow/common/exceptions.py` | 保持不变，`ProviderError` 被 import 调用 |
| `src/botflow/storage/db.py` | 保持不变（P1-1 已完成） |
| `src/botflow/storage/models.py` | 保持不变（P1-1 已完成） |

---

## 设计决策

1. **从 router.py 搬逻辑而非 import**：`_shared.py` 复制了 `_endpoint_cache`、`_provider_cache`、`_provider_semaphores` 等全局状态。不从 router.py import 是因为 P7 会清理 router.py，避免循环依赖和未来删除困难。
2. **保留 router.py 的 `is_retryable_error` 和 `exponential_backoff` import**：这两个是纯函数，无状态，直接 import 即可，不需要搬。
3. **`_apply_model_extra_config` 搬到 `_shared.py`**：因为 `call_llm` 依赖它，且它是纯函数，搬过来更内聚。
4. **策略不存 db/cooldown 为实例属性**：避免 db 实例过期问题，每次调用通过参数传入。
5. **`execute()` 是可选的默认实现**：简单策略不需要覆盖；复杂策略（如 langgraph）可以只实现 `select_endpoints()` 由 engine 调用。
