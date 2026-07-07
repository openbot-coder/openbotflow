"""SQLite unified database layer (async).

WAL mode enabled. All botflow data in a single botflow.db.

Tables:
  - providers: LLM provider configurations
  - models: LLM model configurations
  - model_groups: Group definitions for weighted routing
  - group_models: Association table (group <-> model with weight)
  - call_logs: Audit log for API calls
  - config: Key-value configuration store
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import aiosqlite
from loguru import logger

from botflow.storage.models import (
    CallLog,
    GroupModel,
    GroupModelWithDetails,
    GroupStats,
    Model,
    ModelGroup,
    ModelStats,
    Provider,
)

CREATE_TABLES_SQL = """
-- Providers
CREATE TABLE IF NOT EXISTS providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    provider_type TEXT NOT NULL,
    api_key TEXT NOT NULL DEFAULT '',
    base_url TEXT NOT NULL DEFAULT '',
    extra_config TEXT NOT NULL DEFAULT '{}',
    is_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Models
CREATE TABLE IF NOT EXISTS models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    provider_id INTEGER NOT NULL REFERENCES providers(id),
    display_name TEXT NOT NULL DEFAULT '',
    max_retries INTEGER NOT NULL DEFAULT 3,
    cooldown_seconds INTEGER NOT NULL DEFAULT 60,
    cooldown_failure_threshold INTEGER NOT NULL DEFAULT 3,
    extra_config TEXT NOT NULL DEFAULT '{}',
    is_enabled INTEGER NOT NULL DEFAULT 1,
    context_window INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE(name, provider_id)
);

-- Model Groups
CREATE TABLE IF NOT EXISTS model_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    is_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Group-Model Association
CREATE TABLE IF NOT EXISTS group_models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL REFERENCES model_groups(id),
    model_id INTEGER NOT NULL REFERENCES models(id),
    weight REAL NOT NULL DEFAULT 1.0,
    is_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE(group_id, model_id)
);

-- Call Audit Logs
CREATE TABLE IF NOT EXISTS call_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER,
    model_id INTEGER,
    provider_id INTEGER,
    request_body TEXT,
    response_body TEXT,
    status TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cache_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    tool_calls TEXT,
    cost REAL DEFAULT 0.0,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Config Key-Value Store
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""

CREATE_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_call_logs_created_at ON call_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_call_logs_model_id ON call_logs(model_id);
CREATE INDEX IF NOT EXISTS idx_call_logs_group_id ON call_logs(group_id);
CREATE INDEX IF NOT EXISTS idx_call_logs_status ON call_logs(status);
CREATE INDEX IF NOT EXISTS idx_group_models_group_id ON group_models(group_id);
CREATE INDEX IF NOT EXISTS idx_group_models_model_id ON group_models(model_id);
CREATE INDEX IF NOT EXISTS idx_models_provider_id ON models(provider_id);
"""


class Database:
    """Unified SQLite database manager (async)."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._conn: Optional[aiosqlite.Connection] = None

    @property
    def path(self) -> Path:
        return self._db_path

    async def initialize(self) -> None:
        """Create database file, tables, and indexes if not present."""
        await self._ensure_connection()
        assert self._conn is not None
        await self._conn.executescript(CREATE_TABLES_SQL)
        await self._conn.executescript(CREATE_INDEXES_SQL)

        # Migrations: add columns that may be missing from older databases
        try:
            await self._conn.execute("SELECT context_window FROM models LIMIT 1")
        except sqlite3.OperationalError:
            await self._conn.execute("ALTER TABLE models ADD COLUMN context_window INTEGER NOT NULL DEFAULT 0")

        await self._conn.commit()

    async def _ensure_connection(self) -> aiosqlite.Connection:
        """Get or create the database connection with WAL mode."""
        if self._conn is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = await aiosqlite.connect(str(self._db_path))
            self._conn.row_factory = sqlite3.Row
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA busy_timeout=5000")
            await self._conn.execute("PRAGMA foreign_keys=ON")
            await self._conn.commit()
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Config store (key-value)
    # ------------------------------------------------------------------

    async def get_config(self, key: str) -> Optional[str]:
        conn = await self._ensure_connection()
        cursor = await conn.execute("SELECT value FROM config WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row["value"] if row else None

    async def set_config(self, key: str, value: str) -> None:
        conn = await self._ensure_connection()
        await conn.execute(
            "INSERT INTO config (key, value, updated_at) VALUES (?, ?, datetime('now', 'localtime')) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = datetime('now', 'localtime')",
            (key, value),
        )
        await conn.commit()

    # ------------------------------------------------------------------
    # Provider CRUD
    # ------------------------------------------------------------------

    async def create_provider(self, provider: Provider) -> int:
        conn = await self._ensure_connection()
        cursor = await conn.execute(
            "INSERT INTO providers (name, provider_type, api_key, base_url, extra_config, is_enabled) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                provider.name, provider.provider_type, provider.api_key,
                provider.base_url, json.dumps(provider.extra_config),
                1 if provider.is_enabled else 0,
            ),
        )
        await conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    _PROVIDER_UPDATE_COLUMNS = {"name", "provider_type", "api_key", "base_url", "extra_config", "is_enabled"}

    async def update_provider(self, provider_id: int, updates: dict[str, Any]) -> None:
        conn = await self._ensure_connection()
        sets = []
        values = []
        for key, value in updates.items():
            if key not in self._PROVIDER_UPDATE_COLUMNS:
                raise ValueError(f"Invalid column for provider update: {key}")
            if key == "extra_config":
                value = json.dumps(value)
            sets.append(f"{key} = ?")
            values.append(value)
        sets.append("updated_at = datetime('now', 'localtime')")
        values.append(provider_id)
        await conn.execute(f"UPDATE providers SET {', '.join(sets)} WHERE id = ?", values)
        await conn.commit()

    async def delete_provider(self, provider_id: int) -> None:
        conn = await self._ensure_connection()
        await conn.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
        await conn.commit()

    async def get_provider(self, provider_id: int) -> Optional[Provider]:
        conn = await self._ensure_connection()
        cursor = await conn.execute("SELECT * FROM providers WHERE id = ?", (provider_id,))
        row = await cursor.fetchone()
        return self._row_to_provider(row) if row else None

    async def list_providers(self, enabled_only: bool = False) -> list[Provider]:
        conn = await self._ensure_connection()
        sql = "SELECT * FROM providers"
        params: list[Any] = []
        if enabled_only:
            sql += " WHERE is_enabled = 1"
        sql += " ORDER BY name"
        cursor = await conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [self._row_to_provider(r) for r in rows]

    def _row_to_provider(self, row: sqlite3.Row) -> Provider:
        return Provider(
            id=row["id"],
            name=row["name"],
            provider_type=row["provider_type"],
            api_key=row["api_key"],
            base_url=row["base_url"],
            extra_config=json.loads(row["extra_config"]) if row["extra_config"] else {},
            is_enabled=bool(row["is_enabled"]),
        )

    # ------------------------------------------------------------------
    # Model CRUD
    # ------------------------------------------------------------------

    async def create_model(self, model: Model) -> int:
        conn = await self._ensure_connection()
        cursor = await conn.execute(
            "INSERT INTO models (name, provider_id, display_name, max_retries, "
            "cooldown_seconds, cooldown_failure_threshold, extra_config, is_enabled) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                model.name, model.provider_id, model.display_name,
                model.max_retries, model.cooldown_seconds,
                model.cooldown_failure_threshold, json.dumps(model.extra_config),
                1 if model.is_enabled else 0,
            ),
        )
        await conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    _MODEL_UPDATE_COLUMNS = {
        "name", "display_name", "max_retries", "cooldown_seconds",
        "cooldown_failure_threshold", "extra_config", "is_enabled",
    }

    async def update_model(self, model_id: int, updates: dict[str, Any]) -> None:
        conn = await self._ensure_connection()
        sets = []
        values = []
        for key, value in updates.items():
            if key not in self._MODEL_UPDATE_COLUMNS:
                raise ValueError(f"Invalid column for model update: {key}")
            if key == "extra_config":
                value = json.dumps(value)
            sets.append(f"{key} = ?")
            values.append(value)
        sets.append("updated_at = datetime('now', 'localtime')")
        values.append(model_id)
        await conn.execute(f"UPDATE models SET {', '.join(sets)} WHERE id = ?", values)
        await conn.commit()

    async def delete_model(self, model_id: int) -> None:
        conn = await self._ensure_connection()
        await conn.execute("DELETE FROM models WHERE id = ?", (model_id,))
        await conn.commit()

    async def get_model(self, model_id: int) -> Optional[Model]:
        conn = await self._ensure_connection()
        cursor = await conn.execute("SELECT * FROM models WHERE id = ?", (model_id,))
        row = await cursor.fetchone()
        return self._row_to_model(row) if row else None

    async def list_models(self, enabled_only: bool = False) -> list[Model]:
        conn = await self._ensure_connection()
        sql = "SELECT * FROM models"
        params: list[Any] = []
        if enabled_only:
            sql += " WHERE is_enabled = 1"
        sql += " ORDER BY name"
        cursor = await conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [self._row_to_model(r) for r in rows]

    def _row_to_model(self, row: sqlite3.Row) -> Model:
        return Model(
            id=row["id"],
            name=row["name"],
            provider_id=row["provider_id"],
            display_name=row["display_name"],
            max_retries=row["max_retries"],
            cooldown_seconds=row["cooldown_seconds"],
            cooldown_failure_threshold=row["cooldown_failure_threshold"],
            extra_config=json.loads(row["extra_config"]) if row["extra_config"] else {},
            is_enabled=bool(row["is_enabled"]),
        )

    # ------------------------------------------------------------------
    # Group CRUD
    # ------------------------------------------------------------------

    async def create_group(self, group: ModelGroup) -> int:
        conn = await self._ensure_connection()
        cursor = await conn.execute(
            "INSERT INTO model_groups (name, description, is_enabled) VALUES (?, ?, ?)",
            (group.name, group.description, 1 if group.is_enabled else 0),
        )
        await conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    _GROUP_UPDATE_COLUMNS = {"name", "description", "is_enabled"}

    async def update_group(self, group_id: int, updates: dict[str, Any]) -> None:
        conn = await self._ensure_connection()
        for key in updates:
            if key not in self._GROUP_UPDATE_COLUMNS:
                raise ValueError(f"Invalid column for group update: {key}")
        sets = [f"{k} = ?" for k in updates]
        values = list(updates.values()) + [group_id]
        sets.append("updated_at = datetime('now', 'localtime')")
        await conn.execute(f"UPDATE model_groups SET {', '.join(sets)} WHERE id = ?", values)
        await conn.commit()

    async def delete_group(self, group_id: int) -> None:
        conn = await self._ensure_connection()
        # Delete association rows first to avoid FK violation
        await conn.execute("DELETE FROM group_models WHERE group_id = ?", (group_id,))
        await conn.execute("DELETE FROM model_groups WHERE id = ?", (group_id,))
        await conn.commit()

    async def get_group(self, group_id: int) -> Optional[ModelGroup]:
        conn = await self._ensure_connection()
        cursor = await conn.execute("SELECT * FROM model_groups WHERE id = ?", (group_id,))
        row = await cursor.fetchone()
        return self._row_to_group(row) if row else None

    async def list_groups(self, enabled_only: bool = False) -> list[ModelGroup]:
        conn = await self._ensure_connection()
        sql = "SELECT * FROM model_groups"
        params: list[Any] = []
        if enabled_only:
            sql += " WHERE is_enabled = 1"
        sql += " ORDER BY name"
        cursor = await conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [self._row_to_group(r) for r in rows]

    def _row_to_group(self, row: sqlite3.Row) -> ModelGroup:
        return ModelGroup(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            is_enabled=bool(row["is_enabled"]),
        )

    # ------------------------------------------------------------------
    # Group-Model Association
    # ------------------------------------------------------------------

    async def add_model_to_group(self, group_id: int, model_id: int, weight: float = 1.0) -> int:
        conn = await self._ensure_connection()
        cursor = await conn.execute(
            "INSERT OR REPLACE INTO group_models (group_id, model_id, weight, is_enabled) VALUES (?, ?, ?, 1)",
            (group_id, model_id, weight),
        )
        await conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def remove_model_from_group(self, group_id: int, model_id: int) -> None:
        conn = await self._ensure_connection()
        await conn.execute("DELETE FROM group_models WHERE group_id = ? AND model_id = ?", (group_id, model_id))
        await conn.commit()

    async def update_model_weight(self, group_id: int, model_id: int, weight: float) -> None:
        conn = await self._ensure_connection()
        await conn.execute(
            "UPDATE group_models SET weight = ? WHERE group_id = ? AND model_id = ?",
            (weight, group_id, model_id),
        )
        await conn.commit()

    async def get_group_models(self, group_id: int, enabled_only: bool = True) -> list[GroupModelWithDetails]:
        """Get all models in a group with provider details."""
        conn = await self._ensure_connection()
        sql = """
            SELECT
                gm.id, gm.group_id, gm.model_id, gm.weight, gm.is_enabled,
                m.name AS model_name, m.display_name, m.provider_id,
                m.max_retries, m.cooldown_seconds, m.cooldown_failure_threshold,
                m.context_window,
                p.name AS provider_name, p.provider_type
            FROM group_models gm
            JOIN models m ON m.id = gm.model_id
            JOIN providers p ON p.id = m.provider_id
            WHERE gm.group_id = ?
        """
        params: list[Any] = [group_id]
        if enabled_only:
            sql += " AND gm.is_enabled = 1 AND m.is_enabled = 1 AND p.is_enabled = 1"
        sql += " ORDER BY gm.weight DESC"
        cursor = await conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [self._row_to_group_model_detail(r) for r in rows]

    def _row_to_group_model_detail(self, row: sqlite3.Row) -> GroupModelWithDetails:
        return GroupModelWithDetails(
            id=row["id"],
            group_id=row["group_id"],
            model_id=row["model_id"],
            weight=row["weight"],
            is_enabled=bool(row["is_enabled"]),
            model_name=row["model_name"],
            display_name=row["display_name"],
            provider_id=row["provider_id"],
            provider_name=row["provider_name"],
            provider_type=row["provider_type"],
            max_retries=row["max_retries"],
            cooldown_seconds=row["cooldown_seconds"],
            cooldown_failure_threshold=row["cooldown_failure_threshold"],
            context_window=row["context_window"] or 0,
        )

    # ------------------------------------------------------------------
    # Call Logs
    # ------------------------------------------------------------------

    async def create_call_log(self, log: CallLog) -> int:
        conn = await self._ensure_connection()
        cursor = await conn.execute(
            """INSERT INTO call_logs
               (group_id, model_id, provider_id, request_body, response_body,
                status, duration_ms, prompt_tokens, completion_tokens,
                cache_tokens, total_tokens, tool_calls, cost, error_message, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))""",
            (
                log.group_id, log.model_id, log.provider_id,
                log.request_body, log.response_body, log.status,
                log.duration_ms, log.prompt_tokens, log.completion_tokens,
                log.cache_tokens, log.total_tokens, log.tool_calls,
                log.cost, log.error_message,
            ),
        )
        await conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def delete_old_call_logs(self, cutoff_iso: str) -> int:
        """Delete call logs older than the given cutoff date."""
        conn = await self._ensure_connection()
        cursor = await conn.execute(
            "DELETE FROM call_logs WHERE created_at < ?", (cutoff_iso,)
        )
        await conn.commit()
        return cursor.rowcount

    async def query_call_logs(
        self,
        group_id: Optional[int] = None,
        model_id: Optional[int] = None,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[CallLog]:
        conn = await self._ensure_connection()
        conditions = []
        params: list[Any] = []
        if group_id is not None:
            conditions.append("group_id = ?")
            params.append(group_id)
        if model_id is not None:
            conditions.append("model_id = ?")
            params.append(model_id)
        if status is not None:
            conditions.append("status = ?")
            params.append(status)
        where = " AND ".join(conditions) if conditions else "1=1"
        cursor = await conn.execute(
            f"SELECT * FROM call_logs WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        rows = await cursor.fetchall()
        return [self._row_to_call_log(r) for r in rows]

    def _row_to_call_log(self, row: sqlite3.Row) -> CallLog:
        return CallLog(
            id=row["id"],
            group_id=row["group_id"],
            provider_id=row["provider_id"],
            request_body=row["request_body"],
            response_body=row["response_body"],
            status=row["status"],
            duration_ms=row["duration_ms"],
            prompt_tokens=row["prompt_tokens"],
            completion_tokens=row["completion_tokens"],
            cache_tokens=row["cache_tokens"],
            total_tokens=row["total_tokens"],
            tool_calls=row["tool_calls"],
            cost=row["cost"],
            error_message=row["error_message"],
            created_at=row["created_at"],
        )

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    async def get_model_stats(self, model_id: int) -> Optional[ModelStats]:
        conn = await self._ensure_connection()
        cursor = await conn.execute(
            """SELECT
                   m.name AS model_name,
                   COUNT(*) AS total_calls,
                   SUM(CASE WHEN cl.status = 'success' THEN 1 ELSE 0 END) AS success_calls,
                   SUM(CASE WHEN cl.status = 'error' THEN 1 ELSE 0 END) AS error_calls,
                   AVG(cl.duration_ms) AS avg_duration_ms,
                   MIN(cl.duration_ms) AS min_duration_ms,
                   MAX(cl.duration_ms) AS max_duration_ms,
                   COALESCE(SUM(cl.prompt_tokens), 0) AS total_prompt_tokens,
                   COALESCE(SUM(cl.completion_tokens), 0) AS total_completion_tokens,
                   COALESCE(SUM(cl.cache_tokens), 0) AS total_cache_tokens,
                   COALESCE(SUM(cl.cost), 0.0) AS total_cost
                FROM call_logs cl
                JOIN models m ON m.id = cl.model_id
                WHERE cl.model_id = ?""",
            (model_id,),
        )
        row = await cursor.fetchone()
        # SQLite aggregate always returns a row; check if we got actual data
        if not row or row["model_name"] is None:
            return None
        return ModelStats(
            model_id=model_id,
            model_name=row["model_name"],
            total_calls=row["total_calls"],
            success_calls=row["success_calls"],
            error_calls=row["error_calls"],
            avg_duration_ms=row["avg_duration_ms"],
            min_duration_ms=row["min_duration_ms"],
            max_duration_ms=row["max_duration_ms"],
            total_prompt_tokens=row["total_prompt_tokens"],
            total_completion_tokens=row["total_completion_tokens"],
            total_cache_tokens=row["total_cache_tokens"],
            total_tokens=row["total_prompt_tokens"] + row["total_completion_tokens"] + row["total_cache_tokens"],
            total_cost=row["total_cost"],
        )

    async def get_group_stats(self, group_id: int) -> Optional[GroupStats]:
        conn = await self._ensure_connection()
        cursor = await conn.execute(
            """SELECT
                   mg.name AS group_name,
                   COUNT(*) AS total_calls,
                   SUM(CASE WHEN cl.status = 'success' THEN 1 ELSE 0 END) AS success_calls,
                   SUM(CASE WHEN cl.status = 'error' THEN 1 ELSE 0 END) AS error_calls,
                   AVG(cl.duration_ms) AS avg_duration_ms,
                   COALESCE(SUM(cl.cost), 0.0) AS total_cost
               FROM call_logs cl
               JOIN model_groups mg ON mg.id = cl.group_id
               WHERE cl.group_id = ?""",
            (group_id,),
        )
        row = await cursor.fetchone()
        # SQLite aggregate always returns a row; check if we got actual data
        if not row or row["group_name"] is None:
            return None
        return GroupStats(
            group_id=group_id,
            group_name=row["group_name"],
            total_calls=row["total_calls"],
            success_calls=row["success_calls"],
            error_calls=row["error_calls"],
            avg_duration_ms=row["avg_duration_ms"],
            total_cost=row["total_cost"],
        )

    async def get_cost_summary(self, days: int = 30) -> list[dict]:
        """Get daily cost summary for the last N days."""
        conn = await self._ensure_connection()
        since = (datetime.now() - timedelta(days=days)).isoformat()
        cursor = await conn.execute(
            """SELECT DATE(created_at) AS day,
                      COUNT(*) AS total_calls,
                      COALESCE(SUM(cost), 0.0) AS total_cost,
                      COALESCE(SUM(total_tokens), 0) AS total_tokens
               FROM call_logs
               WHERE created_at >= ?
               GROUP BY DATE(created_at)
               ORDER BY day DESC""",
            (since,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
