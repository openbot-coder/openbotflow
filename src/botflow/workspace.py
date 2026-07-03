"""Workspace path management.

The workspace is the root directory for all botflow runtime data:
  {workspace}/
    .env           - Environment variables (API keys, etc.)
    data/
      botflow.db   - SQLite database
    logs/          - Log files
"""

from pathlib import Path


def get_workspace_path(custom_path: str | None = None) -> Path:
    """Resolve the workspace path.

    Priority:
        1. Custom path from CLI --workspace argument
        2. Default: current directory (./)

    Args:
        custom_path: Optional custom workspace path from CLI.

    Returns:
        Resolved Path object.
    """
    if custom_path:
        return Path(custom_path).expanduser().resolve()
    return Path.cwd().resolve()


def init_workspace(workspace: Path) -> Path:
    """Ensure workspace directory structure exists.

    Creates the following structure if not present:
      {workspace}/
        data/
        logs/

    Args:
        workspace: Workspace root path.

    Returns:
        The workspace path.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "data").mkdir(exist_ok=True)
    (workspace / "logs").mkdir(exist_ok=True)
    return workspace
