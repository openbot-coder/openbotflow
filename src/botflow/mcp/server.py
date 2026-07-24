"""MCP server initialization and configuration."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings


def create_mcp_server() -> FastMCP:
    """Create and configure the botflow MCP server."""
    return FastMCP(
        "botflow",
        sse_path="/",
        message_path="/",
        streamable_http_path="/",
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
