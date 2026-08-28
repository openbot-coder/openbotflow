"""Coverage for remaining db.py methods (raw CRUD, cooldown state, stats listing)."""

from __future__ import annotations

import pytest

from botflow.storage.db import Database
from botflow.storage.models import CallLog, Model, ModelGroup, Provider


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "db.db"))
    await d.initialize()
    yield d
    await d.close()


async def test_initialize_sets_active_db():
    import botflow.storage.db as dbmod
    d = Database(":memory:")
    await d.initialize()
    assert dbmod._active_db is d
    await d.close()


async def test_path_property(db):
    assert str(db.path).endswith(".db")


async def test_cooldown_state_roundtrip(db):
    await db.save_cooldown_state([{"group_id": 1, "model_id": 1, "consecutive_failures": 2, "cooldown_until": 123.0}])
    states = await db.load_cooldown_states()
    assert len(states) == 1
    assert states[0]["consecutive_failures"] == 2
    await db.clear_cooldown_states()
    assert await db.load_cooldown_states() == []


async def test_update_provider(db):
    pid = await db.create_provider(Provider(name="old", provider_type="openai"))
    await db.update_provider(pid, {"name": "new", "is_enabled": False})
    p = await db.get_provider(pid)
    assert p.name == "new" and p.is_enabled is False


async def test_create_model_and_list(db):
    pid = await db.create_provider(Provider(name="p", provider_type="openai"))
    mid = await db.create_model(Model(name="m", provider_id=pid))
    assert len(await db.list_models(provider_id=pid)) == 1
    assert len(await db.list_models()) == 1


async def test_update_group(db):
    gid = await db.create_group(ModelGroup(name="g"))
    await db.update_group(gid, {"name": "g2", "is_enabled": False})
    g = await db.get_group(gid)
    assert g.name == "g2" and g.is_enabled is False


async def test_list_groups(db):
    await db.create_group(ModelGroup(name="a"))
    await db.create_group(ModelGroup(name="b", is_enabled=False))
    assert len(await db.list_groups()) == 2
    assert len(await db.list_groups(enabled_only=True)) == 1


async def test_group_model_weight_ops(db):
    pid = await db.create_provider(Provider(name="p", provider_type="openai"))
    mid = await db.create_model(Model(name="m", provider_id=pid))
    gid = await db.create_group(ModelGroup(name="g"))
    await db.add_model_to_group(gid, mid, weight=2.0)
    await db.update_model_weight(gid, mid, weight=5.0)
    det = await db.get_group_models(gid)
    assert det[0].weight == 5.0
    await db.remove_model_from_group(gid, mid)
    assert await db.get_group_models(gid) == []


async def test_stats_listing(db):
    pid = await db.create_provider(Provider(name="p", provider_type="openai"))
    mid = await db.create_model(Model(name="m", provider_id=pid))
    gid = await db.create_group(ModelGroup(name="g"))
    await db.add_model_to_group(gid, mid, 1.0)
    await db.create_call_log(CallLog(model_id=mid, group_id=gid, status="success", cost=0.1))
    assert isinstance(await db.list_model_stats(), list)
    assert isinstance(await db.list_group_stats(), list)


async def test_raw_provider_crud(db):
    pid = await db.create_provider_raw(name="rp", type="openai", base_url="http://x", api_key="k")
    assert pid > 0
    assert (await db.get_provider_raw(pid)) is not None
    assert len(await db.list_providers_raw()) >= 1
    await db.update_provider_raw(pid, name="rp2", type="openai", base_url="http://x", api_key="k", is_enabled=True)
    assert (await db.get_provider_raw(pid)).name == "rp2"
    assert await db.delete_provider_raw(pid) >= 1


async def test_raw_model_crud(db):
    pid = await db.create_provider_raw(name="rp", type="openai", base_url="http://x", api_key="k")
    mid = await db.create_model_raw(provider_id=pid, name="rm", is_enabled=True)
    assert mid > 0
    assert (await db.get_model_raw(mid)) is not None
    assert len(await db.list_models_raw(provider_id=pid)) >= 1
    await db.update_model_raw(mid, name="rm2", is_enabled=True)
    assert (await db.get_model_raw(mid)).name == "rm2"
    assert await db.delete_model_raw(mid) >= 1


async def test_update_model_raw_keeps_existing_type(db):
    pid = await db.create_provider_raw(name="rp", type="openai", base_url="http://x", api_key="k")
    mid = await db.create_model_raw(provider_id=pid, name="rm")
    # type/base_url/api_key 已从 create_model_raw 移除（模型不再存储连接配置）
    await db.update_model_raw(mid, name="rm3")
    assert (await db.get_model_raw(mid)).name == "rm3"


async def test_raw_group_crud(db):
    gid = await db.create_group_raw(name="rg", description="d", fallback_group_id=None)
    assert gid > 0
    assert (await db.get_group_raw(gid)) is not None
    assert len(await db.list_groups_raw()) >= 1
    await db.update_group_raw(gid, name="rg2", description="d2", is_enabled=True, fallback_group_id=None)
    assert (await db.get_group_raw(gid)).name == "rg2"
    pid = await db.create_provider_raw(name="rp", type="openai", base_url="http://x", api_key="k")
    mid = await db.create_model_raw(provider_id=pid, name="rm")
    await db.add_model_to_group_raw(gid, mid, weight=3.0)
    await db.update_model_weight_raw(gid, mid, weight=9.0)
    det = await db.get_group_models_raw(gid)
    assert det[0].weight == 9.0
    await db.remove_model_from_group_raw(gid, mid)
    assert await db.get_group_models_raw(gid) == []
    assert await db.delete_group_raw(gid) >= 1


async def test_cleanup_config_by_prefix(db):
    # Insert with an old updated_at so the cleanup threshold matches.
    conn = await db._ensure_connection()
    await conn.execute("INSERT INTO config (key, value, updated_at) VALUES ('tmp:1','a', datetime('now', '-2 hours'))")
    await conn.execute("INSERT INTO config (key, value, updated_at) VALUES ('tmp:2','b', datetime('now', '-2 hours'))")
    await conn.execute("INSERT INTO config (key, value, updated_at) VALUES ('keep:1','c', datetime('now'))")
    await conn.commit()
    deleted = await db.cleanup_config_by_prefix("tmp:%", older_than_seconds=3600)
    assert deleted >= 2
    assert await db.get_config("keep:1") == "c"
    assert await db.get_config("tmp:1") is None


async def test_query_call_logs_combined_filters(db):
    pid = await db.create_provider_raw(name="rp", type="openai", base_url="http://x", api_key="k")
    mid = await db.create_model_raw(provider_id=pid, name="rm")
    from botflow.storage.models import ApiKey
    k = await db.create_api_key("secret", label="t")
    await db.create_call_log(CallLog(model_id=mid, provider_id=pid, group_id=1, status="success", api_key_id=k.id))
    await db.create_call_log(CallLog(model_id=mid, provider_id=pid, group_id=1, status="error", error_type="boom", api_key_id=k.id))
    assert len(await db.query_call_logs(status="error", error_type="boom")) == 1
    assert len(await db.query_call_logs(group_id=1, limit=1)) == 1
    assert len(await db.query_call_logs(model_id=mid, provider_id=pid, status="success")) == 1
