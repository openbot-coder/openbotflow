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
    async def query_model_stats(model_id: int) -> str:
        """Get aggregated statistics for a specific model.

        Args:
            model_id: ID of the model.
        """
        stats = await db.get_model_stats(model_id)
        if stats is None:
            return f"No stats found for model id={model_id}."

        avg_line = (
            f"  Avg duration: {stats.avg_duration_ms:.1f}ms\n"
            if stats.avg_duration_ms else "  Avg duration: N/A\n"
        )
        return (
            f"Model [{stats.model_id}] {stats.model_name}:\n"
            f"  Total calls: {stats.total_calls}\n"
            f"  Success: {stats.success_calls}\n"
            f"  Errors: {stats.error_calls}\n"
            f"{avg_line}"
            f"  Prompt tokens: {stats.total_prompt_tokens:,}\n"
            f"  Completion tokens: {stats.total_completion_tokens:,}\n"
            f"  Cache tokens: {stats.total_cache_tokens:,}\n"
            f"  Total cost: ${stats.total_cost:.4f}\n"
        )

    @mcp.tool()
    async def query_group_stats(group_id: int) -> str:
        """Get aggregated statistics for a specific group.

        Args:
            group_id: ID of the group.
        """
        stats = await db.get_group_stats(group_id)
        if stats is None:
            return f"No stats found for group id={group_id}."

        avg_line = (
            f"  Avg duration: {stats.avg_duration_ms:.1f}ms\n"
            if stats.avg_duration_ms else "  Avg duration: N/A\n"
        )
        return (
            f"Group [{stats.group_id}] {stats.group_name}:\n"
            f"  Total calls: {stats.total_calls}\n"
            f"  Success: {stats.success_calls}\n"
            f"  Errors: {stats.error_calls}\n"
            f"{avg_line}"
            f"  Total cost: ${stats.total_cost:.4f}\n"
        )

    @mcp.tool()
    async def query_messages(
        group_id: int | None = None,
        model_id: int | None = None,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> str:
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
            return "No messages found."

        lines = [f"Found {len(logs)} message(s):"]
        for l in logs:
            tokens_info = (
                f"prompt={l.prompt_tokens}, completion={l.completion_tokens}"
                f"{f', cache={l.cache_tokens}' if l.cache_tokens and l.cache_tokens > 0 else ''}"
                if l.total_tokens and l.total_tokens > 0
                else "no tokens"
            )
            lines.append(
                f"  [{l.id}] status={l.status} | {tokens_info} | "
                f"duration={l.duration_ms}ms | {l.created_at}"
            )
        return "\n".join(lines)

    @mcp.tool()
    async def query_cost_summary(days: int = 30) -> str:
        """Get daily cost summary for the last N days.

        Args:
            days: Number of days to look back (default 30).
        """
        summary = await db.get_cost_summary(days=days)
        if not summary:
            return f"No cost data found for the last {days} days."

        total_cost = sum(s["total_cost"] for s in summary)
        total_calls = sum(s["total_calls"] for s in summary)
        total_tokens = sum(s["total_tokens"] for s in summary)

        lines = [
            f"Cost summary for the last {days} days:",
            f"  Total calls: {total_calls}",
            f"  Total cost: ${total_cost:.4f}",
            f"  Total tokens: {total_tokens:,}",
            "",
            "Daily breakdown:",
        ]
        for s in summary:
            lines.append(f"  {s['day']}: {s['total_calls']} calls, ${s['total_cost']:.4f}")

        return "\n".join(lines)

    log.info("MCP stats tools registered.")
