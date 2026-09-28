"""admin_panel 6 项需求：设置端点（§6.1）+ 单供应商同步端点（§6.2）。

审核依据：docs/tasks/admin_panel_features.md §6 用例 1-23。
fixture 照抄 tests/test_admin_api.py（TestClient + dependency_overrides[get_db]），
额外保存/还原 botflow.config 单例 —— PATCH 会对内存单例 setattr，不还原会把
改过的值泄漏给后续测试文件。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import botflow.config as config_mod
import botflow.core as core_mod
from botflow.admin_api import admin_router
from botflow.config import ADMIN_SETTINGS_FIELDS, BotflowSettings, set_config
from botflow.storage import db as dbmod
from botflow.storage.db import Database


@pytest.fixture
def client(tmp_path):
    d = Database(str(tmp_path / "test.db"))
    asyncio.new_event_loop().run_until_complete(d.initialize())
    original_config = config_mod._config
    set_config(BotflowSettings(admin_key="admin-secret"))
    app = FastAPI()
    app.include_router(admin_router)
    app.dependency_overrides[dbmod.get_db] = lambda: d
    with TestClient(app) as c:
        yield c
    asyncio.new_event_loop().run_until_complete(d.close())
    config_mod._config = original_config


AUTH = {"Authorization": "Bearer admin-secret"}


def _get_db(client) -> Database:
    return client.app.dependency_overrides[dbmod.get_db]()


# ===========================================================================
# §6.1 设置端点（用例 1-13）
# ===========================================================================


class TestGetSettings:
    def test_get_settings_returns_exactly_nine_keys(self, client):
        """用例1（正例）：200；settings 键集恰为 9 键；值 == 默认值。"""
        r = client.get("/admin/settings", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert set(body["settings"].keys()) == set(ADMIN_SETTINGS_FIELDS)
        assert len(body["settings"]) == 9
        defaults = BotflowSettings()
        for k, v in body["settings"].items():
            assert v == getattr(defaults, k), f"{k} 应为默认值"

    def test_get_settings_requires_admin_key(self, client):
        """用例2（反例）：无 Authorization → 401 Invalid admin key."""
        r = client.get("/admin/settings")
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid admin key."

    def test_get_settings_never_leaks_secrets(self, client):
        """用例3（反例）：响应全文不含任何密钥字段。"""
        r = client.get("/admin/settings", headers=AUTH)
        text = json.dumps(r.json())
        for secret in ("admin_key", "llm_key", "api_keys", "pwd_hash"):
            assert secret not in text, f"设置响应不得泄漏 {secret}"


class TestPatchSettings:
    def test_patch_settings_updates_kv_and_memory(self, client):
        """用例4（正例）：PATCH → 响应回显 + KV 持久化 + 内存单例立即生效。"""
        r = client.patch("/admin/settings", json={"stream_timeout": 45.5}, headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert body["settings"]["stream_timeout"] == 45.5
        raw = asyncio.new_event_loop().run_until_complete(
            _get_db(client).get_config("admin_settings"))
        assert json.loads(raw) == {"stream_timeout": 45.5}
        assert config_mod.get_config().stream_timeout == 45.5

    def test_patch_settings_partial_only_touches_sent_keys(self, client):
        """用例5（边界）：两次 PATCH 各写一键 → 已存键保留、未触键默认。"""
        assert client.patch("/admin/settings", json={"log_level": "DEBUG"},
                            headers=AUTH).status_code == 200
        assert client.patch("/admin/settings", json={"call_log_detail_days": 3},
                            headers=AUTH).status_code == 200
        raw = asyncio.new_event_loop().run_until_complete(
            _get_db(client).get_config("admin_settings"))
        stored = json.loads(raw)
        assert stored["log_level"] == "DEBUG"
        assert stored["call_log_detail_days"] == 3
        # 未发送的键不落 KV，内存值仍为默认
        assert "stream_timeout" not in stored
        assert config_mod.get_config().stream_timeout == BotflowSettings().stream_timeout

    def test_patch_settings_rejects_bad_log_level(self, client):
        """用例6（反例）：非枚举 log_level → 422，loc 含 log_level。"""
        r = client.patch("/admin/settings", json={"log_level": "VERBOSE"}, headers=AUTH)
        assert r.status_code == 422
        assert "log_level" in r.json()["detail"][0]["loc"]

    def test_patch_settings_daily_summary_hour_bounds(self, client):
        """用例7（边界）：hour -1/24 → 422；0/23 → 200。"""
        for bad in (-1, 24):
            r = client.patch("/admin/settings", json={"daily_summary_hour": bad},
                             headers=AUTH)
            assert r.status_code == 422, f"{bad} 应被拒"
            assert "daily_summary_hour" in r.json()["detail"][0]["loc"]
        for good in (0, 23):
            r = client.patch("/admin/settings", json={"daily_summary_hour": good},
                             headers=AUTH)
            assert r.status_code == 200, f"{good} 应通过"

    def test_patch_settings_stream_timeout_must_be_positive(self, client):
        """用例8（边界）：stream_timeout 0/-0.5 → 422；0.5 → 200。"""
        for bad in (0, -0.5):
            r = client.patch("/admin/settings", json={"stream_timeout": bad},
                             headers=AUTH)
            assert r.status_code == 422, f"{bad} 应被拒"
        r = client.patch("/admin/settings", json={"stream_timeout": 0.5}, headers=AUTH)
        assert r.status_code == 200

    @pytest.mark.parametrize("field", [
        "upstream_semaphore_size", "call_log_detail_days",
        "raw_session_retention_days", "call_logs_retention_days",
        "model_sync_interval",
    ])
    def test_patch_settings_non_negative_ints(self, client, field):
        """用例9（反例，参数化）：5 个 int 字段传 -1 → 422。"""
        r = client.patch("/admin/settings", json={field: -1}, headers=AUTH)
        assert r.status_code == 422, f"{field}=-1 应被拒"
        assert field in r.json()["detail"][0]["loc"]

    def test_patch_settings_empty_body_is_noop(self, client):
        """用例10（边界）：{} → 200，不写 KV、不改任何值。"""
        before = client.get("/admin/settings", headers=AUTH).json()["settings"]
        r = client.patch("/admin/settings", json={}, headers=AUTH)
        assert r.status_code == 200
        raw = asyncio.new_event_loop().run_until_complete(
            _get_db(client).get_config("admin_settings"))
        assert raw is None, "空 body 不得写 KV"
        after = client.get("/admin/settings", headers=AUTH).json()["settings"]
        assert after == before

    def test_patch_settings_summary_group_empty_string_saved(self, client):
        """用例11（边界）：summary_group "" 合法 = 显式存空串（≠ 不改）。"""
        r = client.patch("/admin/settings", json={"summary_group": ""}, headers=AUTH)
        assert r.status_code == 200
        raw = asyncio.new_event_loop().run_until_complete(
            _get_db(client).get_config("admin_settings"))
        assert json.loads(raw) == {"summary_group": ""}
        assert r.json()["settings"]["summary_group"] == ""

    def test_patch_settings_null_field_means_unchanged(self, client):
        """用例12（边界）：显式 null = 不改（exclude_none），同报其它键更新。"""
        before = client.get("/admin/settings", headers=AUTH).json()["settings"]
        r = client.patch("/admin/settings",
                         json={"stream_timeout": None, "log_level": "WARNING"},
                         headers=AUTH)
        assert r.status_code == 200
        after = r.json()["settings"]
        assert after["stream_timeout"] == before["stream_timeout"]
        assert after["log_level"] == "WARNING"
        raw = asyncio.new_event_loop().run_until_complete(
            _get_db(client).get_config("admin_settings"))
        assert json.loads(raw) == {"log_level": "WARNING"}

    def test_patch_settings_requires_admin_key(self, client):
        """用例13（反例）：无 key → 401。"""
        r = client.patch("/admin/settings", json={"log_level": "DEBUG"})
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid admin key."

    def test_patch_settings_corrupt_kv_rebuilds_from_change(self, client):
        """补充（覆盖率必需）：KV 被手改坏（非法 JSON）→ PATCH 不 500，
        以本次变更为准重建（admin_api.update_settings 的 except ValueError 分支）。"""
        db = _get_db(client)
        asyncio.new_event_loop().run_until_complete(
            db.set_config("admin_settings", "{not json"))
        r = client.patch("/admin/settings", json={"log_level": "ERROR"}, headers=AUTH)
        assert r.status_code == 200
        raw = asyncio.new_event_loop().run_until_complete(
            db.get_config("admin_settings"))
        assert json.loads(raw) == {"log_level": "ERROR"}

    def test_patch_settings_non_dict_kv_rebuilds_from_change(self, client):
        """补充（覆盖率必需）：KV 是合法 JSON 但非 dict → 以本次变更为准重建
        （update_settings 的 isinstance(stored, dict) 兜底分支）。"""
        db = _get_db(client)
        asyncio.new_event_loop().run_until_complete(
            db.set_config("admin_settings", '["a", "b"]'))
        r = client.patch("/admin/settings", json={"model_sync_interval": 5},
                         headers=AUTH)
        assert r.status_code == 200
        raw = asyncio.new_event_loop().run_until_complete(
            db.get_config("admin_settings"))
        assert json.loads(raw) == {"model_sync_interval": 5}


# ===========================================================================
# §6.1 启动合并（用例 14-17，async —— 本机 Windows 不跑，mq3 远端取证）
# ===========================================================================


class TestStartupMerge:
    async def _seed_settings(self, tmp_path, value):
        """向 workspace 的 botflow.db 预写 admin_settings KV（create_app 前）。"""
        db = Database(tmp_path / "data" / "botflow.db")
        await db.initialize()
        await db.set_config("admin_settings", value if isinstance(value, str)
                            else json.dumps(value))
        await db.close()

    @asynccontextmanager
    async def _created_app(self, tmp_path, cfg):
        """create_app 会改 core._db/_config 与 config 单例 —— 断言完成后还原
        （照 tests/test_core_runtime.py TestAppAssembly 的保存/还原写法）。"""
        saved_db, saved_cfg = core_mod._db, core_mod._config
        saved_singleton = config_mod._config
        try:
            await core_mod.create_app(tmp_path, cfg)
            yield
        finally:
            if core_mod._db is not None:
                await core_mod._db.close()
            core_mod._db, core_mod._config = saved_db, saved_cfg
            config_mod._config = saved_singleton

    async def test_settings_startup_merge_overrides_env(self, tmp_path):
        """用例14（正例）：KV stream_timeout=99 覆盖 env 初值 1.0；
        core._config 与 get_config() 是同一对象。"""
        await self._seed_settings(tmp_path, {"stream_timeout": 99})
        cfg = BotflowSettings(workspace=str(tmp_path), stream_timeout=1.0)
        async with self._created_app(tmp_path, cfg):
            assert cfg.stream_timeout == 99
            assert core_mod._config is config_mod.get_config()

    async def test_settings_startup_merge_ignores_unknown_keys(self, tmp_path):
        """用例15（反例）：白名单而非 hasattr —— KV 混入 admin_key 不得被合并。"""
        await self._seed_settings(
            tmp_path, {"admin_key": "hacked", "stream_timeout": 5})
        cfg = BotflowSettings(workspace=str(tmp_path), admin_key="orig-admin")
        async with self._created_app(tmp_path, cfg):
            assert cfg.stream_timeout == 5
            assert cfg.admin_key == "orig-admin", "非白名单键不得被 KV 覆盖"

    async def test_settings_startup_merge_bad_json_is_tolerated(self, tmp_path):
        """用例16（反例）：KV 非法 JSON → create_app 不抛，保持传入初值。"""
        await self._seed_settings(tmp_path, "{not json")
        cfg = BotflowSettings(workspace=str(tmp_path), stream_timeout=1.0)
        async with self._created_app(tmp_path, cfg):
            assert cfg.stream_timeout == 1.0

    async def test_settings_startup_merge_missing_key_keeps_defaults(self, tmp_path):
        """用例17（边界）：无 admin_settings 行 → 9 参数全为 env/默认值。"""
        defaults = BotflowSettings(workspace=str(tmp_path))
        cfg = BotflowSettings(workspace=str(tmp_path))
        async with self._created_app(tmp_path, cfg):
            for k in ADMIN_SETTINGS_FIELDS:
                assert getattr(cfg, k) == getattr(defaults, k), f"{k} 应保持默认"


# ===========================================================================
# §6.2 同步端点（用例 18-23）
# ===========================================================================


class _FakeSyncResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        pass


class _FakeSyncAsyncClient:
    """复刻 tests/test_core_runtime.py 的 _FakeAsyncClient（用例23 真链路用）。"""

    def __init__(self, payload: dict, **kwargs) -> None:
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def get(self, url, headers=None):
        return _FakeSyncResponse(self._payload)


class TestProviderSync:
    @pytest.fixture
    def _patch_invalidate(self, monkeypatch):
        """把 admin_api 的 invalidate_all_caches 换成 MagicMock 供断言
        （照 tests/test_p0_1_admin_invalidation.py 的模式）。"""
        inv = MagicMock()
        monkeypatch.setattr("botflow.admin_api.invalidate_all_caches", inv)
        return inv

    def test_sync_provider_not_found_404(self, client, _patch_invalidate):
        """用例18（反例）：404 + detail 形状与既有 404 一致；不触发失效。"""
        r = client.post("/admin/providers/9999/sync", headers=AUTH)
        assert r.status_code == 404
        assert r.json()["detail"] == {
            "success": False, "error": "Provider 9999 not found"}
        _patch_invalidate.assert_not_called()

    def test_sync_provider_success(self, client, monkeypatch, _patch_invalidate):
        """用例19（正例）：mock core.sync_models_from_provider —— 断言参数
        provider_id/db 正确、被 await、回传 added/skipped/errors、失效恰 1 次。"""
        pid = client.post("/admin/providers", json={"req": {
            "name": "p", "base_url": "https://x"}}, headers=AUTH).json()["provider_id"]
        calls = []

        async def fake(provider_id, db=None):
            calls.append((provider_id, db))
            return {"added": 2, "skipped": 1, "errors": []}

        monkeypatch.setattr("botflow.core.sync_models_from_provider", fake)
        r = client.post(f"/admin/providers/{pid}/sync", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body == {"success": True, "added": 2, "skipped": 1, "errors": []}
        assert len(calls) == 1
        assert calls[0][0] == pid
        assert calls[0][1] is _get_db(client), "必须传 admin 端点拿到的 db 实例"
        _patch_invalidate.assert_called_once_with()

    def test_sync_provider_upstream_error_success_false(self, client, monkeypatch,
                                                        _patch_invalidate):
        """用例20（反例）：上游 errors → HTTP 200 但 success=False（§8-4 定案）。"""
        pid = client.post("/admin/providers", json={"req": {
            "name": "p", "base_url": "https://x"}}, headers=AUTH).json()["provider_id"]

        async def fake(provider_id, db=None):
            return {"added": 0, "skipped": 0, "errors": ["connect timeout"]}

        monkeypatch.setattr("botflow.core.sync_models_from_provider", fake)
        r = client.post(f"/admin/providers/{pid}/sync", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is False
        assert body["errors"] == ["connect timeout"]

    def test_sync_provider_requires_admin_key(self, client, _patch_invalidate):
        """用例21（反例）：无 key → 401。"""
        r = client.post("/admin/providers/1/sync")
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid admin key."

    def test_sync_provider_real_upstream_via_transport(self, client, monkeypatch,
                                                       _patch_invalidate):
        """用例23（正例，集成链路）：admin 端点 → core.sync_models_from_provider
        真跑 → 落库（守「只加新不改旧」）：existing 跳过、brand-new 入库。"""
        pid = client.post("/admin/providers", json={"req": {
            "name": "p", "base_url": "https://x/", "api_key": "sk"}}, headers=AUTH
        ).json()["provider_id"]
        client.post("/admin/models", json={"req": {
            "provider_id": pid, "name": "existing"}}, headers=AUTH)

        monkeypatch.setattr(
            core_mod.httpx, "AsyncClient",
            lambda **kw: _FakeSyncAsyncClient({"data": [
                {"id": "existing"}, {"id": "brand-new"}, {"no_id_key": 1},
            ]}, **kw))

        r = client.post(f"/admin/providers/{pid}/sync", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert body["added"] == 1
        assert body["skipped"] == 1
        assert body["errors"] == []
        models = client.get("/admin/models", params={"provider_id": pid},
                            headers=AUTH).json()["models"]
        names = {m["name"] for m in models}
        assert "brand-new" in names
        assert "existing" in names, "只加新不改旧：既有模型必须保留"
        _patch_invalidate.assert_called_once_with()
