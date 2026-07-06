"""MCP Tools — thin wrappers that register MemWiki operations with the MCP server."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from botflow.common.logger import get_logger
from botflow.wiki.agent import MemoryAgent

log = get_logger("wiki.tools")

# Global agent reference, set during registration
_agent: MemoryAgent | None = None


def register_tools(mcp: FastMCP, agent: MemoryAgent) -> None:
    """Register all MemWiki MCP tools.

    Args:
        mcp: FastMCP server instance.
        agent: Initialized MemoryAgent instance.
    """
    global _agent
    _agent = agent

    @mcp.tool()
    async def remember(
        title: str,
        content: str,
        type: str = "concept",
        description: str = "",
        tags: str = "",
        source_url: str = "",
    ) -> str:
        """Write knowledge to the MemWiki knowledge base.

        Args:
            title: Title of the knowledge entry.
            content: Knowledge content (markdown body).
            type: Entry type: concept (default), entity, source, or synthesis.
            description: One-line summary of the entry.
            tags: Comma-separated tags.
            source_url: Original source URL if applicable.
        """
        args = f"title={title}\ntype={type}\ndescription={description}\ntags={tags}\nsource_url={source_url}\n\n{content}"
        return await _run_agent("remember", args)

    @mcp.tool()
    async def recall(
        path: str = "",
        title: str = "",
        tag: str = "",
        type: str = "",
    ) -> str:
        """Retrieve details from MemWiki by path, title, tag, or type.

        Args:
            path: Direct file path (e.g. concepts/rag.md).
            title: Title to search for.
            tag: Tag to filter by.
            type: Type to filter by (source, concept, entity, synthesis).
        """
        parts = []
        if path:
            parts.append(f"path={path}")
        if title:
            parts.append(f"title={title}")
        if tag:
            parts.append(f"tag={tag}")
        if type:
            parts.append(f"type={type}")
        return await _run_agent("recall", "\n".join(parts) or "list all entries")

    @mcp.tool()
    async def query(
        query: str,
        limit: int = 10,
        type: str = "",
    ) -> str:
        """Full-text search across the MemWiki knowledge base.

        Args:
            query: Search query (regex supported).
            limit: Maximum results to return.
            type: Filter by entry type.
        """
        args = f"query={query}\nlimit={limit}"
        if type:
            args += f"\ntype={type}"
        return await _run_agent("query", args)

    @mcp.tool()
    async def learn(
        content: str = "",
        url: str = "",
        file_path: str = "",
        type: str = "source",
        tags: str = "",
    ) -> str:
        """Ingest raw material (URL, file, or text) into MemWiki.

        Args:
            content: Raw text content to ingest.
            url: URL to fetch content from.
            file_path: Local file path to read.
            type: Entry type (default: source).
            tags: Comma-separated tags.
        """
        parts = [f"type={type}", f"tags={tags}"]
        if url:
            parts.append(f"url={url}")
        if file_path:
            parts.append(f"file_path={file_path}")
        if content:
            parts.append(f"content={content}")
        return await _run_agent("learn", "\n".join(parts))

    @mcp.tool()
    async def research(
        topic: str,
        model_group: str = "fast",
    ) -> str:
        """LLM-driven research: search wiki + generate analysis, saved as synthesis.

        Args:
            topic: Research topic or question.
            model_group: Model group to use for LLM calls.
        """
        args = f"topic={topic}\nmodel_group={model_group}"
        return await _run_agent("research", args)

    log.info("MemWiki MCP tools registered: remember, recall, query, learn, research")


async def _run_agent(skill_name: str, args: str) -> str:
    """Run the memory agent with the given skill and arguments."""
    if _agent is None:
        return "Error: MemWiki agent not initialized."
    try:
        return await _agent.run(skill_name, args)
    except Exception as e:
        log.error("Agent execution failed for {}: {}", skill_name, e)
        return f"Agent error: {e}"
