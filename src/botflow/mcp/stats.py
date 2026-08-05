"""MCP Tools for Stats querying.

Tools registered via ToolRegistry (not directly on FastMCP).
Uses the flat Database API.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from botflow.storage.db import Database

if TYPE_CHECKING:
    from botflow.mcp.registry import ToolRegistry


def register_stats_tools(registry: ToolRegistry, db: Database) -> None:
    """Register all stats tools into the internal ToolRegistry."""

    # W11: pre-build name→id lookups via helper
    async def _find_model_id(model_name: str) -> int | None:
        models = await db.list_models()
        return next((m.id for m in models if m.name == model_name), None)

    async def _find_group_id(group_name: str) -> int | None:
        groups = await db.list_groups()
        return next((g.id for g in groups if g.name == group_name), None)

    # W12: consistent JSON serializer
    def _json(obj: object) -> str:
        return json.dumps(obj, ensure_ascii=False, default=str, indent=2)

    async def query_model_stats(model_name: str) -> str:
        """Query stats for a model by name."""
        mid = await _find_model_id(model_name)
        if mid is None:
            return _json({"error": f"Model '{model_name}' not found"})
        stats = await db.get_model_stats(mid)
        if stats is None:
            return _json({"model_name": model_name, "total_calls": 0})
        return _json(stats.model_dump())

    registry.register(
        name="query_model_stats",
        description="查询模型调用统计：总调用次数、成功/失败次数、Token 用量、平均耗时等",
        parameters={
            "type": "object",
            "properties": {
                "model_name": {"type": "string", "description": "模型名称"},
            },
            "required": ["model_name"],
        },
        handler=query_model_stats,
    )

    async def query_group_stats(group_name: str) -> str:
        """Query stats for a group by name."""
        gid = await _find_group_id(group_name)
        if gid is None:
            return _json({"error": f"Group '{group_name}' not found"})
        stats = await db.get_group_stats(gid)
        if stats is None:
            return _json({"group_name": group_name, "total_calls": 0})
        return _json(stats.model_dump())

    registry.register(
        name="query_group_stats",
        description="查询分组调用统计：总调用次数、成功/失败次数、总 Token、估算费用",
        parameters={
            "type": "object",
            "properties": {
                "group_name": {"type": "string", "description": "分组名称"},
            },
            "required": ["group_name"],
        },
        handler=query_group_stats,
    )

    async def query_call_logs(
        status: str | None = None, limit: int = 100, offset: int = 0,
    ) -> str:
        """Query call log records with optional status filter and pagination."""
        # W10: bounds check
        limit = max(1, min(limit, 1000))
        offset = max(0, offset)
        items = await db.query_call_logs(status=status, limit=limit, offset=offset)
        # W9: total should reflect actual count, not page count
        all_items = await db.query_call_logs(status=status, limit=10000, offset=0)
        result = {
            "total": len(all_items),
            "offset": offset,
            "count": len(items),
            "items": [m.model_dump() for m in items],
        }
        return _json(result)

    registry.register(
        name="query_call_logs",
        description="查询消息调用记录，支持按状态筛选和分页",
        parameters={
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "状态筛选（可选，如 success/error）"},
                "limit": {"type": "integer", "description": "返回数量上限，默认 100（最大 1000）"},
                "offset": {"type": "integer", "description": "偏移量，默认 0"},
            },
        },
        handler=query_call_logs,
    )

    async def query_cost_summary(days: int = 30) -> str:
        """Query daily cost summary for the last N days."""
        # W10: bounds check
        days = max(1, min(days, 365))
        items = await db.get_cost_summary(days=days)
        return _json({"items": items})

    registry.register(
        name="query_cost_summary",
        description="查询近 N 天的每日费用汇总：调用次数、Token 用量、费用",
        parameters={
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "查询天数，默认 30（最大 365）"},
            },
        },
        handler=query_cost_summary,
    )
