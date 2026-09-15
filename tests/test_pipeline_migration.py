"""Tests for P1-1: DB migration + Model type/params columns.

Covers TC-01 through TC-19 from docs/tasks/P1-1_tests.md.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from botflow.storage.db import Database
from botflow.storage.models import Model, ModelGroup, Provider


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def db(tmp_path):
    """In-memory-style temp file DB for each test."""
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


# ---------------------------------------------------------------------------
# 一、正例 (TC-01 ~ TC-08)
# ---------------------------------------------------------------------------


class TestPositiveCases:
    """TC-01 to TC-08: positive / happy-path tests."""

    # TC-01: 创建 group 指定 type/params → 持久化正确
    async def test_TC01_create_group_with_type_params(self, db):
        group = ModelGroup(name="test-group", type="round_robin", params={"some_key": "value"})
        gid = await db.create_group(group)

        result = await db.get_group(gid)
        assert result is not None
        assert result.type == "round_robin"
        assert result.params == {"some_key": "value"}

    # TC-02: 创建 group 不指定 type/params → 默认值正确
    async def test_TC02_create_group_defaults(self, db):
        group = ModelGroup(name="default-group")
        gid = await db.create_group(group)

        result = await db.get_group(gid)
        assert result.type == "random_weights"
        assert result.params == {}

    # TC-03: 读取 group → type/params 在 get/list/find 三条路径均正确
    async def test_TC03_read_group_type_params(self, db):
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

    # TC-04: 更新 group type → 生效
    async def test_TC04_update_group_type(self, db):
        gid = await db.create_group(ModelGroup(name="upd-type"))
        await db.update_group(gid, {"type": "round_robin"})

        result = await db.get_group(gid)
        assert result.type == "round_robin"

    # TC-05: 更新 group params（dict）→ 自动 json.dumps
    async def test_TC05_update_group_params_dict(self, db):
        gid = await db.create_group(ModelGroup(name="upd-params"))
        new_params = {"max_depth": 5, "strategy": "aggressive"}
        await db.update_group(gid, {"params": new_params})

        result = await db.get_group(gid)
        assert result.params == new_params

    # TC-06: 更新 group type + params 同时 → 两者都生效
    async def test_TC06_update_group_type_and_params(self, db):
        gid = await db.create_group(ModelGroup(name="upd-both"))
        await db.update_group(gid, {"type": "sequential", "params": {"order": [3, 1, 2]}})

        result = await db.get_group(gid)
        assert result.type == "sequential"
        assert result.params == {"order": [3, 1, 2]}

    # TC-07: 新库直接建表 → type/params 两列存在
    async def test_TC07_new_db_has_type_params_columns(self, db):
        rows = await db.execute_read("PRAGMA table_info(model_groups)")
        col_names = {row["name"] for row in rows}
        assert "type" in col_names
        assert "params" in col_names

    # TC-08: 旧库迁移 → ALTER TABLE 成功 + 旧数据保留
    async def test_TC08_old_db_migration(self, tmp_path):
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


# ---------------------------------------------------------------------------
# 二、反例 (TC-09 ~ TC-11)
# ---------------------------------------------------------------------------


class TestNegativeCases:
    """TC-09 to TC-11: defensive / error-handling tests."""

    # TC-09: 更新不存在的 group → 不崩溃
    async def test_TC09_update_nonexistent_group(self, db):
        # update_group 不抛异常（SQLite UPDATE ... WHERE id=9999 影响 0 行）
        await db.update_group(9999, {"type": "round_robin"})
        # 确认不存在
        result = await db.get_group(9999)
        assert result is None

    # TC-10: params 传非法 JSON 字符串 → 防御性回退到 {}
    async def test_TC10_params_invalid_json_defensive(self, db):
        gid = await db.create_group(ModelGroup(name="bad-json"))
        # 手动写入非法 JSON
        await db.execute_write(
            "UPDATE model_groups SET params = ? WHERE id = ?",
            ("NOT_VALID_JSON{", gid),
        )

        result = await db.get_group(gid)
        assert result is not None
        assert result.params == {}  # 防御性回退

    # TC-11: type 传未知值 → 存储但不校验
    async def test_TC11_unknown_type_stored(self, db):
        gid = await db.create_group(ModelGroup(name="unknown", type="nonexistent_strategy"))
        result = await db.get_group(gid)
        assert result.type == "nonexistent_strategy"


# ---------------------------------------------------------------------------
# 三、边界值 (TC-12 ~ TC-19)
# ---------------------------------------------------------------------------


class TestBoundaryCases:
    """TC-12 to TC-19: boundary / edge-case tests."""

    # TC-12: params 为空 dict → 存储为 '{}' 字符串
    async def test_TC12_params_empty_dict(self, db):
        gid = await db.create_group(ModelGroup(name="empty-params", params={}))
        result = await db.get_group(gid)
        assert result.params == {}

        # 验证 DB 中存储的是 '{}' 字符串
        rows = await db.execute_read("SELECT params FROM model_groups WHERE id = ?", (gid,))
        assert rows[0]["params"] == "{}"

    # TC-13: params 为嵌套复杂 JSON → 正确序列化/反序列化
    async def test_TC13_params_nested_complex_json(self, db):
        complex_params = {
            "entry": "classify",
            "nodes": {
                "classify": {
                    "type": "llm_call",
                    "group": "fast",
                    "system_prompt": "判断用户意图",
                    "input_key": "user_message",
                    "output_key": "intent",
                },
                "handler": {
                    "type": "llm_call",
                    "group": "code-expert",
                    "input_key": "messages",
                },
            },
            "edges": [
                {"from": "classify", "to": "handler", "condition": "intent == 'code'"},
                {"from": "classify", "to": "handler", "condition": "default"},
            ],
            "nested_list": [1, [2, 3], {"a": True, "b": None}],
            "unicode": "中文测试🚀",
        }
        gid = await db.create_group(ModelGroup(name="complex", params=complex_params))
        result = await db.get_group(gid)
        assert result.params == complex_params

    # TC-14: DB 中 params 为损坏数据 → 防御性回退到 {}
    async def test_TC14_params_corrupted_defensive(self, db):
        gid = await db.create_group(ModelGroup(name="corrupted-params"))
        # 手动写入非法 JSON（模拟损坏数据）
        import sqlite3
        conn = await db._ensure_connection()
        await conn.execute(
            "UPDATE model_groups SET params = 'not-valid-json{{{' WHERE id = ?", (gid,)
        )
        await conn.commit()

        result = await db.get_group(gid)
        assert result.params == {}  # 损坏 JSON → 回退到 {}

    # TC-15: type 为空字符串 → 存储但不报错
    async def test_TC15_empty_type_stored(self, db):
        gid = await db.create_group(ModelGroup(name="empty-type", type=""))
        result = await db.get_group(gid)
        assert result.type == ""

    # TC-16: list_groups_with_models 返回 type/params
    async def test_TC16_list_groups_with_models_type_params(self, db):
        # 创建 provider + model
        pid = await db.create_provider(Provider(name="p1", provider_type="openai"))
        mid = await db.create_model(Model(name="m1", provider_id=pid))

        # 创建 group 并关联 model
        gid = await db.create_group(ModelGroup(
            name="list-test",
            type="round_robin",
            params={"max_retries": 2},
        ))
        await db.add_model_to_group(gid, mid)

        result = await db.list_groups_with_models()
        assert len(result) == 1
        group = result[0]
        assert group["type"] == "round_robin"
        assert group["params"] == {"max_retries": 2}
        assert group["model_names"] == ["m1"]

    # TC-17: list_groups_with_models — params 非法 JSON 防御
    async def test_TC17_list_groups_with_models_invalid_params(self, db):
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

    # TC-18: list_groups() 独立验证 type/params
    async def test_TC18_list_groups_type_params(self, db):
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

    # TC-19: get_group_raw() 独立验证 type/params
    async def test_TC19_get_group_raw_type_params(self, db):
        """get_group_raw 返回的 ModelGroup 包含正确的 type/params。"""
        gid = await db.create_group(ModelGroup(
            name="raw-test", type="langgraph", params={"entry": "start", "nodes": {}},
        ))
        group = await db.get_group_raw(gid)
        assert group is not None
        assert group.type == "langgraph"
        assert group.params == {"entry": "start", "nodes": {}}
