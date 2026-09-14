"""Tests for botflow.admin_api REST management endpoints (100% coverage)."""

from __future__ import annotations

import asyncio
import json
import os
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botflow.admin_dashboard import mount_admin_ui
from botflow.admin_api import admin_router
from botflow.config import BotflowSettings, set_config
from botflow.storage import db as dbmod
from botflow.storage.db import Database
from botflow.storage.models import CallLog, Model, Provider


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
