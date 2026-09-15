# P1-1 功能点文档：DB 迁移 + Model 变更

> 对应设计文档：`docs/pipeline_router_design.md` v2.0
> 子任务：P1 — DB migration + Model 变更
> 日期：2026-09-09

---

## 概述

在 `model_groups` 表和 `ModelGroup` 模型上新增 `type`（路由策略类型）和 `params`（策略参数 JSON）两个字段，为 Pipeline Router 重构提供数据基础。

---

## 功能点 1：`CREATE_TABLES_SQL` 加 `type`/`params` 列

### 改动位置

- 文件：`src/botflow/storage/db.py`
- 行号范围：70-79（`model_groups` 表的 CREATE 语句）

### 改动前的代码

```sql
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
```

### 改动后的代码

```sql
-- Model Groups
CREATE TABLE IF NOT EXISTS model_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL DEFAULT 'random_weights',
    params TEXT NOT NULL DEFAULT '{}',
    is_enabled INTEGER NOT NULL DEFAULT 1,
    fallback_group_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

### 改动原因

全新安装的数据库需要在建表时就包含 `type` 和 `params` 两列。`type` 默认 `random_weights` 确保所有新建 group 向后兼容（不指定 type 时行为与重构前一致）。`params` 默认 `'{}'` 避免 NULL。

---

## 功能点 2：旧库迁移 — try SELECT → ALTER TABLE

### 改动位置

- 文件：`src/botflow/storage/db.py`
- 行号范围：200-221（`initialize()` 方法中，现有 migration 代码块之后）

### 改动前的代码

```python
# Migrations for call_logs (new audit columns)
for col in ALTER_CALL_LOGS_COLUMNS:
    col_name = col.split()[0]
    try:
        await self._conn.execute(f"SELECT {col_name} FROM call_logs LIMIT 1")
    except sqlite3.OperationalError:  # UNCOVERED: 旧库迁移路径
        await self._conn.execute(f"ALTER TABLE call_logs ADD COLUMN {col}")  # UNCOVERED

await self._conn.executescript(CREATE_INDEXES_SQL)
await self._conn.commit()
```

### 改动后的代码

```python
# Migrations for call_logs (new audit columns)
for col in ALTER_CALL_LOGS_COLUMNS:
    col_name = col.split()[0]
    try:
        await self._conn.execute(f"SELECT {col_name} FROM call_logs LIMIT 1")
    except sqlite3.OperationalError:  # UNCOVERED: 旧库迁移路径
        await self._conn.execute(f"ALTER TABLE call_logs ADD COLUMN {col}")  # UNCOVERED

# Migrations for model_groups (new type/params columns)
try:
    await self._conn.execute("SELECT type FROM model_groups LIMIT 1")
except sqlite3.OperationalError:  # UNCOVERED: 旧库迁移路径，全新数据库已含该列，无法单元测试触发
    await self._conn.execute("ALTER TABLE model_groups ADD COLUMN type TEXT NOT NULL DEFAULT 'random_weights'")  # UNCOVERED
    await self._conn.execute("ALTER TABLE model_groups ADD COLUMN params TEXT NOT NULL DEFAULT '{}'")  # UNCOVERED

await self._conn.executescript(CREATE_INDEXES_SQL)
await self._conn.commit()
```

### 改动原因

已有数据库（升级用户）没有 `type`/`params` 列。采用与 `context_window`/`api_format` 迁移完全一致的模式：先 `SELECT` 检测列是否存在，不存在则 `ALTER TABLE ADD COLUMN`。只检查 `type` 列，如果存在则认为 `params` 也已存在（两个列在同一次迁移中添加）。加 `# UNCOVERED` 标注，因为全新数据库永远存在该列，无法在单元测试中触发此路径。

---

## 功能点 3：`ModelGroup` 加 `type`/`params` 字段

### 改动位置

- 文件：`src/botflow/storage/models.py`
- 行号范围：43-52（`ModelGroup` 类定义）

### 改动前的代码

```python
class ModelGroup(BaseModel):
    """Model group for weighted routing."""

    id: int = 0
    name: str
    description: str = ""
    is_enabled: bool = True
    fallback_group_id: Optional[int] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
```

### 改动后的代码

```python
class ModelGroup(BaseModel):
    """Model group for weighted routing."""

    id: int = 0
    name: str
    description: str = ""
    type: str = "random_weights"
    params: dict[str, Any] = Field(default_factory=dict)
    is_enabled: bool = True
    fallback_group_id: Optional[int] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
```

### 改动原因

Python 模型需要与 DB schema 对齐。`type` 默认 `"random_weights"` 确保不指定 type 的现有代码不受影响。`params` 用 `dict[str, Any]` 而非 `str`，因为 Python 层面应该处理结构化数据，序列化/反序列化由 DB 层（`json.dumps`/`json.loads`）负责。

---

## 功能点 4：`_row_to_group()` 解析新字段 + JSON 防御

### 改动位置

- 文件：`src/botflow/storage/db.py`
- 行号范围：579-586（`_row_to_group` 方法）

### 改动前的代码

```python
def _row_to_group(self, row: sqlite3.Row) -> ModelGroup:
    return ModelGroup(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        is_enabled=bool(row["is_enabled"]),
        fallback_group_id=row["fallback_group_id"],
    )
```

### 改动后的代码

```python
def _row_to_group(self, row: sqlite3.Row) -> ModelGroup:
    params_raw = row["params"]
    try:
        params = json.loads(params_raw) if params_raw else {}
    except (json.JSONDecodeError, TypeError):
        params = {}
    return ModelGroup(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        type=row["type"],
        params=params,
        is_enabled=bool(row["is_enabled"]),
        fallback_group_id=row["fallback_group_id"],
    )
```

### 改动原因

所有从 DB 读取 group 的路径（`get_group`、`list_groups`、`find_groups_by_model_name`）都经过 `_row_to_group`。此方法是读取的唯一瓶颈，必须在此处完成 `type`/`params` 的解析。`params` 是 JSON 字符串，需要 `json.loads` 转为 dict；防御性 try/except 处理损坏数据（如手动编辑 DB 导致非法 JSON），回退到空 dict 而非崩溃。

---

## 功能点 5：`create_group()` INSERT 补新列

### 改动位置

- 文件：`src/botflow/storage/db.py`
- 行号范围：532-539（`create_group` 方法）

### 改动前的代码

```python
async def create_group(self, group: ModelGroup) -> int:
    conn = await self._ensure_connection()
    cursor = await conn.execute(
        "INSERT INTO model_groups (name, description, is_enabled) VALUES (?, ?, ?)",
        (group.name, group.description, 1 if group.is_enabled else 0),
    )
    await conn.commit()
    return cursor.lastrowid  # type: ignore[return-value]
```

### 改动后的代码

```python
async def create_group(self, group: ModelGroup) -> int:
    conn = await self._ensure_connection()
    cursor = await conn.execute(
        "INSERT INTO model_groups (name, description, type, params, is_enabled) VALUES (?, ?, ?, ?, ?)",
        (group.name, group.description, group.type, json.dumps(group.params), 1 if group.is_enabled else 0),
    )
    await conn.commit()
    return cursor.lastrowid  # type: ignore[return-value]
```

### 改动原因

INSERT 语句必须包含新增的 `type` 和 `params` 列，否则新字段值不会被持久化。`params` 在 Python 层是 dict，存入 SQLite 时需要 `json.dumps` 序列化为 JSON 字符串。

---

## 功能点 6：`_GROUP_UPDATE_COLUMNS` 白名单扩展

### 改动位置

- 文件：`src/botflow/storage/db.py`
- 行号：541

### 改动前的代码

```python
_GROUP_UPDATE_COLUMNS = {"name", "description", "is_enabled", "fallback_group_id"}
```

### 改动后的代码

```python
_GROUP_UPDATE_COLUMNS = {"name", "description", "is_enabled", "fallback_group_id", "type", "params"}
```

### 改动原因

`update_group()` 方法使用此白名单校验传入的 key 是否合法。如果不扩展，传入 `type` 或 `params` 会抛出 `ValueError("Invalid column for group update: ...")`，导致无法更新新字段。

---

## 功能点 7：`update_group()` params 自动 json.dumps

### 改动位置

- 文件：`src/botflow/storage/db.py`
- 行号范围：543-552（`update_group` 方法）

### 改动前的代码

```python
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
```

### 改动后的代码

```python
async def update_group(self, group_id: int, updates: dict[str, Any]) -> None:
    conn = await self._ensure_connection()
    for key in updates:
        if key not in self._GROUP_UPDATE_COLUMNS:
            raise ValueError(f"Invalid column for group update: {key}")
    sets = []
    values = []
    for key, value in updates.items():
        if key == "params":
            # params 必须是 dict 或 None；若已为 JSON 字符串则直接存储
            value = json.dumps(value) if isinstance(value, dict) else (value or '{}')
        sets.append(f"{key} = ?")
        values.append(value)
    sets.append("updated_at = datetime('now')")
    values.append(group_id)
    await conn.execute(f"UPDATE model_groups SET {', '.join(sets)} WHERE id = ?", values)
    await conn.commit()
```

### 改动原因

与 `update_provider`/`update_model` 中 `extra_config` 的处理模式一致：Python 层传入 dict，存入 DB 前自动 `json.dumps`。如果不做转换，直接将 dict 写入 SQLite TEXT 列会存储为 Python repr 字符串（如 `{'key': 'value'}`），导致读取时 `json.loads` 失败。同时参考 `update_provider()`（行 361-375）对 `extra_config` 的处理方式，保持代码风格一致。

---

## 功能点 8：`list_groups_with_models()` 返回补全 type/params

### 改动位置

- 文件：`src/botflow/storage/db.py`
- 行号范围：610-649（`list_groups_with_models` 方法）

### 改动前的代码

```python
async def list_groups_with_models(self, enabled_only: bool = True) -> list[dict[str, Any]]:
    conn = await self._ensure_connection()
    sql = """
        SELECT
            mg.id, mg.name, mg.description, mg.is_enabled,
            m.name AS model_name
        FROM model_groups mg
        ...
    """
    ...
    for row in rows:
        gid = row["id"]
        if gid not in groups_map:
            groups_map[gid] = {
                "id": gid,
                "name": row["name"],
                "description": row["description"],
                "is_enabled": bool(row["is_enabled"]),
                "model_names": [],
            }
        groups_map[gid]["model_names"].append(row["model_name"])
    return list(groups_map.values())
```

### 改动后的代码

```python
async def list_groups_with_models(self, enabled_only: bool = True) -> list[dict[str, Any]]:
    conn = await self._ensure_connection()
    sql = """
        SELECT
            mg.id, mg.name, mg.description, mg.type, mg.params, mg.is_enabled,
            m.name AS model_name
        FROM model_groups mg
        ...
    """
    ...
    for row in rows:
        gid = row["id"]
        if gid not in groups_map:
            params_raw = row["params"]
            try:
                params = json.loads(params_raw) if params_raw else {}
            except (json.JSONDecodeError, TypeError):
                params = {}
            groups_map[gid] = {
                "id": gid,
                "name": row["name"],
                "description": row["description"],
                "type": row["type"],
                "params": params,
                "is_enabled": bool(row["is_enabled"]),
                "model_names": [],
            }
        groups_map[gid]["model_names"].append(row["model_name"])
    return list(groups_map.values())
```

### 改动原因

`list_groups_with_models()` 是 `/v1/models` 端点的数据源，返回的 dict 直接暴露给 API 消费者。必须补全 `type` 和 `params` 字段，否则前端/客户端无法获取 group 的路由策略信息。SQL SELECT 也需加入 `mg.type, mg.params`，否则 `row["type"]` 会抛 `IndexError`。`params` JSON 解析采用与 `_row_to_group` 相同的防御性逻辑。

---

## 附：`create_group_raw` / `update_group_raw` 同步更新

虽然不在核心 8 个功能点中，但 Admin API 依赖的便捷方法也需同步更新：

### `create_group_raw`（行 1037-1040）

需加 `type="random_weights"`, `params=None` 参数：

```python
# 改动后
async def create_group_raw(self, *, name, description="", is_enabled=True,
                           fallback_group_id=None, type="random_weights", params=None) -> ModelGroup:
    return await self.create_group(ModelGroup(
        name=name, description=description, type=type,
        params=params if params is not None else {},
        is_enabled=is_enabled, fallback_group_id=fallback_group_id
    ))
```

### `update_group_raw`（行 1051-1057）

需加 `type`/`params` 参数：

```python
# 改动后
async def update_group_raw(self, group_id, *, name, description, is_enabled,
                           fallback_group_id, type="random_weights", params=None) -> None:
    conn = await self._ensure_connection()
    await conn.execute(
        "UPDATE model_groups SET name=?, description=?, type=?, params=?, is_enabled=?, fallback_group_id=? WHERE id=?",
        # params 必须是 dict 或 None；若已为 JSON 字符串则直接存储
        (name, description, type, json.dumps(params) if isinstance(params, dict) else (params or '{}'),
         1 if is_enabled else 0, fallback_group_id, group_id),
    )
    await conn.commit()
```

---

## 变更影响范围

| 调用者 | 影响 |
|--------|------|
| `admin_api.py` — group CRUD | 需支持 `type`/`params` 参数 |
| `router.py` — `GroupRouter` | 不变：不读取 `type`/`params`（Phase P7 才 deprecated） |
| `core.py` — `_get_group_id` | 不变：只做 model name → group id 映射 |
| `/v1/models` 端点 | 通过 `list_groups_with_models()` 自动获得新字段 |
| 现有测试 | group 的断言需补全 `type`/`params` 默认值 |
