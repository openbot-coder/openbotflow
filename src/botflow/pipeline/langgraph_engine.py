"""LangGraph-based routing engine.

Replaces the ad-hoc PipelineEngine loop with a typed StateGraph that models
the full retry / fallback / cooldown lifecycle as explicit nodes and edges.

Two execution modes
--------------------
1. **Non-streaming** (``route()``) — the graph drives the full lifecycle:
   load_endpoints → select → call_llm → (retry | fallback | done).

2. **Streaming** (``route_stream()``) — the graph only selects endpoints;
   the caller (``core._stream_common``) handles the actual token iteration,
   keeping the provider-level SSE plumbing where it belongs.

All nodes are ``async`` and the graph is executed via ``ainvoke()``.
The previous ``ainvoke`` deadlock on Windows/Python 3.13 was traced to the
LangSmith tracing initialisation running in the wrong thread; in production
under uvicorn (async) it is not an issue.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Literal, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.config import get_config

from botflow.common.exceptions import (
    ConfigurationError,
    NoAvailableModelError,
    ProviderError,
)
from botflow.common.logger import get_logger
from botflow.pipeline._shared import call_llm
from botflow.pipeline.base import STRATEGY_REGISTRY
from botflow.router import CooldownManager
from botflow.storage.db import Database
from botflow.storage.models import ModelGroup

log = get_logger("pipeline.langgraph_engine")


# ---------------------------------------------------------------------------
# Graph context — non-serialisable dependencies passed via RunnableConfig
# ---------------------------------------------------------------------------

@dataclass
class GraphContext:
    """Holds references to DB, cooldown manager, etc.

    Passed into every node via ``config["configurable"]["ctx"]``.
    """
    db: Database
    cooldown: CooldownManager


# ---------------------------------------------------------------------------
# State schema
# ---------------------------------------------------------------------------

class RouteState(TypedDict, total=False):
    """State carried through the LangGraph routing graph."""

    # ── Input ──────────────────────────────────────────────
    messages: list[dict[str, Any]]
    model_name: str
    temperature: float | None
    max_tokens: int | None
    extra_kwargs: dict[str, Any]

    # ── Group / strategy ───────────────────────────────────
    group: Any  # ModelGroup (not fully serialisable, kept in memory)
    strategy_name: str  # e.g. "random_weights"

    # ── Endpoint queue ─────────────────────────────────────
    endpoints: list  # list[ModelEndpoint], ordered by strategy
    current_ep_idx: int  # index into endpoints
    truncated_messages: list[dict[str, Any]]

    # ── Retry / cooldown ──────────────────────────────────
    attempt: int  # current retry count for the active endpoint

    # ── Fallback ──────────────────────────────────────────
    fallback_group_id: int | None
    fallback_depth: int  # how many fallback hops (max 3)
    visited_groups: list[int]  # cycle detection

    # ── Result ────────────────────────────────────────────
    result: dict[str, Any] | None
    error: str | None        # recoverable (endpoint failed → fallback)
    fatal_error: str | None  # unrecoverable (cycle, depth, config → no fallback)
    used_model_id: int | None
    used_provider_id: int | None

    # ── Metadata ──────────────────────────────────────────
    stream: bool  # True = select-only mode (no LLM call in graph)
    _initialized: bool  # True after first resolve_group pass


# ---------------------------------------------------------------------------
# Graph nodes (ALL ASYNC — run under uvicorn's event loop via ainvoke)
# ---------------------------------------------------------------------------

async def _resolve_group(state: RouteState) -> dict:
    """Resolve the active ModelGroup.

    First invocation (``_initialized`` is falsy): pass the initial group through.
    Subsequent invocations (after fallback): load the fallback group from DB.

    **Never raises** — fatal errors (cycle, depth, config) are stored in
    ``state["fatal_error"]`` so ``_route_after_call`` can route directly
    to ``_finalize_error`` and the graph stays consistent.
    """
    ctx: GraphContext = get_config()["configurable"]["ctx"]

    if not state.get("_initialized"):
        # First pass — group was already provided in initial state
        group = state["group"]
        fallback_depth = state.get("fallback_depth", 0)
        visited = list(state.get("visited_groups", []))
        if group.id not in visited:
            visited.append(group.id)
        return {
            "_initialized": True,
            "visited_groups": visited,
            "fallback_depth": fallback_depth,
        }

    # Fallback pass — load the fallback group
    fallback_gid = state.get("fallback_group_id")
    visited = list(state.get("visited_groups", []))
    fallback_depth = state.get("fallback_depth", 0)

    if fallback_depth > 3:
        return {"fatal_error": "Fallback chain too deep"}

    if not fallback_gid or fallback_gid in visited:
        return {"fatal_error": "No fallback group available"}

    group = await ctx.db.get_group(fallback_gid)
    if group is None:
        return {"fatal_error": f"Fallback group {fallback_gid} not found"}
    visited.append(group.id)

    return {
        "group": group,
        "fallback_group_id": group.fallback_group_id,
        "fallback_depth": fallback_depth + 1,
        "visited_groups": visited,
        "_initialized": True,
    }


async def _load_and_select(state: RouteState) -> dict:
    """Load endpoints and apply registered strategy selection.

    Delegates entirely to ``STRATEGY_REGISTRY[group.type].select_endpoints()``
    which handles load → filter → select → truncate internally.

    If ``state["fatal_error"]`` is already set (e.g. cycle/depth from
    ``_resolve_group``), short-circuit — ``_route_after_resolve`` will
    route to ``finalize_error`` before this node runs, but this guard
    handles edge cases.
    """
    if state.get("fatal_error"):
        return {}  # pass-through so _route_after_call routes to error

    ctx: GraphContext = get_config()["configurable"]["ctx"]
    group = state["group"]

    strategy_name = group.type or "random_weights"

    # langgraph strategy is a multi-step workflow; not compatible with
    # the select→call graph pattern.  Raise early so it fails clearly.
    if strategy_name == "langgraph":
        raise ConfigurationError(
            "LangGraph strategy is a multi-step workflow and cannot be used "
            "with route()/route_stream(). Use LangGraphStrategy.execute() directly."
        )

    strategy_cls = STRATEGY_REGISTRY.get(strategy_name)
    if strategy_cls is None:
        raise ConfigurationError(
            f"Unknown strategy: '{strategy_name}'. "
            f"Available: {', '.join(sorted(STRATEGY_REGISTRY))}"
        )

    strategy = strategy_cls(params=group.params or {})

    try:
        result = await strategy.select_endpoints(
            messages=state["messages"],
            db=ctx.db,
            cooldown=ctx.cooldown,
            group_id=group.id,
            temperature=state.get("temperature"),
            max_tokens=state.get("max_tokens"),
            **state.get("extra_kwargs", {}),
        )
    except Exception as exc:
        # Recoverable — store in error (not fatal) so graph can try fallback
        return {"error": str(exc)}

    return {
        "endpoints": result.endpoints,
        "current_ep_idx": 0,
        "truncated_messages": result.messages,
        "attempt": 0,
        "strategy_name": strategy_name,
    }


async def _try_call(state: RouteState) -> dict:
    """Attempt a non-streaming LLM call on the current endpoint.

    Tries each endpoint in the selected list in order.  On success,
    returns the result.  When all endpoints fail, clears the list so
    the graph routes to fallback/error — never loops back to try_call.
    """
    ctx: GraphContext = get_config()["configurable"]["ctx"]
    endpoints = state.get("endpoints", [])
    idx = state.get("current_ep_idx", 0)
    group = state["group"]
    group_id = group.id
    truncated = state.get("truncated_messages", state.get("messages", []))
    temperature = state.get("temperature")
    max_tokens = state.get("max_tokens")
    extra = state.get("extra_kwargs", {})

    # Walk through all remaining endpoints, call_llm once per endpoint
    while idx < len(endpoints):
        ep = endpoints[idx]
        resp = await call_llm(
            ep, truncated, group_id, ctx.cooldown,
            temperature, max_tokens, **extra,
        )
        if resp is not None:
            routing = resp.get("_routing", {})
            return {
                "result": resp,
                "error": None,
                "used_model_id": routing.get("model_id"),
                "used_provider_id": routing.get("provider_id"),
                # Clear so graph routes to success/end, not fallback
                "endpoints": [],
                "current_ep_idx": 0,
            }
        idx += 1

    # All endpoints failed — clear so graph routes to fallback/error.
    # Preserve any pre-existing fatal_error (e.g. from _resolve_group cycle/depth check).
    if not state.get("fatal_error"):
        return {"endpoints": [], "current_ep_idx": 0, "error": "All endpoints in group failed"}
    return {"endpoints": [], "current_ep_idx": 0}


async def _finalize_error(state: RouteState) -> dict:
    """Build the final error response.

    Preserves the original error/exception message so callers receive
    an accurate diagnostic (e.g. "No fallback group available",
    "Fallback chain too deep").
    """
    # Prefer fatal_error (cycle, depth, config) over recoverable error
    raw = state.get("fatal_error") or state.get("error")
    if isinstance(raw, Exception):
        msg = str(raw)
    elif isinstance(raw, dict):
        msg = raw.get("message", "Routing failed")
    else:
        msg = raw or "Routing failed"

    return {
        "result": {
            "error": {
                "message": msg,
                "type": "server_error",
            }
        }
    }


# ---------------------------------------------------------------------------
# Conditional edge routers
# ---------------------------------------------------------------------------

def _route_after_load(state: RouteState) -> Literal["select", "done"]:
    """After load_and_select: stream mode → END, non-stream → try_call."""
    if state.get("stream"):
        return "done"
    return "select"


def _route_after_call(state: RouteState) -> Literal["success", "fallback", "error"]:
    """After try_call or resolve_group, decide next step.

    ``fatal_error`` (cycle, depth, config) → straight to ``error``.
    ``error`` (endpoint failure) → always attempt fallback;
    ``_resolve_group`` handles cycle detection, depth limits,
    and missing fallback groups (stores them in ``fatal_error``).
    """
    # Fatal error (cycle, depth, config) — no fallback possible
    if state.get("fatal_error"):
        return "error"

    if state.get("result") is not None:
        return "success"

    # Endpoint failed — always attempt fallback; _resolve_group handles
    # cycle detection (fallback_gid in visited), depth limits (>3),
    # missing fallback group (gid=None), and not-found (group=None).
    return "fallback"


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def build_route_graph() -> StateGraph:
    """Build and compile the LangGraph routing graph.

    The graph models::

        START
          │
          ▼
      resolve_group ──▶ load_and_select ──▶ [stream: END / non-stream: try_call]
                                                      │
                                              ┌───────┴───────┐
                                              │               │
                                              ▼               ▼
                                          success     next_ep / try_call
                                                            │
                                                     ┌──────┴──────┐
                                                     │             │
                                                   fallback     error
                                                     │             │
                                                     ▼             ▼
                                              resolve_group    finalize_error → END
                                              (loop back)
    """
    g = StateGraph(RouteState)

    # Nodes
    g.add_node("resolve_group", _resolve_group)
    g.add_node("load_and_select", _load_and_select)
    g.add_node("try_call", _try_call)
    g.add_node("finalize_error", _finalize_error)

    # Edges
    g.set_entry_point("resolve_group")

    # After resolve_group: fatal_error → error; otherwise continue to load_and_select
    def _route_after_resolve(state: RouteState) -> Literal["continue", "error"]:
        return "error" if state.get("fatal_error") else "continue"

    g.add_conditional_edges(
        "resolve_group",
        _route_after_resolve,
        {"continue": "load_and_select", "error": "finalize_error"},
    )

    g.add_conditional_edges(
        "load_and_select",
        _route_after_load,
        {"select": "try_call", "done": END},
    )

    g.add_conditional_edges(
        "try_call",
        _route_after_call,
        {
            "success": END,
            "fallback": "resolve_group",
            "error": "finalize_error",
        },
    )

    g.add_edge("finalize_error", END)

    return g


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class LangGraphEngine:
    """LangGraph-based routing engine that replaces PipelineEngine.

    Usage::

        engine = LangGraphEngine(db_factory, cooldown)
        # non-streaming
        result = await engine.route(group, messages, temperature=..., max_tokens=...)
        # streaming (returns candidate endpoints for caller to iterate)
        result = await engine.route_stream(group, messages, temperature=..., max_tokens=...)

    All nodes are ``async`` and the graph is executed via ``ainvoke()``
    under uvicorn's event loop.
    """

    def __init__(
        self,
        db_factory: Callable[[], Database],
        cooldown: CooldownManager,
    ):
        self._db_factory = db_factory
        self.cooldown = cooldown
        self._graph = build_route_graph().compile()
        self._group_cache: dict[int, tuple[ModelGroup, float]] = {}
        self._GROUP_CACHE_TTL = 60

    @property
    def db(self) -> Database:
        return self._db_factory()

    async def _load_group(self, group_id: int) -> ModelGroup:
        now = time.time()
        cached = self._group_cache.get(group_id)
        if cached:
            group, ts = cached
            if now - ts < self._GROUP_CACHE_TTL:
                return group
        group = await self.db.get_group(group_id)
        if group is None:
            raise ConfigurationError(f"Group {group_id} not found")
        self._group_cache[group_id] = (group, now)
        return group

    async def route(
        self,
        group: ModelGroup,
        messages: list[dict],
        stream: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> dict:
        """Non-streaming: full graph execution via async ``ainvoke()``."""
        ctx = GraphContext(db=self.db, cooldown=self.cooldown)
        initial: RouteState = {
            "messages": messages,
            "model_name": "",
            "temperature": temperature,
            "max_tokens": max_tokens,
            "extra_kwargs": kwargs,
            "group": group,
            "fallback_group_id": group.fallback_group_id,
            "fallback_depth": 0,
            "visited_groups": [group.id],
            "stream": stream,
            "_initialized": False,
        }

        result = await self._graph.ainvoke(
            initial,
            config={"configurable": {"ctx": ctx}},
        )

        result_dict = result.get("result")
        if result_dict:
            # `_finalize_error` puts error info inside result["error"]
            if isinstance(result_dict, dict) and "error" in result_dict:
                err = result_dict["error"]
                msg = err.get("message", "Routing failed") if isinstance(err, dict) else str(err)
                raise ProviderError(msg)
            return result_dict

        # graph ended without setting result — treat state["error"] as failure
        err = result.get("error")
        if err:
            msg = err.get("message", "Routing failed") if isinstance(err, dict) else str(err)
            raise ProviderError(msg)
        raise ProviderError("Routing failed with no result")

    async def route_stream(
        self,
        group: ModelGroup,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Streaming: select endpoints only, return for caller to iterate.

        Uses async ``ainvoke()`` for endpoint selection (stream=True
        short-circuits the graph after ``load_and_select``).
        """
        ctx = GraphContext(db=self.db, cooldown=self.cooldown)
        initial: RouteState = {
            "messages": messages,
            "model_name": "",
            "temperature": temperature,
            "max_tokens": max_tokens,
            "extra_kwargs": kwargs,
            "group": group,
            "fallback_group_id": group.fallback_group_id,
            "fallback_depth": 0,
            "visited_groups": [group.id],
            "stream": True,
            "_initialized": False,
        }

        result = await self._graph.ainvoke(
            initial,
            config={"configurable": {"ctx": ctx}},
        )

        endpoints = result.get("endpoints", [])
        if not endpoints:
            raise NoAvailableModelError(
                f"Group {group.id} has no available models for streaming"
            )

        return {
            "endpoints": endpoints,
            "group_id": group.id,
            "messages": result.get("truncated_messages", messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "kwargs": kwargs,
            "fallback_group_id": group.fallback_group_id,
        }
