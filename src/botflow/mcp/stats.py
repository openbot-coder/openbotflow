"""MCP statistics query tools."""

from __future__ import annotations

from loguru import logger
from mcp.server.fastmcp import FastMCP

from botflow.common.logger import get_logger
from botflow.storage.db import Database

log = get_logger("mcp.stats")


def register_stats_tools(mcp: FastMCP, db: Database) -> None:
    """Register all statistics tools with the provided MCP server."""

    @mcp.tool()
    async def query_model_stats(model_id: int) -> dict:
        """Get aggregated statistics for a specific model.

        Args:
            model_id: ID of the model.
        """
        stats = await db.get_model_stats(model_id)
        if stats is None:
            return {"error": f"No stats found for model id={model_id}."}

        return {
            "model_id": stats.model_id,
            "model_name": stats.model_name,
            "total_calls": stats.total_calls,
            "success": stats.success_calls,
            "errors": stats.error_calls,
            "avg_duration_ms": stats.avg_duration_ms,
            "min_duration_ms": stats.min_duration_ms,
            "max_duration_ms": stats.max_duration_ms,
            "prompt_tokens": stats.total_prompt_tokens,
            "completion_tokens": stats.total_completion_tokens,
            "cache_tokens": stats.total_cache_tokens,
            "total_tokens": stats.total_tokens,
            "total_cost": round(stats.total_cost, 4),
        }

    @mcp.tool()
    async def query_group_stats(group_id: int) -> dict:
        """Get aggregated statistics for a specific group.

        Args:
            group_id: ID of the group.
        """
        stats = await db.get_group_stats(group_id)
        if stats is None:
            return {"error": f"No stats found for group id={group_id}."}

        return {
            "group_id": stats.group_id,
            "group_name": stats.group_name,
            "total_calls": stats.total_calls,
            "success": stats.success_calls,
            "errors": stats.error_calls,
            "avg_duration_ms": stats.avg_duration_ms,
            "total_cost": round(stats.total_cost, 4),
        }

    @mcp.tool()
    async def query_messages(
        group_id: int | None = None,
        model_id: int | None = None,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict:
        """Query recent call logs with optional filters.

        Args:
            group_id: Filter by group ID (optional).
            model_id: Filter by model ID (optional).
            status: Filter by status (success/error).
            limit: Maximum number of results (default 20, max 100).
            offset: Number of results to skip.
        """
        limit = min(limit, 100)
        logs = await db.query_call_logs(
            group_id=group_id,
            model_id=model_id,
            status=status,
            limit=limit,
            offset=offset,
        )
        if not logs:
            return {"messages": [], "total": 0}

        items = []
        for l in logs:
            items.append({
                "id": l.id,
                "status": l.status,
                "duration_ms": l.duration_ms,
                "prompt_tokens": l.prompt_tokens,
                "completion_tokens": l.completion_tokens,
                "cache_tokens": l.cache_tokens,
                "total_tokens": l.total_tokens,
                "cost": round(l.cost, 4) if l.cost is not None else None,
                "created_at": l.created_at.isoformat() if hasattr(l.created_at, 'isoformat') else str(l.created_at),
            })

        return {
            "messages": items,
            "total": len(items),
        }

    @mcp.tool()
    async def query_cost_summary(days: int = 30) -> dict:
        """Get daily cost summary for the last N days.

        Args:
            days: Number of days to look back (default 30).
        """
        summary = await db.get_cost_summary(days=days)
        if not summary:
            return {"error": f"No cost data found for the last {days} days."}

        total_cost = sum(s["total_cost"] for s in summary)
        total_calls = sum(s["total_calls"] for s in summary)
        total_tokens = sum(s["total_tokens"] for s in summary)

        daily = []
        for s in summary:
            daily.append({
                "day": s["day"],
                "calls": s["total_calls"],
                "cost": round(s["total_cost"], 4),
                "tokens": s["total_tokens"],
            })

        return {
            "total_calls": total_calls,
            "total_cost": round(total_cost, 4),
            "total_tokens": total_tokens,
            "daily": daily,
        }

    log.info("MCP stats tools registered.")
