"""补充覆盖：storage.db 的零散分支缺口。

- 模块级 ``get_db()`` 未初始化时的 RuntimeError
- ``load_cooldown_states`` 对脏数据（key 非法 / value 非 JSON）的容错
- ``update_provider`` / ``update_model`` / ``update_group`` 的非法列校验与 JSON 序列化
- ``upsert_model`` 的「已存在则更新 / 不存在则插入」两条路径
- ``_row_to_group_model_detail`` 对非法 model_extra_config 的容错
- ``get_api_key_by_hash`` 命中与未命中
- ``update_api_key`` 在无任何字段时的短路返回
"""

from __future__ import annotations

import json

import pytest

from botflow.storage import db as dbmod
from botflow.storage.db import Database
from botflow.storage.models import Model, ModelGroup, Provider


@pytest.fixture
async def db(tmp_path):
    """A real, schema-initialised Database that is always closed afterwards."""
    instance = Database(tmp_path / "data" / "botflow.db")
    await instance.initialize()
    yield instance
    await instance.close()


# ---------------------------------------------------------------------------
# module-level get_db()
# ---------------------------------------------------------------------------


def test_module_get_db_raises_when_not_initialized(monkeypatch):
    monkeypatch.setattr(dbmod, "_active_db", None)
    with pytest.raises(RuntimeError, match="not initialized"):
        dbmod.get_db()


# ---------------------------------------------------------------------------
# load_cooldown_states — dirty rows
# ---------------------------------------------------------------------------


async def test_load_cooldown_states_skips_malformed_rows(db):
    await db.set_config("cooldown:not-an-int:1", json.dumps({"failures": 1, "cooldown_until": 0.0}))
    await db.set_config("cooldown:1:2", "definitely-not-json")
    await db.set_config("cooldown:9:9", json.dumps({"failures": 3, "cooldown_until": 12.5}))
    await db.set_config("unrelated", "x")

    states = await db.load_cooldown_states()
    assert states == [
        {"group_id": 9, "model_id": 9, "consecutive_failures": 3, "cooldown_until": 12.5}
    ]


# ---------------------------------------------------------------------------
# update_provider
# ---------------------------------------------------------------------------


async def test_update_provider_rejects_unknown_column(db):
    pid = await db.create_provider(Provider(name="p", provider_type="openai"))
    with pytest.raises(ValueError, match="Invalid column for provider update"):
        await db.update_provider(pid, {"bogus": 1})


async def test_update_provider_serialises_extra_config(db):
    pid = await db.create_provider(Provider(name="p", provider_type="openai"))
    await db.update_provider(pid, {"extra_config": {"proxy": "http://proxy.local:1"}})
    stored = await db.get_provider(pid)
    assert stored.extra_config == {"proxy": "http://proxy.local:1"}


# ---------------------------------------------------------------------------
# update_model / upsert_model
# ---------------------------------------------------------------------------


async def test_update_model_rejects_unknown_column(db):
    pid = await db.create_provider(Provider(name="p", provider_type="openai"))
    mid = await db.create_model(Model(name="m", provider_id=pid))
    with pytest.raises(ValueError, match="Invalid column for model update"):
        await db.update_model(mid, {"bogus": 1})


async def test_upsert_model_inserts_then_updates_same_row(db):
    pid = await db.create_provider(Provider(name="p", provider_type="openai"))

    mid = await db.upsert_model(
        Model(name="m1", provider_id=pid, display_name="first", api_format="openai")
    )
    assert mid

    same_mid = await db.upsert_model(
        Model(name="m1", provider_id=pid, display_name="second", api_format="deepseek")
    )
    assert same_mid == mid

    stored = await db.get_model(mid)
    assert stored.display_name == "second"
    assert stored.api_format == "deepseek"
    # upsert must not create a duplicate row.
    assert len(await db.list_models(provider_id=pid)) == 1


# ---------------------------------------------------------------------------
# update_group
# ---------------------------------------------------------------------------


async def test_update_group_rejects_unknown_column(db):
    gid = await db.create_group(ModelGroup(name="g"))
    with pytest.raises(ValueError, match="Invalid column for group update"):
        await db.update_group(gid, {"bogus": 1})


# ---------------------------------------------------------------------------
# _row_to_group_model_detail
# ---------------------------------------------------------------------------


def _detail_row(**overrides):
    row = {
        "id": 1,
        "group_id": 1,
        "model_id": 2,
        "weight": 1.0,
        "is_enabled": 1,
        "model_name": "m",
        "display_name": "M",
        "api_format": "openai",
        "provider_id": 3,
        "provider_name": "p",
        "provider_type": "openai",
        "max_retries": 2,
        "cooldown_seconds": 30,
        "cooldown_failure_threshold": 3,
        "context_window": 8192,
        "model_extra_config": None,
    }
    row.update(overrides)
    return row


def test_row_to_group_model_detail_tolerates_invalid_json():
    db = Database(":memory:")
    detail = db._row_to_group_model_detail(
        _detail_row(model_extra_config="{not valid json")
    )
    assert detail.model_id == 2
    assert detail.proxy == ""
    assert detail.extra_config == {}


def test_row_to_group_model_detail_reads_proxy_from_extra_config():
    db = Database(":memory:")
    detail = db._row_to_group_model_detail(
        _detail_row(model_extra_config=json.dumps({"proxy": "http://p:1"}))
    )
    assert detail.proxy == "http://p:1"
    assert detail.extra_config == {"proxy": "http://p:1"}


# ---------------------------------------------------------------------------
# api key lookups
# ---------------------------------------------------------------------------


async def test_get_api_key_by_hash_hit_and_miss(db):
    created = await db.create_api_key("raw-client-key", label="c")
    found = await db.get_api_key_by_hash(created.key_hash)
    assert found is not None
    assert found.id == created.id
    assert await db.get_api_key_by_hash("no-such-hash") is None


async def test_update_api_key_without_fields_returns_false(db):
    created = await db.create_api_key("raw-client-key-2", label="c")
    assert await db.update_api_key(created.id) is False
    assert await db.update_api_key(created.id, label="renamed") is True
