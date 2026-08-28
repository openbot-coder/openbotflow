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
    admin_key: str = ""
    # Multiple client API keys (comma-separated). Empty => use llm_key for all.
    api_keys: str = ""

    # Logging
    log_level: str = "INFO"

    # CORS
    cors_origins: str = "*"

    # Streaming: max seconds to wait for the first chunk before failed
    stream_timeout: float = 30.0

    # ── Call-log retention ──
    # Detailed call_logs are kept for this many days, then big fields are purged
    # but the stats columns are retained.
    call_log_detail_days: int = 1
    # Raw (compressed) conversation sessions are kept for this many days.
    raw_session_retention_days: int = 7
    # Whole call_log rows are deleted after this many days.
    call_logs_retention_days: int = 180
    # Hour (0-23, UTC) at which the daily summary job runs.
    daily_summary_hour: int = 0
    # Group used by the daily summary LLM call. Empty => default group.
    summary_group: str = ""

    # Model sync interval in minutes (0 = disabled). Fetches models from upstream providers.
    model_sync_interval: int = 60

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


# Module-level current settings, set by core.create_app() at startup so that
# helper modules (auth, daily_summary) can read config without re-loading .env.
_config: Optional["BotflowSettings"] = None


def set_config(settings: "BotflowSettings") -> None:
    global _config
    _config = settings


def get_config() -> "BotflowSettings":
    """Return the active settings, loading a default if not yet initialized."""
    global _config
    if _config is None:
        _config = load_config()
    return _config
