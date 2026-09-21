"""LangGraph-based routing engine (SG-1 slim graph).

The graph models a SINGLE strategy execution::

    START → select_endpoints → (stream? try_stream : try_call) → (END | finalize_error)

Group-level fallback is NO LONGER part of the graph (removed in SG-1): the
driving layer — ``LangGraphEngine.route`` (legacy, for frozen R-09~R-14) and
``core._drive`` (unified streaming + non-streaming) — owns the ≤3-hop fallback
loop with cycle / depth detection. The graph only carries ``recoverable`` so the
driver knows whether a failed group may be retried on a backup group.

Two execution modes
-------------------
1. **Non-streaming** (``route()`` / ``run(mode="chat")``) — ``select_endpoints``
   → ``try_call`` → ``finalize_error``; the driving layer does fallback.
2. **Streaming** (``run(mode="stream")`` / ``stream_events``) — ``select_endpoints``
   → ``try_stream``; the node pushes chunks via LangGraph's ``get_stream_writer()``
   and carries ``recoverable`` in the final state so the driver can fall back.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal, NoReturn, TypedDict

from langgraph.config import get_config, get_stream_writer
from langgraph.graph import END, StateGraph

from botflow.common.exceptions import (
    AllModelsCooldownError,
    BotflowError,
    ConfigurationError,
    NoAvailableModelError,
    ProviderError,
)
from botflow.common.logger import get_logger
from botflow.pipeline._shared import call_llm
from botflow.pipeline.base import STRATEGY_REGISTRY, StrategyError
from botflow.router import CooldownManager, exponential_backoff, is_retryable_error
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
    ``request`` is optional and used by ``try_stream`` for disconnect detection.
    """

    db: Database
    cooldown: CooldownManager
    request: Any | None = None


# ---------------------------------------------------------------------------
# State schema
# ---------------------------------------------------------------------------


class RouteState(TypedDict, total=False):
    """State carried through the (slim) LangGraph routing graph."""

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

    # ── Fallback bookkeeping (driver-owned; graph ignores it) ─
    fallback_group_id: int | None
    fallback_depth: int
    visited_groups: list[int]

    # ── Result ────────────────────────────────────────────
    result: dict[str, Any] | None
    # Recoverable (group failed → driver may fall back to a backup group).
    # True for AllModelsCooldownError / NoAvailableModelError / (retryable)
    # ProviderError / "all endpoints failed"; False for ConfigurationError /
    # StrategyError / TypeError / other unexpected errors.
    recoverable: bool
    error: Any  # original exception object or message
    used_model_id: int | None
    used_provider_id: int | None

    # ── Failed-attempt audit trail (SG-0) ─────────────────
    attempts: list[dict]

    # ── Mode / stream bookkeeping ─────────────────────────
    mode: Literal["chat", "stream"]
    stream: bool  # True = streaming path (selected by ``mode``)
    stream_started: bool  # a chunk was already emitted to the client


# ---------------------------------------------------------------------------
# Strategy selection helper (shared by the node + run()'s eager pre-selection)
# ---------------------------------------------------------------------------


def _build_strategy(group: ModelGroup):
    """Build a strategy instance from ``group.type`` (raises on bad type)."""
    strategy_name = group.type or "random_weights"
    if strategy_name == "langgraph":
        raise ConfigurationError(
            "LangGraph strategy is a multi-step workflow and cannot be used "
            "with route()/run(). Use LangGraphStrategy.execute() directly."
        )
    strategy_cls = STRATEGY_REGISTRY.get(strategy_name)
    if strategy_cls is None:
        raise ConfigurationError(
            f"Unknown strategy: '{strategy_name}'. "
            f"Available: {', '.join(sorted(STRATEGY_REGISTRY))}"
        )
    return strategy_cls(params=group.params or {})


async def _select_endpoints(
    group: ModelGroup,
    messages: list[dict] | None,
    temperature: float | None,
    max_tokens: int | None,
    extra: dict[str, Any],
    db: Database,
    cooldown: CooldownManager,
):
    """Run strategy selection (load → filter → select → truncate).

    Raises the original exception (NoAvailableModelError / AllModelsCooldownError
    / ConfigurationError / StrategyError / …) so callers can decide on fallback.
    """
    strategy = _build_strategy(group)
    return await strategy.select_endpoints(
        messages=messages or [],
        db=db,
        cooldown=cooldown,
        group_id=group.id,
        temperature=temperature,
        max_tokens=max_tokens,
        **extra,
    )


# ---------------------------------------------------------------------------
# Graph nodes (ALL ASYNC — run under uvicorn's event loop via ainvoke/astream)
# ---------------------------------------------------------------------------


def _raise_routing_error(
    raw: Any,
    default_exc: BotflowError,
    attempts: list[dict] | None = None,
    used_model_id: int | None = None,
    used_provider_id: int | None = None,
) -> NoReturn:
    """Re-raise a routing failure, preferring its original exception type.

    Reporting *everything* as ``ProviderError`` would erase why routing failed
    (e.g. ``AllModelsCooldownError`` vs ``NoAvailableModelError``). The driver
    records ``type(e).__name__`` in ``call_logs.error_type``, so the distinction
    is operationally load-bearing.

    ``attempts`` / ``used_model_id`` / ``used_provider_id`` are smuggled onto the
    raised exception (SG-0) so the driver can persist the audit trail and
    attribute the final error row even on the failure path.
    """
    exc = raw if isinstance(raw, BotflowError) else default_exc
    if attempts is not None:
        exc.attempts = attempts
    if used_model_id is not None or used_provider_id is not None:
        exc.used_model_id = used_model_id
        exc.used_provider_id = used_provider_id
    raise exc


def _error_message(raw: Any) -> str:
    """Extract a human-readable message from a graph-exit ``error`` value.

    The graph exit writes ``state["error"]`` as either the original exception,
    a ``{"message": ...}`` dict, or a plain string. When the value is not
    already a ``BotflowError`` the caller has to synthesise a ``ProviderError``
    — and it must preserve the underlying message rather than replacing it with
    a generic placeholder, otherwise ``call_logs.error_message`` (and the 502
    detail) lose all diagnostic value.
    """
    if isinstance(raw, Exception):
        msg = str(raw)
    elif isinstance(raw, dict):
        msg = str(raw.get("message") or "")
    else:
        msg = str(raw) if raw else ""
    return msg or "no result"


async def select_endpoints(state: RouteState) -> dict:
    """Select candidate endpoints for the active group (SG-1 F3).

    If ``endpoints`` are already present (streaming fast-path pre-selected by
    ``run()``), skip re-selection. Otherwise run ``_select_endpoints`` and store
    the result, or — on failure — the original exception with ``recoverable`` set.
    Recoverable failures (AllModelsCooldownError / NoAvailableModelError /
    retryable ProviderError) keep the exception object so its type survives;
    fatal failures (ConfigurationError / StrategyError / TypeError / …) are also
    stored but with ``recoverable=False`` so the driver refuses to fall back.
    """
    ctx: GraphContext = get_config()["configurable"]["ctx"]
    group = state["group"]

    if state.get("endpoints"):
        return {}  # already selected (streaming fast-path)

    try:
        result = await _select_endpoints(
            group, state["messages"], state.get("temperature"),
            state.get("max_tokens"), state.get("extra_kwargs", {}),
            ctx.db, ctx.cooldown,
        )
    except (AllModelsCooldownError, NoAvailableModelError, ProviderError) as exc:
        return {"error": exc, "recoverable": True}
    except Exception as exc:
        return {"error": exc, "recoverable": False}

    return {
        "endpoints": result.endpoints,
        "current_ep_idx": 0,
        "truncated_messages": result.messages,
        "attempt": 0,
    }


async def try_call(state: RouteState) -> dict:
    """Attempt a non-streaming LLM call on each endpoint in order (SG-1 F3).

    On success returns ``result`` + ``recoverable=False``. When all endpoints
    fail, clears the queue and writes ``recoverable=True`` (group failure → the
    driver may fall back) together with the original typed error when one was
    already captured by ``select_endpoints``.

    SG-0: each failed endpoint attempt is appended to ``state["attempts"]`` so
    the driver can persist it (G1) and attribute the final error row (G2).
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

    attempts = list(state.get("attempts", []))
    last_model_id = state.get("used_model_id")
    last_provider_id = state.get("used_provider_id")

    while idx < len(endpoints):
        ep = endpoints[idx]
        t0 = time.monotonic()
        resp, err = await call_llm(
            ep, truncated, group_id, ctx.cooldown,
            temperature, max_tokens, **extra,
        )
        duration_ms = int((time.monotonic() - t0) * 1000)
        if resp is not None:
            routing = resp.get("_routing", {})
            return {
                "result": resp,
                "error": None,
                "recoverable": False,
                "used_model_id": routing.get("model_id"),
                "used_provider_id": routing.get("provider_id"),
                "endpoints": [],
                "current_ep_idx": 0,
                "attempts": attempts,
            }
        # Failed attempt — record it (G1). call_llm hides endpoint-internal
        # retries, so we log one row per endpoint with its final error.
        last_model_id = ep.model_id
        last_provider_id = ep.detail.provider_id
        attempts.append({
            "group_id": group_id,
            "model_id": ep.model_id,
            "provider_id": ep.detail.provider_id,
            "stage": "call",
            "endpoint_idx": idx,
            "attempt_no": ep.max_retries,
            "error_type": type(err).__name__ if err is not None else "UnknownError",
            "error_message": str(err) if err is not None else "All retries exhausted",
            "duration_ms": duration_ms,
        })
        idx += 1

    # All endpoints failed — clear so the graph routes to finalize_error/END.
    # Preserve a pre-existing typed cause (e.g. NoAvailableModelError from
    # select_endpoints) so its type reaches the driver; otherwise this is a
    # plain "all endpoints failed" group failure (recoverable).
    if state.get("error") is not None:
        return {
            "endpoints": [], "current_ep_idx": 0,
            "attempts": attempts,
            "used_model_id": last_model_id, "used_provider_id": last_provider_id,
            "error": state.get("error"), "recoverable": state.get("recoverable", True),
        }
    return {
        "endpoints": [], "current_ep_idx": 0,
        "error": "All endpoints in group failed",
        "recoverable": True,
        "attempts": attempts,
        "used_model_id": last_model_id, "used_provider_id": last_provider_id,
    }


async def _chain_first(first_chunk: dict, rest) -> Any:
    """Yield the first chunk (already pulled for disconnect detection), then the rest."""
    yield first_chunk
    async for chunk in rest:
        yield chunk


async def try_stream(state: RouteState) -> dict:
    """Attempt a streaming LLM call, pushing chunks via ``get_stream_writer()``.

    SG-1 F4: the streaming token iteration that used to live in
    ``core._stream_common`` now runs INSIDE the graph node. Behaviour:

    * Endpoint-level retry/backoff for the *first* chunk (retryable errors and
      timeouts are retried; non-retryable errors move to the next endpoint).
    * Each emitted chunk is pushed through LangGraph's custom stream channel so
      the transport layer (``core._drive``) can serialise it.
    * If the client disconnects (``ctx.request.is_disconnected()``) the provider
      generator is ``aclose()``-ed and we stop without emitting anything.
    * Once a chunk has been emitted, later failures cannot be retried — the
      stream is already committed, so ``recoverable`` is forced to ``False``.

    A ``RuntimeError`` from ``get_stream_writer()`` (non-streaming ainvoke
    context, e.g. T4.7) is caught so the node never blows up — it just does not
    emit chunks.
    """
    ctx: GraphContext = get_config()["configurable"]["ctx"]
    group = state["group"]
    group_id = group.id
    endpoints = state.get("endpoints", [])
    temperature = state.get("temperature")
    max_tokens = state.get("max_tokens")
    extra = state.get("extra_kwargs", {})
    truncated = state.get("truncated_messages", state.get("messages", []))
    attempts = list(state.get("attempts", []))
    last_model_id = state.get("used_model_id")
    last_provider_id = state.get("used_provider_id")
    request = getattr(ctx, "request", None)

    try:
        writer = get_stream_writer()
    except RuntimeError:
        writer = None  # non-streaming ainvoke context — never raise (T4.7)

    for ep in endpoints:
        max_retries = max(getattr(ep, "max_retries", 1), 1)
        first: dict | None = None
        gen = None

        # First-chunk acquisition with endpoint-level retry/backoff.
        for attempt in range(max_retries):
            try:
                gen = ep.provider.chat_stream(
                    messages=truncated,
                    model=ep.detail.model_name,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **extra,
                )
                first = await gen.__anext__()
                break
            except asyncio.TimeoutError:
                last_model_id = ep.model_id
                last_provider_id = ep.detail.provider_id
                attempts.append({
                    "group_id": group_id,
                    "model_id": ep.model_id,
                    "provider_id": ep.detail.provider_id,
                    "stage": "stream",
                    "endpoint_idx": 0,
                    "attempt_no": attempt + 1,
                    "error_type": "TimeoutError",
                    "error_message": f"{ep.detail.model_name} timed out waiting for first chunk",
                    "duration_ms": 0,
                })
                if gen is not None:
                    await gen.aclose()
                    gen = None
                if attempt < max_retries - 1:
                    await exponential_backoff(attempt)
                    continue
                break
            except StopAsyncIteration:
                # Empty stream → cool it down and move to the next endpoint (T4.3).
                last_model_id = ep.model_id
                last_provider_id = ep.detail.provider_id
                attempts.append({
                    "group_id": group_id,
                    "model_id": ep.model_id,
                    "provider_id": ep.detail.provider_id,
                    "stage": "stream",
                    "endpoint_idx": 0,
                    "attempt_no": attempt + 1,
                    "error_type": "EmptyStream",
                    "error_message": f"{ep.detail.model_name} returned an empty stream",
                    "duration_ms": 0,
                })
                ctx.cooldown.record_failure(
                    group_id, ep.model_id, ep.cooldown_threshold, ep.cooldown_seconds,
                )
                if gen is not None:
                    await gen.aclose()
                    gen = None
                break
            except Exception as e:
                last_model_id = ep.model_id
                last_provider_id = ep.detail.provider_id
                retryable = isinstance(e, asyncio.TimeoutError) or is_retryable_error(e)
                attempts.append({
                    "group_id": group_id,
                    "model_id": ep.model_id,
                    "provider_id": ep.detail.provider_id,
                    "stage": "stream",
                    "endpoint_idx": 0,
                    "attempt_no": attempt + 1,
                    "error_type": type(e).__name__,
                    "error_message": str(e),
                    "duration_ms": 0,
                })
                if gen is not None:
                    await gen.aclose()
                    gen = None
                if retryable and attempt < max_retries - 1:
                    await exponential_backoff(attempt)
                    continue
                break

        if first is None:
            # This endpoint failed every attempt — try the next one.
            continue

        # First chunk acquired: the stream has started.
        ctx.cooldown.record_success(group_id, ep.model_id)
        try:
            async for chunk in _chain_first(first, gen):
                if request is not None and await request.is_disconnected():
                    await gen.aclose()
                    return {
                        "stream_started": True,
                        "recoverable": False,  # already committed — cannot fall back
                        "used_model_id": ep.model_id,
                        "used_provider_id": ep.detail.provider_id,
                        "attempts": attempts,
                        "endpoints": [],
                        "current_ep_idx": 0,
                    }
                if writer is not None:
                    writer({"chunk": chunk})
            return {
                "result": {"stream": "ok"},
                "stream_started": True,
                "recoverable": False,
                "used_model_id": ep.model_id,
                "used_provider_id": ep.detail.provider_id,
                "attempts": attempts,
                "endpoints": [],
                "current_ep_idx": 0,
            }
        except Exception:
            # Mid-stream failure after content was already sent — unrecoverable.
            if gen is not None:
                await gen.aclose()
            return {
                "stream_started": True,
                "recoverable": False,
                "used_model_id": ep.model_id,
                "used_provider_id": ep.detail.provider_id,
                "attempts": attempts,
                "endpoints": [],
                "current_ep_idx": 0,
            }
        finally:
            if gen is not None:
                await gen.aclose()

    # All endpoints failed before any chunk was emitted → group-level fallback
    # is possible (recoverable). Preserve a typed cause from select_endpoints.
    return {
        "endpoints": [],
        "current_ep_idx": 0,
        "attempts": attempts,
        "used_model_id": last_model_id,
        "used_provider_id": last_provider_id,
        "error": state.get("error"),
        "recoverable": state.get("recoverable", True),
    }


async def finalize_error(state: RouteState) -> dict:
    """Build the final error response from ``state["error"]`` (preserves message)."""
    raw = state.get("error")
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


def _route_after_select(state: RouteState) -> Literal["call", "stream", "error"]:
    """After select_endpoints: stream → try_stream; failure → finalize_error; else try_call."""
    if state.get("error") is not None:
        return "error"
    is_stream = state.get("mode") == "stream" or state.get("stream")
    return "stream" if is_stream else "call"


def _route_after_call(state: RouteState) -> Literal["success", "error"]:
    """After try_call: result present → success (END); otherwise finalize_error."""
    if state.get("result") is not None:
        return "success"
    return "error"


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_route_graph() -> StateGraph:
    """Build and compile the (slim) LangGraph routing graph.

    The graph models a SINGLE strategy execution::

        START
          │
          ▼
        select_endpoints ──▶ [stream: try_stream] ──▶ END
                          └─▶ [call: try_call]   ──▶ END
                          └─▶ [error: finalize_error] ──▶ END

    Group-level fallback is deliberately absent (moved to the driver).
    """
    g = StateGraph(RouteState)

    g.add_node("select_endpoints", select_endpoints)
    g.add_node("try_call", try_call)
    g.add_node("try_stream", try_stream)
    g.add_node("finalize_error", finalize_error)

    g.set_entry_point("select_endpoints")

    g.add_conditional_edges(
        "select_endpoints",
        _route_after_select,
        {"call": "try_call", "stream": "try_stream", "error": "finalize_error"},
    )

    g.add_conditional_edges(
        "try_call",
        _route_after_call,
        {"success": END, "error": "finalize_error"},
    )

    g.add_edge("try_stream", END)
    g.add_edge("finalize_error", END)

    return g


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class LangGraphEngine:
    """LangGraph-based routing engine.

    Two public entry points:

    * ``route(group, messages, ...)`` — legacy non-streaming entry that still
      performs the group-level fallback loop (required by frozen R-09~R-14).
    * ``run(strategy, group, mode, ...)`` / ``stream_events(...)`` — the slim
      single-group execution used by the unified driver ``core._drive``.

    All nodes are ``async`` and executed via ``ainvoke`` / ``astream`` under
    uvicorn's event loop.
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

    # -- single-group execution ------------------------------------------------

    def _initial_state(
        self,
        group: ModelGroup,
        messages: list[dict] | None,
        mode: str,
        temperature: float | None,
        max_tokens: int | None,
        extra: dict[str, Any],
        preselected=None,
    ) -> RouteState:
        if preselected is not None:
            endpoints = preselected.endpoints
            truncated = preselected.messages
        else:
            endpoints = []
            truncated = []
        return {
            "messages": messages or [],
            "model_name": "",
            "temperature": temperature,
            "max_tokens": max_tokens,
            "extra_kwargs": extra or {},
            "group": group,
            "mode": mode,  # type: ignore[typeddict-item]
            "stream": mode == "stream",
            "fallback_group_id": group.fallback_group_id,
            "fallback_depth": 0,
            "visited_groups": [group.id],
            "endpoints": endpoints,
            "current_ep_idx": 0,
            "attempt": 0,
            "truncated_messages": truncated,
            "attempts": [],
        }

    async def _run_one(
        self,
        strategy: Any,
        group: ModelGroup,
        messages: list[dict] | None,
        mode: str,
        temperature: float | None,
        max_tokens: int | None,
        request: Any | None,
        extra: dict[str, Any],
    ) -> dict:
        """Run the graph once (non-streaming) and return the result dict.

        Raises the stored error with its original type on failure so the caller
        can decide on group-level fallback.
        """
        ctx = GraphContext(db=self.db, cooldown=self.cooldown, request=request)
        initial = self._initial_state(group, messages, "chat", temperature, max_tokens, extra)
        final = await self._graph.ainvoke(initial, config={"configurable": {"ctx": ctx}})

        if final.get("error") is None and final.get("result") is not None:
            result_dict = dict(final["result"])
            result_dict["_attempts"] = final.get("attempts", [])
            return result_dict

        # Failure — re-raise with the original type so the caller can tell
        # a recoverable failure (AllModelsCooldownError / NoAvailableModelError /
        # ProviderError) from a fatal one (ConfigurationError / StrategyError).
        raw_err = final.get("error")
        _raise_routing_error(
            raw_err,
            ProviderError(_error_message(raw_err)),
            attempts=final.get("attempts", []),
            used_model_id=final.get("used_model_id"),
            used_provider_id=final.get("used_provider_id"),
        )

    async def _run_stream(
        self,
        strategy: Any,
        group: ModelGroup,
        messages: list[dict] | None,
        temperature: float | None,
        max_tokens: int | None,
        request: Any | None,
        extra: dict[str, Any],
        preselected=None,
    ):
        """Run the graph (streaming) and yield ``("chunk", c)`` / ``("state", s)``.

        ``("chunk", c)`` events are emitted by ``try_stream`` via the custom
        stream channel; the final ``("state", s)`` carries ``recoverable`` and the
        last attempted endpoint so the driver can decide on group-level fallback.
        """
        ctx = GraphContext(db=self.db, cooldown=self.cooldown, request=request)
        initial = self._initial_state(group, messages, "stream", temperature, max_tokens, extra, preselected)
        final_state: dict | None = None
        async for stream_mode, data in self._graph.astream(
            initial,
            config={"configurable": {"ctx": ctx}},
            stream_mode=["custom", "values"],
        ):
            if stream_mode == "custom":
                # data == {"chunk": chunk}
                yield ("chunk", data["chunk"])
            elif stream_mode == "values":
                final_state = data

        if final_state is not None and final_state.get("error") is not None:
            # Group-level failure before any chunk → raise so the driver falls back.
            raw_err = final_state.get("error")
            _raise_routing_error(
                raw_err,
                ProviderError(_error_message(raw_err)),
                attempts=final_state.get("attempts", []),
                used_model_id=final_state.get("used_model_id"),
                used_provider_id=final_state.get("used_provider_id"),
            )
        yield ("state", final_state)

    async def run(
        self,
        strategy: Any = None,
        group: ModelGroup | None = None,
        *,
        messages: list[dict] | None = None,
        mode: str = "chat",
        temperature: float | None = None,
        max_tokens: int | None = None,
        request: Any | None = None,
        **kwargs: Any,
    ):
        """Single-group execution.

        ``mode="chat"`` → returns the final result dict (raises on failure).
        ``mode="stream"`` → returns an async generator of ``("chunk", c)`` /
        ``("state", s)`` events.

        For ``mode="stream"`` a selection is performed eagerly so a clean error
        (``NoAvailableModelError`` / ``AllModelsCooldownError`` / …) surfaces on
        ``await run(...)`` rather than only when the returned generator is
        iterated (test_route_stream_no_endpoints_raises).  The selected endpoints
        are handed to the graph to avoid a second selection.
        """
        if mode == "stream":
            preselected = await _select_endpoints(
                group, messages, temperature, max_tokens, kwargs or {}, self.db, self.cooldown,
            )
            return self._run_stream(strategy, group, messages, temperature, max_tokens, request, kwargs, preselected)
        return await self._run_one(strategy, group, messages, mode, temperature, max_tokens, request, kwargs)

    async def stream_events(self, strategy: Any = None, group: ModelGroup | None = None, **kwargs: Any):
        """Alias for ``run(mode="stream")`` used by the transport layer.

        Must ``await`` the inner ``run`` — ``run`` is itself a coroutine that
        *returns* the event async generator, so a single ``await`` on
        ``stream_events`` has to yield that generator (same contract as
        ``core._drive_stream``: ``gen = await engine.run(...)``).
        """
        return await self.run(strategy, group, mode="stream", **kwargs)

    # -- legacy entry with driver-owned group fallback (R-09~R-14) --------------

    async def route(
        self,
        group: ModelGroup,
        messages: list[dict],
        stream: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> dict:
        """Non-streaming route with the group-level fallback loop.

        Group fallback (≤3 groups, cycle / depth detection) lives here for the
        frozen R-09~R-14 contract; the unified driver ``core._drive`` has its own
        equivalent loop.
        """
        primary = group
        visited: set[int] = {primary.id}
        while True:
            try:
                return await self._run_one(None, primary, messages, "chat", temperature, max_tokens, None, kwargs)
            except (AllModelsCooldownError, NoAvailableModelError, ProviderError) as e:
                fb = primary.fallback_group_id
                if fb is None:
                    raise  # preserve the original typed error (no backup)
                if fb in visited:
                    raise ProviderError("No fallback group available")
                if len(visited) >= 3:
                    raise ProviderError("Fallback chain too deep")
                primary = await self._load_group(fb)
                visited.add(primary.id)
            except Exception:
                raise
