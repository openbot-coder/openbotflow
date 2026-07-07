"""Agent tools for MemWiki — LangChain @tool definitions with path safety."""

from __future__ import annotations

import os
import re
from pathlib import Path

from langchain_core.tools import tool

from botflow.common.exceptions import PathTraversalError
from botflow.common.logger import get_logger

log = get_logger("wiki.tools")

# Set by agent.py at init time
_wiki_dir: Path | None = None


def set_wiki_dir(path: Path) -> None:
    """Set the wiki root directory (called once during agent init)."""
    global _wiki_dir
    _wiki_dir = path


def _get_wiki_dir() -> Path:
    if _wiki_dir is None:
        raise RuntimeError("Wiki directory not initialized. Call set_wiki_dir() first.")
    return _wiki_dir


def _safe_path(rel_path: str) -> Path:
    """Resolve a URI-style relative path to an absolute path within the wiki sandbox.

    Rules:
        - Path must be relative (no leading /)
        - No ./ or ../ components
        - Final resolved path must be within _wiki_dir
    """
    if rel_path.startswith("/") or rel_path.startswith("\\"):
        raise PathTraversalError(f"Absolute path not allowed: {rel_path}")
    if ".." in rel_path.split("/") or ".." in rel_path.split("\\"):
        raise PathTraversalError(f"Path traversal not allowed: {rel_path}")

    resolved = (_get_wiki_dir() / rel_path).resolve()
    wiki_root = _get_wiki_dir().resolve()
    if not str(resolved).startswith(str(wiki_root)):
        raise PathTraversalError(f"Path escapes wiki sandbox: {rel_path}")

    return resolved


@tool
def read_file(path: str) -> str:
    """Read the content of a file in the MemWiki knowledge base.

    Args:
        path: URI-style relative path (e.g. concepts/rag.md, index.md).

    Returns:
        File content as string, or error message.
    """
    try:
        target = _safe_path(path)
        if not target.exists():
            return f"File not found: {path}"
        return target.read_text(encoding="utf-8")
    except PathTraversalError as e:
        return f"Error: {e}"
    except Exception as e:
        log.error("read_file failed: {}", e)
        return f"Error reading file: {e}"


@tool
def write_file(path: str, content: str) -> str:
    """Write content to a file in the MemWiki knowledge base. Creates parent directories automatically.

    Args:
        path: URI-style relative path (e.g. concepts/rag.md).
        content: File content to write.

    Returns:
        Success message with path, or error message.
    """
    try:
        target = _safe_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        log.info("write_file: {}", path)
        return f"Written to {path}"
    except PathTraversalError as e:
        return f"Error: {e}"
    except Exception as e:
        log.error("write_file failed: {}", e)
        return f"Error writing file: {e}"


@tool
def wiki_ripgrep(pattern: str, path: str | None = None) -> str:
    """Search the MemWiki knowledge base using ripgrep (regex pattern matching).

    Args:
        pattern: Regex pattern to search for.
        path: Optional subdirectory to search in (e.g. concepts/). Searches entire wiki if omitted.

    Returns:
        Search results as string.
    """
    try:
        wiki = _get_wiki_dir()
        search_dir = wiki
        if path:
            search_dir = _safe_path(path)

        regex = re.compile(pattern, re.IGNORECASE)
        lines = []
        for root, _dirs, files in os.walk(str(search_dir)):
            for fname in files:
                if not fname.endswith(".md"):
                    continue
                fpath = Path(root) / fname
                try:
                    content = fpath.read_text(encoding="utf-8")
                except Exception:
                    continue
                for i, line in enumerate(content.splitlines(), 1):
                    if regex.search(line):
                        rel = fpath.relative_to(wiki).as_posix()
                        lines.append(f"{rel}:{i}:{line.strip()}")
        if not lines:
            return "No matches found."
        if len(lines) > 50:
            return "\n".join(lines[:50]) + f"\n... ({len(lines)} total matches, truncated)"
        return "\n".join(lines)
    except PathTraversalError as e:
        return f"Error: {e}"
    except Exception as e:
        log.error("wiki_ripgrep failed: {}", e)
        return f"Search error: {e}"


@tool
def wiki_glob(pattern: str) -> str:
    """Find files in the MemWiki knowledge base matching a glob pattern.

    Args:
        pattern: Glob pattern (e.g. concepts/*.md, **/*.md).

    Returns:
        List of matching file paths, or 'No matches found.'.
    """
    try:
        wiki = _get_wiki_dir()
        matches = sorted(str(p.relative_to(wiki).as_posix()) for p in wiki.glob(pattern))
        if not matches:
            return "No matches found."
        if len(matches) > 100:
            return "\n".join(matches[:100]) + f"\n... ({len(matches)} total, truncated)"
        return "\n".join(matches)
    except Exception as e:
        log.error("wiki_glob failed: {}", e)
        return f"Glob error: {e}"


@tool
def call_llm(messages: str, model_group: str = "fast") -> str:
    """Call the LLM Proxy via botflow's GroupRouter (for research/learn tasks).

    Args:
        messages: JSON-serialized messages array, e.g. '[{"role":"user","content":"..."}]'
        model_group: Model group name to use (default: "fast").

    Returns:
        LLM response content as string.
    """
    import json

    try:
        msg_list = json.loads(messages)
        if not isinstance(msg_list, list):
            return "Error: messages must be a JSON array."
    except json.JSONDecodeError:
        return "Error: invalid JSON in messages."

    # Lazy import to avoid circular dependency
    from botflow.router import CooldownManager, GroupRouter
    from botflow.storage.db import Database

    # Use the global DB instance
    from botflow.core import _get_db

    db = _get_db()
    cooldown = CooldownManager()
    router = GroupRouter(group_id=0, db=db, cooldown_manager=cooldown)  # placeholder group_id

    try:
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(
            router.route(messages=msg_list)
        )
        choices = result.get("choices", [])
        if choices:
            return choices[0].get("message", {}).get("content", "No response content.")
        return "No response from LLM."
    except Exception as e:
        log.error("call_llm failed: {}", e)
        return f"LLM call error: {e}"


# Export all tools as a list for agent binding
wiki_tools = [read_file, write_file, wiki_ripgrep, wiki_glob, call_llm]
