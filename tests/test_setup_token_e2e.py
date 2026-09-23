"""setup token 集成端到端 TI.1–TI.5（契约：docs/tasks/setup_token_features.md §3）。

文件级 ``pytestmark = integration``：pyproject 的 ``addopts = ["-m", "not
integration"]`` 会把它们从默认全量跑里摘掉，指名文件也逃不掉 —— 必须显式加
``-m integration``：

    PYTHONPATH=src python -m pytest tests/test_setup_token_e2e.py -m integration

（不用 ``pytest tests/ -m integration``：那会把打真实服务 127.0.0.1:4000 的
tests/test_integration.py 一并无差别收集，无实服务时被无关失败挡住。）
TestClient/ASGI 直连 app，不起真端口、不碰 live 库。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import botflow.storage.db as dbmod
from botflow import auth as auth_mod
from botflow.admin_api import admin_router
from botflow.auth import ensure_setup_token, generate_setup_token, write_setup_token_file
from botflow.config import BotflowSettings, set_config
from botflow.storage.db import Database

pytestmark = pytest.mark.integration

SETUP_401 = "Invalid setup token."
ADMIN_KEY = "admin-secret-setup-e2e"
AUTH = {"Authorization": f"Bearer {ADMIN_KEY}"}
USER = {"username": "alice", "password": "s3cret-pass"}
_HEX32 = set("0123456789abcdef")


@pytest.fixture(autouse=True)
def fast_pbkdf2(monkeypatch):
    # 链路里 setup/login 各要跑一次哈希；600k 迭代在 CI/远端仍嫌慢，调到 1000。
    monkeypatch.setattr(auth_mod, "PBKDF2_ITERATIONS", 1000)


@pytest.fixture
def client(tmp_path):
    """HTTP 层 fixture：沿用 TestClient + dependency_overrides 模式。

    Database.initialize() 会把全局 _active_db 设成 d —— verify_admin_key /
    端点体内直调的 get_db() 因此拿到同一个库，session 通道才走得通。
    """
    d = Database(str(tmp_path / "setup_token_e2e.db"))
    asyncio.new_event_loop().run_until_complete(d.initialize())
    set_config(BotflowSettings(admin_key=ADMIN_KEY))
    app = FastAPI()
    app.include_router(admin_router)
    app.dependency_overrides[dbmod.get_db] = lambda: d
    with TestClient(app) as c:
        yield c
    asyncio.new_event_loop().run_until_complete(d.close())
    set_config(None)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _db_of(client) -> Database:
    return client.app.dependency_overrides[dbmod.get_db]()


def _path():
    return auth_mod.get_config().setup_token_path


def _ensure(db) -> str:
    """跑一次启动钩子（TI.x 的「启动 ensure」前置），返回文件明文 token。"""
    _run(ensure_setup_token(db))
    token = _path().read_text(encoding="utf-8")
    assert len(token) == 32 and set(token) <= _HEX32
    return token


def _preset(db) -> str:
    """幂等预置 KV 哈希 + `.setup_token` 文件，返回明文。

    成功 setup 会双删凭证（F4），重置链（TI.5 二次 setup）必须重新预置 ——
    与 T4.2 同一测试语义。
    """
    token = generate_setup_token()
    write_setup_token_file(_path(), token)
    _run(db.set_config(
        auth_mod.SETUP_TOKEN_KEY,
        json.dumps({
            "hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
            "created_at": time.time(),
        }),
    ))
    return token


def _setup(c, token: str):
    r = c.post("/admin/auth/setup", json={"token": token, **USER})
    assert r.status_code == 200 and r.json() is not None
    return r


def _login(c) -> str:
    r = c.post("/admin/auth/login", json=USER)
    assert r.status_code == 200
    token = r.json()["token"]
    assert isinstance(token, str) and len(token) >= 40
    return token


class TestSetupTokenIntegration:

    def test_ti1_full_chain(self, client):
        """TI.1 正例：启动 ensure → 读文件 → status(未开通) → setup → 双删 →
        login → session 调 API 200 → logout → 再调 401。"""
        db = _db_of(client)

        # ① 启动 ensure → 读 `.setup_token` 拿 token
        token = _ensure(db)

        # ② status（未开通）
        r = client.get("/admin/auth/status")
        assert r.status_code == 200 and r.json()["configured"] is False

        # ③ setup（该 token）
        r = _setup(client, token)
        assert r.status_code == 200 and r.json() == {"success": True}

        # ④ 再查文件/KV 已双删
        assert not _path().exists()
        assert _run(db.get_config(auth_mod.SETUP_TOKEN_KEY)) is None

        # ⑤ login → session 调 API 200
        sess = _login(client)
        h = {"Authorization": f"Bearer {sess}"}
        r = client.get("/admin/providers", headers=h)
        assert r.status_code == 200 and r.json()["success"] is True

        # ⑥ logout → 再调 401
        r = client.post("/admin/auth/logout", headers=h)
        assert r.status_code == 200 and r.json() == {"success": True}
        assert client.get("/admin/providers", headers=h).status_code == 401

    def test_ti2_admin_key_cannot_setup(self, client):
        """TI.2 反例：未开通环境用 Bearer/body 双形式尝试 token =
        BOTFLOW_ADMIN_KEY → setup 401、admin_account 未创建、status 仍 false。"""
        db = _db_of(client)
        _ensure(db)  # 未开通环境（KV 已有合法 setup token 记录）

        # 形式①：body token = BOTFLOW_ADMIN_KEY
        r = client.post("/admin/auth/setup", json={"token": ADMIN_KEY, **USER})
        assert r.status_code == 401
        assert r.json()["detail"] == SETUP_401

        # 形式②：Bearer 头 + body token 双通道同带 BOTFLOW_ADMIN_KEY
        r = client.post(
            "/admin/auth/setup", json={"token": ADMIN_KEY, **USER}, headers=AUTH,
        )
        assert r.status_code == 401
        assert r.json()["detail"] == SETUP_401

        assert _run(db.get_config("admin_account")) is None  # 账号未创建
        r = client.get("/admin/auth/status")
        assert r.status_code == 200 and r.json()["configured"] is False

    def test_ti3_bearer_admin_key_compat(self, client):
        """TI.3 正例：setup 完成后 `Authorization: Bearer <BOTFLOW_ADMIN_KEY>`
        调 GET /admin/providers → 200（Bearer 通道兼容回归）。"""
        db = _db_of(client)
        token = _ensure(db)
        assert _setup(client, token).status_code == 200

        r = client.get("/admin/providers", headers=AUTH)
        assert r.status_code == 200 and r.json()["success"] is True

    def test_ti4_restart_no_rotation(self, client):
        """TI.4 正例：ensure → 记录 token → 再次 ensure（模拟重启，双在分支④）
        → 用原 token setup → 200（旧 token 未失效）。"""
        db = _db_of(client)
        first = _ensure(db)
        second = _ensure(db)  # 第二次 ensure = 重启模拟
        assert second == first  # 双在不轮换

        r = _setup(client, first)
        assert r.status_code == 200

    def test_ti5_reset_purges_old_session(self, client):
        """TI.5 反例：setup → login → 再 setup（重置）→ 旧 session 调 API →
        401（purge 生效）。"""
        db = _db_of(client)
        token = _ensure(db)
        assert _setup(client, token).status_code == 200
        old = _login(client)

        # 二次 setup：首设成功已双删凭证 → 按 T4.2 同法重新预置。
        token = _preset(db)
        assert _setup(client, token).status_code == 200

        r = client.get(
            "/admin/providers", headers={"Authorization": f"Bearer {old}"}
        )
        assert r.status_code == 401
