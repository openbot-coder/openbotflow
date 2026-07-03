"""FastAPI main service for botflow.

Starts the HTTP API server and MCP service.
Endpoints:
  - POST /v1/chat/completions  (OpenAI)
  - POST /v1/completions       (OpenAI, compatibility)
  - POST /v1/embeddings         (OpenAI, compatibility)
  - GET  /v1/models             (OpenAI / Anthropic)
  - POST /v1/messages           (Anthropic)
  - GET  /mcp/sse               (MCP SSE transport)
  - POST /mcp/messages          (MCP messages endpoint)
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import traceback
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from botflow.auth import verify_llm_key, verify_mcp_key
from botflow.common.logger import get_logger, setup_logging
from botflow.config import BotflowSettings
from botflow.protocol_adapter import (
    anthropic_to_internal,
    internal_chunk_to_anthropic_sse,
    internal_chunk_to_openai_sse,
    internal_to_anthropic,
    internal_to_openai,
    models_to_anthropic,
    models_to_openai,
    openai_to_internal,
)
from botflow.router import CooldownManager, GroupRouter
from botflow.storage.db import Database
from botflow.storage.models import CallLog
from botflow.storage.cleanup import cleanup_call_logs
from botflow.workspace import get_workspace_path, init_workspace

log = get_logger("core")

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_db: Optional[Database] = None
_cooldown_manager = CooldownManager()
_config: Optional[BotflowSettings] = None


def _get_db() -> Database:
    assert _db is not None, "Database not initialized"
    return _db


async def _get_llm_key() -> str:
    key = await _get_db().get_config("llm_key")
    return key or ""


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup/shutdown."""
    global _db, _config

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

    # Register MCP tools
    from botflow.mcp.manager import register_manager_tools
    from botflow.mcp.stats import register_stats_tools
    register_manager_tools(mcp_server, _get_db())
    register_stats_tools(mcp_server, _get_db())
    log.info(f"MCP tools registered: {list(mcp_server._tool_manager._tools.keys())}")

    # Warn if MCP key is not configured
    mcp_key = await _get_db().get_config("mcp_key") if _db else None
    if not mcp_key:
        log.warning("No MCP key configured - MCP tools have no authentication! "
                     "Use 'botflow set mcp-key <key>' to configure.")
    else:
        log.info("MCP authentication is enabled.")

    # Log MCP endpoints info
    log.info("MCP management service available via SSE at /mcp/sse")
    log.info("MCP tools: provider CRUD, model CRUD, group CRUD, stats queries")

    # Auto-configure keys from environment variables (for Docker deployment)
    db = _get_db()
    env_llm_key = os.environ.get("LLM_KEY", "")
    env_mcp_key = os.environ.get("MCP_KEY", "")
    if env_llm_key and not await db.get_config("llm_key"):
        await db.set_config("llm_key", env_llm_key)
        log.info("LLM key configured from environment variable.")
    if env_mcp_key and not await db.get_config("mcp_key"):
        await db.set_config("mcp_key", env_mcp_key)
        log.info("MCP key configured from environment variable.")

    # Start background cleanup task (every 24 hours)
    async def _periodic_cleanup():
        while True:
            await asyncio.sleep(24 * 60 * 60)
            try:
                deleted = await cleanup_call_logs(_get_db())
                if deleted > 0:
                    log.info("Periodic cleanup: deleted {} old call_log records", deleted)
            except Exception as e:
                log.error("Periodic cleanup failed: {}", e)

    cleanup_task = asyncio.create_task(_periodic_cleanup())

    yield

    # Shutdown: gracefully cancel cleanup task
    cleanup_task.cancel()
    try:
        await cleanup_task
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
    version="0.1.0",
    lifespan=lifespan,
)

# CORS - 允许所有来源
cors_origins = os.environ.get("BOTFLOW_CORS_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
)

# ---------------------------------------------------------------------------
# MCP Server (SSE transport)
# ---------------------------------------------------------------------------

mcp_server = FastMCP(
    "botflow",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "api.vxquant.com",
            "api.vxquant.com:*",
            "127.0.0.1",
            "127.0.0.1:*",
            "localhost",
            "localhost:*",
        ],
    ),
)

# Mount MCP SSE app under /mcp path
app.mount("/mcp", mcp_server.sse_app())


# ---------------------------------------------------------------------------
# Middleware: Rate Limiting (per API key)
# ---------------------------------------------------------------------------


class RateLimitMiddleware:
    """Rate limiter middleware using fixed-size deque per API key.

    Pre-allocates deque with maxlen=max_requests.
    Oldest timestamp at dq[0] determines if window has expired.
    """

    def __init__(self, app, max_requests: int = 300, window_seconds: int = 60):
        self.app = app
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[str, deque[float]] = {}

    def _get_rate_limit_key(self, request) -> str:
        """Extract rate limit key from request (LLM key or MCP key)."""
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]

        api_key = request.query_params.get("api_key")
        if api_key:
            return api_key

        return request.client.host if request.client else "anonymous"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        from starlette.requests import Request
        from starlette.responses import JSONResponse

        request = Request(scope, receive)

        if request.url.path == "/health":
            return await self.app(scope, receive, send)

        key = self._get_rate_limit_key(request)
        now = time.time()

        dq = self._requests.setdefault(key, deque([now-1],maxlen=self.max_requests))

        # If deque full and oldest timestamp still in window -> rate limited
        if  now - self.window_seconds <= dq[0] and len(dq) == self.max_requests:
            retry_after = int(dq[0] + self.window_seconds - now) + 1
            response = JSONResponse(
                status_code=429,
                content={"error": "Too many requests", "retry_after": retry_after},
            )
            return await response(scope, receive, send)

        dq.append(now)
        return await self.app(scope, receive, send)


# 添加速率限制中间件（每个 KEY 300 次/分钟）
app.add_middleware(RateLimitMiddleware, max_requests=300, window_seconds=60)


# ---------------------------------------------------------------------------
# Middleware: LLM-Key auth
# ---------------------------------------------------------------------------


class AuthMiddleware:
    """Pure ASGI middleware for LLM key authentication."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        from starlette.requests import Request
        from starlette.responses import JSONResponse

        request = Request(scope, receive)

        # Skip auth for health check and MCP endpoints
        if request.url.path == "/health" or request.url.path.startswith("/mcp/"):
            return await self.app(scope, receive, send)

        llm_key = await _get_llm_key()
        if llm_key:
            error_response = verify_llm_key(request, llm_key)
            if error_response:
                return await error_response(scope, receive, send)

        return await self.app(scope, receive, send)


app.add_middleware(AuthMiddleware)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_router(group_id: int) -> GroupRouter:
    return GroupRouter(group_id=group_id, db=_get_db(), cooldown_manager=_cooldown_manager)


async def _get_group_id(request_body: dict) -> int:
    """Determine the group ID from the model name in the request.

    The model field specifies which model group to route through.
    Defaults to group 1 if not found.
    """
    model_name = request_body.get("model", "")
    db = _get_db()
    groups = await db.list_groups(enabled_only=True)

    # Try exact name match
    for g in groups:
        if g.name == model_name:
            return g.id

    # Try first enabled group as default
    if groups:
        return groups[0].id

    raise HTTPException(status_code=400, detail=f"No enabled group found for model '{model_name}'")


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
) -> None:
    """Write a call log entry asynchronously."""
    log_entry = CallLog(
        group_id=group_id,
        model_id=model_id,
        provider_id=provider_id,
        request_body=request_body,
        response_body=response_body,
        status=status,
        duration_ms=duration_ms,
        prompt_tokens=usage.get("prompt_tokens") if usage else None,
        completion_tokens=usage.get("completion_tokens") if usage else None,
        cache_tokens=usage.get("cache_tokens") if usage else None,
        total_tokens=usage.get("total_tokens") if usage else None,
        cost=_estimate_cost(usage) if usage else None,
        error_message=error_message,
    )
    await _get_db().create_call_log(log_entry)


def _estimate_cost(usage: dict[str, Any]) -> float:
    """Estimate cost based on token usage.

    Currently returns 0 (cost tracking not implemented).
    For accurate cost tracking, implement per-model pricing tables.
    """
    return 0.0


def _extract_model_route_info(response: dict, internal_params: dict) -> tuple[int | None, int | None, int | None]:
    """Extract model/provider IDs from response metadata if available."""
    model_id = None
    provider_id = None
    group_id = internal_params.get("_group_id")
    return group_id, model_id, provider_id


# ---------------------------------------------------------------------------
# Endpoint: OpenAI /v1/chat/completions
# ---------------------------------------------------------------------------


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    internal = openai_to_internal(body)
    stream = internal["stream"]

    if stream:
        return StreamingResponse(
            _stream_openai(internal, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    return await _handle_chat_non_stream(internal, request, internal_to_openai)


@app.post("/v1/completions")
async def completions(request: Request):
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
# Endpoint: Models list
# ---------------------------------------------------------------------------


@app.get("/v1/models")
async def list_models(request: Request):
    """List available models. Supports both OpenAI and Anthropic clients."""
    accept = request.headers.get("accept", "")
    db = _get_db()
    models = await db.list_models(enabled_only=True)
    providers_list = await db.list_providers(enabled_only=True)

    model_list = []
    for m in models:
        provider = next((p for p in providers_list if p.id == m.provider_id), None)
        model_list.append({
            "id": m.name,
            "name": m.name,
            "display_name": m.display_name or m.name,
            "provider_type": provider.provider_type if provider else "unknown",
            "created_at": m.created_at.isoformat() if m.created_at else "",
        })

    # Return Anthropic format if client is Anthropic
    if "anthropic" in accept.lower():
        return models_to_anthropic(model_list)

    return models_to_openai(model_list)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "botflow"}


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    """Stub endpoint for OpenAI-compatible embeddings."""
    import time
    from fastapi.responses import JSONResponse
    body = await request.json()
    model = body.get("model", "")
    input_text = body.get("input", [])
    if isinstance(input_text, str):
        input_text = [input_text]

    log.warning("/v1/embeddings called but not fully implemented (model={})", model)
    return JSONResponse(
        status_code=501,
        content={
            "error": {
                "message": "Embeddings endpoint is not yet implemented. "
                           "It will be available in a future Phase 2 release.",
                "type": "not_implemented",
            }
        },
    )


# ---------------------------------------------------------------------------
# Non-streaming handler
# ---------------------------------------------------------------------------


async def _handle_chat_non_stream(
    internal: dict,
    request: Request,
    format_response,
) -> JSONResponse:
    """Handle a non-streaming chat request through the router."""
    group_id = await _get_group_id({"model": internal.get("model", "")})
    internal["_group_id"] = group_id
    router = _get_router(group_id)

    start = time.monotonic()

    try:
        result = await router.route(
            messages=internal["messages"],
            temperature=internal.get("temperature"),
            max_tokens=internal.get("max_tokens"),
            stream=False,
            **(internal.get("extra", {})),
        )

        duration = int((time.monotonic() - start) * 1000)
        usage = result.get("usage", {})
        routing = result.pop("_routing", {})

        # Log the call
        await _log_call(
            group_id=group_id,
            model_id=routing.get("model_id"),
            provider_id=routing.get("provider_id"),
            request_body=json.dumps(internal.get("messages", [])[:500]),
            response_body=json.dumps(result)[:500],
            status="success",
            duration_ms=duration,
            usage=usage,
        )

        return JSONResponse(content=format_response(result))

    except Exception as e:
        duration = int((time.monotonic() - start) * 1000)
        log.error("Chat request failed: {}", e)

        await _log_call(
            group_id=group_id,
            model_id=None,
            provider_id=None,
            request_body=json.dumps(internal.get("messages", [])[:500]),
            response_body=None,
            status="error",
            duration_ms=duration,
            usage=None,
            error_message=str(e),
        )

        raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
# Streaming handlers
# ---------------------------------------------------------------------------


async def _stream_openai(
    internal: dict,
    request: Request,
) -> AsyncGenerator[str, None]:
    """Stream response in OpenAI SSE format."""
    model_name = internal.get("model", "")
    group_id = None
    try:
        group_id = await _get_group_id({"model": model_name})
        router = _get_router(group_id)

        route_result = await router.route(
            messages=internal["messages"],
            temperature=internal.get("temperature"),
            max_tokens=internal.get("max_tokens"),
            stream=True,
            **(internal.get("extra", {})),
        )

        ep = route_result["endpoint"]
        start = time.monotonic()

        usage_final = None
        async for chunk in ep.provider.chat_stream(
            messages=route_result["messages"],
            model=ep.detail.model_name,
            temperature=route_result.get("temperature"),
            max_tokens=route_result.get("max_tokens"),
            **route_result.get("kwargs", {}),
        ):
            sse_data = internal_chunk_to_openai_sse(chunk)
            if sse_data.get("usage"):
                usage_final = sse_data["usage"]
            yield f"data: {json.dumps(sse_data, ensure_ascii=False)}\n\n"

        yield "data: [DONE]\n\n"

        duration = int((time.monotonic() - start) * 1000)
        await _log_call(
            group_id=group_id,
            model_id=ep.model_id,
            provider_id=ep.detail.provider_id,
            request_body=json.dumps(internal.get("messages", [])[:500]),
            response_body=None,
            status="success",
            duration_ms=duration,
            usage=usage_final,
        )

    except Exception as e:
        log.error("Stream failed for model {}: {}", model_name, e)
        await _log_call(
            group_id=group_id,
            model_id=None,
            provider_id=None,
            request_body=json.dumps(internal.get("messages", [])[:500]),
            response_body=None,
            status="error",
            duration_ms=None,
            usage=None,
            error_message=str(e),
        )
        error_data = {"error": {"message": str(e), "type": "server_error"}}
        yield f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"


async def _stream_anthropic(
    internal: dict,
    request: Request,
) -> AsyncGenerator[str, None]:
    """Stream response in Anthropic SSE format."""
    model_name = internal.get("model", "")
    group_id = None
    try:
        group_id = await _get_group_id({"model": model_name})
        router = _get_router(group_id)

        route_result = await router.route(
            messages=internal["messages"],
            temperature=internal.get("temperature"),
            max_tokens=internal.get("max_tokens"),
            stream=True,
            **(internal.get("extra", {})),
        )

        ep = route_result["endpoint"]
        start = time.monotonic()

        usage_final = None
        async for chunk in ep.provider.chat_stream(
            messages=route_result["messages"],
            model=ep.detail.model_name,
            temperature=route_result.get("temperature"),
            max_tokens=route_result.get("max_tokens"),
            **route_result.get("kwargs", {}),
        ):
            events = internal_chunk_to_anthropic_sse(chunk)
            for event in events:
                yield f"event: {event['type']}\n"
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            if chunk.get("usage"):
                usage_final = chunk["usage"]

        duration = int((time.monotonic() - start) * 1000)
        await _log_call(
            group_id=group_id,
            model_id=ep.model_id,
            provider_id=ep.detail.provider_id,
            request_body=json.dumps(internal.get("messages", [])[:500]),
            response_body=None,
            status="success",
            duration_ms=duration,
            usage=usage_final,
        )

    except Exception as e:
        log.error("Anthropic stream failed: {}", e)
        await _log_call(
            group_id=group_id,
            model_id=None,
            provider_id=None,
            request_body=json.dumps(internal.get("messages", [])[:500]),
            response_body=None,
            status="error",
            duration_ms=None,
            usage=None,
            error_message=str(e),
        )
        error_event = {"type": "error", "error": {"message": str(e)}}
        yield f"event: error\n"
        yield f"data: {json.dumps(error_event, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"


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
