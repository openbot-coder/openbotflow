"""Global configuration management."""

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings


class BotflowSettings(BaseSettings):
    """botflow global settings.

    Priority: CLI args > environment variables > .env file > defaults.
    """

    # Workspace
    workspace: str = str(Path.home() / ".botflow")

    # Server
    host: str = "0.0.0.0"
    port: int = 8080

    # Authentication
    llm_key: str = ""
    mcp_key: str = ""

    # Logging
    log_level: str = "INFO"

    # CORS
    cors_origins: str = "*"

    # Streaming: max seconds to wait for the first chunk before failing
    stream_timeout: float = 30.0

    model_config = {"env_prefix": "BOTFLOW_", "env_file": ".env", "extra": "ignore"}


def load_config(workspace_path: Optional[Path] = None) -> BotflowSettings:
    """Load configuration with workspace-aware .env loading.

    Args:
        workspace_path: Optional workspace path. If provided, loads .env from it.

    Returns:
        BotflowSettings instance.
    """
    env_file = None
    if workspace_path is not None:
        dotenv = workspace_path / ".env"
        if dotenv.exists():
            env_file = str(dotenv)

    settings = BotflowSettings(_env_file=env_file)  # type: ignore[call-arg]
    return settings
