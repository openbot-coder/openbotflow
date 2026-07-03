"""Logging configuration.

Log format:
  2026-07-03 10:30:00.123 | botflow.proxy | INFO | message key=value

Log rotation: 100MB per file, daily rotation, 30 days retention.
"""

import sys
from pathlib import Path

from loguru import logger


def setup_logging(log_dir: Path, level: str = "INFO") -> None:
    """Configure loguru logger.

    Args:
        log_dir: Directory for log files.
        level: Log level (DEBUG, INFO, WARNING, ERROR).
    """
    log_dir.mkdir(parents=True, exist_ok=True)

    # Remove default handler
    logger.remove()

    # Console handler (structured, colorized)
    logger.add(
        sys.stderr,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<cyan>{extra[module]:12}</cyan> | "
            "<level>{level:8}</level> | "
            "<level>{message}</level>"
        ),
        level=level.upper(),
        colorize=True,
    )

    # File handler (rotation by size + time)
    logger.add(
        log_dir / "botflow-{time:YYYY-MM-DD}.log",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {extra[module]:12} | {level:8} | {message}",
        level=level.upper(),
        rotation="100 MB",
        retention="30 days",
        encoding="utf-8",
    )


def get_logger(module_name: str):
    """Get a logger instance bound to a module name.

    Args:
        module_name: Module identifier (e.g., "proxy", "mcp.manager").

    Returns:
        Logger with bound module context.
    """
    return logger.bind(module=module_name)
