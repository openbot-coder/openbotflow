"""MCP server factory with meta-tools.

Exposes exactly 3 tools to external MCP clients:
  - tool_search:   BM25-powered tool search
  - tool_describe: detailed tool info (parameters, description)
  - tool_call:     invoke any internal tool by name
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings

from botflow import __version__

if TYPE_CHECKING:
    from botflow.mcp.registry import ToolRegistry

# W4: maximum arguments payload size (1 MB)
_MAX_ARGS_SIZE = 1 << 20


def create_mcp_server(registry: ToolRegistry) -> FastMCP:
    """Create the MCP server with 3 meta-tools backed by a ToolRegistry."""

    mcp = FastMCP(
        instructions=(
            "BotFlow MCP Server — manages LLM providers, models and model groups.\n"
            "You have 3 meta-tools:\n"
            "  1) tool_search  — search available tools by keyword\n"
            "  2) tool_describe — view tool parameter details\n"
            "  3) tool_call    — invoke any internal tool by name + arguments\n\n"
            "Workflow: tool_search → tool_describe → tool_call"
        ),
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
            allowed_hosts=["api.vxquant.com", "localhost", "127.0.0.1", "0.0.0.0"],
        ),
    )

    # ── tool_search ──

    @mcp.tool()
    async def tool_search(query: str, top_k: int = 10) -> str:
        """Search available tools by keyword.

        Uses BM25 ranking to find the most relevant tools.
        Returns a list of matching tool names and descriptions.

        Args:
            query: Search keywords (e.g. "create model", "provider", "stats").
                   Use "*" to list all tools.
            top_k: Max results to return (default 10, max 100).
        """
        # W3: expose top_k parameter with bounds
        top_k = max(1, min(top_k, 100))
        results = registry.search(query, top_k=top_k)
        if not results:
            return json.dumps({"results": [], "hint": "No tools found. Try different keywords."}, ensure_ascii=False)
        return json.dumps({"results": results}, ensure_ascii=False)

    # ── tool_describe ──

    @mcp.tool()
    async def tool_describe(tool_name: str) -> str:
        """View detailed description and parameter schema of a tool.

        Use tool_search first to find the tool name, then use this
        to understand its parameters before calling it.

        Args:
            tool_name: The tool name (from tool_search results)
        """
        td = registry.get(tool_name)
        if td is None:
            return json.dumps({
                "error": f"Unknown tool '{tool_name}'",
                "hint": "Use tool_search to find available tools.",
            }, ensure_ascii=False)
        return json.dumps({
            "name": td.name,
            "description": td.description,
            "parameters": td.parameters,
        }, ensure_ascii=False)

    # ── tool_call ──

    @mcp.tool()
    async def tool_call(tool_name: str, arguments: dict[str, Any] | None = None) -> str:
        """Invoke any internal tool by name with arguments.

        Workflow:
          1) Use tool_search to find the tool
          2) Use tool_describe to understand parameters
          3) Use this tool to execute it

        Args:
            tool_name: The tool name to call
            arguments: Tool arguments as a JSON object (default: {})
        """
        if arguments is None:
            arguments = {}

        # W4: validate arguments payload size
        args_json = json.dumps(arguments, ensure_ascii=False)
        if len(args_json) > _MAX_ARGS_SIZE:
            return json.dumps({"error": f"arguments too large (>{_MAX_ARGS_SIZE} bytes)"}, ensure_ascii=False)

        # C6: distinguish unknown-tool from handler errors
        try:
            result = await registry.call(tool_name, arguments)
        except KeyError as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)

        # C7: Handlers already return JSON strings; if result is a string,
        # return it as-is to avoid double-encoding.
        if isinstance(result, str):
            return result
        try:
            return json.dumps(result, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return json.dumps({"result": str(result)}, ensure_ascii=False)

    return mcp
