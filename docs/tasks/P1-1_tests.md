# P1-1 测试用例文档：DB 迁移 + Model 变更

> 对应功能点文档：`docs/tasks/P1-1_features.md`
> 子任务：P1 — DB migration + Model 变更
> 日期：2026-09-09

---

## 测试基础设施

所有测试使用 **in-memory SQLite**（`Database(":memory:")`），每次测试新建独立库，避免状态污染。

```python
@pytest.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    yield database
    await database.close()
```

---

## 一、正例

### TC-01：创建 group 指定 type/params → 持久化正确

**场景**：创建一个 `type="round_robin"`、`params={"some_key": "value"}` 的 group，读回验证。

```python
async def test_create_group_with_type_params(db):
    group = ModelGroup(name="test-group", type="round_robin", params={"some_key": "value"})
    gid = await db.create_group(group)

    result = await db.get_group(gid)
    assert result is not None
    assert result.type == "round_robin"
    assert result.params == {"some_key": "value"}
```

### TC-02：创建 group 不指定 type/params → 默认值正确

**场景**：不传 `type`/`params`，验证默认 `type="random_weights"`、`params={}`。

```python
async def test_create_group_defaults(db):
    group = ModelGroup(name="default-group")
    gid = await db.create_group(group)

    result = await db.get_group(gid)
    assert result.type == "random_weights"
    assert result.params == {}
```

### TC-03：读取 group → type/params 正确解析

**场景**：验证所有读取路径（`get_group`、`list_groups`、`find_groups_by_model_name`）都返回 `type`/`params`。

```python
async def test_read_group_type_params(db):
    group = ModelGroup(name="read-test", type="sequential", params={"order": [1, 2, 3]})
    await db.create_group(group)

    # get_group
    g1 = await db.get_group(1)
    assert g1.type == "sequential"
    assert g1.params == {"order": [1, 2, 3]}

    # list_groups
    groups = await db.list_groups()
    assert any(g.name == "read-test" and g.type == "sequential" for g in groups)

    # find_groups_by_model_name（需要创建关联的 model）
    provider = Provider(name="p1", provider_type="openai")
    pid = await db.create_provider(provider)
    model = Model(name="m1", provider_id=pid)
    mid = await db.create_model(model)
    await db.add_model_to_group(1, mid)

    found = await db.find_groups_by_model_name("m1")
    assert len(found) == 1
    assert found[0].type == "sequential"
    assert found[0].params == {"order": [1, 2, 3]}
```

### TC-04：更新 group type → 生效

**场景**：创建 group 后，更新 `type` 字段。

```python
async def test_update_group_type(db):
    gid = await db.create_group(ModelGroup(name="upd-type"))
    await db.update_group(gid, {"type": "round_robin"})

    result = await db.get_group(gid)
    assert result.type == "round_robin"
```

### TC-05：更新 group params（dict）→ 自动 json.dumps

**场景**：更新 `params` 为 dict，验证存入 DB 后读回仍是 dict。

```python
async def test_update_group_params_dict(db):
    gid = await db.create_group(ModelGroup(name="upd-params"))
    new_params = {"max_depth": 5, "strategy": "aggressive"}
    await db.update_group(gid, {"params": new_params})

    result = await db.get_group(gid)
    assert result.params == new_params
```

### TC-06：更新 group type + params 同时 → 两者都生效

**场景**：一次 update 传入 `type` 和 `params`。

```python
async def test_update_group_type_and_params(db):
    gid = await db.create_group(ModelGroup(name="upd-both"))
    await db.update_group(gid, {"type": "sequential", "params": {"order": [3, 1, 2]}})

    result = await db.get_group(gid)
    assert result.type == "sequential"
    assert result.params == {"order": [3, 1, 2]}
```

### TC-07：新库直接建表 → 两列存在

**场景**：验证 `initialize()` 创建的新库包含 `type` 和 `params` 两列。

```python
async def test_new_db_has_type_params_columns(db):
    rows = await db.execute_read("PRAGMA table_info(model_groups)")
    col_names = {row["name"] for row in rows}
    assert "type" in col_names
    assert "params" in col_names
```

### TC-08：旧库迁移 → ALTER TABLE 成功

**场景**：模拟旧库（无 `type`/`params` 列），调用 `initialize()` 后验证列存在。

```python
async def test_old_db_migration(tmp_path):
    # 用旧版 schema 创建库
    old_sql = """
    CREATE TABLE model_groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        description TEXT NOT NULL DEFAULT '',
        is_enabled INTEGER NOT NULL DEFAULT 1,
        fallback_group_id INTEGER,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    INSERT INTO model_groups (name) VALUES ('old-group');
    """
    db_path = tmp_path / "old.db"
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.executescript(old_sql)
    conn.close()

    # 用新版 initialize() 打开
    db = Database(db_path)
    await db.initialize()

    # 验证列存在
    rows = await db.execute_read("PRAGMA table_info(model_groups)")
    col_names = {row["name"] for row in rows}
    assert "type" in col_names
    assert "params" in col_names

    # 验证旧数据保留，且新列有默认值
    groups = await db.list_groups()
    assert len(groups) == 1
    assert groups[0].name == "old-group"
    assert groups[0].type == "random_weights"
    assert groups[0].params == {}

    await db.close()
```

---

## 二、反例

### TC-09：更新不存在的 group → 报错

**场景**：更新一个 id 不存在的 group，UPDATE 影响 0 行但不抛异常（SQLite 行为）。确认不会崩溃，且后续 `get_group` 返回 None。

```python
async def test_update_nonexistent_group(db):
    # update_group 不抛异常（SQLite UPDATE ... WHERE id=9999 影响 0 行）
    await db.update_group(9999, {"type": "round_robin"})
    # 确认不存在
    result = await db.get_group(9999)
    assert result is None
```

### TC-10：params 传非法 JSON 字符串 → 防御性处理

**场景**：直接向 DB 写入非法 JSON 字符串作为 `params`，读取时应防御性回退到 `{}`。

```python
async def test_params_invalid_json_defensive(db):
    # 手动写入非法 JSON
    gid = await db.create_group(ModelGroup(name="bad-json"))
    await db.execute_write(
        "UPDATE model_groups SET params = ? WHERE id = ?",
        ("NOT_VALID_JSON{", gid),
    )

    result = await db.get_group(gid)
    assert result is not None
    assert result.params == {}  # 防御性回退
```

### TC-11：type 传未知值 → 存储但后续路由时报错

**场景**：DB 层不校验 `type` 值，允许存储任意字符串。路由层（PipelineEngine）会在查找策略时报 `ConfigurationError`。本子任务只验证存储层行为。

```python
async def test_unknown_type_stored(db):
    gid = await db.create_group(ModelGroup(name="unknown", type="nonexistent_strategy"))
    result = await db.get_group(gid)
    assert result.type == "nonexistent_strategy"
    # 注意：路由层会在 PipelineEngine._create_strategy() 中报错，不在本子任务范围
```

---

## 三、边界值

### TC-12：params 为空 dict `{}` → 存储为 `'{}'`

**场景**：验证空 dict 的序列化/反序列化往返正确。

```python
async def test_params_empty_dict(db):
    gid = await db.create_group(ModelGroup(name="empty-params", params={}))
    result = await db.get_group(gid)
    assert result.params == {}

    # 验证 DB 中存储的是 '{}' 字符串
    rows = await db.execute_read("SELECT params FROM model_groups WHERE id = ?", (gid,))
    assert rows[0]["params"] == "{}"
```

### TC-13：params 为嵌套复杂 JSON → 正确序列化/反序列化

**场景**：深层嵌套 dict、列表、混合类型。

```python
async def test_params_nested_complex_json(db):
    complex_params = {
        "entry": "classify",
        "nodes": {
            "classify": {
                "type": "llm_call",
                "group": "fast",
                "system_prompt": "判断用户意图",
                "input_key": "user_message",
                "output_key": "intent"
            },
            "handler": {
                "type": "llm_call",
                "group": "code-expert",
                "input_key": "messages"
            }
        },
        "edges": [
            {"from": "classify", "to": "handler", "condition": "intent == 'code'"},
            {"from": "classify", "to": "handler", "condition": "default"}
        ],
        "nested_list": [1, [2, 3], {"a": True, "b": None}],
        "unicode": "中文测试🚀"
    }
    gid = await db.create_group(ModelGroup(name="complex", params=complex_params))
    result = await db.get_group(gid)
    assert result.params == complex_params
```

### TC-14：params 为 null/None → 存储为 `'{}'`

**场景**：Pydantic `default_factory=dict` 确保 `params=None` 不会出现在正常路径，但需测试 `_row_to_group` 防御 `params_raw` 为 None 的情况。

```python
async def test_params_none_defensive(db):
    gid = await db.create_group(ModelGroup(name="null-params"))
    # 手动将 params 设为 NULL
    await db.execute_write("UPDATE model_groups SET params = NULL WHERE id = ?", (gid,))

    result = await db.get_group(gid)
    assert result.params == {}  # None → 回退到 {}
```

### TC-15：type 为空字符串 → 存储但后续报错

**场景**：DB 层允许空字符串，路由层会在查找策略时报错。

```python
async def test_empty_type_stored(db):
    gid = await db.create_group(ModelGroup(name="empty-type", type=""))
    result = await db.get_group(gid)
    assert result.type == ""
    # 路由层 PipelineEngine._create_strategy() 会报 ConfigurationError，不在本子任务范围
```

### TC-16：list_groups_with_models 返回 type/params

**场景**：验证批量查询路径也正确返回新字段。

```python
async def test_list_groups_with_models_type_params(db):
    # 创建 provider + model
    pid = await db.create_provider(Provider(name="p1", provider_type="openai"))
    mid = await db.create_model(Model(name="m1", provider_id=pid))

    # 创建 group 并关联 model
    gid = await db.create_group(ModelGroup(
        name="list-test",
        type="round_robin",
        params={"max_retries": 2}
    ))
    await db.add_model_to_group(gid, mid)

    result = await db.list_groups_with_models()
    assert len(result) == 1
    group = result[0]
    assert group["type"] == "round_robin"
    assert group["params"] == {"max_retries": 2}
    assert group["model_names"] == ["m1"]
```

### TC-17：list_groups_with_models — params 非法 JSON 防御

**场景**：list_groups_with_models 中的 params 解析也需要防御性处理。

```python
async def test_list_groups_with_models_invalid_params(db):
    pid = await db.create_provider(Provider(name="p1", provider_type="openai"))
    mid = await db.create_model(Model(name="m1", provider_id=pid))
    gid = await db.create_group(ModelGroup(name="bad-params"))
    await db.add_model_to_group(gid, mid)

    # 手动写入非法 JSON
    await db.execute_write(
        "UPDATE model_groups SET params = ? WHERE id = ?",
        ("broken", gid),
    )

    result = await db.list_groups_with_models()
    assert len(result) == 1
    assert result[0]["params"] == {}  # 防御性回退
```

### TC-18：list_groups() 独立验证 type/params

**场景**：`list_groups()` 返回的 `ModelGroup` 包含正确的 `type`/`params`。

```python
async def test_list_groups_type_params(db):
    """list_groups 返回的 ModelGroup 包含正确的 type/params。"""
    await db.create_group(ModelGroup(name="g1", type="round_robin", params={"k": "v"}))
    await db.create_group(ModelGroup(name="g2", type="sequential", params={}))
    groups = await db.list_groups()
    g1 = next(g for g in groups if g.name == "g1")
    g2 = next(g for g in groups if g.name == "g2")
    assert g1.type == "round_robin"
    assert g1.params == {"k": "v"}
    assert g2.type == "sequential"
    assert g2.params == {}
```

### TC-19：get_group_raw() 独立验证 type/params

**场景**：`get_group_raw()` 返回的 `ModelGroup` 包含正确的 `type`/`params`。

```python
async def test_get_group_raw_type_params(db):
    """get_group_raw 返回的 ModelGroup 包含正确的 type/params。"""
    gid = await db.create_group(ModelGroup(
        name="raw-test", type="langgraph", params={"entry": "start", "nodes": {}}
    ))
    group = await db.get_group_raw(gid)
    assert group is not None
    assert group.type == "langgraph"
    assert group.params == {"entry": "start", "nodes": {}}
```

---

## 四、测试矩阵总结

| 编号 | 类别 | 场景 | 验证点 |
|------|------|------|--------|
| TC-01 | 正例 | 创建指定 type/params | 持久化正确 |
| TC-02 | 正例 | 创建不指定 type/params | 默认值 random_weights / {} |
| TC-03 | 正例 | 三种读取路径 | get / list / find_by_model |
| TC-04 | 正例 | 更新 type | 生效 |
| TC-05 | 正例 | 更新 params (dict) | json.dumps 自动处理 |
| TC-06 | 正例 | 同时更新 type + params | 两者生效 |
| TC-07 | 正例 | 新库建表 | 列存在 |
| TC-08 | 正例 | 旧库迁移 | ALTER TABLE + 旧数据保留 |
| TC-09 | 反例 | 更新不存在 group | 不崩溃 |
| TC-10 | 反例 | params 非法 JSON | 防御性回退 {} |
| TC-11 | 反例 | 未知 type | 存储但不校验 |
| TC-12 | 边界值 | params = {} | '{}' 字符串存储 |
| TC-13 | 边界值 | 嵌套复杂 JSON | 序列化/反序列化往返 |
| TC-14 | 边界值 | params = None/NULL | 防御性回退 {} |
| TC-15 | 边界值 | type = "" | 存储但后续报错 |
| TC-16 | 正例 | list_groups_with_models | 返回 type/params |
| TC-17 | 边界值 | list_groups_with_models 非法 params | 防御性回退 |
| TC-18 | 正例 | list_groups 返回 type/params | ModelGroup 字段正确 |
| TC-19 | 正例 | get_group_raw 返回 type/params | ModelGroup 字段正确 |

---

## 五、集成测试建议

### IT-01：Admin API 创建/更新 group 端到端

**场景**：通过 `POST /admin/groups` 创建带 `type`/`params` 的 group，再 `GET /admin/groups/{id}` 验证。

> 注意：此测试属于 P5 阶段（Admin API 支持 type/params），但验证数据流完整性可在 P1-1 阶段先写骨架。

### IT-02：迁移后旧数据兼容性

**场景**：用旧版 schema 建库 → 插入旧数据 → 新版 `initialize()` → 读回旧数据验证默认值 → 插入新数据验证新字段。

此测试与 TC-08 类似，但覆盖完整的「迁移 → 读旧 → 写新 → 读新」流程。

---

## 六、与现有测试的关系

| 现有测试文件 | 影响 |
|-------------|------|
| `tests/test_db.py` | 需补充 group 相关断言（`type`/`params` 默认值） |
| `tests/test_admin_api.py` | group CRUD 相关用例需补 `type`/`params` |
| `tests/test_router.py` | **不变**：`weighted_random_select` 等纯函数不涉及 group 新字段 |
| `tests/test_router_full.py` | **不变**：`GroupRouter` 不读取 `type`/`params` |
| `tests/test_group_routing.py` | **不变**：集成测试的 group 不指定 type，走默认 random_weights |
