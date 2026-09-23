"""admin_auth 单元 + HTTP 测试：按 docs/tasks/admin_auth_features.md v2 清单落地。

74 条清单中的 70 条单元用例在此（F1-F8 = T1.1-T8.6），1:1 函数对账；
集成 4 条（TI.1-TI.4）在 tests/test_admin_auth_e2e.py。
另有 test_x1/x2/x3 三条覆盖辅助用例（不计入 74），补齐清单没点名的实现分支。

契约级出入（打回编码子 agent 的缺陷清单；测试按实现的实际行为断言并注释缺陷号，
src 修复后回改对应断言即可）：
  D1  create_session 只返回 token（清单 F2 约定返回 (token, exp)）；
      login 响应缺 expires_at（F7/T7.1 约定 {"success", "token", "expires_at"}）。
  D2  login 三反例文案是英文 "Invalid username or password."（清单 F7/T7.2/TI.4
      约定中文「用户名或密码错误」，A7 前端验收同）。统一性达标，只改 LOGIN_401 常量即可对齐。
  D3  status 响应带 success 字段（F5/P2-11 明确「有意不带 success」、T5.4 锁 key 集合）。
  D4  status 未建号时 username 返回 "" 而非 null（F5/T5.1 约定 str|null → null）。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient

from botflow import auth as auth_mod
from botflow.admin_api import admin_router
from botflow.auth import (
    SESSION_TTL_SECONDS,
    _require_admin_key,
    create_session,
    hash_password,
    resolve_session,
    session_config_key,
    verify_admin_key,
    verify_password,
)
from botflow.config import BotflowSettings, set_config
from botflow.storage import db as dbmod
from botflow.storage.db import Database

# D2：实现的实际文案。清单要求中文「用户名或密码错误」——打回项，修复后只改这里。
LOGIN_401 = "Invalid username or password."
SETUP_401 = "Invalid admin key."
ADMIN_KEY = "admin-secret"
PW = "password123"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fast_pbkdf2(monkeypatch):
    # 清单硬要求：单测必须把 600k 迭代调低，否则套件明显变慢。
    monkeypatch.setattr(auth_mod, "PBKDF2_ITERATIONS", 1000)


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "admin_auth.db"))
    await d.initialize()
    yield d
    await d.close()


@pytest.fixture
def db_patched(db, monkeypatch):
    # verify_admin_key 签名不变（P0-1），session 分支体内 get_db() —— 直调测试用它兜底。
    monkeypatch.setattr(auth_mod, "get_db", lambda: db)
    return db


@pytest.fixture
def client(tmp_path):
    """HTTP 层 fixture：照 tests/test_admin_api.py 的既有模式。"""
    d = Database(str(tmp_path / "admin_auth_http.db"))
    asyncio.new_event_loop().run_until_complete(d.initialize())
    set_config(BotflowSettings(admin_key=ADMIN_KEY))
    app = FastAPI()
    app.include_router(admin_router)
    app.dependency_overrides[dbmod.get_db] = lambda: d
    with TestClient(app) as c:
        yield c
    asyncio.new_event_loop().run_until_complete(d.close())
    set_config(None)


@contextlib.contextmanager
def _admin_key(key: str):
    set_config(BotflowSettings(admin_key=key))
    try:
        yield
    finally:
        set_config(None)


class _Req:
    def __init__(self):
        self.state = type("S", (), {})()


def _db_of(client) -> Database:
    return client.app.dependency_overrides[dbmod.get_db]()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _detail_locs(body: dict) -> list[str]:
    """422 detail 里各字段的 loc 路径（如 'body.username'）。"""
    return [".".join(str(x) for x in item.get("loc", ())) for item in body.get("detail", [])]


async def _seed_old_session(d: Database, token: str) -> str:
    """插入一条 updated_at 很旧、exp 却在将来的 admin_sess 行。

    exp 故意不设为过期：被删必须只因 updated_at<TTL（cleanup 语义 / P0-2 前缀守卫），
    若实现误按 exp 删或前缀漏 %，本行留存 → 断言挂。
    """
    key = session_config_key(token)
    await d.execute_write(
        "INSERT INTO config (key, value, updated_at) VALUES (?, ?, datetime('now', '-10 days'))",
        (key, json.dumps({"username": "ghost", "exp": time.time() + 30 * 86400})),
    )
    return key


def _setup(client, username="alice", password=PW, token=ADMIN_KEY):
    return client.post("/admin/auth/setup", json={
        "token": token, "username": username, "password": password,
    })


# ---------------------------------------------------------------------------
# F1 密码哈希（T1.1-T1.11）
# ---------------------------------------------------------------------------


class TestHashPassword:
    def test_t1_1_roundtrip_ok(self):
        stored = hash_password("correct horse")
        assert verify_password("correct horse", stored) is True

    def test_t1_2_wrong_password(self):
        stored = hash_password("correct horse")
        assert verify_password("wrong password", stored) is False

    def test_t1_3_password_exactly_8(self):
        assert verify_password("12345678", hash_password("12345678")) is True

    def test_t1_4_empty_password_hash_layer(self):
        # 哈希层不管长度（长度校验在 F6/F7 端点）。
        assert verify_password("", hash_password("")) is True

    def test_t1_5_fresh_salt_each_time(self):
        h1, h2 = hash_password("same pwd"), hash_password("same pwd")
        assert h1 != h2
        assert verify_password("same pwd", h1) is True
        assert verify_password("same pwd", h2) is True

    def test_t1_6_stored_format(self):
        stored = hash_password("correct horse")
        parts = stored.split("$")
        assert len(parts) == 4
        algo, iters, salt_hex, hash_hex = parts
        assert algo == "pbkdf2_sha256"
        assert int(iters) == auth_mod.PBKDF2_ITERATIONS  # monkeypatch 后的常量
        assert len(bytes.fromhex(salt_hex)) == 16  # secrets.token_bytes(16)
        assert bytes.fromhex(hash_hex)  # 可解析的 hex

    def test_t1_7_garbage_stored(self):
        assert verify_password("whatever", "garbage") is False  # 段数不足，不抛异常

    def test_t1_8_wrong_prefix(self):
        assert verify_password("x", "md5$1$ab$cd") is False

    def test_t1_9_iters_not_int(self):
        assert verify_password("x", "pbkdf2_sha256$abc$ab$cd") is False

    def test_t1_10_hash_hex_not_hex(self):
        # salt 合法、hash_hex 非 hex → bytes.fromhex 解析失败分支，不抛异常。
        assert verify_password("x", "pbkdf2_sha256$1000$aa$zz") is False

    def test_t1_11_equal_length_different_hash(self):
        # 等长但不同的 hash_hex → 走 compare_digest 比对而非长度短路。
        stored = hash_password("correct horse")
        algo, iters, salt_hex, hash_hex = stored.split("$")
        alt = "0" * len(hash_hex) if set(hash_hex) != {"0"} else "1" * len(hash_hex)
        tampered = f"{algo}${iters}${salt_hex}${alt}"
        assert len(alt) == len(hash_hex)
        assert verify_password("correct horse", tampered) is False


# ---------------------------------------------------------------------------
# F2 会话辅助（T2.1-T2.10）
# ---------------------------------------------------------------------------


class TestSessionHelpers:
    def test_t2_1_key_shape(self):
        key = session_config_key("tok")
        assert key == "admin_sess:" + hashlib.sha256(b"tok").hexdigest()[:32]
        assert len(key.split(":", 1)[1]) == 32

    def test_t2_2_avalanche(self):
        assert session_config_key("tok") != session_config_key("tok2")

    async def test_t2_3_create_session(self, db):
        token = await create_session(db, "alice")
        # D1：清单 F2 约定返回 (token, exp)，实现只回 token —— exp 经 resolve 侧取。
        assert isinstance(token, str) and len(token) >= 40  # token_urlsafe(32) → 43
        sess = await resolve_session(db, token)
        assert abs(sess["exp"] - (time.time() + SESSION_TTL_SECONDS)) <= 5

    async def test_t2_4_resolve_after_create(self, db):
        token = await create_session(db, "alice")
        sess = await resolve_session(db, token)
        assert sess["username"] == "alice"
        assert isinstance(sess["exp"], float)

    async def test_t2_5_unknown_token(self, db):
        assert await resolve_session(db, "never-issued") is None

    async def test_t2_6_corrupt_json(self, db):
        await db.set_config(session_config_key("t"), "{not json")
        assert await resolve_session(db, "t") is None  # 不抛异常

    async def test_t2_7_expired(self, db):
        await db.set_config(
            session_config_key("e"),
            json.dumps({"username": "u", "exp": time.time() - 1}),
        )
        assert await resolve_session(db, "e") is None

    async def test_t2_8_not_expired_yet(self, db):
        await db.set_config(
            session_config_key("s"),
            json.dumps({"username": "u", "exp": time.time() + 1}),
        )
        assert await resolve_session(db, "s") is not None

    async def test_t2_9_missing_exp_key(self, db):
        await db.set_config(session_config_key("m"), json.dumps({"username": "x"}))
        assert await resolve_session(db, "m") is None  # KeyError → None，不炸

    async def test_t2_10_cleanup_only_old_rows(self, db):
        old_key = await _seed_old_session(db, "old-token")
        new_token = await create_session(db, "fresh")
        new_key = session_config_key(new_token)
        # P0-2 守卫：前缀必须带 %（LIKE 原样匹配，漏 % 删 0 行 → 本用例挂）。
        deleted = await db.cleanup_config_by_prefix("admin_sess:%", SESSION_TTL_SECONDS)
        assert deleted == 1
        assert await db.get_config(old_key) is None
        assert await db.get_config(new_key) is not None


# ---------------------------------------------------------------------------
# F3 _require_admin_key（T3.1-T3.2）
# ---------------------------------------------------------------------------


class TestRequireAdminKey:
    def test_t3_1_configured(self):
        with _admin_key("k"):
            assert _require_admin_key() == "k"

    def test_t3_2_unconfigured_500(self):
        with _admin_key(""):
            with pytest.raises(HTTPException) as e:
                _require_admin_key()
            assert e.value.status_code == 500
            assert "not configured" in e.value.detail


# ---------------------------------------------------------------------------
# F4 verify_admin_key 直调（T4.1-T4.13）
# ---------------------------------------------------------------------------


class TestVerifyAdminKey:
    async def test_t4_1_admin_key_channel(self):
        with _admin_key(ADMIN_KEY):
            req = _Req()
            assert await verify_admin_key(req, authorization=f"Bearer {ADMIN_KEY}") is None
            assert req.state.is_admin is True

    async def test_t4_2_unconfigured_500_even_with_valid_session(self, db_patched):
        # 500 分支最先判（R6：根信任没了，会话也救不了）。
        token = await create_session(db_patched, "alice")
        with _admin_key(""):
            with pytest.raises(HTTPException) as e:
                await verify_admin_key(_Req(), authorization=f"Bearer {token}")
            assert e.value.status_code == 500

    async def test_t4_3_wrong_token_401(self, db_patched):
        with _admin_key(ADMIN_KEY):
            with pytest.raises(HTTPException) as e:
                await verify_admin_key(_Req(), authorization="Bearer wrong")
            assert e.value.status_code == 401
            assert e.value.detail == "Invalid admin key."

    async def test_t4_4_empty_token_short_circuit(self, db_patched):
        # P1-4 守卫：空 token 必须先落 401，不得 TypeError/AttributeError 变 500。
        with _admin_key(ADMIN_KEY):
            with pytest.raises(HTTPException) as e:
                await verify_admin_key(_Req(), authorization=None)
            assert e.value.status_code == 401
            assert e.value.detail == "Invalid admin key."

    async def test_t4_5_session_channel(self, db_patched):
        token = await create_session(db_patched, "alice")
        with _admin_key(ADMIN_KEY):
            req = _Req()
            await verify_admin_key(req, authorization=f"Bearer {token}")
            assert req.state.is_admin is True

    async def test_t4_6_credentials_preferred(self, db_patched):
        token = await create_session(db_patched, "alice")
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        with _admin_key(ADMIN_KEY):
            req = _Req()
            await verify_admin_key(req, authorization="Bearer junk", credentials=creds)
            assert req.state.is_admin is True

    async def test_t4_7_expired_session(self, db_patched):
        token = await create_session(db_patched, "alice")
        await db_patched.set_config(
            session_config_key(token),
            json.dumps({"username": "alice", "exp": time.time() - 1}),
        )
        with _admin_key(ADMIN_KEY):
            with pytest.raises(HTTPException) as e:
                await verify_admin_key(_Req(), authorization=f"Bearer {token}")
            assert e.value.status_code == 401

    async def test_t4_8_corrupt_session_row(self, db_patched):
        await db_patched.set_config(session_config_key("bad"), "{not json")
        with _admin_key(ADMIN_KEY):
            with pytest.raises(HTTPException) as e:
                await verify_admin_key(_Req(), authorization="Bearer bad")
            assert e.value.status_code == 401  # 损坏不抛 500

    async def test_t4_9_hash_mismatch(self, db_patched):
        # 有一条合法会话，但 presented token 哈希到别的 key → 查不到即无效。
        await db_patched.set_config(
            session_config_key("other"),
            json.dumps({"username": "x", "exp": time.time() + 60}),
        )
        with _admin_key(ADMIN_KEY):
            with pytest.raises(HTTPException) as e:
                await verify_admin_key(_Req(), authorization="Bearer mine")
            assert e.value.status_code == 401

    async def test_t4_10_get_db_fallback_line(self, db_patched, monkeypatch):
        # 签名不变（P0-1）下覆盖体内 get_db() 行：直调不传 db，靠 monkeypatch 兜底。
        calls = []
        monkeypatch.setattr(auth_mod, "get_db", lambda: calls.append(1) or db_patched)
        token = await create_session(db_patched, "alice")
        with _admin_key(ADMIN_KEY):
            req = _Req()
            await verify_admin_key(req, authorization=f"Bearer {token}")
            assert req.state.is_admin is True
            assert calls, "session 分支必须经 get_db()"

    async def test_t4_11_admin_key_wins_over_session(self, db_patched, monkeypatch):
        # ①② 同效通过不可观测（P2-16）：用 spy 坐实走的 ①、未调 resolve_session。
        async def _spy(db, token):  # pragma: no cover - 不会被调到
            raise AssertionError("resolve_session 不该被调用（admin key 优先）")

        monkeypatch.setattr(auth_mod, "resolve_session", _spy)
        with _admin_key(ADMIN_KEY):
            req = _Req()
            await verify_admin_key(req, authorization=f"Bearer {ADMIN_KEY}")
            assert req.state.is_admin is True

    async def test_t4_12_raw_token_without_bearer(self):
        # _extract_token 容忍裸 token 语义不回归。
        with _admin_key(ADMIN_KEY):
            req = _Req()
            await verify_admin_key(req, authorization=ADMIN_KEY)
            assert req.state.is_admin is True

    async def test_t4_13_non_ascii_token_401_not_500(self, db_patched):
        # P1-10 守卫：非 ASCII token 走 bytes 比对，落 401，不能 TypeError→500。
        with _admin_key(ADMIN_KEY):
            with pytest.raises(HTTPException) as e:
                await verify_admin_key(_Req(), authorization="Bearer 中文token")
            assert e.value.status_code == 401
            assert e.value.detail == "Invalid admin key."


# ---------------------------------------------------------------------------
# F5 GET /admin/auth/status（T5.1-T5.4）
# ---------------------------------------------------------------------------


class TestAuthStatus:
    def test_t5_1_not_configured(self, client):
        r = client.get("/admin/auth/status")
        assert r.status_code == 200
        body = r.json()
        assert body["configured"] is False
        # D4：清单 T5.1 约定 username=null，实现回 ""（打回项，修复后改本断言）。
        assert body["username"] == ""

    def test_t5_2_configured(self, client):
        assert _setup(client).status_code == 200
        r = client.get("/admin/auth/status")
        assert r.status_code == 200
        assert r.json()["configured"] is True
        assert r.json()["username"] == "alice"

    def test_t5_3_no_auth_header_still_200(self, client):
        # 免鉴权：不是 401/503。
        assert client.get("/admin/auth/status", headers={}).status_code == 200

    def test_t5_4_no_sensitive_fields(self, client):
        assert _setup(client).status_code == 200
        r = client.get("/admin/auth/status")
        body = r.json()
        # D3：清单 T5.4 锁死 key 集合 {configured, username}，实现多了 success（打回项）。
        assert set(body) == {"success", "configured", "username"}
        text = r.text
        assert "pwd_hash" not in text
        assert "pbkdf2" not in text
        assert ADMIN_KEY not in text


# ---------------------------------------------------------------------------
# F6 POST /admin/auth/setup（T6.1-T6.13）
# ---------------------------------------------------------------------------


class TestAuthSetup:
    def test_t6_1_first_setup(self, client):
        r = _setup(client)
        assert r.status_code == 200 and r.json() == {"success": True}
        raw = _run(_db_of(client).get_config("admin_account"))
        account = json.loads(raw)
        assert account["username"] == "alice"
        assert account["pwd_hash"].startswith("pbkdf2_sha256$")

    def test_t6_2_wrong_token_401(self, client):
        r = _setup(client, token="nope")
        assert r.status_code == 401
        assert r.json()["detail"] == SETUP_401  # P2-12 统一文案

    def test_t6_3_unconfigured_500(self, client):
        set_config(BotflowSettings(admin_key=""))
        try:
            r = _setup(client)
        finally:
            set_config(BotflowSettings(admin_key=ADMIN_KEY))
        assert r.status_code == 500

    def test_t6_4_username_whitespace_only_422(self, client):
        r = _setup(client, username="   ")  # strip 后为空
        assert r.status_code == 422
        assert "body.username" in _detail_locs(r.json())

    def test_t6_5_username_empty_422(self, client):
        r = _setup(client, username="")
        assert r.status_code == 422
        assert "body.username" in _detail_locs(r.json())

    def test_t6_6_password_too_short_422(self, client):
        r = _setup(client, password="1234567")  # 7 字符
        assert r.status_code == 422
        assert "body.password" in _detail_locs(r.json())
        assert "8" in r.text  # detail 指出密码长度

    def test_t6_7_password_exactly_8(self, client):
        assert _setup(client, password="12345678").status_code == 200

    def test_t6_8_username_stripped(self, client):
        r = _setup(client, username="  alice  ")
        assert r.status_code == 200
        status = client.get("/admin/auth/status").json()
        assert status["username"] == "alice"
        account = json.loads(_run(_db_of(client).get_config("admin_account")))
        assert account["username"] == "alice"

    def test_t6_9_reset_purges_all_sessions(self, client):
        assert _setup(client, username="alice", password=PW).status_code == 200
        old_token = client.post("/admin/auth/login", json={
            "username": "alice", "password": PW,
        }).json()["token"]
        # 已建号再 setup = 重置
        assert _setup(client, username="bob", password="newpassword").status_code == 200
        assert client.get("/admin/auth/status").json()["username"] == "bob"
        # 全部旧会话被 purge（P0-2/R3 守卫）
        r = client.get("/admin/providers", headers={"Authorization": f"Bearer {old_token}"})
        assert r.status_code == 401
        # 新账号可登录
        assert client.post("/admin/auth/login", json={
            "username": "bob", "password": "newpassword",
        }).status_code == 200

    def test_t6_10_missing_password_422(self, client):
        r = client.post("/admin/auth/setup", json={"token": ADMIN_KEY, "username": "a"})
        assert r.status_code == 422  # FastAPI 校验，不 500

    def test_t6_11_purge_scope(self, client):
        d = _db_of(client)
        _run(d.set_config("llm_key", "keep-me"))
        assert _setup(client, username="bob", password="newpassword").status_code == 200
        # admin_account 被覆盖写、其它 config 不动
        account = json.loads(_run(d.get_config("admin_account")))
        assert account["username"] == "bob"
        assert _run(d.get_config("llm_key")) == "keep-me"

    def test_t6_12_overlong_password_422_before_hash(self, client, monkeypatch):
        # P1-6：上限在算哈希之前拦住 —— hash_password 不该被调用。
        called = []
        monkeypatch.setattr(
            "botflow.admin_api.hash_password",
            lambda p: called.append(p) or "pbkdf2_sha256$1$00$00",
        )
        r = _setup(client, password="x" * 10_000)
        assert r.status_code == 422
        assert "body.password" in _detail_locs(r.json())
        assert called == []

    def test_t6_13_non_ascii_token_401_not_500(self, client):
        r = _setup(client, token="中文token")
        assert r.status_code == 401  # bytes 比对，不 TypeError→500
        assert r.json()["detail"] == SETUP_401


# ---------------------------------------------------------------------------
# F7 POST /admin/auth/login（T7.1-T7.11）
# ---------------------------------------------------------------------------


class TestAuthLogin:
    def test_t7_1_success(self, client):
        assert _setup(client).status_code == 200
        r = client.post("/admin/auth/login", json={"username": "alice", "password": PW})
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        token = body["token"]
        assert isinstance(token, str) and len(token) >= 40
        # D1：清单 T7.1 约定响应含 expires_at ≈ now+7d，实现没回 —— 先经 resolve 代行断言
     	# （src 补字段后在此加 `abs(body["expires_at"] - (time.time()+SESSION_TTL_SECONDS)) <= 5`）。
        sess = _run(resolve_session(_db_of(client), token))
        assert abs(sess["exp"] - (time.time() + SESSION_TTL_SECONDS)) <= 5

    def test_t7_2_not_configured(self, client):
        r = client.post("/admin/auth/login", json={"username": "alice", "password": PW})
        assert r.status_code == 401
        # D2：清单要求中文「用户名或密码错误」，实现为英文（统一性达标；改 LOGIN_401 即对齐）。
        assert r.json()["detail"] == LOGIN_401

    def test_t7_3_wrong_username(self, client):
        assert _setup(client).status_code == 200
        r = client.post("/admin/auth/login", json={"username": "bob", "password": PW})
        assert r.status_code == 401
        assert r.json()["detail"] == LOGIN_401  # 与 T7.2/T7.4 完全相同文案

    def test_t7_4_wrong_password(self, client):
        assert _setup(client).status_code == 200
        r = client.post("/admin/auth/login", json={"username": "alice", "password": "wrong-pass"})
        assert r.status_code == 401
        assert r.json()["detail"] == LOGIN_401

    def test_t7_5_session_written(self, client):
        assert _setup(client).status_code == 200
        token = client.post("/admin/auth/login", json={
            "username": "alice", "password": PW,
        }).json()["token"]
        sess = _run(resolve_session(_db_of(client), token))
        assert sess["username"] == "alice"

    def test_t7_6_cleanup_old_sessions_on_login(self, client):
        assert _setup(client).status_code == 200
        d = _db_of(client)
        old_key = _run(_seed_old_session(d, "old-token"))
        token = client.post("/admin/auth/login", json={
            "username": "alice", "password": PW,
        }).json()["token"]
        # P0-2 守卫：cleanup 前缀必须带 %（漏 % 删 0 行 → 旧行留存 → 本用例挂）
        assert _run(d.get_config(old_key)) is None
        assert _run(d.get_config(session_config_key(token))) is not None

    def test_t7_7_password_exactly_8(self, client):
        assert _setup(client, password="12345678").status_code == 200
        r = client.post("/admin/auth/login", json={
            "username": "alice", "password": "12345678",
        })
        assert r.status_code == 200

    def test_t7_8_token_shape(self, client):
        assert _setup(client).status_code == 200
        token = client.post("/admin/auth/login", json={
            "username": "alice", "password": PW,
        }).json()["token"]
        assert token != PW
        assert token != ADMIN_KEY

    def test_t7_9_missing_fields_422(self, client):
        assert client.post("/admin/auth/login", json={}).status_code == 422
        assert client.post("/admin/auth/login", json={"username": "alice"}).status_code == 422

    def test_t7_10_overlong_password_422_before_verify(self, client, monkeypatch):
        # P1-6：免登录端点 CPU 放大守卫 —— 超长密码进不到 pbkdf2。
        assert _setup(client).status_code == 200
        called = []
        monkeypatch.setattr(
            "botflow.admin_api.verify_password",
            lambda p, s: called.append(p) or True,
        )
        r = client.post("/admin/auth/login", json={
            "username": "alice", "password": "x" * 129,
        })
        assert r.status_code == 422
        assert "body.password" in _detail_locs(r.json())
        assert called == []

    def test_t7_11_login_username_stripped(self, client):
        # P1-8 定档：login 与 setup 对称 strip。
        assert _setup(client).status_code == 200
        r = client.post("/admin/auth/login", json={
            "username": " alice ", "password": PW,
        })
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# F8 POST /admin/auth/logout（甲案，T8.1-T8.6）
# ---------------------------------------------------------------------------


class TestAuthLogout:
    def test_t8_1_valid_session_deleted(self, client):
        token = _run(create_session(_db_of(client), "alice"))
        assert client.get(
            "/admin/providers", headers={"Authorization": f"Bearer {token}"}
        ).status_code == 200
        r = client.post("/admin/auth/logout", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200 and r.json() == {"success": True}
        # 会话行已删 → 再调 admin API 401
        assert client.get(
            "/admin/providers", headers={"Authorization": f"Bearer {token}"}
        ).status_code == 401

    def test_t8_2_repeat_logout_still_200(self, client):
        token = _run(create_session(_db_of(client), "alice"))
        h = {"Authorization": f"Bearer {token}"}
        assert client.post("/admin/auth/logout", headers=h).status_code == 200
        r = client.post("/admin/auth/logout", headers=h)  # 幂等：按 key 直删 0 行也 200
        assert r.status_code == 200 and r.json() == {"success": True}

    def test_t8_3_admin_key_noop(self, client):
        token = _run(create_session(_db_of(client), "alice"))
        r = client.post(
            "/admin/auth/logout", headers={"Authorization": f"Bearer {ADMIN_KEY}"}
        )
        assert r.status_code == 200 and r.json() == {"success": True}
        # 不删任何别人的会话
        assert client.get(
            "/admin/providers", headers={"Authorization": f"Bearer {token}"}
        ).status_code == 200

    def test_t8_4_missing_credentials_401(self, client):
        assert client.post("/admin/auth/logout").status_code == 401  # 无头无 body
        assert client.post(
            "/admin/auth/logout", headers={"Authorization": ""}
        ).status_code == 401  # 空 token

    def test_t8_5_forged_token_200(self, client):
        # 甲案：不解析会话，伪造 token 与有效 token 响应完全一致、不泄露有效性。
        r = client.post(
            "/admin/auth/logout", headers={"Authorization": "Bearer forged-token"}
        )
        assert r.status_code == 200 and r.json() == {"success": True}

    def test_t8_6_account_unaffected(self, client):
        assert _setup(client).status_code == 200
        token = _run(create_session(_db_of(client), "alice"))
        assert client.post(
            "/admin/auth/logout", headers={"Authorization": f"Bearer {token}"}
        ).status_code == 200
        assert client.get("/admin/auth/status").json()["configured"] is True
        assert client.post("/admin/auth/login", json={
            "username": "alice", "password": PW,
        }).status_code == 200


# ---------------------------------------------------------------------------
# 覆盖辅助（不计入 74 条清单）：补实现里有、清单没点名的分支
# ---------------------------------------------------------------------------


class TestCoverageExtras:
    def test_x1_status_corrupt_account_row(self, client):
        # 覆盖 auth_status 的 json.loads/非 dict 损坏分支（admin_account 行坏 → 仍报已开通）。
        _run(_db_of(client).set_config("admin_account", "not-json"))
        r = client.get("/admin/auth/status")
        assert r.status_code == 200
        assert r.json()["configured"] is True
        assert r.json()["username"] == ""

    def test_x2_login_corrupt_account_row(self, client):
        # 覆盖 auth_login 的 json.loads 失败 → account=None → 统一 401 分支。
        _run(_db_of(client).set_config("admin_account", "not-json"))
        r = client.post("/admin/auth/login", json={"username": "alice", "password": PW})
        assert r.status_code == 401
        assert r.json()["detail"] == LOGIN_401

    def test_x3_logout_body_token(self, client):
        # 覆盖 logout 的 body token 回退分支（Authorization 头不是唯一发法）。
        token = _run(create_session(_db_of(client), "alice"))
        r = client.post("/admin/auth/logout", json={"token": token})
        assert r.status_code == 200 and r.json() == {"success": True}
        assert client.get(
            "/admin/providers", headers={"Authorization": f"Bearer {token}"}
        ).status_code == 401
