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

import gzip
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite
from loguru import logger

from botflow.storage.models import (
    ApiKey,
    CallLog,
    DailySummary,
    GroupModel,
    GroupModelWithDetails,
    GroupStats,
    Model,
    ModelGroup,
    ModelStats,
    Provider,
    RawSession,
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
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Models
CREATE TABLE IF NOT EXISTS models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    display_name TEXT NOT NULL DEFAULT '',
    api_format TEXT NOT NULL DEFAULT '',
    max_retries INTEGER NOT NULL DEFAULT 3,
    cooldown_seconds INTEGER NOT NULL DEFAULT 60,
    cooldown_failure_threshold INTEGER NOT NULL DEFAULT 3,
    extra_config TEXT NOT NULL DEFAULT '{}',
    is_enabled INTEGER NOT NULL DEFAULT 1,
    context_window INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(name, provider_id)
);

-- Model Groups
CREATE TABLE IF NOT EXISTS model_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    is_enabled INTEGER NOT NULL DEFAULT 1,
    fallback_group_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Group-Model Association
CREATE TABLE IF NOT EXISTS group_models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL REFERENCES model_groups(id) ON DELETE CASCADE,
    model_id INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    weight REAL NOT NULL DEFAULT 1.0,
    is_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
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
    api_key_id INTEGER,
    error_type TEXT,
    traceback TEXT,
    request_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Config Key-Value Store
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Client API keys (multi-tenant). Raw key is never stored, only its sha256.
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash TEXT UNIQUE NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    is_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Daily LLM-generated wiki summary of conversations
CREATE TABLE IF NOT EXISTS daily_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT UNIQUE NOT NULL,
    summary_md TEXT NOT NULL DEFAULT '',
    stats_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Compressed (gzip) raw conversation sessions, retained for a rolling window
CREATE TABLE IF NOT EXISTS raw_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT UNIQUE NOT NULL,
    blob BLOB NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_call_logs_created_at ON call_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_call_logs_model_id ON call_logs(model_id);
CREATE INDEX IF NOT EXISTS idx_call_logs_group_id ON call_logs(group_id);
CREATE INDEX IF NOT EXISTS idx_call_logs_api_key_id ON call_logs(api_key_id);
CREATE INDEX IF NOT EXISTS idx_call_logs_status ON call_logs(status);
CREATE INDEX IF NOT EXISTS idx_group_models_group_id ON group_models(group_id);
CREATE INDEX IF NOT EXISTS idx_group_models_model_id ON group_models(model_id);
CREATE INDEX IF NOT EXISTS idx_models_provider_id ON models(provider_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_key_hash ON api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_daily_summaries_day ON daily_summaries(day);
CREATE INDEX IF NOT EXISTS idx_raw_sessions_day ON raw_sessions(day);
"""

# New call_logs columns added via migration (see initialize)
ALTER_CALL_LOGS_COLUMNS = [
    "api_key_id INTEGER",
    "error_type TEXT",
    "traceback TEXT",
    "request_id TEXT",
]

# The active Database instance, set in Database.initialize() so that helper
# modules (auth, daily_summary, admin_api) can access it without threading the
# instance through every call.
_active_db: Optional["Database"] = None


def get_db() -> "Database":
    """Return the active Database instance (must be initialized first)."""
    if _active_db is None:
        raise RuntimeError("Database is not initialized. Call Database.initialize() first.")
    return _active_db


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
        except sqlite3.OperationalError:  # UNCOVERED: 旧库迁移路径，全新数据库永远存在该列，无法单元测试触发
            await self._conn.execute("ALTER TABLE models ADD COLUMN context_window INTEGER NOT NULL DEFAULT 0")  # UNCOVERED
        try:
            await self._conn.execute("SELECT api_format FROM models LIMIT 1")
        except sqlite3.OperationalError:  # UNCOVERED: 旧库迁移路径，全新数据库永远存在该列，无法单元测试触发
            await self._conn.execute("ALTER TABLE models ADD COLUMN api_format TEXT NOT NULL DEFAULT ''")  # UNCOVERED

        # Migrations for call_logs (new audit columns)
        for col in ALTER_CALL_LOGS_COLUMNS:
            col_name = col.split()[0]
            try:
                await self._conn.execute(f"SELECT {col_name} FROM call_logs LIMIT 1")
            except sqlite3.OperationalError:  # UNCOVERED: 旧库迁移路径，全新数据库已含审计列，无法单元测试触发
                await self._conn.execute(f"ALTER TABLE call_logs ADD COLUMN {col}")  # UNCOVERED

        await self._conn.commit()
        global _active_db
        _active_db = self

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

    async def __aenter__(self) -> "Database":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Raw SQL helpers (public API for external callers)
    # ------------------------------------------------------------------

    async def execute_write(self, sql: str, params: tuple = ()) -> int:
        """Execute a write SQL statement and return rowcount."""
        conn = await self._ensure_connection()
        cursor = await conn.execute(sql, params)
        await conn.commit()
        return cursor.rowcount

    async def execute_read(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """Execute a read SQL statement and return all rows."""
        conn = await self._ensure_connection()
        cursor = await conn.execute(sql, params)
        return await cursor.fetchall()

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
            "INSERT INTO config (key, value, updated_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')",
            (key, value),
        )
        await conn.commit()

    # W14: public method for cleaning up config entries by key prefix
    async def cleanup_config_by_prefix(self, prefix: str, older_than_seconds: int) -> int:
        """Delete config entries matching a key prefix older than N seconds. Returns count deleted."""
        conn = await self._ensure_connection()
        cutoff = time.time() - older_than_seconds
        cursor = await conn.execute(
            "DELETE FROM config WHERE key LIKE ? AND updated_at < datetime(?, 'unixepoch')",
            (prefix, cutoff),
        )
        await conn.commit()
        return cursor.rowcount

    async def save_cooldown_state(self, states: list[dict]) -> None:
        """Save cooldown states to config table."""
        import json
        for state in states:
            key = f"cooldown:{state['group_id']}:{state['model_id']}"
            value = json.dumps({
                "failures": state["consecutive_failures"],
                "cooldown_until": state["cooldown_until"],
            })
            await self.set_config(key, value)

    async def load_cooldown_states(self) -> list[dict]:
        """Load all cooldown states from config table."""
        import json
        conn = await self._ensure_connection()
        cursor = await conn.execute(
            "SELECT key, value FROM config WHERE key LIKE 'cooldown:%'"
        )
        rows = await cursor.fetchall()
        states = []
        for row in rows:
            try:
                key = row["key"]
                parts = key.split(":")
                if len(parts) == 3:
                    group_id = int(parts[1])
                    model_id = int(parts[2])
                    data = json.loads(row["value"])
                    states.append({
                        "group_id": group_id,
                        "model_id": model_id,
                        "consecutive_failures": data["failures"],
                        "cooldown_until": data["cooldown_until"],
                    })
            except Exception:
                pass
        return states

    async def clear_cooldown_states(self) -> None:
        """Clear all cooldown states from config table."""
        conn = await self._ensure_connection()
        await conn.execute("DELETE FROM config WHERE key LIKE 'cooldown:%'")
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
        sets.append("updated_at = datetime('now')")
        values.append(provider_id)
        await conn.execute(f"UPDATE providers SET {', '.join(sets)} WHERE id = ?", values)
        await conn.commit()

    async def delete_provider(self, provider_id: int) -> int:
        conn = await self._ensure_connection()
        cursor = await conn.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
        await conn.commit()
        return cursor.rowcount

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
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    # ------------------------------------------------------------------
    # Model CRUD
    # ------------------------------------------------------------------

    async def create_model(self, model: Model) -> int:
        conn = await self._ensure_connection()
        cursor = await conn.execute(
            "INSERT INTO models (name, provider_id, display_name, api_format, max_retries, "
            "cooldown_seconds, cooldown_failure_threshold, extra_config, is_enabled) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                model.name, model.provider_id, model.display_name, model.api_format,
                model.max_retries, model.cooldown_seconds,
                model.cooldown_failure_threshold, json.dumps(model.extra_config),
                1 if model.is_enabled else 0,
            ),
        )
        await conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    _MODEL_UPDATE_COLUMNS = {
        "name", "display_name", "api_format", "max_retries", "cooldown_seconds",
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
        sets.append("updated_at = datetime('now')")
        values.append(model_id)
        await conn.execute(f"UPDATE models SET {', '.join(sets)} WHERE id = ?", values)
        await conn.commit()

    async def delete_model(self, model_id: int) -> int:
        conn = await self._ensure_connection()
        cursor = await conn.execute("DELETE FROM models WHERE id = ?", (model_id,))
        await conn.commit()
        return cursor.rowcount

    async def get_model(self, model_id: int) -> Optional[Model]:
        conn = await self._ensure_connection()
        cursor = await conn.execute("SELECT * FROM models WHERE id = ?", (model_id,))
        row = await cursor.fetchone()
        return self._row_to_model(row) if row else None

    async def upsert_model(self, model: Model) -> int:
        """Insert or update a model by (name, provider_id). Returns model id."""
        conn = await self._ensure_connection()
        cursor = await conn.execute(
            "SELECT id FROM models WHERE name = ? AND provider_id = ?",
            (model.name, model.provider_id),
        )
        row = await cursor.fetchone()
        if row:
            await conn.execute(
                "UPDATE models SET display_name = ?, api_format = ?, "
                "max_retries = ?, cooldown_seconds = ?, cooldown_failure_threshold = ?, "
                "extra_config = ?, is_enabled = ?, updated_at = datetime('now') "
                "WHERE id = ?",
                (
                    model.display_name, model.api_format,
                    model.max_retries, model.cooldown_seconds,
                    model.cooldown_failure_threshold,
                    json.dumps(model.extra_config),
                    1 if model.is_enabled else 0,
                    row["id"],
                ),
            )
            await conn.commit()
            return row["id"]
        else:
            return await self.create_model(model)

    async def list_models(self, provider_id: Optional[int] = None, enabled_only: bool = False) -> list[Model]:
        conn = await self._ensure_connection()
        sql = "SELECT * FROM models"
        params: list[Any] = []
        where = []
        if provider_id is not None:
            where.append("provider_id = ?")
            params.append(provider_id)
        if enabled_only:
            where.append("is_enabled = 1")
        if where:
            sql += " WHERE " + " AND ".join(where)
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
            api_format=row["api_format"] or "",
            max_retries=row["max_retries"],
            cooldown_seconds=row["cooldown_seconds"],
            cooldown_failure_threshold=row["cooldown_failure_threshold"],
            extra_config=json.loads(row["extra_config"]) if row["extra_config"] else {},
            is_enabled=bool(row["is_enabled"]),
            context_window=row["context_window"] or 0,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
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

    _GROUP_UPDATE_COLUMNS = {"name", "description", "is_enabled", "fallback_group_id"}

    async def update_group(self, group_id: int, updates: dict[str, Any]) -> None:
        conn = await self._ensure_connection()
        for key in updates:
            if key not in self._GROUP_UPDATE_COLUMNS:
                raise ValueError(f"Invalid column for group update: {key}")
        sets = [f"{k} = ?" for k in updates]
        values = list(updates.values()) + [group_id]
        sets.append("updated_at = datetime('now')")
        await conn.execute(f"UPDATE model_groups SET {', '.join(sets)} WHERE id = ?", values)
        await conn.commit()

    async def delete_group(self, group_id: int) -> int:
        conn = await self._ensure_connection()
        # Delete association rows first to avoid FK violation
        await conn.execute("DELETE FROM group_models WHERE group_id = ?", (group_id,))
        cursor = await conn.execute("DELETE FROM model_groups WHERE id = ?", (group_id,))
        await conn.commit()
        return cursor.rowcount

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
            fallback_group_id=row["fallback_group_id"],
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
                m.name AS model_name, m.display_name, m.api_format, m.provider_id,
                m.max_retries, m.cooldown_seconds, m.cooldown_failure_threshold,
                m.context_window, m.extra_config AS model_extra_config,
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
        import json as _json
        model_extra = {}
        raw = row["model_extra_config"]
        if raw:
            try:
                model_extra = _json.loads(raw)
            except (TypeError, ValueError):
                pass
        return GroupModelWithDetails(
            id=row["id"],
            group_id=row["group_id"],
            model_id=row["model_id"],
            weight=row["weight"],
            is_enabled=bool(row["is_enabled"]),
            model_name=row["model_name"],
            display_name=row["display_name"],
            api_format=row["api_format"] or "",
            provider_id=row["provider_id"],
            provider_name=row["provider_name"],
            provider_type=row["provider_type"],
            max_retries=row["max_retries"],
            cooldown_seconds=row["cooldown_seconds"],
            cooldown_failure_threshold=row["cooldown_failure_threshold"],
            context_window=row["context_window"] or 0,
            proxy=model_extra.get("proxy", ""),
        )

    # ------------------------------------------------------------------
    # Call Logs
    # ------------------------------------------------------------------

    async def create_call_log(self, log: CallLog) -> int:
        conn = await self._ensure_connection()
        cursor = await conn.execute(
            """INSERT INTO call_logs
               (api_key_id, group_id, model_id, provider_id, request_body, response_body,
                status, error_type, error_message, traceback, request_id,
                duration_ms, prompt_tokens, completion_tokens,
                cache_tokens, total_tokens, tool_calls, cost, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            (
                log.api_key_id, log.group_id, log.model_id, log.provider_id,
                log.request_body, log.response_body, log.status,
                log.error_type, log.error_message, log.traceback, log.request_id,
                log.duration_ms, log.prompt_tokens, log.completion_tokens,
                log.cache_tokens, log.total_tokens, log.tool_calls,
                log.cost,
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
        provider_id: Optional[int] = None,
        api_key_id: Optional[int] = None,
        status: Optional[str] = None,
        error_type: Optional[str] = None,
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
        if provider_id is not None:
            conditions.append("provider_id = ?")
            params.append(provider_id)
        if api_key_id is not None:
            conditions.append("api_key_id = ?")
            params.append(api_key_id)
        if status is not None:
            conditions.append("status = ?")
            params.append(status)
        if error_type is not None:
            conditions.append("error_type = ?")
            params.append(error_type)
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
            api_key_id=row["api_key_id"],
            group_id=row["group_id"],
            model_id=row["model_id"],
            provider_id=row["provider_id"],
            request_body=row["request_body"],
            response_body=row["response_body"],
            status=row["status"],
            error_type=row["error_type"],
            error_message=row["error_message"],
            traceback=row["traceback"],
            request_id=row["request_id"],
            duration_ms=row["duration_ms"],
            prompt_tokens=row["prompt_tokens"],
            completion_tokens=row["completion_tokens"],
            cache_tokens=row["cache_tokens"],
            total_tokens=row["total_tokens"],
            tool_calls=row["tool_calls"],
            cost=row["cost"],
            created_at=row["created_at"],
        )

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    async def get_model_stats(self, model_id: int, api_key_id: Optional[int] = None) -> Optional[ModelStats]:
        conn = await self._ensure_connection()
        where = "cl.model_id = ?"
        params: list[Any] = [model_id]
        if api_key_id is not None:
            where += " AND cl.api_key_id = ?"
            params.append(api_key_id)
        cursor = await conn.execute(
            f"""SELECT
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
                WHERE {where}""",
            params,
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

    async def get_group_stats(self, group_id: int, api_key_id: Optional[int] = None) -> Optional[GroupStats]:
        conn = await self._ensure_connection()
        where = "cl.group_id = ?"
        params: list[Any] = [group_id]
        if api_key_id is not None:
            where += " AND cl.api_key_id = ?"
            params.append(api_key_id)
        cursor = await conn.execute(
            f"""SELECT
                   mg.name AS group_name,
                   COUNT(*) AS total_calls,
                   SUM(CASE WHEN cl.status = 'success' THEN 1 ELSE 0 END) AS success_calls,
                   SUM(CASE WHEN cl.status = 'error' THEN 1 ELSE 0 END) AS error_calls,
                   AVG(cl.duration_ms) AS avg_duration_ms,
                   COALESCE(SUM(cl.cost), 0.0) AS total_cost
               FROM call_logs cl
               JOIN model_groups mg ON mg.id = cl.group_id
               WHERE {where}""",
            params,
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

    async def get_cost_summary(self, days: int = 30, api_key_id: Optional[int] = None) -> list[dict]:
        """Daily cost summary for the last N days, optionally filtered by api_key_id."""
        conn = await self._ensure_connection()
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        where = "created_at >= ?"
        params: list[Any] = [since]
        if api_key_id is not None:
            where += " AND api_key_id = ?"
            params.append(api_key_id)
        cursor = await conn.execute(
            f"""SELECT DATE(created_at) AS day,
                       COUNT(*) AS total_calls,
                       COALESCE(SUM(cost), 0.0) AS total_cost,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens
                FROM call_logs
                WHERE {where}
                GROUP BY DATE(created_at)
                ORDER BY day DESC""",
            params,
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def list_model_stats(self, limit: int = 20, api_key_id: Optional[int] = None) -> list[dict]:
        conn = await self._ensure_connection()
        where = "1=1"
        params: list[Any] = []
        if api_key_id is not None:
            where += " AND cl.api_key_id = ?"
            params.append(api_key_id)
        cursor = await conn.execute(
            f"""SELECT m.id AS model_id, m.name AS model_name,
                       COUNT(*) AS total_calls,
                       SUM(CASE WHEN cl.status='success' THEN 1 ELSE 0 END) AS success_calls,
                       SUM(CASE WHEN cl.status='error' THEN 1 ELSE 0 END) AS error_calls,
                       COALESCE(SUM(cl.cost), 0.0) AS total_cost
                FROM call_logs cl JOIN models m ON m.id = cl.model_id
                WHERE {where}
                GROUP BY m.id, m.name
                ORDER BY total_calls DESC LIMIT ?""",
            params + [limit],
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def list_group_stats(self, limit: int = 20, api_key_id: Optional[int] = None) -> list[dict]:
        conn = await self._ensure_connection()
        where = "1=1"
        params: list[Any] = []
        if api_key_id is not None:
            where += " AND cl.api_key_id = ?"
            params.append(api_key_id)
        cursor = await conn.execute(
            f"""SELECT mg.id AS group_id, mg.name AS group_name,
                       COUNT(*) AS total_calls,
                       SUM(CASE WHEN cl.status='success' THEN 1 ELSE 0 END) AS success_calls,
                       SUM(CASE WHEN cl.status='error' THEN 1 ELSE 0 END) AS error_calls,
                       COALESCE(SUM(cl.cost), 0.0) AS total_cost
                FROM call_logs cl JOIN model_groups mg ON mg.id = cl.group_id
                WHERE {where}
                GROUP BY mg.id, mg.name
                ORDER BY total_calls DESC LIMIT ?""",
            params + [limit],
        )
        return [dict(r) for r in await cursor.fetchall()]

    # ------------------------------------------------------------------
    # Scalar-arg convenience CRUD (used by REST admin API)
    # ------------------------------------------------------------------

    async def create_provider_raw(self, *, name, type, base_url, api_key="", is_enabled=True) -> Provider:
        return await self.create_provider(Provider(
            name=name, provider_type=type, base_url=base_url, api_key=api_key, is_enabled=is_enabled
        ))

    async def get_provider_raw(self, provider_id: int) -> Optional[Provider]:
        conn = await self._ensure_connection()
        cursor = await conn.execute("SELECT * FROM providers WHERE id = ?", (provider_id,))
        row = await cursor.fetchone()
        return self._row_to_provider(row) if row else None

    async def list_providers_raw(self) -> list[Provider]:
        return await self.list_providers()

    async def update_provider_raw(self, provider_id, *, name, type, base_url, api_key, is_enabled) -> None:
        conn = await self._ensure_connection()
        await conn.execute(
            "UPDATE providers SET name=?, provider_type=?, base_url=?, api_key=?, is_enabled=? WHERE id=?",
            (name, type, base_url, api_key, 1 if is_enabled else 0, provider_id),
        )
        await conn.commit()

    async def delete_provider_raw(self, provider_id: int) -> int:
        return await self.delete_provider(provider_id)

    async def create_model_raw(self, *, provider_id, name, context_window=0,
                              is_enabled=True, display_name="", api_format="",
                              max_retries=3, cooldown_seconds=60,
                              cooldown_failure_threshold=3) -> Model:
        return await self.create_model(Model(
            provider_id=provider_id, name=name, display_name=display_name, api_format=api_format,
            max_retries=max_retries, cooldown_seconds=cooldown_seconds,
            cooldown_failure_threshold=cooldown_failure_threshold, is_enabled=is_enabled,
            context_window=context_window,
        ))

    async def get_model_raw(self, model_id: int) -> Optional[Model]:
        conn = await self._ensure_connection()
        cursor = await conn.execute("SELECT * FROM models WHERE id = ?", (model_id,))
        row = await cursor.fetchone()
        return self._row_to_model(row) if row else None

    async def list_models_raw(self, provider_id: Optional[int] = None, enabled_only: bool = False) -> list[Model]:
        return await self.list_models(provider_id=provider_id, enabled_only=enabled_only)

    async def update_model_raw(self, model_id, *, name, context_window=0, is_enabled=True,
                              display_name="", api_format="", max_retries=3, cooldown_seconds=60,
                              cooldown_failure_threshold=3) -> None:
        conn = await self._ensure_connection()
        sql = "UPDATE models SET name=?, display_name=?, api_format=?, max_retries=?, cooldown_seconds=?, cooldown_failure_threshold=?, context_window=?, is_enabled=? WHERE id=?"
        params = (name, display_name, api_format, max_retries, cooldown_seconds, cooldown_failure_threshold,
                  context_window, 1 if is_enabled else 0, model_id)
        await conn.execute(sql, params)
        await conn.commit()

    async def delete_model_raw(self, model_id: int) -> int:
        return await self.delete_model(model_id)

    async def create_group_raw(self, *, name, description="", is_enabled=True, fallback_group_id=None) -> ModelGroup:
        return await self.create_group(ModelGroup(
            name=name, description=description, is_enabled=is_enabled, fallback_group_id=fallback_group_id
        ))

    async def get_group_raw(self, group_id: int) -> Optional[ModelGroup]:
        conn = await self._ensure_connection()
        cursor = await conn.execute("SELECT * FROM model_groups WHERE id = ?", (group_id,))
        row = await cursor.fetchone()
        return self._row_to_group(row) if row else None

    async def list_groups_raw(self, enabled_only: bool = False) -> list[ModelGroup]:
        return await self.list_groups(enabled_only=enabled_only)

    async def update_group_raw(self, group_id, *, name, description, is_enabled, fallback_group_id) -> None:
        conn = await self._ensure_connection()
        await conn.execute(
            "UPDATE model_groups SET name=?, description=?, is_enabled=?, fallback_group_id=? WHERE id=?",
            (name, description, 1 if is_enabled else 0, fallback_group_id, group_id),
        )
        await conn.commit()

    async def delete_group_raw(self, group_id: int) -> int:
        return await self.delete_group(group_id)

    async def add_model_to_group_raw(self, group_id, model_id, *, weight) -> None:
        await self.add_model_to_group(group_id, model_id, weight=weight)

    async def remove_model_from_group_raw(self, group_id, model_id) -> None:
        await self.remove_model_from_group(group_id, model_id)

    async def update_model_weight_raw(self, group_id, model_id, *, weight) -> None:
        await self.update_model_weight(group_id, model_id, weight=weight)

    async def get_group_models_raw(self, group_id) -> list[GroupModelWithDetails]:
        return await self.get_group_models(group_id)

    # ------------------------------------------------------------------
    # Client API keys (multi-tenant)
    # ------------------------------------------------------------------

    @staticmethod
    def hash_key(raw_key: str) -> str:
        """Return the stable sha256 hash of a raw API key (never store raw)."""
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    async def create_api_key(self, raw_key: str, label: str = "") -> ApiKey:
        conn = await self._ensure_connection()
        cursor = await conn.execute(
            """INSERT INTO api_keys (key_hash, label, is_enabled, created_at)
               VALUES (?, ?, 1, datetime('now'))""",
            (self.hash_key(raw_key), label),
        )
        await conn.commit()
        return await self.get_api_key(cursor.lastrowid)  # type: ignore[arg-type]

    async def get_api_key(self, key_id: int) -> Optional[ApiKey]:
        conn = await self._ensure_connection()
        cursor = await conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,))
        row = await cursor.fetchone()
        return self._row_to_api_key(row) if row else None

    async def get_api_key_by_hash(self, key_hash: str) -> Optional[ApiKey]:
        conn = await self._ensure_connection()
        cursor = await conn.execute("SELECT * FROM api_keys WHERE key_hash = ?", (key_hash,))
        row = await cursor.fetchone()
        return self._row_to_api_key(row) if row else None

    async def list_api_keys(self) -> list[ApiKey]:
        conn = await self._ensure_connection()
        cursor = await conn.execute("SELECT * FROM api_keys ORDER BY id")
        rows = await cursor.fetchall()
        return [self._row_to_api_key(r) for r in rows]

    async def update_api_key(self, key_id: int, label: Optional[str] = None, is_enabled: Optional[bool] = None) -> bool:
        """Update label and/or is_enabled for an existing API key."""
        sets: list[str] = []
        values: list[Any] = []
        if label is not None:
            sets.append("label = ?")
            values.append(label)
        if is_enabled is not None:
            sets.append("is_enabled = ?")
            values.append(1 if is_enabled else 0)
        if not sets:
            return False
        values.append(key_id)
        conn = await self._ensure_connection()
        cursor = await conn.execute(f"UPDATE api_keys SET {', '.join(sets)} WHERE id = ?", values)
        await conn.commit()
        return cursor.rowcount > 0

    async def set_api_key_enabled(self, key_id: int, is_enabled: bool) -> bool:
        conn = await self._ensure_connection()
        cursor = await conn.execute(
            "UPDATE api_keys SET is_enabled = ? WHERE id = ?", (1 if is_enabled else 0, key_id)
        )
        await conn.commit()
        return cursor.rowcount > 0

    async def delete_api_key(self, key_id: int) -> bool:
        conn = await self._ensure_connection()
        cursor = await conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
        await conn.commit()
        return cursor.rowcount > 0

    def _row_to_api_key(self, row: sqlite3.Row) -> ApiKey:
        return ApiKey(
            id=row["id"],
            key_hash=row["key_hash"],
            label=row["label"],
            is_enabled=bool(row["is_enabled"]),
            created_at=row["created_at"],
        )

    # ------------------------------------------------------------------
    # Daily summaries (LLM wiki) + compressed raw sessions
    # ------------------------------------------------------------------

    async def upsert_daily_summary(self, day: str, summary_md: str, stats_json: str) -> None:
        conn = await self._ensure_connection()
        await conn.execute(
            """INSERT INTO daily_summaries (day, summary_md, stats_json, created_at)
               VALUES (?, ?, ?, datetime('now'))
               ON CONFLICT(day) DO UPDATE SET
                 summary_md = excluded.summary_md,
                 stats_json = excluded.stats_json,
                 created_at = datetime('now')""",
            (day, summary_md, stats_json),
        )
        await conn.commit()

    async def get_daily_summary(self, day: str) -> Optional[DailySummary]:
        conn = await self._ensure_connection()
        cursor = await conn.execute("SELECT * FROM daily_summaries WHERE day = ?", (day,))
        row = await cursor.fetchone()
        if not row:
            return None
        return DailySummary(
            id=row["id"], day=row["day"], summary_md=row["summary_md"],
            stats_json=row["stats_json"], created_at=row["created_at"],
        )

    async def get_call_logs_for_day(self, day: str) -> list[CallLog]:
        """All call logs for a given YYYY-MM-DD (used by daily summary job)."""
        conn = await self._ensure_connection()
        cursor = await conn.execute(
            "SELECT * FROM call_logs WHERE DATE(created_at) = ? ORDER BY created_at", (day,)
        )
        rows = await cursor.fetchall()
        return [self._row_to_call_log(r) for r in rows]

    async def save_raw_session(self, day: str, blob: bytes) -> None:
        conn = await self._ensure_connection()
        await conn.execute(
            """INSERT INTO raw_sessions (day, blob, created_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(day) DO UPDATE SET blob = excluded.blob,
                 created_at = datetime('now')""",
            (day, blob),
        )
        await conn.commit()

    async def get_raw_session(self, day: str) -> Optional[bytes]:
        conn = await self._ensure_connection()
        cursor = await conn.execute("SELECT blob FROM raw_sessions WHERE day = ?", (day,))
        row = await cursor.fetchone()
        return bytes(row["blob"]) if row else None

    async def delete_old_raw_sessions(self, cutoff_day: str) -> int:
        """Delete raw sessions older than cutoff_day (YYYY-MM-DD)."""
        conn = await self._ensure_connection()
        cursor = await conn.execute("DELETE FROM raw_sessions WHERE day < ?", (cutoff_day,))
        await conn.commit()
        return cursor.rowcount

    async def delete_old_daily_summaries(self, cutoff_day: str) -> int:
        conn = await self._ensure_connection()
        cursor = await conn.execute("DELETE FROM daily_summaries WHERE day < ?", (cutoff_day,))
        await conn.commit()
        return cursor.rowcount
