"""Tests for botflow.admin_api REST management endpoints (100% coverage)."""

from __future__ import annotations

import asyncio
import json
import os
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botflow import auth as auth_mod
from botflow.admin_dashboard import mount_admin_ui
from botflow.admin_api import admin_router
from botflow.auth import create_session, hash_password, verify_password
from botflow.config import BotflowSettings, set_config
from botflow.storage import db as dbmod
from botflow.storage.db import Database
from botflow.storage.models import CallAttempt, CallLog, Model, Provider


@pytest.fixture
def client(tmp_path):
    d = Database(str(tmp_path / "test.db"))
    asyncio.new_event_loop().run_until_complete(d.initialize())
    set_config(BotflowSettings(admin_key="admin-secret"))
    app = FastAPI()
    app.include_router(admin_router)
    app.dependency_overrides[dbmod.get_db] = lambda: d
    with TestClient(app) as c:
        yield c
    asyncio.new_event_loop().run_until_complete(d.close())


AUTH = {"Authorization": "Bearer admin-secret"}


class TestAuth:
    def test_unauthorized(self, client):
        assert client.get("/admin/providers").status_code == 401

    def test_authorized(self, client):
        assert client.get("/admin/providers", headers=AUTH).status_code == 200


class TestProviders:
    def test_crud(self, client):
        p = client.post("/admin/providers", json={"req": {"name": "openai", "type": "openai",
                                                    "base_url": "https://api.openai.com/v1",
                                                    "api_key": "sk-x"}}, headers=AUTH)
        assert p.status_code == 200 and p.json()["success"] is True
        pid = p.json()["provider_id"]
        assert client.get("/admin/providers", headers=AUTH).json()["providers"]
        assert client.get(f"/admin/providers/{pid}", headers=AUTH).json()["success"] is True
        u = client.patch(f"/admin/providers/{pid}", json={"base_url": "https://new"}, headers=AUTH)
        assert u.status_code == 200 and u.json()["provider_id"] == pid
        assert client.get(f"/admin/providers/{pid}", headers=AUTH).json()["provider"]["base_url"] == "https://new"
        assert client.get(f"/admin/providers/{pid}", headers=AUTH).status_code == 200
        assert client.delete(f"/admin/providers/{pid}", headers=AUTH).json()["success"] is True
        assert client.get(f"/admin/providers/{pid}", headers=AUTH).status_code == 404

    def test_create_without_type(self, client):
        p = client.post("/admin/providers", json={"req": {"name": "x", "base_url": "https://x"}}, headers=AUTH)
        assert p.status_code == 200 and p.json()["success"] is True

    def test_update_missing(self, client):
        assert client.patch("/admin/providers/9999", json={"base_url": "x"}, headers=AUTH).status_code == 404

    def test_delete_missing(self, client):
        assert client.delete("/admin/providers/9999", headers=AUTH).status_code == 404


class TestModels:
    def _create_provider(self, client):
        return client.post("/admin/providers", json={"req": {"name": "openai", "base_url": "https://x"}}, headers=AUTH).json()["provider_id"]

    def test_crud(self, client):
        pid = self._create_provider(client)
        m = client.post("/admin/models", json={"req": {"provider_id": pid, "name": "gpt-4", "type": "openai"}}, headers=AUTH)
        assert m.status_code == 200 and m.json()["success"] is True
        mid = m.json()["model_id"]
        assert client.get("/admin/models", headers=AUTH).json()["models"]
        assert client.get(f"/admin/models/{mid}", headers=AUTH).json()["success"] is True
        u = client.patch(f"/admin/models/{mid}", json={"display_name": "GPT4"}, headers=AUTH)
        assert u.status_code == 200 and u.json()["model_id"] == mid
        assert client.get(f"/admin/models/{mid}", headers=AUTH).json()["model"]["display_name"] == "GPT4"
        assert client.delete(f"/admin/models/{mid}", headers=AUTH).json()["success"] is True
        assert client.get(f"/admin/models/{mid}", headers=AUTH).status_code == 404

    def test_create_without_type(self, client):
        pid = self._create_provider(client)
        m = client.post("/admin/models", json={"req": {"provider_id": pid, "name": "m"}}, headers=AUTH)
        assert m.status_code == 200

    def test_update_missing(self, client):
        assert client.patch("/admin/models/9999", json={"display_name": "x"}, headers=AUTH).status_code == 404

    def test_create_with_missing_provider(self, client):
        m = client.post("/admin/models", json={"req": {"provider_id": 9999, "name": "ghost"}}, headers=AUTH)
        assert m.status_code == 404

    def test_delete_missing(self, client):
        assert client.delete("/admin/models/9999", headers=AUTH).status_code == 404


class TestProviderExtraConfig:
    """§6.3 用例 24-30：供应商 extra_config 整体覆盖语义 + type(D1) + 启用禁用。"""

    def test_create_provider_with_extra_config(self, client):
        # 用例 24：create 带 extra_config → GET 原样返回
        p = client.post("/admin/providers", json={"req": {
            "name": "px", "base_url": "https://x",
            "extra_config": {"proxy": "http://p:1", "timeout": 30},
        }}, headers=AUTH)
        assert p.status_code == 200
        pid = p.json()["provider_id"]
        got = client.get(f"/admin/providers/{pid}", headers=AUTH).json()["provider"]
        assert got["extra_config"] == {"proxy": "http://p:1", "timeout": 30}

    def test_create_provider_defaults_type_openai(self, client):
        # 用例 25（D1 回归）：前端不再发 type → 后端默认 "openai"
        p = client.post("/admin/providers", json={"req": {"name": "p25", "base_url": "https://x"}}, headers=AUTH)
        assert p.status_code == 200
        got = client.get(f"/admin/providers/{p.json()['provider_id']}", headers=AUTH).json()["provider"]
        assert got["provider_type"] == "openai"

    def test_patch_provider_extra_config_none_keeps_existing(self, client):
        # 用例 26：PATCH 不带 extra_config → 原值不变
        pid = client.post("/admin/providers", json={"req": {
            "name": "p26", "base_url": "https://x", "extra_config": {"proxy": "http://p:1"},
        }}, headers=AUTH).json()["provider_id"]
        r = client.patch(f"/admin/providers/{pid}", json={"name": "p26-renamed"}, headers=AUTH)
        assert r.status_code == 200
        got = client.get(f"/admin/providers/{pid}", headers=AUTH).json()["provider"]
        assert got["extra_config"] == {"proxy": "http://p:1"}
        assert got["name"] == "p26-renamed"

    def test_patch_provider_extra_config_replaces_whole_dict(self, client):
        # 用例 27：整体覆盖（非键级合并）——proxy 键必须消失
        pid = client.post("/admin/providers", json={"req": {
            "name": "p27", "base_url": "https://x",
            "extra_config": {"proxy": "http://p:1", "timeout": 30},
        }}, headers=AUTH).json()["provider_id"]
        r = client.patch(f"/admin/providers/{pid}", json={"extra_config": {"timeout": 5}}, headers=AUTH)
        assert r.status_code == 200
        got = client.get(f"/admin/providers/{pid}", headers=AUTH).json()["provider"]
        assert got["extra_config"] == {"timeout": 5}

    def test_patch_provider_empty_extra_config_clears(self, client):
        # 用例 28：{} 是「给出」不是「不改」→ 必须写列清空
        pid = client.post("/admin/providers", json={"req": {
            "name": "p28", "base_url": "https://x", "extra_config": {"proxy": "http://p:1"},
        }}, headers=AUTH).json()["provider_id"]
        r = client.patch(f"/admin/providers/{pid}", json={"extra_config": {}}, headers=AUTH)
        assert r.status_code == 200
        got = client.get(f"/admin/providers/{pid}", headers=AUTH).json()["provider"]
        assert got["extra_config"] == {}

    def test_patch_provider_without_type_preserves_type(self, client):
        # 用例 29（D1 回归）：PATCH 不带 type → provider_type 保持原值
        pid = client.post("/admin/providers", json={"req": {
            "name": "p29", "base_url": "https://x", "type": "anthropic",
        }}, headers=AUTH).json()["provider_id"]
        r = client.patch(f"/admin/providers/{pid}", json={"base_url": "https://y"}, headers=AUTH)
        assert r.status_code == 200
        got = client.get(f"/admin/providers/{pid}", headers=AUTH).json()["provider"]
        assert got["provider_type"] == "anthropic"
        assert got["base_url"] == "https://y"

    def test_patch_provider_toggles_is_enabled(self, client):
        # 用例 30：启用/禁用翻转
        pid = client.post("/admin/providers", json={"req": {"name": "p30", "base_url": "https://x"}}, headers=AUTH).json()["provider_id"]
        assert client.patch(f"/admin/providers/{pid}", json={"is_enabled": False}, headers=AUTH).status_code == 200
        assert client.get(f"/admin/providers/{pid}", headers=AUTH).json()["provider"]["is_enabled"] is False
        client.patch(f"/admin/providers/{pid}", json={"is_enabled": True}, headers=AUTH)
        assert client.get(f"/admin/providers/{pid}", headers=AUTH).json()["provider"]["is_enabled"] is True


class TestModelExtraConfig:
    """§6.4 用例 34-36：模型 extra_config（temperature/modalities 存这里）。"""

    def _create_provider(self, client):
        return client.post("/admin/providers", json={"req": {"name": "pm", "base_url": "https://x"}}, headers=AUTH).json()["provider_id"]

    def test_create_model_with_extra_config(self, client):
        # 用例 34：create 带 extra_config → GET 返回；不传 → {}
        pid = self._create_provider(client)
        m = client.post("/admin/models", json={"req": {
            "provider_id": pid, "name": "m34", "extra_config": {"temperature": 0.2},
        }}, headers=AUTH)
        assert m.status_code == 200
        mid = m.json()["model_id"]
        got = client.get(f"/admin/models/{mid}", headers=AUTH).json()["model"]
        assert got["extra_config"] == {"temperature": 0.2}
        # 不传 → 默认 {}
        m2 = client.post("/admin/models", json={"req": {"provider_id": pid, "name": "m34b"}}, headers=AUTH)
        got2 = client.get(f"/admin/models/{m2.json()['model_id']}", headers=AUTH).json()["model"]
        assert got2["extra_config"] == {}

    def test_patch_model_extra_config_replaces(self, client):
        # 用例 35：PATCH extra_config → GET 原样返回（整体覆盖）
        pid = self._create_provider(client)
        mid = client.post("/admin/models", json={"req": {"provider_id": pid, "name": "m35"}}, headers=AUTH).json()["model_id"]
        new_cfg = {"temperature": 0.7, "modalities": ["text", "image"]}
        r = client.patch(f"/admin/models/{mid}", json={"extra_config": new_cfg}, headers=AUTH)
        assert r.status_code == 200
        got = client.get(f"/admin/models/{mid}", headers=AUTH).json()["model"]
        assert got["extra_config"] == new_cfg

    def test_patch_model_extra_config_none_keeps(self, client):
        # 用例 36：PATCH 不带 extra_config → 原值不变
        pid = self._create_provider(client)
        mid = client.post("/admin/models", json={"req": {
            "provider_id": pid, "name": "m36", "extra_config": {"temperature": 0.3},
        }}, headers=AUTH).json()["model_id"]
        r = client.patch(f"/admin/models/{mid}", json={"name": "m36-renamed"}, headers=AUTH)
        assert r.status_code == 200
        got = client.get(f"/admin/models/{mid}", headers=AUTH).json()["model"]
        assert got["extra_config"] == {"temperature": 0.3}
        assert got["name"] == "m36-renamed"


class TestGroups:
    def test_crud(self, client):
        g = client.post("/admin/groups", json={"req": {"name": "prod", "description": "d"}}, headers=AUTH)
        assert g.status_code == 200 and g.json()["success"] is True
        gid = g.json()["group_id"]
        assert client.get("/admin/groups", headers=AUTH).json()["groups"]
        assert client.get(f"/admin/groups/{gid}", headers=AUTH).json()["success"] is True
        u = client.patch(f"/admin/groups/{gid}", json={"description": "up"}, headers=AUTH)
        assert u.status_code == 200 and u.json()["group_id"] == gid
        assert client.delete(f"/admin/groups/{gid}", headers=AUTH).json()["success"] is True
        assert client.get(f"/admin/groups/{gid}", headers=AUTH).status_code == 404

    def test_missing_branches(self, client):
        assert client.get("/admin/groups/9999", headers=AUTH).status_code == 404
        assert client.patch("/admin/groups/9999", json={}, headers=AUTH).status_code == 404
        assert client.delete("/admin/groups/9999", headers=AUTH).status_code == 404


class TestGroupModels:
    def test_full(self, client):
        pid = client.post("/admin/providers", json={"req": {"name": "openai", "base_url": "https://x"}}, headers=AUTH).json()["provider_id"]
        mid = client.post("/admin/models", json={"req": {"provider_id": pid, "name": "gpt-4"}}, headers=AUTH).json()["model_id"]
        gid = client.post("/admin/groups", json={"req": {"name": "prod"}}, headers=AUTH).json()["group_id"]
        a = client.post(f"/admin/groups/{gid}/models", params={"model_id": mid, "weight": 3}, headers=AUTH)
        assert a.status_code == 200 and a.json()["success"] is True
        det = client.get(f"/admin/groups/{gid}/details", headers=AUTH)
        assert det.status_code == 200 and len(det.json()["models"]) == 1
        w = client.patch(f"/admin/groups/{gid}/models/{mid}", params={"weight": 5}, headers=AUTH)
        assert w.status_code == 200
        r = client.delete(f"/admin/groups/{gid}/models/{mid}", headers=AUTH)
        assert r.status_code == 200
        assert len(client.get(f"/admin/groups/{gid}/details", headers=AUTH).json()["models"]) == 0

    def test_404_branches(self, client):
        assert client.post("/admin/groups/9999/models", params={"model_id": 1}, headers=AUTH).status_code == 404
        gid = client.post("/admin/groups", json={"req": {"name": "g"}}, headers=AUTH).json()["group_id"]
        assert client.post(f"/admin/groups/{gid}/models", params={"model_id": 9999}, headers=AUTH).status_code == 404
        assert client.get("/admin/groups/9999/details", headers=AUTH).status_code == 404
        assert client.patch(f"/admin/groups/{gid}/models/9999", params={"weight": 1}, headers=AUTH).status_code == 200
        assert client.delete(f"/admin/groups/{gid}/models/9999", headers=AUTH).status_code == 200


class TestStats:
    def test_models(self, client):
        pid = client.post("/admin/providers", json={"req": {"name": "openai", "base_url": "https://x"}}, headers=AUTH).json()["provider_id"]
        mid = client.post("/admin/models", json={"req": {"provider_id": pid, "name": "gpt-4"}}, headers=AUTH).json()["model_id"]
        r = client.get("/admin/stats/models", headers=AUTH)
        assert r.status_code == 200 and r.json()["success"] is True
        r2 = client.get("/admin/stats/models", params={"api_key_id": 1}, headers=AUTH)
        assert r2.status_code == 200

    def test_groups(self, client):
        gid = client.post("/admin/groups", json={"req": {"name": "empty"}}, headers=AUTH).json()["group_id"]
        r = client.get("/admin/stats/groups", headers=AUTH)
        assert r.status_code == 200 and r.json()["success"] is True
        r2 = client.get("/admin/stats/groups", params={"api_key_id": 1}, headers=AUTH)
        assert r2.status_code == 200

    def test_cost(self, client):
        r = client.get("/admin/stats/cost", params={"days": 30}, headers=AUTH)
        assert r.status_code == 200 and r.json()["success"] is True
        assert isinstance(r.json()["cost_summary"], list)
        r2 = client.get("/admin/stats/cost", params={"api_key_id": 1}, headers=AUTH)
        assert r2.status_code == 200


class TestLogs:
    def test_logs(self, client):
        r = client.get("/admin/logs", headers=AUTH)
        assert r.status_code == 200 and r.json()["success"] is True
        d = client.app.dependency_overrides[dbmod.get_db]()
        pid = asyncio.new_event_loop().run_until_complete(d.create_provider(Provider(name="p", provider_type="openai")))
        mid = asyncio.new_event_loop().run_until_complete(d.create_model(Model(name="gpt-4", provider_id=pid)))
        kid = asyncio.new_event_loop().run_until_complete(d.create_api_key("key-x", label="team"))
        asyncio.new_event_loop().run_until_complete(
            d.create_call_log(CallLog(model_id=mid, status="success", api_key_id=kid.id)))
        r = client.get("/admin/logs", params={"api_key_id": kid.id}, headers=AUTH)
        assert r.status_code == 200 and len(r.json()["logs"]) == 1


class TestStrategies:
    def test_list_strategies(self, client):
        from botflow.pipeline.base import STRATEGY_REGISTRY

        r = client.get("/admin/strategies", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert body["strategies"] == sorted(STRATEGY_REGISTRY.keys())
        assert body["strategies"], "at least one routing strategy must be registered"


class TestSummaries:
    def test_get_summary(self, client):
        r = client.get("/admin/summaries/2099-01-01", headers=AUTH)
        assert r.status_code == 404

    def test_get_existing_summary(self, client):
        d = client.app.dependency_overrides[dbmod.get_db]()
        asyncio.new_event_loop().run_until_complete(
            d.upsert_daily_summary("2099-01-02", "# daily md", "{}"))
        r = client.get("/admin/summaries/2099-01-02", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["summary"]["summary_md"] == "# daily md"


class TestApiKeys:
    def test_crud_and_redaction(self, client):
        assert client.get("/admin/apikeys", headers=AUTH).json()["api_keys"] == []
        c = client.post("/admin/apikeys", json={"req": {"raw_key": "secret-key", "label": "team-a"}}, headers=AUTH)
        assert c.status_code == 200
        body = c.json()
        assert body["success"] is True and "key_hash_prefix" in body
        assert body["label"] == "team-a"
        assert "secret-key" not in str(body)
        kid = body["id"]
        listed = client.get("/admin/apikeys", headers=AUTH).json()["api_keys"]
        assert len(listed) == 1 and "secret-key" not in str(listed)
        d = client.patch(f"/admin/apikeys/{kid}", json={"is_enabled": False}, headers=AUTH)
        assert d.status_code == 200 and d.json()["is_enabled"] is False
        rm = client.delete(f"/admin/apikeys/{kid}", headers=AUTH)
        assert rm.status_code == 200 and rm.json()["success"] is True
        assert client.get("/admin/apikeys", headers=AUTH).json()["api_keys"] == []

    def test_not_found(self, client):
        assert client.patch("/admin/apikeys/9999", json={"is_enabled": False}, headers=AUTH).status_code == 404
        assert client.delete("/admin/apikeys/9999", headers=AUTH).status_code == 404


class TestWriteEndpointsUseJsonBody:
    """Regression guard: non-GET admin endpoints must read their payload from
    the JSON body.

    The auth dependency used to declare
    ``credentials: HTTPAuthorizationCredentials = None``. FastAPI classifies a
    Pydantic-typed *default* as a request-body field, and because the update
    handlers had no other body parameter, that lone field captured the whole
    body. Consequences ("点禁用报错" / "删除后列表不变"):

      * PATCH /admin/models|providers|apikeys -> 422
        ``{"loc": ["body", "scheme"], "msg": "Field required"}``
      * PATCH /admin/groups -> 200 but the payload was silently dropped
        (``params: dict`` was a second body field, so FastAPI switched to
        embedding and every other value stayed at its default).

    ``test_no_request_body_exposes_the_auth_model`` reads the generated OpenAPI
    document so the whole bug class cannot silently return.
    """

    def _seed(self, client):
        pid = client.post("/admin/providers", headers=AUTH,
                          json={"req": {"name": "p1", "base_url": "https://x", "api_key": "sk-1"}}).json()["provider_id"]
        mid = client.post("/admin/models", headers=AUTH,
                          json={"req": {"provider_id": pid, "name": "m1"}}).json()["model_id"]
        gid = client.post("/admin/groups", headers=AUTH, json={"req": {"name": "g1"}}).json()["group_id"]
        kid = client.post("/admin/apikeys", headers=AUTH,
                          json={"req": {"raw_key": "secret-key", "label": "t"}}).json()["id"]
        return pid, mid, gid, kid

    def test_no_request_body_exposes_the_auth_model(self, client):
        spec = client.get("/openapi.json").json()
        assert spec["paths"], "openapi spec should describe the admin routes"
        for path, ops in spec["paths"].items():
            for method, op in ops.items():
                body = json.dumps(op.get("requestBody") or {})
                assert "HTTPAuthorizationCredentials" not in body, (
                    f"{method.upper()} {path} exposes the auth model as a request body"
                )

    def test_patch_provider_reads_body(self, client):
        pid, _, _, _ = self._seed(client)
        r = client.patch(f"/admin/providers/{pid}", headers=AUTH, json={
            "name": "p2", "base_url": "https://y", "api_key": "sk-2",
            "type": "anthropic", "is_enabled": False,
        })
        assert r.status_code == 200
        p = client.get(f"/admin/providers/{pid}", headers=AUTH).json()["provider"]
        assert p["name"] == "p2" and p["base_url"] == "https://y"
        assert p["api_key"] == "sk-2" and p["provider_type"] == "anthropic"
        assert p["is_enabled"] is False

    def test_patch_model_toggle_persists(self, client):
        _, mid, _, _ = self._seed(client)
        assert client.patch(f"/admin/models/{mid}", headers=AUTH,
                            json={"is_enabled": False}).status_code == 200
        assert client.get(f"/admin/models/{mid}", headers=AUTH).json()["model"]["is_enabled"] is False
        assert client.patch(f"/admin/models/{mid}", headers=AUTH,
                            json={"is_enabled": True}).status_code == 200
        assert client.get(f"/admin/models/{mid}", headers=AUTH).json()["model"]["is_enabled"] is True

    def test_patch_model_reads_every_field(self, client):
        _, mid, _, _ = self._seed(client)
        r = client.patch(f"/admin/models/{mid}", headers=AUTH, json={
            "name": "m2", "context_window": 8192, "display_name": "M2",
            "api_format": "anthropic", "max_retries": 5,
            "cooldown_seconds": 30, "cooldown_failure_threshold": 7,
        })
        assert r.status_code == 200
        m = client.get(f"/admin/models/{mid}", headers=AUTH).json()["model"]
        assert m["name"] == "m2" and m["context_window"] == 8192
        assert m["display_name"] == "M2" and m["api_format"] == "anthropic"
        assert (m["max_retries"], m["cooldown_seconds"], m["cooldown_failure_threshold"]) == (5, 30, 7)

    def test_patch_group_reads_body(self, client):
        _, _, gid, _ = self._seed(client)
        fallback = client.post("/admin/groups", headers=AUTH,
                               json={"req": {"name": "fallback"}}).json()["group_id"]
        r = client.patch(f"/admin/groups/{gid}", headers=AUTH, json={
            "name": "g2", "description": "d2", "is_enabled": False,
            "fallback_group_id": fallback, "type": "round_robin", "params": {"weights": [1, 2]},
        })
        assert r.status_code == 200
        g = client.get(f"/admin/groups/{gid}", headers=AUTH).json()["group"]
        assert g["name"] == "g2" and g["description"] == "d2"
        assert g["is_enabled"] is False and g["fallback_group_id"] == fallback
        assert g["type"] == "round_robin" and g["params"] == {"weights": [1, 2]}

    def test_patch_apikey_toggle_persists(self, client):
        _, _, _, kid = self._seed(client)
        r = client.patch(f"/admin/apikeys/{kid}", headers=AUTH, json={"is_enabled": False})
        assert r.status_code == 200 and r.json()["is_enabled"] is False
        assert client.get("/admin/apikeys", headers=AUTH).json()["api_keys"][0]["is_enabled"] is False

    def test_patch_empty_body_keeps_existing_values(self, client):
        pid, mid, gid, _ = self._seed(client)
        assert client.patch(f"/admin/providers/{pid}", headers=AUTH, json={}).status_code == 200
        assert client.patch(f"/admin/models/{mid}", headers=AUTH, json={}).status_code == 200
        assert client.patch(f"/admin/groups/{gid}", headers=AUTH, json={}).status_code == 200
        assert client.get(f"/admin/providers/{pid}", headers=AUTH).json()["provider"]["name"] == "p1"
        assert client.get(f"/admin/models/{mid}", headers=AUTH).json()["model"]["name"] == "m1"
        assert client.get(f"/admin/groups/{gid}", headers=AUTH).json()["group"]["name"] == "g1"


class TestAdminDashboard:
    """The admin SPA must be served no-store and must not gate deletes on the
    native ``confirm()`` — that silently returns False in embedded/iframe
    previews, making the delete button look dead ("点删除没反应")."""

    def test_served_with_no_store(self, tmp_path, monkeypatch):
        import botflow.admin_dashboard as ad

        f = tmp_path / "index.html"
        f.write_text("<html><body>spa</body></html>", encoding="utf-8")
        monkeypatch.setattr(ad, "_HTML_PATH", f)

        app = FastAPI()
        mount_admin_ui(app)
        with TestClient(app) as c:
            r = c.get("/admin/")

        assert r.status_code == 200
        assert "no-store" in r.headers["cache-control"]
        assert r.text == "<html><body>spa</body></html>"

    def test_rereads_html_when_file_changes(self, tmp_path, monkeypatch):
        import botflow.admin_dashboard as ad

        f = tmp_path / "index.html"
        f.write_text("v1", encoding="utf-8")
        monkeypatch.setattr(ad, "_HTML_PATH", f)

        app = FastAPI()
        mount_admin_ui(app)
        with TestClient(app) as c:
            assert c.get("/admin/").text == "v1"
            f.write_text("v2", encoding="utf-8")
            os.utime(f, (time.time() + 10, time.time() + 10))
            assert c.get("/admin/").text == "v2"

    def test_missing_asset_registers_nothing(self, tmp_path, monkeypatch):
        import botflow.admin_dashboard as ad

        monkeypatch.setattr(ad, "_HTML_PATH", tmp_path / "absent.html")
        app = FastAPI()
        mount_admin_ui(app)
        with TestClient(app) as c:
            assert c.get("/admin/").status_code == 404

    def test_shipped_spa_uses_inpage_confirm(self):
        """Regression guard: delete handlers must not depend on native confirm()."""
        import botflow.admin_dashboard as ad

        html = ad._HTML_PATH.read_text(encoding="utf-8")
        assert "askConfirm" in html
        assert "confirmState" in html
        assert "!confirm(" not in html  # no `if(!confirm(...)) return;` dead-button gate
        assert "Promise.allSettled" in html  # one failing endpoint can't freeze every list


class TestAttemptQueries:
    """SG-0 F8：call_attempts 只读查询端点。

    依赖编码子 agent 实现的 ``GET /admin/attempts``（按 request_id / model_id 过滤，
    返回 ``{"success": True, "attempts": [...]}``；无匹配返回空列表而非 404）。
    """

    def _seed(self, client, request_id, model_id, provider_id=1, group_id=1):
        d = client.app.dependency_overrides[dbmod.get_db]()
        asyncio.new_event_loop().run_until_complete(d.create_call_attempts([
            CallAttempt(
                request_id=request_id, group_id=group_id, model_id=model_id,
                provider_id=provider_id, stage="non_stream", endpoint_idx=0,
                attempt_no=1, error_type="ProviderError", error_message="boom",
                duration_ms=5, created_at="2026-01-01T00:00:00Z",
            ),
        ]))

    def test_admin_query_attempts_by_request_id(self, client):
        self._seed(client, "r1", model_id=1)
        self._seed(client, "r2", model_id=2)
        r = client.get("/admin/attempts", params={"request_id": "r1"}, headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert len(body["attempts"]) == 1
        assert body["attempts"][0]["request_id"] == "r1"

    def test_admin_query_attempts_by_model_id(self, client):
        self._seed(client, "r1", model_id=1)
        self._seed(client, "r2", model_id=2)
        r = client.get("/admin/attempts", params={"model_id": 2}, headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert len(body["attempts"]) == 1
        assert body["attempts"][0]["model_id"] == 2

    def test_admin_query_attempts_empty_result(self, client):
        r = client.get("/admin/attempts", params={"request_id": "nope"}, headers=AUTH)
        assert r.status_code == 200  # 不是 404
        body = r.json()
        assert body["success"] is True
        assert body["attempts"] == []


# ---------------------------------------------------------------------------
# 需求1：GET /admin/models{,/{id}} 附带 provider_enabled（显示层有效状态）
# 契约 docs/tasks/provider_cascade_pwd_features.md §3.1 P1-1 ~ P1-11（D1 零写库）
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _detail_locs(body: dict) -> list[str]:
    """422 detail 里各字段的 loc 路径（如 'body.new_password'）。"""
    return [".".join(str(x) for x in item.get("loc", ())) for item in body.get("detail", [])]


class TestModelsProviderEnabled:
    """P1-1 ~ P1-11：模型按「显示层有效状态」返回 provider_enabled。

    硬约束：不写库（P1-8）、不动路由层 SQL（P1-9）、enabled_only 语义不变
    （P1-10）、孤儿 provider_id 不崩（P1-5）。
    """

    def _mk_provider(self, client, name="prov", enabled=True):
        pid = client.post("/admin/providers", json={"req": {
            "name": name, "base_url": "https://x"}}, headers=AUTH).json()["provider_id"]
        if not enabled:
            r = client.patch(f"/admin/providers/{pid}", json={"is_enabled": False}, headers=AUTH)
            assert r.status_code == 200
        return pid

    def _mk_model(self, client, pid, name="m", enabled=True):
        mid = client.post("/admin/models", json={"req": {
            "provider_id": pid, "name": name, "is_enabled": enabled}}, headers=AUTH).json()["model_id"]
        return mid

    def _db(self, client):
        return client.app.dependency_overrides[dbmod.get_db]()

    def _find(self, client, mid):
        models = client.get("/admin/models", headers=AUTH).json()["models"]
        return next(m for m in models if m["id"] == mid)

    def test_p1_1_provider_on_model_on(self, client):
        # P1-1：双启用 → provider_enabled=true、is_enabled=true、其余键与改前一致
        pid = self._mk_provider(client)
        mid = self._mk_model(client, pid)
        m = self._find(client, mid)
        assert m["provider_enabled"] is True
        assert m["is_enabled"] is True
        # 「其余键与改前一致」= model_dump 的全部键原样保留，仅新增一个键
        assert set(m) == set(Model.model_fields) | {"provider_enabled"}

    def test_p1_2_provider_off_model_on(self, client):
        # P1-2：供应商停用 + 模型自身启用 → 两字段独立
        pid = self._mk_provider(client, enabled=False)
        mid = self._mk_model(client, pid)
        m = self._find(client, mid)
        assert m["provider_enabled"] is False
        assert m["is_enabled"] is True

    def test_p1_3_provider_off_model_off(self, client):
        # P1-3：双停用
        pid = self._mk_provider(client, enabled=False)
        mid = self._mk_model(client, pid, enabled=False)
        m = self._find(client, mid)
        assert m["provider_enabled"] is False
        assert m["is_enabled"] is False

    def test_p1_4_provider_on_model_off(self, client):
        # P1-4：供应商启用 + 模型自身停用
        pid = self._mk_provider(client)
        mid = self._mk_model(client, pid, enabled=False)
        m = self._find(client, mid)
        assert m["provider_enabled"] is True
        assert m["is_enabled"] is False

    def test_p1_5_orphan_provider_id(self, client):
        # P1-5：孤儿 provider_id → provider_enabled=false，200 不崩
        pid = self._mk_provider(client)
        mid = self._mk_model(client, pid)
        d = self._db(client)
        # 关连接级外键后删供应商行 → 模型成孤儿引用（用完立即复原）
        _run(d.execute_write("PRAGMA foreign_keys=OFF"))
        _run(d.execute_write("DELETE FROM providers WHERE id = ?", (pid,)))
        _run(d.execute_write("PRAGMA foreign_keys=ON"))
        r = client.get("/admin/models", headers=AUTH)
        assert r.status_code == 200
        m = next(x for x in r.json()["models"] if x["id"] == mid)
        assert m["provider_enabled"] is False

    def test_p1_6_detail_same_semantics(self, client):
        # P1-6：单模型详情同口径（停用/启用两种组合都取一遍）
        off_pid = self._mk_provider(client, name="prov-off", enabled=False)
        off_mid = self._mk_model(client, off_pid, name="m-off")
        on_pid = self._mk_provider(client, name="prov-on")
        on_mid = self._mk_model(client, on_pid, name="m-on", enabled=False)
        got_off = client.get(f"/admin/models/{off_mid}", headers=AUTH).json()["model"]
        got_on = client.get(f"/admin/models/{on_mid}", headers=AUTH).json()["model"]
        assert got_off["provider_enabled"] is False and got_off["is_enabled"] is True
        assert got_on["provider_enabled"] is True and got_on["is_enabled"] is False
        assert set(got_off) == set(Model.model_fields) | {"provider_enabled"}

    def test_p1_7_detail_404(self, client):
        # P1-7：id 不存在 → 404，既有文案形状 {"success":false,"error":...}
        r = client.get("/admin/models/9999", headers=AUTH)
        assert r.status_code == 404
        detail = r.json()["detail"]
        assert detail["success"] is False
        assert "Model 9999 not found" in detail["error"]

    def test_p1_8_zero_write(self, client):
        # P1-8：GET 前后 models 表逐行一致（GET 不产生任何 UPDATE，含 updated_at）
        pid = self._mk_provider(client, enabled=False)
        mid = self._mk_model(client, pid)
        d = self._db(client)

        def dump():
            rows = _run(d.execute_read("SELECT * FROM models ORDER BY id"))
            return [tuple(r) for r in rows]

        before = dump()
        assert client.get("/admin/models", headers=AUTH).status_code == 200
        assert dump() == before
        assert client.get(f"/admin/models/{mid}", headers=AUTH).status_code == 200
        assert dump() == before

    def test_p1_9_route_layer_still_excludes_disabled_provider(self, client):
        # P1-9：回归确认 —— 分组加载 SQL 的 AND p.is_enabled = 1 未被改动
        pid = self._mk_provider(client)
        mid = self._mk_model(client, pid, name="gpt-4")
        gid = client.post("/admin/groups", json={"req": {"name": "g"}},
                          headers=AUTH).json()["group_id"]
        assert client.post(f"/admin/groups/{gid}/models",
                           params={"model_id": mid, "weight": 1}, headers=AUTH).status_code == 200
        d = self._db(client)
        groups = _run(d.list_groups_with_models())
        assert groups and groups[0]["model_names"] == ["gpt-4"]  # 供应商启用时在
        assert client.patch(f"/admin/providers/{pid}",
                            json={"is_enabled": False}, headers=AUTH).status_code == 200
        # 供应商停用 → 该模型仍被路由层 JOIN 排除（分组整行消失）
        assert _run(d.list_groups_with_models()) == []

    def test_p1_10_enabled_only_semantics_unchanged(self, client):
        # P1-10：enabled_only 只看 models 表 —— 供应商停用的启用模型仍在结果里
        pid = self._mk_provider(client, enabled=False)
        kept = self._mk_model(client, pid, name="kept")
        dropped = self._mk_model(client, pid, name="dropped", enabled=False)
        r = client.get("/admin/models", params={"enabled_only": True}, headers=AUTH)
        assert r.status_code == 200
        ids = [m["id"] for m in r.json()["models"]]
        assert kept in ids and dropped not in ids
        m = next(x for x in r.json()["models"] if x["id"] == kept)
        assert m["provider_enabled"] is False and m["is_enabled"] is True

    def test_p1_11_unauthorized(self, client):
        # P1-11：无 Bearer/无 token → 401 Invalid admin key.（依赖未变）
        r = client.get("/admin/models")
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid admin key."


# ---------------------------------------------------------------------------
# 需求2：POST /admin/auth/change-password
# 契约 docs/tasks/provider_cascade_pwd_features.md §3.3 P2-1 ~ P2-18（D2 不吊销会话）
# ---------------------------------------------------------------------------


class TestChangePassword:
    """P2-1 ~ P2-18：改密码端点（分支顺序 404 → 401 → 400 → 200，不吊销会话）。"""

    CUR = "OldPass123"
    NEW = "NewPass456"
    CHG = "/admin/auth/change-password"

    @pytest.fixture(autouse=True)
    def fast_pbkdf2(self, monkeypatch):
        # 与 test_admin_auth.py 同口径：单测把 600k 迭代调低，否则套件明显变慢。
        monkeypatch.setattr(auth_mod, "PBKDF2_ITERATIONS", 1000)

    def _seed(self, client, username="alice", password=None, raw=None):
        """造 admin_account 状态：raw 给定则原样写入（损坏行用），否则按正常账号写。"""
        d = client.app.dependency_overrides[dbmod.get_db]()
        if raw is not None:
            _run(d.set_config("admin_account", raw))
            return
        _run(d.set_config("admin_account", json.dumps({
            "username": username,
            "pwd_hash": hash_password(password if password is not None else self.CUR),
        })))

    def _account(self, client):
        d = client.app.dependency_overrides[dbmod.get_db]()
        return _run(d.get_config("admin_account"))

    def _sess_count(self, client):
        d = client.app.dependency_overrides[dbmod.get_db]()
        rows = _run(d.execute_read("SELECT key FROM config WHERE key LIKE 'admin_sess:%'"))
        return len(rows)

    def _login(self, client, password, username="alice"):
        return client.post("/admin/auth/login",
                           json={"username": username, "password": password})

    def _change(self, client, current, new, headers=None):
        return client.post(self.CHG, json={
            "current_password": current, "new_password": new,
        }, headers=AUTH if headers is None else headers)

    def test_p2_1_success_full_cycle(self, client):
        # P2-1：200 + pwd_hash 换 salt + username 不变 + 会话行数不变 + 新旧密码登录
        self._seed(client)
        d = client.app.dependency_overrides[dbmod.get_db]()
        _run(create_session(d, "alice"))  # 预置一条会话，验证不被吊销
        raw_before = self._account(client)
        n_before = self._sess_count(client)
        r = self._change(client, self.CUR, self.NEW)
        assert r.status_code == 200 and r.json() == {"success": True}
        raw_after = self._account(client)
        acct = json.loads(raw_after)
        assert acct["username"] == "alice"  # username 保留
        assert acct["pwd_hash"] != json.loads(raw_before)["pwd_hash"]  # 新 salt 重新 hash
        assert acct["pwd_hash"].startswith("pbkdf2_sha256$")
        assert verify_password(self.NEW, acct["pwd_hash"]) is True
        assert self._sess_count(client) == n_before  # admin_sess:* 行数不变（D2）
        assert self._login(client, self.CUR).status_code == 401  # 旧密码失效
        assert self._login(client, self.NEW).status_code == 200  # 新密码可用

    def test_p2_2_wrong_current_401(self, client):
        # P2-2：当前密码错 → 401；admin_account 不变；会话不吊销
        self._seed(client)
        _run(create_session(client.app.dependency_overrides[dbmod.get_db](), "alice"))
        raw_before = self._account(client)
        n_before = self._sess_count(client)
        r = self._change(client, "wrong-pass-x", self.NEW)
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid current password."
        assert self._account(client) == raw_before
        assert self._sess_count(client) == n_before

    def test_p2_3_account_missing_404(self, client):
        # P2-3：账号行不存在 → 404（鉴权另走 admin key）
        r = self._change(client, self.CUR, self.NEW)
        assert r.status_code == 404
        assert r.json()["detail"] == "Admin account not configured."

    def test_p2_4_account_json_corrupt_404_row_untouched(self, client):
        # P2-4：JSON 损坏 → 404；损坏行不被修复/覆盖
        self._seed(client, raw="{not json")
        r = self._change(client, self.CUR, self.NEW)
        assert r.status_code == 404
        assert r.json()["detail"] == "Admin account not configured."
        assert self._account(client) == "{not json"

    def test_p2_5_account_not_dict_404(self, client):
        # P2-5：account 非 dict（[1,2]）→ 404 同上
        self._seed(client, raw="[1,2]")
        r = self._change(client, self.CUR, self.NEW)
        assert r.status_code == 404
        assert r.json()["detail"] == "Admin account not configured."
        assert self._account(client) == "[1,2]"

    def test_p2_6_same_password_400_no_write(self, client):
        # P2-6：新旧相同 → 400；verify 之后才判（未发生写库，pwd_hash 逐字节不变）
        self._seed(client)
        raw_before = self._account(client)
        r = self._change(client, self.CUR, self.CUR)
        assert r.status_code == 400
        assert r.json()["detail"] == "New password must differ from current password."
        assert self._account(client) == raw_before

    def test_p2_7_new_password_too_short_422_before_pbkdf2(self, client, monkeypatch):
        # P2-7：7 位 → pydantic 422，端点不执行、不跑 pbkdf2
        self._seed(client)
        calls = []
        monkeypatch.setattr("botflow.admin_api.verify_password",
                            lambda p, s: calls.append(("v", p)) or True)
        monkeypatch.setattr("botflow.admin_api.hash_password",
                            lambda p: calls.append(("h", p)) or "")
        r = self._change(client, self.CUR, "1234567")
        assert r.status_code == 422
        assert "body.new_password" in _detail_locs(r.json())
        assert calls == []

    def test_p2_8_new_password_overlong_422_before_hash(self, client, monkeypatch):
        # P2-8：129 位 → 422，不进哈希计算（契约 §0-6）
        self._seed(client)
        calls = []
        monkeypatch.setattr("botflow.admin_api.hash_password",
                            lambda p: calls.append(p) or "")
        r = self._change(client, self.CUR, "x" * 129)
        assert r.status_code == 422
        assert "body.new_password" in _detail_locs(r.json())
        assert calls == []

    def test_p2_9_current_password_empty_422(self, client):
        # P2-9：current 空串 → 422（min_length=1），不落 401
        self._seed(client)
        r = self._change(client, "", self.NEW)
        assert r.status_code == 422
        assert "body.current_password" in _detail_locs(r.json())

    def test_p2_10_missing_fields_422(self, client):
        # P2-10：缺字段 → 422（{} 与缺 new_password 两种形态）
        self._seed(client)
        r0 = client.post(self.CHG, json={}, headers=AUTH)
        assert r0.status_code == 422
        assert set(_detail_locs(r0.json())) >= {"body.current_password", "body.new_password"}
        r1 = client.post(self.CHG, json={"current_password": self.CUR}, headers=AUTH)
        assert r1.status_code == 422
        assert "body.new_password" in _detail_locs(r1.json())

    def test_p2_11_body_is_json_body_not_query_param(self, client):
        # P2-11：显式 Pydantic body —— JSON body 被读到（非 query 参数坑）
        self._seed(client)
        spec = client.get("/openapi.json").json()
        op = spec["paths"][self.CHG]["post"]
        assert "requestBody" in op  # body 声明存在，不是 query-only 端点
        # body 正确读到 → 不是「422 缺 body.current_password」而是走完分支 200
        assert self._change(client, self.CUR, self.NEW).status_code == 200

    def test_p2_12_unauthorized_401(self, client):
        # P2-12：无 Bearer、无 token → 401 Invalid admin key.
        r = client.post(self.CHG, json={"current_password": self.CUR,
                                        "new_password": self.NEW})
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid admin key."

    def test_p2_13_admin_key_channel_200(self, client):
        # P2-13：Authorization: Bearer <BOTFLOW_ADMIN_KEY> → 200
        self._seed(client)
        assert self._change(client, self.CUR, self.NEW, headers=AUTH).status_code == 200

    def test_p2_14_session_token_channel_and_stays_valid(self, client):
        # P2-14：会话 token 通道 → 200，且该 token 改密后仍有效（D2）
        self._seed(client)
        token = self._login(client, self.CUR).json()["token"]
        h = {"Authorization": f"Bearer {token}"}
        assert self._change(client, self.CUR, self.NEW, headers=h).status_code == 200
        assert client.get("/admin/providers", headers=h).status_code == 200

    def test_p2_15_other_sessions_not_revoked(self, client):
        # P2-15：会话 A 改密 → 会话 B 继续 200（不被 DELETE）
        self._seed(client)
        tok_a = self._login(client, self.CUR).json()["token"]
        tok_b = self._login(client, self.CUR).json()["token"]
        ha = {"Authorization": f"Bearer {tok_a}"}
        hb = {"Authorization": f"Bearer {tok_b}"}
        assert self._change(client, self.CUR, self.NEW, headers=ha).status_code == 200
        assert client.get("/admin/providers", headers=hb).status_code == 200
        assert client.get("/admin/providers", headers=ha).status_code == 200

    def test_p2_16_pwd_hash_key_missing_401_no_crash(self, client):
        # P2-16：{"username":"a"} 缺 pwd_hash → verify_password("") 返回 False → 401 不崩
        self._seed(client, raw=json.dumps({"username": "a"}))
        r = self._change(client, self.CUR, self.NEW)
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid current password."

    def test_p2_17_non_ascii_passwords(self, client):
        # P2-17：非 ASCII → compare_digest 走 utf-8 bytes：改密 200 而非 TypeError 500；
        # 新旧相同（非 ASCII）也必须 400 而非 500。
        self._seed(client, password="中文密码旧123")
        r = self._change(client, "中文密码旧123", "新密码新12345")
        assert r.status_code == 200
        r2 = self._change(client, "新密码新12345", "新密码新12345")
        assert r2.status_code == 400
        assert r2.json()["detail"] == "New password must differ from current password."

    def test_p2_18_401_takes_priority_over_400(self, client):
        # P2-18：current 错 + new == current 两者皆违 → 401 优先（verify 在 same 检查之前）
        self._seed(client)
        r = self._change(client, "wrong-pass-abc", "wrong-pass-abc")
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid current password."
