"""Tests for botflow.admin_api REST management endpoints (100% coverage)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
        p = client.post("/admin/providers", params={"name": "openai", "type": "openai",
                                                    "base_url": "https://api.openai.com/v1",
                                                    "api_key": "sk-x"}, headers=AUTH)
        assert p.status_code == 200 and p.json()["success"] is True
        pid = p.json()["provider_id"]
        assert client.get("/admin/providers", headers=AUTH).json()["providers"]
        assert client.get(f"/admin/providers/{pid}", headers=AUTH).json()["success"] is True
        u = client.patch(f"/admin/providers/{pid}", params={"base_url": "https://new"}, headers=AUTH)
        assert u.status_code == 200 and u.json()["provider_id"] == pid
        assert client.get(f"/admin/providers/{pid}", headers=AUTH).json()["provider"]["base_url"] == "https://new"
        assert client.get(f"/admin/providers/{pid}", headers=AUTH).status_code == 200
        assert client.delete(f"/admin/providers/{pid}", headers=AUTH).json()["success"] is True
        assert client.get(f"/admin/providers/{pid}", headers=AUTH).status_code == 404

    def test_create_without_type(self, client):
        p = client.post("/admin/providers", params={"name": "x", "base_url": "https://x"}, headers=AUTH)
        assert p.status_code == 200 and p.json()["success"] is True

    def test_update_missing(self, client):
        assert client.patch("/admin/providers/9999", params={"base_url": "x"}, headers=AUTH).status_code == 404

    def test_delete_missing(self, client):
        assert client.delete("/admin/providers/9999", headers=AUTH).status_code == 404


class TestModels:
    def test_crud(self, client):
        pid = client.post("/admin/providers", params={"name": "openai", "base_url": "https://x"}, headers=AUTH).json()["provider_id"]
        m = client.post("/admin/models", params={"provider_id": pid, "name": "gpt-4", "type": "openai"}, headers=AUTH)
        assert m.status_code == 200 and m.json()["success"] is True
        mid = m.json()["model_id"]
        assert client.get("/admin/models", headers=AUTH).json()["models"]
        assert client.get(f"/admin/models/{mid}", headers=AUTH).json()["success"] is True
        u = client.patch(f"/admin/models/{mid}", params={"display_name": "GPT4"}, headers=AUTH)
        assert u.status_code == 200 and u.json()["model_id"] == mid
        assert client.get(f"/admin/models/{mid}", headers=AUTH).json()["model"]["display_name"] == "GPT4"
        assert client.delete(f"/admin/models/{mid}", headers=AUTH).json()["success"] is True
        assert client.get(f"/admin/models/{mid}", headers=AUTH).status_code == 404

    def test_create_without_type(self, client):
        pid = client.post("/admin/providers", params={"name": "openai", "base_url": "https://x"}, headers=AUTH).json()["provider_id"]
        m = client.post("/admin/models", params={"provider_id": pid, "name": "m"}, headers=AUTH)
        assert m.status_code == 200

    def test_update_missing(self, client):
        assert client.patch("/admin/models/9999", params={"display_name": "x"}, headers=AUTH).status_code == 404

    def test_create_with_missing_provider(self, client):
        m = client.post("/admin/models", params={"provider_id": 9999, "name": "ghost"}, headers=AUTH)
        assert m.status_code == 404

    def test_delete_missing(self, client):
        assert client.delete("/admin/models/9999", headers=AUTH).status_code == 404


class TestGroups:
    def test_crud(self, client):
        g = client.post("/admin/groups", params={"name": "prod", "description": "d"}, headers=AUTH)
        assert g.status_code == 200 and g.json()["success"] is True
        gid = g.json()["group_id"]
        assert client.get("/admin/groups", headers=AUTH).json()["groups"]
        assert client.get(f"/admin/groups/{gid}", headers=AUTH).json()["success"] is True
        u = client.patch(f"/admin/groups/{gid}", params={"description": "up"}, headers=AUTH)
        assert u.status_code == 200 and u.json()["group_id"] == gid
        assert client.delete(f"/admin/groups/{gid}", headers=AUTH).json()["success"] is True
        assert client.get(f"/admin/groups/{gid}", headers=AUTH).status_code == 404

    def test_missing_branches(self, client):
        assert client.get("/admin/groups/9999", headers=AUTH).status_code == 404
        assert client.patch("/admin/groups/9999", params={}, headers=AUTH).status_code == 404
        assert client.delete("/admin/groups/9999", headers=AUTH).status_code == 404


class TestGroupModels:
    def test_full(self, client):
        pid = client.post("/admin/providers", params={"name": "openai", "base_url": "https://x"}, headers=AUTH).json()["provider_id"]
        mid = client.post("/admin/models", params={"provider_id": pid, "name": "gpt-4"}, headers=AUTH).json()["model_id"]
        gid = client.post("/admin/groups", params={"name": "prod"}, headers=AUTH).json()["group_id"]
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
        gid = client.post("/admin/groups", params={"name": "g"}, headers=AUTH).json()["group_id"]
        assert client.post(f"/admin/groups/{gid}/models", params={"model_id": 9999}, headers=AUTH).status_code == 404
        assert client.get("/admin/groups/9999/details", headers=AUTH).status_code == 404
        assert client.patch(f"/admin/groups/{gid}/models/9999", params={"weight": 1}, headers=AUTH).status_code == 200
        assert client.delete(f"/admin/groups/{gid}/models/9999", headers=AUTH).status_code == 200


class TestStats:
    def test_models(self, client):
        pid = client.post("/admin/providers", params={"name": "openai", "base_url": "https://x"}, headers=AUTH).json()["provider_id"]
        mid = client.post("/admin/models", params={"provider_id": pid, "name": "gpt-4"}, headers=AUTH).json()["model_id"]
        r = client.get("/admin/stats/models", headers=AUTH)
        assert r.status_code == 200 and r.json()["success"] is True
        r2 = client.get("/admin/stats/models", params={"api_key_id": 1}, headers=AUTH)
        assert r2.status_code == 200

    def test_groups(self, client):
        gid = client.post("/admin/groups", params={"name": "empty"}, headers=AUTH).json()["group_id"]
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


class TestSummaries:
    def test_get_summary(self, client):
        r = client.get("/admin/summaries/2099-01-01", headers=AUTH)
        assert r.status_code == 404


class TestApiKeys:
    def test_crud_and_redaction(self, client):
        assert client.get("/admin/apikeys", headers=AUTH).json()["api_keys"] == []
        c = client.post("/admin/apikeys", params={"raw_key": "secret-key", "label": "team-a"}, headers=AUTH)
        assert c.status_code == 200
        body = c.json()
        assert body["success"] is True and "key_hash_prefix" in body
        assert body["label"] == "team-a"
        assert "secret-key" not in str(body)
        kid = body["id"]
        listed = client.get("/admin/apikeys", headers=AUTH).json()["api_keys"]
        assert len(listed) == 1 and "secret-key" not in str(listed)
        d = client.patch(f"/admin/apikeys/{kid}", params={"is_enabled": False}, headers=AUTH)
        assert d.status_code == 200 and d.json()["is_enabled"] is False
        rm = client.delete(f"/admin/apikeys/{kid}", headers=AUTH)
        assert rm.status_code == 200 and rm.json()["success"] is True
        assert client.get("/admin/apikeys", headers=AUTH).json()["api_keys"] == []

    def test_not_found(self, client):
        assert client.patch("/admin/apikeys/9999", params={"is_enabled": False}, headers=AUTH).status_code == 404
        assert client.delete("/admin/apikeys/9999", headers=AUTH).status_code == 404
