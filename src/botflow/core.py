"""FastAPI main service for botflow (LLM Proxy).

Endpoints:
  - POST /v1/chat/completions  (OpenAI)
  - POST /v1/completions       (OpenAI, compatibility)
  - GET  /v1/models             (OpenAI / Anthropic)
  - POST /v1/messages           (Anthropic)
  - /admin/*                    (REST management API, admin-key guarded)
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import time
import traceback as tb
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from botflow.admin_api import admin_router
from botflow.auth import ApiKey, ensure_setup_token, resolve_api_key, verify_admin_key, verify_llm_key
from botflow.common.exceptions import (
    AllModelsCooldownError,
    NoAvailableModelError,
    ProviderError,
)
from botflow.pipeline import PipelineEngine
from botflow.common.logger import get_logger, setup_logging
from botflow.config import BotflowSettings, get_config, set_config
from botflow.protocol_adapter import (
    anthropic_to_internal,
    internal_chunk_to_anthropic_sse,
    internal_chunk_to_openai_sse,
    internal_chunk_to_responses_sse,
    internal_to_anthropic,
    internal_to_openai,
    internal_to_responses,
    models_to_anthropic,
    models_to_openai,
    openai_to_internal,
    responses_to_internal,
)
from botflow.router import (
    CooldownManager,
    exponential_backoff,
    is_retryable_error,
)
from botflow.storage.daily_summary import (
    purge_old_call_logs,
    purge_old_detail,
    purge_old_raw_sessions,
    run_daily_summary,
)
import httpx

from botflow.storage.db import Database
from botflow.storage.models import CallAttempt, CallLog, Model, ModelGroup
from botflow.workspace import get_workspace_path, init_workspace

log = get_logger("core")

# Per-request context for call logging (API key id + request id).
_request_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar("request_ctx", default=None)


def _set_request_ctx(api_key_id: int | None, request_id: str | None) -> contextvars.Token:
    return _request_ctx.set({"api_key_id": api_key_id, "request_id": request_id})


def _request_id(request: Request) -> str:
    """Stable per-request id (from header or generated)."""
    return request.headers.get("X-Request-Id") or uuid.uuid4().hex

# Whitelist of extra kwargs that can be safely passed to LLM providers.
# All three streaming/non-streaming handlers share this list.
SAFE_EXTRA_KEYS = frozenset({
    "audio", "frequency_penalty", "function_call", "functions",
    "logit_bias", "logprobs", "max_completion_tokens", "max_tokens",
    "metadata", "modalities", "moderation", "n", "parallel_tool_calls",
    "prediction", "presence_penalty", "prompt_cache_key",
    "prompt_cache_retention", "reasoning_effort", "reasoning_content",
    "response_format",
    "safety_identifier", "seed", "service_tier", "stop", "store",
    "stream_options", "temperature", "tool_choice", "tools",
    "top_logprobs", "top_p", "user", "verbosity", "web_search_options",
    "extra_headers", "extra_query", "extra_body", "timeout",
})


def _filter_safe_extra(extra: dict[str, Any]) -> dict[str, Any]:
    """Filter extra kwargs to only include safe keys for LLM providers."""
    return {k: v for k, v in extra.items() if k in SAFE_EXTRA_KEYS}

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_db: Optional[Database] = None
_cooldown_manager = CooldownManager()
_config: Optional[BotflowSettings] = None
_engine: Optional[PipelineEngine] = None


class CallLogWriter:
    """Batched call log writer with background flush.

    Buffers log entries and flushes them to the database in batches
    to reduce database write overhead under high load.
    """

    def __init__(self, db: Database, flush_interval: float = 5.0, max_buffer: int = 100):
        self._db = db
        self._buffer: list[CallLog] = []
        self._attempts_buffer: list[CallAttempt] = []
        self._flush_interval = flush_interval
        self._max_buffer = max_buffer
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the background flush task."""
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        """Stop the background flush task and flush remaining entries."""
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self._flush()  # Final flush

    async def log(self, log_entry: CallLog) -> None:
        """Add a log entry to the buffer."""
        async with self._lock:
            self._buffer.append(log_entry)
            reached_threshold = len(self._buffer) >= self._max_buffer
        # Flush *outside* the lock: _flush_unlocked() acquires it itself, and
        # asyncio.Lock is not reentrant — flushing while holding it deadlocks.
        if reached_threshold:
            await self._flush_unlocked()

    async def log_attempt(self, attempt: CallAttempt) -> None:
        """Add a failed-attempt row to the (separate) buffer (SG-0)."""
        async with self._lock:
            self._attempts_buffer.append(attempt)
            reached_threshold = len(self._attempts_buffer) >= self._max_buffer
        if reached_threshold:
            await self._flush_unlocked()

    async def _flush_loop(self) -> None:
        """Background task to periodically flush the buffer."""
        while True:
            await asyncio.sleep(self._flush_interval)
            await self._flush()

    async def _flush_unlocked(self) -> None:
        """Flush buffered log entries to the database (caller must NOT hold _lock)."""
        async with self._lock:
            if not self._buffer and not self._attempts_buffer:
                return
            log_batch = self._buffer.copy()
            self._buffer.clear()
            attempt_batch = self._attempts_buffer.copy()
            self._attempts_buffer.clear()

        try:
            for entry in log_batch:
                await self._db.create_call_log(entry)
        except Exception as e:
            log.error("Failed to flush {} call log entries: {}", len(log_batch), e)

        try:
            if attempt_batch:
                await self._db.create_call_attempts(attempt_batch)
        except Exception as e:
            log.error("Failed to flush {} call attempt entries: {}", len(attempt_batch), e)

    async def _flush(self) -> None:
        """Flush buffered log entries to the database (acquires lock)."""
        await self._flush_unlocked()


_log_writer: Optional[CallLogWriter] = None


def _get_db() -> Database:
    assert _db is not None, "Database not initialized"
    return _db


def _get_engine() -> PipelineEngine:
    global _engine
    if _engine is None:
        _engine = PipelineEngine(db_factory=_get_db, cooldown=_cooldown_manager)
    return _engine


# ---------------------------------------------------------------------------
# Model sync from upstream providers
# ---------------------------------------------------------------------------


async def sync_models_from_provider(provider_id: int, db: Optional[Database] = None) -> dict[str, Any]:
    """Fetch model list from a provider's /v1/models endpoint and upsert into DB.

    Only adds models that don't already exist locally.
    Returns a summary dict: {"added": N, "skipped": N, "errors": [...]}.
    """
    db = db or _get_db()
    provider = await db.get_provider(provider_id)
    if not provider:
        return {"added": 0, "skipped": 0, "errors": [f"Provider {provider_id} not found"]}

    base_url = provider.base_url.rstrip("/")
    models_url = f"{base_url}/models"
    headers = {"Authorization": f"Bearer {provider.api_key}"} if provider.api_key else {}

    summary: dict[str, Any] = {"added": 0, "skipped": 0, "errors": []}
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(models_url, headers=headers)
            resp.raise_for_status()
        except Exception as e:
            summary["errors"].append(str(e))
            log.error("Failed to fetch models from {}: {}", models_url, e)
            return summary

        data = resp.json()
        # Collect existing model names for this provider
        existing_models = await db.list_models(provider_id=provider_id)
        existing_names = {m.name for m in existing_models}

        for item in data.get("data", []):
            name = item.get("id", "")
            if not name:
                continue
            if name in existing_names:
                summary["skipped"] += 1
                continue
            model = Model(
                name=name,
                provider_id=provider_id,
                display_name=name,
            )
            await db.create_model(model)
            summary["added"] += 1
            log.info("Synced new model: {} (provider {})", name, provider_id)

    log.info("Model sync for provider {}: added={} skipped={} errors={}",
             provider_id, summary["added"], summary["skipped"], len(summary["errors"]))
    return summary


async def sync_all_models() -> dict[str, Any]:
    """Sync models from all enabled providers."""
    db = _get_db()
    providers = await db.list_providers(enabled_only=True)
    combined: dict[str, Any] = {"added": 0, "skipped": 0, "errors": []}
    for p in providers:
        result = await sync_models_from_provider(p.id)
        combined["added"] += result.get("added", 0)
        combined["skipped"] += result.get("skipped", 0)
        combined["errors"].extend(result.get("errors", []))
    return combined


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup/shutdown."""
    global _db, _config, _log_writer

    # Auto-initialize if not already configured (e.g., direct uvicorn start)
    if _db is None:
        workspace = get_workspace_path(None)
        init_workspace(workspace)
        config = BotflowSettings()
        config.workspace = str(workspace)
        await create_app(workspace, config)

    # Setup logging
    if _config:
        log_dir = Path(_config.workspace) / "logs"
        setup_logging(log_dir, _config.log_level)

    log.info("botflow service starting...")

    # Initialize call log writer
    _log_writer = CallLogWriter(_get_db(), flush_interval=5.0, max_buffer=100)
    await _log_writer.start()
    log.info("Call log writer started (buffer=100, flush=5s)")

    # Mount REST admin API (management of providers/models/groups/keys/stats).
    app.include_router(admin_router)

    # Mount admin SPA dashboard AFTER the REST router so API routes win.
    from botflow.admin_dashboard import mount_admin_ui
    mount_admin_ui(app)

    db = _get_db()

    # Sync env LLM_KEY to DB config if not already set (single source of truth).
    env_llm_key = os.environ.get("LLM_KEY", "")
    if env_llm_key:
        db_llm_key = await db.get_config("llm_key")
        if not db_llm_key:
            await db.set_config("llm_key", env_llm_key)
            log.info("Synced LLM_KEY from environment to DB config.")

    # Setup token 生命周期维护（生成/轮换/幂等清理，失败非致命，函数内自降级）。
    await ensure_setup_token(db)

    # Auto-configure legacy LLM key into the multi-key table (for log attribution).
    existing = await db.list_api_keys()
    if not existing:
        # Try DB config first (``botflow set llm-key``), then env var fallback.
        db_llm_key = await db.get_config("llm_key")
        if not db_llm_key:
            db_llm_key = os.environ.get("LLM_KEY", "")
        if db_llm_key:
            await db.create_api_key(db_llm_key, label="legacy:llm_key")
            log.info("Legacy LLM key registered as API key for log attribution.")

    if not get_config().admin_key:
        log.warning("No admin key configured (BOTFLOW_ADMIN_KEY). "
                     "Set it to protect the /admin REST API.")

    # Restore cooldown states from database
    try:
        cooldown_states = await db.load_cooldown_states()
        for state in cooldown_states:
            _cooldown_manager.restore_state(
                state["group_id"],
                state["model_id"],
                state["consecutive_failures"],
                state["cooldown_until"],
            )
        if cooldown_states:
            log.info("Restored {} cooldown states from database", len(cooldown_states))
    except Exception as e:
        log.warning("Failed to restore cooldown states: {}", e)

    # Daily maintenance job: runs once per day at config.daily_summary_hour.
    # Aggregates the previous day into a wiki summary, compresses raw sessions,
    # and purges old detailed log fields (rolling windows).
    async def _daily_maintenance():
        cfg = get_config()
        now = datetime.now(timezone.utc)
        first_run_hour = cfg.daily_summary_hour
        # Sleep until the target hour today (or tomorrow if already past).
        seconds_until = (first_run_hour - now.hour) * 3600 - now.minute * 60 - now.second
        if seconds_until <= 0:
            seconds_until += 24 * 3600
        while True:
            await asyncio.sleep(seconds_until)
            seconds_until = 24 * 3600
            try:
                db = _get_db()
                await run_daily_summary(db)
                purged_detail = await purge_old_detail(db)
                purged_raw = await purge_old_raw_sessions(db)
                deleted = await purge_old_call_logs(db)
                if purged_detail or purged_raw or deleted:
                    log.info(
                        "Daily maintenance done: detail={} raw={} records={}",
                        purged_detail, purged_raw, deleted,
                    )
            except Exception as e:
                log.error("Daily maintenance failed: {}", e)

    daily_task = asyncio.create_task(_daily_maintenance())

    # Periodic cooldown state save (every 5 minutes)
    async def _periodic_cooldown_save():
        while True:
            await asyncio.sleep(5 * 60)  # 5 minutes
            try:
                active = _cooldown_manager.get_all_active_cooldowns()
                if active:
                    await _get_db().save_cooldown_state(active)
                    log.debug("Saved {} active cooldown states", len(active))
            except Exception as e:
                log.error("Cooldown state save failed: {}", e)

    cooldown_save_task = asyncio.create_task(_periodic_cooldown_save())

    # Periodic deduplication cache cleanup (every 10 minutes)
    async def _periodic_dedup_cleanup():
        while True:
            await asyncio.sleep(10 * 60)  # 10 minutes
            try:
                db = _get_db()
                # W14: use public API instead of private _ensure_connection
                deleted = await db.cleanup_config_by_prefix("dedup:%", older_than_seconds=600)
                if deleted:
                    log.debug("Cleaned up {} old deduplication entries", deleted)
            except Exception as e:
                log.debug("Dedup cleanup failed: {}", e)

    dedup_cleanup_task = asyncio.create_task(_periodic_dedup_cleanup())

    # Periodic model sync from upstream providers (configurable interval)
    async def _periodic_model_sync():
        interval = get_config().model_sync_interval
        if interval <= 0:
            return
        # Initial sync after 30 seconds (let the server start first)
        await asyncio.sleep(30)
        while True:
            try:
                result = await sync_all_models()
                if result.get("added", 0):
                    log.info("Model sync: added={} skipped={} errors={}",
                             result["added"], result["skipped"], len(result["errors"]))
                elif result["errors"]:
                    log.warning("Model sync errors: {}", result["errors"])
            except Exception as e:
                log.error("Model sync failed: {}", e)
            await asyncio.sleep(interval * 60)

    model_sync_task = asyncio.create_task(_periodic_model_sync())

    # botflow's background tasks run until the app shuts down.
    yield

    # Shutdown: gracefully stop log writer
    if _log_writer:
        await _log_writer.stop()
        log.info("Call log writer stopped.")

    # Shutdown: gracefully cancel cooldown save task
    cooldown_save_task.cancel()
    try:
        await cooldown_save_task
    except asyncio.CancelledError:
        pass

    # Shutdown: gracefully cancel dedup cleanup task
    dedup_cleanup_task.cancel()
    try:
        await dedup_cleanup_task
    except asyncio.CancelledError:
        pass

    # Shutdown: gracefully cancel daily maintenance task
    daily_task.cancel()
    try:
        await daily_task
    except asyncio.CancelledError:
        pass

    # Shutdown: gracefully cancel model sync task
    model_sync_task.cancel()
    try:
        await model_sync_task
    except asyncio.CancelledError:
        pass

    if _db:
        await _db.close()
    log.info("botflow service stopped.")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

app = FastAPI(
    title="botflow",
    description="AI Middleware Platform",
    version="3.1.0",
    lifespan=lifespan,
)

# CORS - 允许所有来源
cors_origins = os.environ.get("BOTFLOW_CORS_ORIGINS", "*").split(",")
# allow_credentials=True is incompatible with allow_origins=["*"] per CORS spec
_use_credentials = cors_origins != ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=_use_credentials,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
)

# ---------------------------------------------------------------------------
# Middleware: Rate Limiting (per API key)
# ---------------------------------------------------------------------------


class RateLimitMiddleware:
    """Rate limiter middleware using fixed-size deque per API key.

    Pre-allocates deque with maxlen=max_requests.
    Oldest timestamp at dq[0] determines if window has expired.
    Includes automatic cleanup of inactive keys to prevent memory leaks.
    """

    def __init__(self, app, max_requests: int = 300, window_seconds: int = 60, max_keys: int = 10000):
        self.app = app
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._max_keys = max_keys
        self._requests: dict[str, deque[float]] = {}
        self._last_access: dict[str, float] = {}  # Track last access time for cleanup
        self._request_count: int = 0  # Counter for periodic cleanup

    def _get_rate_limit_key(self, request) -> str:
        """Extract rate limit key from request (LLM key or MCP key)."""
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]

        api_key = request.query_params.get("api_key")
        if api_key:
            return api_key

        return request.client.host if request.client else "anonymous"

    def _cleanup_old_keys(self, now: float) -> None:
        """Remove keys that haven't been accessed in the last 5 minutes."""
        cutoff = now - 300  # 5 minutes
        stale_keys = [k for k, ts in self._last_access.items() if ts < cutoff]
        for k in stale_keys:
            self._requests.pop(k, None)
            self._last_access.pop(k, None)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        request = Request(scope, receive)

        if request.url.path == "/health":
            return await self.app(scope, receive, send)

        key = self._get_rate_limit_key(request)
        now = time.time()

        # Periodic cleanup every 1000 requests
        self._request_count += 1
        if self._request_count % 1000 == 0 and len(self._requests) > self._max_keys:
            self._cleanup_old_keys(now)

        dq = self._requests.setdefault(key, deque([now-1], maxlen=self.max_requests))
        self._last_access[key] = now

        # If deque full and oldest timestamp still in window -> rate limited
        if now - self.window_seconds <= dq[0] and len(dq) == self.max_requests:
            retry_after = int(dq[0] + self.window_seconds - now) + 1
            response = JSONResponse(
                status_code=429,
                content={"error": "Too many requests", "retry_after": retry_after},
            )
            return await response(scope, receive, send)

        dq.append(now)
        return await self.app(scope, receive, send)


# 添加速率限制中间件（每个 KEY 300 次/分钟）
app.add_middleware(RateLimitMiddleware, max_requests=300, window_seconds=60, max_keys=10000)


# ---------------------------------------------------------------------------
# Middleware: LLM-Key auth
# ---------------------------------------------------------------------------


class AuthMiddleware:
    """Pure ASGI middleware for LLM client-key authentication.

    Resolves the Bearer token to an api_keys row (or legacy single key), stores
    the id on request.state, and rejects invalid tokens with 401.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        request = Request(scope, receive)

        # Health check is public.
        if request.url.path == "/health":
            return await self.app(scope, receive, send)

        # Admin REST API has its own dependency-based guard; skip middleware here
        # (admin_key is checked per-route).
        if request.url.path.startswith("/admin/"):
            return await self.app(scope, receive, send)

        # Support both "Authorization: Bearer <key>" and "x-api-key: <key>" headers.
        # x-api-key takes precedence when present (Anthropic/Google native clients use it).
        x_api_key = request.headers.get("x-api-key", "")
        auth_header = request.headers.get("Authorization", "")
        token = None
        if x_api_key:
            token = x_api_key.strip()
        elif auth_header.startswith("Bearer "):
            token = auth_header.removeprefix("Bearer ").strip()
        elif auth_header:
            token = auth_header.strip()

        if not token:
            response = JSONResponse(
                status_code=401,
                content={"error": "Missing API key. Provide Authorization: Bearer <key> or x-api-key: <key>."},
            )
            return await response(scope, receive, send)

        db = _get_db()
        api_key = await resolve_api_key(db, token)
        if api_key is None:
            response = JSONResponse(status_code=401, content={"error": "Invalid or disabled API key."})
            return await response(scope, receive, send)

        request.state.api_key_id = api_key.id
        return await self.app(scope, receive, send)


app.add_middleware(AuthMiddleware)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_group_id(request_body: dict) -> int:
    """Determine the group ID from the model name in the request.

    Resolution order (backward-compatible):
    1. Exact group name match.
    2. Look up groups containing a model with the given name.
    3. Fallback to the first enabled group (legacy behaviour) with a
       deprecation warning — never returns 404.
    """
    model_name = request_body.get("model", "")
    db = _get_db()

    # 1) Exact group name match
    groups = await db.list_groups(enabled_only=True)
    for g in groups:
        if g.name == model_name:
            return g.id

    # 2) Try to find a group that contains this model
    matched = await db.find_groups_by_model_name(model_name, enabled_only=True)
    if matched:
        log.debug("Model '{}' resolved to group '{}' via find_groups_by_model_name", model_name, matched[0].name)
        return matched[0].id

    # 3) Fallback to first enabled group (legacy behaviour)
    if groups:
        log.warning(
            "DEPRECATION: Model '{}' does not match any group name or member model. "
            "Falling back to group '{}'. Please use a group name directly.",
            model_name, groups[0].name,
        )
        return groups[0].id

    raise HTTPException(status_code=404, detail="No enabled groups configured. Create a group via the admin API.")


async def _log_call(
    group_id: int | None,
    model_id: int | None,
    provider_id: int | None,
    request_body: str | None,
    response_body: str | None,
    status: str,
    duration_ms: int | None,
    usage: dict[str, Any] | None,
    error_message: str | None = None,
    *,
    api_key_id: int | None = None,
    error_type: str | None = None,
    traceback_text: str | None = None,
    request_id: str | None = None,
) -> None:
    """Write a call log entry asynchronously (buffered).

    失败调用会保留完整 request_body 与 traceback，便于排查。
    成功调用仅保留截断后的 response_body（节省空间）。
    """
    global _log_writer
    ctx = _request_ctx.get() or {}
    if api_key_id is None:
        api_key_id = ctx.get("api_key_id")
    if request_id is None:
        request_id = ctx.get("request_id")
    log_entry = CallLog(
        api_key_id=api_key_id,
        group_id=group_id,
        model_id=model_id,
        provider_id=provider_id,
        request_body=request_body,
        response_body=response_body,
        status=status,
        error_type=error_type,
        error_message=error_message,
        traceback=traceback_text,
        request_id=request_id,
        duration_ms=duration_ms,
        prompt_tokens=usage.get("prompt_tokens") if usage else None,
        completion_tokens=usage.get("completion_tokens") if usage else None,
        cache_tokens=usage.get("cache_tokens") if usage else None,
        total_tokens=usage.get("total_tokens") if usage else None,
        cost=None,  # Cost tracking not yet implemented (pricing tables needed)
    )
    if _log_writer:
        await _log_writer.log(log_entry)
    else:
        # Fallback: direct write if writer not initialized
        await _get_db().create_call_log(log_entry)


async def _log_attempts(attempts: list[dict]) -> None:
    """Buffer failed-attempt rows for ``call_attempts`` (SG-0).

    Mirrors ``_log_call``: pulls ``request_id`` from the per-request context and
    feeds the shared ``CallLogWriter`` batch buffer. A writer flush failure only
    logs (``CallLogWriter._flush_unlocked`` swallows it) — it must never break
    the main request path (this is observation code, not business code). An
    empty list is a no-op.
    """
    global _log_writer
    if not attempts:
        return
    try:
        ctx = _request_ctx.get() or {}
        request_id = ctx.get("request_id")
        entries = [
            CallAttempt(
                request_id=request_id,
                group_id=a.get("group_id"),
                model_id=a.get("model_id"),
                provider_id=a.get("provider_id"),
                stage=a.get("stage", ""),
                endpoint_idx=a.get("endpoint_idx"),
                attempt_no=a.get("attempt_no"),
                error_type=a.get("error_type"),
                error_message=a.get("error_message"),
                duration_ms=a.get("duration_ms"),
            )
            for a in attempts
        ]
        if _log_writer:
            for entry in entries:
                await _log_writer.log_attempt(entry)
        else:
            # Fallback: direct write if writer not initialized (mirrors _log_call).
            await _get_db().create_call_attempts(entries)
    except Exception as e:
        # SG-0 硬约束 3：这是观测代码，不是业务代码 —— 留痕失败只记日志，
        # 绝不冒泡到驱动（否则驱动得再包一层 try，变成两处防御）。
        log.error("Failed to record {} call attempts: {}", len(attempts), e)


def _extract_model_route_info(response: dict, internal_params: dict) -> tuple[int | None, int | None, int | None]:
    """Extract model/provider IDs from response metadata if available."""
    model_id = None
    provider_id = None
    group_id = internal_params.get("_group_id")
    return group_id, model_id, provider_id


def _generate_request_id(body: dict, api_key_id: int | None = None) -> str:
    """Generate a deterministic request ID for deduplication.

    The api_key_id is included in the hash so that identical payloads from
    different tenants never share a dedup cache entry (cross-tenant leak).
    """
    import hashlib
    # Create a hash based on request content + tenant
    content = json.dumps({"body": body, "api_key_id": api_key_id}, sort_keys=True, default=str)
    return hashlib.sha256(content.encode()).hexdigest()[:16]


async def _check_request_deduplication(request_id: str, ttl_seconds: int = 300) -> Optional[dict]:
    """Check if request has been processed recently. Returns cached result if found."""
    import json
    try:
        cached = await _get_db().get_config(f"dedup:{request_id}")
        if cached:
            data = json.loads(cached)
            # Check if still valid (within TTL)
            if time.time() - data.get("timestamp", 0) < ttl_seconds:
                return data.get("result")
    except Exception:
        pass
    return None


async def _cache_request_result(request_id: str, result: dict, ttl_seconds: int = 300) -> None:
    """Cache request result for deduplication."""
    import json
    try:
        data = {
            "result": result,
            "timestamp": time.time(),
        }
        await _get_db().set_config(f"dedup:{request_id}", json.dumps(data))
    except Exception as e:
        log.debug("Failed to cache request result for deduplication: {}", e)


# ---------------------------------------------------------------------------
# Endpoint: OpenAI /v1/chat/completions
# ---------------------------------------------------------------------------


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    _set_request_ctx(getattr(request.state, "api_key_id", None), _request_id(request))
    body = await request.json()
    internal = openai_to_internal(body)
    stream = internal["stream"]

    # Request deduplication for non-streaming requests
    _api_key_id = (_request_ctx.get() or {}).get("api_key_id")
    request_id = body.get("request_id") or _generate_request_id(body, _api_key_id)
    if not stream:
        cached_result = await _check_request_deduplication(request_id)
        if cached_result:
            log.debug("Returning cached result for request {}", request_id)
            return JSONResponse(content=json.loads(cached_result) if isinstance(cached_result, str) else cached_result)

    if stream:
        return StreamingResponse(
            _stream_openai(internal, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    result = await _handle_chat_non_stream(internal, request, internal_to_openai)

    # Cache successful result for deduplication
    if result.status_code == 200:
        await _cache_request_result(request_id, result.body.decode())

    return result


@app.post("/v1/completions")
async def completions(request: Request):
    _set_request_ctx(getattr(request.state, "api_key_id", None), _request_id(request))
    body = await request.json()

    # Legacy completions API uses "prompt" instead of "messages"
    if not body.get("messages") and body.get("prompt"):
        prompt = body["prompt"]
        if isinstance(prompt, list):
            prompt = "\n".join(prompt)
        body["messages"] = [{"role": "user", "content": prompt}]
        # Remove prompt field to avoid passing it to provider SDK
        body.pop("prompt", None)

    internal = openai_to_internal(body)
    stream = internal["stream"]

    if stream:
        return StreamingResponse(
            _stream_openai(internal, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    return await _handle_chat_non_stream(internal, request, internal_to_openai)


# ---------------------------------------------------------------------------
# Endpoint: Anthropic /v1/messages
# ---------------------------------------------------------------------------


@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    _set_request_ctx(getattr(request.state, "api_key_id", None), _request_id(request))
    body = await request.json()
    internal = anthropic_to_internal(body)
    stream = internal["stream"]

    if stream:
        return StreamingResponse(
            _stream_anthropic(internal, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    return await _handle_chat_non_stream(internal, request, internal_to_anthropic)


# ---------------------------------------------------------------------------
# Endpoint: OpenAI Responses API  /v1/responses
# ---------------------------------------------------------------------------


@app.post("/v1/responses")
async def responses_create(request: Request):
    _set_request_ctx(getattr(request.state, "api_key_id", None), _request_id(request))
    body = await request.json()
    internal = responses_to_internal(body)
    stream = internal["stream"]

    if stream:
        return StreamingResponse(
            _stream_responses(internal, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    return await _handle_chat_non_stream(internal, request, internal_to_responses)


# ---------------------------------------------------------------------------
# Endpoint: Models list
# ---------------------------------------------------------------------------


@app.get("/v1/models")
async def list_models(request: Request):
    """List available models (groups).

    Only user-facing groups are returned. Raw backend provider models synced
    from upstream ``/v1/models`` endpoints are filtered out, since callers must
    address a group name (e.g. ``fast``, ``smart``) rather than an individual
    backend model. Each entry also lists the backend models it routes to.
    """
    accept = request.headers.get("accept", "")
    db = _get_db()
    groups = await db.list_groups(enabled_only=True)

    model_list = []
    for g in groups:
        member_models = await db.get_group_models(g.id, enabled_only=True)
        model_names = sorted({gm.model_name for gm in member_models})
        model_list.append({
            "id": g.name,
            "name": g.name,
            "display_name": g.description or g.name,
            "provider_type": "botflow-group",
            "created_at": "",
            "models": model_names,
        })

    # Return Anthropic format if client is Anthropic
    if "anthropic" in accept.lower():
        return models_to_anthropic(model_list)

    return models_to_openai(model_list)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "botflow"}


# ---------------------------------------------------------------------------
# Non-streaming handler
# ---------------------------------------------------------------------------


def _request_summary(internal: dict, full: bool = False) -> str | None:
    """Serialize the request for audit logging.

    Normal calls keep a truncated summary (space saving); failed calls pass
    full=True to retain the entire payload for debugging.
    """
    try:
        text = json.dumps(internal, ensure_ascii=False)
    except Exception:
        return None
    if full or len(text) <= 2000:
        return text
    return text[:2000] + "…[truncated]"


def _limit_traceback(text: str, limit: int = 4000) -> str | None:
    """Keep at most `limit` chars of a traceback for audit logging."""
    if not text:
        return None
    return text if len(text) <= limit else text[:limit] + "\n…[truncated]"


async def _get_extra_route_params(internal: dict, stream: bool = False) -> tuple[int, PipelineEngine, "ModelGroup", dict]:
    """Shared setup: resolve group, engine, group_obj, and safe extra kwargs.

    返回 (group_id, engine, group_obj, safe_extra)。
    """
    model_name = internal.get("model", "")
    group_id = await _get_group_id({"model": model_name})
    engine = _get_engine()
    db = _get_db()
    group = await db.get_group(group_id)
    if group is None:
        from botflow.common.exceptions import ConfigurationError
        raise ConfigurationError(f"Group {group_id} not found")
    extra = internal.get("extra", {})
    if extra:
        log.debug("Extra kwargs for {}: {}", model_name, {k: type(v).__name__ for k, v in extra.items()})
    return group_id, engine, group, _filter_safe_extra(extra)


async def _load_group(group_id: int) -> "ModelGroup":
    """Resolve a (backup) group by id via the active engine.

    Exists as a module-level seam so the unified driver ``_drive`` can load
    fallback groups independently of ``engine.route`` (and so tests can
    monkeypatch it). Delegates to ``PipelineEngine._load_group`` (60s cache).
    """
    engine = _get_engine()
    return await engine._load_group(group_id)


# ---------------------------------------------------------------------------
# Unified driver (SG-1): single entry point for chat + stream
# ---------------------------------------------------------------------------
# ``_drive`` is a *synchronous* dispatcher so it can be used both as
# ``await core._drive(internal, "chat")`` (returns the result dict) and
# ``async for line in core._drive(internal, "stream", serialize, ...)``
# (yields SSE lines).  An async generator cannot be awaited, and a plain
# coroutine cannot be async-iterated, so the two modes are split into
# ``_drive_chat`` (coroutine) and ``_drive_stream`` (async generator) and
# selected here.
# ---------------------------------------------------------------------------


def _drive(
    internal: dict,
    mode: str,
    serialize: Callable[[dict], tuple[list[str], dict | None]] | None = None,
    done_signal: str = "data: [DONE]\n\n",
    request: "Request | None" = None,
):
    """Unified routing driver (SG-1 F1).

    Steps shared by both modes:
      ① group resolution (``_get_extra_route_params``)
      ② template + strategy build  (done inside ``engine.run``)
      ③ single-group execution    (``engine.run`` / ``stream_events``)
      ④ group-level fallback loop  (≤3 groups, cycle / depth detection)

    ``mode="chat"``   → returns the result dict; on failure it re-raises the
                        **original typed exception** (the protocol layer maps it
                        to ``HTTPException(502)``).
    ``mode="stream"`` → returns an async generator of SSE lines.
    """
    if mode == "stream":
        return _drive_stream(internal, serialize, done_signal, request)
    return _drive_chat(internal, serialize, done_signal, request)


_MAX_FALLBACK_GROUPS = 3  # total groups tried: primary + up to 2 backups


async def _drive_chat(
    internal: dict,
    serialize,  # unused for chat; kept for signature symmetry
    done_signal: str,  # unused for chat
    request: "Request | None",
) -> dict:
    """Non-streaming driver: run a group, fall back across backups, return dict."""
    group_id, engine, primary, safe_extra = await _get_extra_route_params(internal, stream=False)
    model_name = internal.get("model", "")
    group = primary
    visited: set[int] = {group.id}
    attempts: list[dict] = []
    start = time.monotonic()

    while True:
        try:
            result = await engine.run(
                None, group, mode="chat",
                messages=internal["messages"],
                temperature=internal.get("temperature"),
                max_tokens=internal.get("max_tokens"),
                request=request,
                **safe_extra,
            )
            # Success — override model name and hand back a clean response.
            result["model"] = model_name
            routing = result.pop("_routing", {})
            grp_attempts = result.pop("_attempts", [])
            attempts.extend(grp_attempts)
            duration = int((time.monotonic() - start) * 1000)
            await _log_attempts(attempts)
            await _log_call(
                group_id=group_id,
                model_id=routing.get("model_id"),
                provider_id=routing.get("provider_id"),
                request_body=_request_summary(internal),
                response_body=json.dumps(result)[:500],
                status="success",
                duration_ms=duration,
                usage=result.get("usage", {}),
            )
            return result
        except (AllModelsCooldownError, NoAvailableModelError, ProviderError) as e:
            # Recoverable group failure — smash the smuggled attempt trail in.
            attempts.extend(getattr(e, "attempts", []) or [])
            fb = group.fallback_group_id
            if fb is None or fb in visited or len(visited) >= _MAX_FALLBACK_GROUPS:
                # Exhausted — preserve the original typed error for call_logs.
                duration = int((time.monotonic() - start) * 1000)
                await _log_attempts(attempts)
                await _log_call(
                    group_id=group_id,
                    model_id=getattr(e, "used_model_id", None),
                    provider_id=getattr(e, "used_provider_id", None),
                    request_body=_request_summary(internal, full=True),
                    response_body=None,
                    status="error",
                    duration_ms=duration,
                    usage=None,
                    error_message=str(e),
                    error_type=type(e).__name__,
                    traceback_text=_limit_traceback(tb.format_exc()),
                )
                # Re-raise the *original* typed error (not ProviderError, not
                # HTTPException) so ``call_logs.error_type`` stays faithful.
                # The protocol layer maps it to HTTP 502.
                raise
            group = await _load_group(fb)
            visited.add(group.id)
        except Exception as e:
            # Non-recoverable (ConfigurationError / StrategyError / TypeError / …).
            # Blacklist failures MUST still leave an attempt row (T6.5).
            attempts.append({
                "group_id": group.id, "model_id": None, "provider_id": None,
                "stage": "route", "endpoint_idx": -1, "attempt_no": 1,
                "error_type": type(e).__name__, "error_message": str(e), "duration_ms": 0,
            })
            duration = int((time.monotonic() - start) * 1000)
            await _log_attempts(attempts)
            await _log_call(
                group_id=group_id, model_id=None, provider_id=None,
                request_body=_request_summary(internal, full=True), response_body=None,
                status="error", duration_ms=duration, usage=None,
                error_message=str(e), error_type=type(e).__name__,
                traceback_text=_limit_traceback(tb.format_exc()),
            )
            # Blacklist failure — no fallback, propagate the original type.
            raise


async def _drive_stream(
    internal: dict,
    serialize: Callable[[dict], tuple[list[str], dict | None]] | None,
    done_signal: str,
    request: "Request | None",
) -> "AsyncGenerator[str, None]":
    """Streaming driver: run a group, push chunks, fall back across backups."""
    group_id, engine, primary, safe_extra = await _get_extra_route_params(internal, stream=True)
    model_name = internal.get("model", "")
    group = primary
    visited: set[int] = {group.id}
    attempts: list[dict] = []
    start = time.monotonic()
    _serialize = serialize or (lambda c: ([json.dumps(c, ensure_ascii=False)], None))

    final_state: dict | None = None
    while True:
        try:
            final_state = None
            gen = await engine.run(
                None, group, mode="stream",
                messages=internal["messages"],
                temperature=internal.get("temperature"),
                max_tokens=internal.get("max_tokens"),
                request=request,
                **safe_extra,
            )
            async for kind, payload in gen:
                if request is not None and await request.is_disconnected():
                    # Client is gone — stop emitting immediately; nothing is
                    # serialised for the already-pulled chunk (§3.3 / T4.6).
                    await gen.aclose()
                    break
                if kind == "chunk":
                    chunk = payload
                    chunk["model"] = model_name
                    lines, _usage = _serialize(chunk)
                    for line in lines:
                        yield line
                elif kind == "state":
                    final_state = payload

            grp_attempts = (final_state or {}).get("attempts", [])
            attempts.extend(grp_attempts)
            if final_state is not None and final_state.get("recoverable"):
                # Group failed before any chunk — fall back to a backup group.
                raise ProviderError(f"Group {group.id} stream exhausted, fallback")
            # Success (or already-committed mid-stream failure).
            duration = int((time.monotonic() - start) * 1000)
            await _log_attempts(attempts)
            await _log_call(
                group_id=group_id,
                model_id=(final_state or {}).get("used_model_id"),
                provider_id=(final_state or {}).get("used_provider_id"),
                request_body=_request_summary(internal),
                response_body=None, status="success",
                duration_ms=duration, usage=None,
            )
            yield done_signal
            return
        except (AllModelsCooldownError, NoAvailableModelError, ProviderError) as e:
            attempts.extend(getattr(e, "attempts", []) or [])
            fb = group.fallback_group_id
            if fb is None or fb in visited or len(visited) >= _MAX_FALLBACK_GROUPS:
                # Exhausted — persist the attempt trail + an error row (§3.6 / SG-0
                # G1: a blacklist/whitelist failure MUST leave a trace, otherwise a
                # stream outage silently disappears from call_logs), emit an error
                # SSE, then close the stream.
                duration = int((time.monotonic() - start) * 1000)
                await _log_attempts(attempts)
                await _log_call(
                    group_id=group_id,
                    model_id=(final_state or {}).get("used_model_id"),
                    provider_id=(final_state or {}).get("used_provider_id"),
                    request_body=_request_summary(internal, full=True),
                    response_body=None, status="error",
                    duration_ms=duration, usage=None,
                    error_message=str(e), error_type=type(e).__name__,
                    traceback_text=_limit_traceback(tb.format_exc()),
                )
                error_data = {"error": {"message": str(e), "type": "server_error"}}
                yield f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n"
                yield done_signal
                return
            group = await _load_group(fb)
            visited.add(group.id)
        except Exception as e:
            # Non-recoverable (ConfigurationError / StrategyError / TypeError / …).
            # Blacklist failures MUST still leave an attempt row (design §3.6).
            attempts.append({
                "group_id": group.id, "model_id": None, "provider_id": None,
                "stage": "route", "endpoint_idx": -1, "attempt_no": 1,
                "error_type": type(e).__name__, "error_message": str(e), "duration_ms": 0,
            })
            duration = int((time.monotonic() - start) * 1000)
            await _log_attempts(attempts)
            await _log_call(
                group_id=group_id, model_id=None, provider_id=None,
                request_body=_request_summary(internal, full=True), response_body=None,
                status="error", duration_ms=duration, usage=None,
                error_message=str(e), error_type=type(e).__name__,
                traceback_text=_limit_traceback(tb.format_exc()),
            )
            error_data = {"error": {"message": str(e), "type": "server_error"}}
            yield f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n"
            yield done_signal
            return


async def _handle_chat_non_stream(
    internal: dict,
    request: Request,
    format_response,
) -> JSONResponse:
    """Handle a non-streaming chat request through the unified driver ``_drive``.

    SG-1: the entire route + group-fallback + attempt-logging + error handling
    now lives in ``_drive`` (shared with the streaming path); this handler only
    formats the result (or lets the 502 propagate).
    """
    try:
        result = await _drive(internal, "chat")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    return JSONResponse(content=format_response(result))


# ---------------------------------------------------------------------------
# Streaming handlers
# ---------------------------------------------------------------------------

# Type alias: serialize_chunk(chunk) -> (sse_lines: list[str], usage: dict|None)
# sse_lines is a list of formatted SSE lines ready to yield.
SerializeFn = Callable[[dict], tuple[list[str], dict | None]]


def _openai_serialize(chunk: dict) -> tuple[list[str], dict | None]:
    """Serialize a raw provider chunk to OpenAI SSE format."""
    sse_data = internal_chunk_to_openai_sse(chunk)
    lines = [f"data: {json.dumps(sse_data, ensure_ascii=False)}\n\n"]
    return lines, sse_data.get("usage")


def _anthropic_serialize(chunk: dict) -> tuple[list[str], dict | None]:
    """Serialize a raw provider chunk to Anthropic SSE format."""
    try:
        events = internal_chunk_to_anthropic_sse(chunk)
    except Exception as e:
        log.error("internal_chunk_to_anthropic_sse failed: {} chunk={}", e, chunk)
        raise
    lines = []
    for event in events:
        lines.append(f"event: {event['type']}\n")
        lines.append(f"data: {json.dumps(event, ensure_ascii=False)}\n\n")
    return lines, chunk.get("usage")


async def _stream_common(
    internal: dict,
    serialize: SerializeFn,
    done_signal: str = "data: [DONE]\n\n",
    request: Request | None = None,
) -> AsyncGenerator[str, None]:
    """SG-1 (F7): thin transport wrapper.

    All routing / endpoint retry / group fallback now lives in the unified
    driver ``_drive`` (single entry point for both chat and stream). This
    wrapper only forwards to ``_drive`` and yields whatever SSE lines it
    produces, so it no longer owns a retry loop nor a fallback-attempted flag
    (T7.2 / T7.3).
    """
    async for line in _drive(internal, "stream", serialize, done_signal, request):
        yield line


async def _stream_openai(
    internal: dict,
    request: Request,
) -> AsyncGenerator[str, None]:
    """Stream response in OpenAI SSE format."""
    async for line in _stream_common(internal, _openai_serialize, request=request):
        yield line


async def _stream_anthropic(
    internal: dict,
    request: Request,
) -> AsyncGenerator[str, None]:
    """Stream response in Anthropic SSE format."""
    async for line in _stream_common(internal, _anthropic_serialize, request=request):
        yield line


def _responses_serialize_raw(
    chunk: dict,
    response_id: str,
    created_at: int,
    *,
    is_first: bool = False,
    is_last: bool = False,
) -> list[str]:
    """Serialize a raw provider chunk to Responses API SSE lines.

    ``is_first`` / ``is_last`` are tracked by the caller because the unified
    chunk format always carries ``role=assistant`` (from ``_chunk_to_unified``),
    making ``delta.get("role")`` unreliable for first-chunk detection.
    """
    events = internal_chunk_to_responses_sse(
        chunk, is_first=is_first, is_last=is_last,
        response_id=response_id, created_at=created_at,
    )

    lines: list[str] = []
    for evt in events:
        lines.append(f"event: {evt['type']}\n")
        lines.append(f"data: {json.dumps(evt, ensure_ascii=False)}\n\n")
    return lines


async def _stream_responses(
    internal: dict,
    request: Request,
) -> AsyncGenerator[str, None]:
    """Stream response in OpenAI Responses API SSE format."""
    import secrets as _secrets

    response_id = "resp_" + _secrets.token_hex(8)
    created_at = int(time.time())
    _first_chunk_seen = False

    def _responses_serialize_wrap(chunk: dict) -> tuple[list[str], dict | None]:
        """Adapter: convert raw chunk to Responses SSE lines."""
        nonlocal _first_chunk_seen
        # Responses API doesn't send [DONE] — just stop.
        choices = chunk.get("choices") or []
        choice = choices[0] if choices and choices[0] is not None else {}
        finish_reason = choice.get("finish_reason")

        is_first = not _first_chunk_seen
        is_last = bool(finish_reason)
        _first_chunk_seen = True

        lines: list[str] = []
        for line in _responses_serialize_raw(
            chunk, response_id, created_at,
            is_first=is_first, is_last=is_last,
        ):
            lines.append(line)
        return lines, chunk.get("usage")

    async for line in _stream_common(
        internal, _responses_serialize_wrap, done_signal="", request=request,
    ):
        if line:
            yield line


# ---------------------------------------------------------------------------
# Service starter
# ---------------------------------------------------------------------------


async def create_app(
    workspace: Path,
    config: BotflowSettings,
) -> FastAPI:
    """Create and configure the botflow FastAPI application.

    Args:
        workspace: Workspace directory path.
        config: Botflow configuration settings.

    Returns:
        Configured FastAPI instance.
    """
    global _db, _config

    _config = config
    set_config(config)

    # 同步 workspace 到 config：DB/日志用 workspace 参数，远程 MCP 配置
    # (mcp.json) 与日志目录依赖 _config.workspace，必须保持与 CLI 参数一致
    config.workspace = str(workspace)

    # Initialize database
    db_path = workspace / "data" / "botflow.db"
    _db = Database(db_path)
    await _db.initialize()

    log.info("Database initialized at: {}", db_path)

    return app


async def start_service(
    workspace: Path,
    host: str,
    port: int,
    config: BotflowSettings,
) -> None:
    """Start the botflow service (HTTP + MCP).

    This is the main entry point called from CLI.
    """
    import uvicorn

    await create_app(workspace, config)

    log.info("Starting botflow HTTP server on {}:{}", host, port)

    # Run uvicorn (blocking, foreground)
    config_obj = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        loop="asyncio",
        forwarded_allow_ips="*",
    )
    server = uvicorn.Server(config_obj)
    await server.serve()
