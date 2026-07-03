"""6-month data cleanup for call_logs."""

from __future__ import annotations

from datetime import datetime, timedelta, UTC
from pathlib import Path

from loguru import logger

from botflow.storage.db import Database


async def cleanup_call_logs(db: Database, retention_days: int = 180) -> int:
    """Delete call_logs older than retention_days.

    Args:
        db: Database instance.
        retention_days: Retention period in days (default 180 = ~6 months).

    Returns:
        Number of deleted rows.
    """
    cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
    deleted = await db.delete_old_call_logs(cutoff)
    if deleted > 0:
        logger.info("Cleaned up {} old call_log records (retention: {} days)", deleted, retention_days)
    return deleted
