"""Tests for botflow.auth (multi-key resolution + admin key verification)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from botflow.auth import _extract_token, resolve_api_key, verify_admin_key, verify_llm_key
from botflow.config import BotflowSettings, set_config
from botflow.storage.db import Database
from botflow.storage.models import Provider


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "test.db"))
    await d.initialize()
    yield d
    await d.close()


async def _provider(d):
    pid = await d.create_provider(Provider(name="p", provider_type="openai"))
    await d.create_model(__import__("botflow.storage.models", fromlist=["Model"]).Model(name="m", provider_id=pid))
    return pid


class TestExtractToken:
    def test_none(self):
        assert _extract_token(None) is None

    def test_bearer(self):
        assert _extract_token("Bearer abc") == "abc"

    def test_empty_after_scheme(self):
        # "Bearer " -> scheme Bearer, token empty -> returns stripped header
        assert _extract_token("Bearer ") == "Bearer"

    def test_raw_key(self):
        assert _extract_token("rawkey") == "rawkey"


class TestResolveApiKey:
    async def test_matches_enabled_key(self, db):
        await _provider(db)
        k = await db.create_api_key("secret-1", label="team-a")
        resolved = await resolve_api_key(db, "secret-1")
        assert resolved is not None and resolved.id == k.id and resolved.is_enabled

    async def test_disabled_key_rejected(self, db):
        await db.create_api_key("secret-2", label="x")
        await db.set_api_key_enabled(1, False)
        assert await resolve_api_key(db, "secret-2") is None

    async def test_wrong_key_rejected(self, db):
        await db.create_api_key("secret-3", label="x")
        assert await resolve_api_key(db, "nope") is None

    async def test_no_keys_legacy_match(self, db, monkeypatch):
        set_config(BotflowSettings(llm_key="legacy-key"))
        try:
            await db.set_config("llm_key", "legacy-key")
            ak = await resolve_api_key(db, "legacy-key")
            assert ak.id == 0 and ak.label == "legacy"
            assert await resolve_api_key(db, "wrong") is None
        finally:
            set_config(None)
            await db.set_config("llm_key", "")

    async def test_no_keys_no_legacy(self, db, monkeypatch):
        set_config(BotflowSettings(llm_key=""))
        try:
            assert await resolve_api_key(db, "anything") is None
        finally:
            set_config(None)


class TestVerifyLLMKey:
    async def test_missing(self, db):
        class Req:
            state = type("S", (), {})()
        with pytest.raises(HTTPException) as e:
            await verify_llm_key(Req(), authorization=None, db=db)
        assert e.value.status_code == 401
        assert "Missing" in e.value.detail

    async def test_invalid(self, db):
        class Req:
            state = type("S", (), {})()
        with pytest.raises(HTTPException) as e:
            await verify_llm_key(Req(), authorization="Bearer bad", db=db)
        assert e.value.status_code == 401

    async def test_valid_stashes_state(self, db, monkeypatch):
        set_config(BotflowSettings(llm_key="legacy-key"))
        try:
            await db.set_config("llm_key", "legacy-key")
            class Req:
                def __init__(self):
                    self.state = type("S", (), {})()
            req = Req()
            ak = await verify_llm_key(req, authorization="legacy-key", db=db)
            assert ak.id == 0 and req.state.api_key_id == 0 and req.state.api_key is ak
        finally:
            set_config(None)
            await db.set_config("llm_key", "")

    async def test_credentials_object_preferred(self, db, monkeypatch):
        set_config(BotflowSettings(llm_key="legacy-key"))
        try:
            await db.set_config("llm_key", "legacy-key")
            class Req:
                def __init__(self):
                    self.state = type("S", (), {})()
            creds = type("C", (), {"credentials": "legacy-key"})()
            req = Req()
            ak = await verify_llm_key(req, authorization="Bearer junk", credentials=creds, db=db)
            assert ak.id == 0
        finally:
            set_config(None)
            await db.set_config("llm_key", "")

    async def test_uses_module_get_db_when_db_none(self, db, monkeypatch):
        # Cover the `if db is None: db = get_db()` branch by not passing db.
        monkeypatch.setattr("botflow.auth.get_db", lambda: db)
        set_config(BotflowSettings(llm_key="legacy-key"))
        try:
            await db.set_config("llm_key", "legacy-key")
            class Req:
                def __init__(self):
                    self.state = type("S", (), {})()
            req = Req()
            ak = await verify_llm_key(req, authorization="legacy-key")
            assert ak.id == 0
        finally:
            set_config(None)
            await db.set_config("llm_key", "")


class TestVerifyAdminKey:
    async def test_unconfigured(self, monkeypatch):
        set_config(BotflowSettings(admin_key=""))
        try:
            class Req:
                state = type("S", (), {})()
            with pytest.raises(HTTPException) as e:
                await verify_admin_key(Req(), authorization=None)
            assert e.value.status_code == 500
        finally:
            set_config(None)

    async def test_invalid(self, monkeypatch):
        set_config(BotflowSettings(admin_key="admin-secret"))
        try:
            class Req:
                state = type("S", (), {})()
            with pytest.raises(HTTPException) as e:
                await verify_admin_key(Req(), authorization="Bearer wrong")
            assert e.value.status_code == 401
        finally:
            set_config(None)

    async def test_valid(self, monkeypatch):
        set_config(BotflowSettings(admin_key="admin-secret"))
        try:
            class Req:
                def __init__(self):
                    self.state = type("S", (), {})()
            req = Req()
            await verify_admin_key(req, authorization="Bearer admin-secret")
            assert req.state.is_admin is True
        finally:
            set_config(None)

    async def test_credentials_preferred(self, monkeypatch):
        set_config(BotflowSettings(admin_key="admin-secret"))
        try:
            class Req:
                def __init__(self):
                    self.state = type("S", (), {})()
            creds = type("C", (), {"credentials": "admin-secret"})()
            req = Req()
            await verify_admin_key(req, authorization="Bearer junk", credentials=creds)
            assert req.state.is_admin is True
        finally:
            set_config(None)
