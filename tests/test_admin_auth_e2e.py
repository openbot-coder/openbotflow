"""admin_auth 端到端集成用例 TI.1-TI.4（清单 docs/tasks/admin_auth_features.md）。

文件级 ``pytestmark = integration``：pyproject 的 ``addopts = ["-m", "not integration"]``
会把它们从默认全量跑里摘掉，指名文件也逃不掉 —— 必须显式加 ``-m integration``：

    PYTHONPATH=src python -m pytest tests/test_admin_auth_e2e.py -m integration

不放 tests/test_integration.py：那是打 127.0.0.1:4000 的 live 测试，混跑会被
无关失败挡住（P1-9）。TestClient/ASGI 直连 app，不起真端口、不碰 live 库。
"""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import botflow.storage.db as dbmod
from botflow import auth as auth_mod
from botflow.admin_api import admin_router
from botflow.config import BotflowSettings, set_config
from botflow.storage.db import Database

pytestmark = pytest.mark.integration

ADMIN_KEY = "admin-secret-e2e"
# D2：实现的 login 401 文案是英文（清单约定中文「用户名或密码错误」）。
# 按实际行为断言；src 统一文案时只改这个常量。
LOGIN_401 = "Invalid username or password."

AUTH = {"Authorization": f"Bearer {ADMIN_KEY}"}
USER = {"username": "alice", "password": "s3cret-pass"}


@pytest.fixture(autouse=True)
def fast_pbkdf2(monkeypatch):
    # 集成链路里 setup/login 各要跑一次哈希；600k 迭代在 CI/远端仍嫌慢，调到 1000。
    monkeypatch.setattr(auth_mod, "PBKDF2_ITERATIONS", 1000)


@pytest.fixture
def client(tmp_path):
    """HTTP 层 fixture：照 tests/test_admin_api.py 的既有模式。

    Database.initialize() 会把全局 _active_db 设成 d —— verify_admin_key 体内
    直调的 auth.get_db() 因此拿到同一个库，session 通道（TI.1④⑥）才走得通。
    """
    d = Database(str(tmp_path / "admin_auth_e2e.db"))
    asyncio.new_event_loop().run_until_complete(d.initialize())
    set_config(BotflowSettings(admin_key=ADMIN_KEY))
    app = FastAPI()
    app.include_router(admin_router)
    app.dependency_overrides[dbmod.get_db] = lambda: d
    with TestClient(app) as c:
        yield c
    asyncio.new_event_loop().run_until_complete(d.close())
    set_config(None)


def _setup(c):
    r = c.post("/admin/auth/setup", json={"token": ADMIN_KEY, **USER})
    assert r.status_code == 200 and r.json()["success"] is True
    return r


def _login(c):
    r = c.post("/admin/auth/login", json=USER)
    assert r.status_code == 200
    token = r.json()["token"]
    assert isinstance(token, str) and len(token) >= 40
    return token


class TestIntegration:
    def test_ti1_full_flow(self, client):
        """TI.1 正例：status(未开通) → setup → login → session 调 API → logout → 再调 401。"""
        # ① 未开通
        r = client.get("/admin/auth/status")
        assert r.status_code == 200 and r.json()["configured"] is False
        # ② 开通（admin key）
        _setup(client)
        # ③ login 拿会话 token
        token = _login(client)
        # ④ 会话 token 调 admin API → 200
        sess = {"Authorization": f"Bearer {token}"}
        r = client.get("/admin/providers", headers=sess)
        assert r.status_code == 200 and r.json()["success"] is True
        # ⑤ logout → 200
        r = client.post("/admin/auth/logout", headers=sess)
        assert r.status_code == 200 and r.json()["success"] is True
        # ⑥ 注销后再调 → 401
        assert client.get("/admin/providers", headers=sess).status_code == 401

    def test_ti2_admin_key_backward_compat(self, client):
        """TI.2 正例：已 setup 环境下，直接 Bearer <admin key> 调 admin API → 200。"""
        _setup(client)
        r = client.get("/admin/providers", headers=AUTH)
        assert r.status_code == 200 and r.json()["success"] is True

    def test_ti3_reset_purges_old_session(self, client):
        """TI.3 反例：setup → login → 再 setup（重置）→ 旧 token 调 API → 401（purge 生效）。"""
        _setup(client)
        old = _login(client)
        _setup(client)  # 重置：必须全量吊销既有会话（P0-2 LIKE 带 %）
        r = client.get("/admin/providers", headers={"Authorization": f"Bearer {old}"})
        assert r.status_code == 401

    def test_ti4_not_configured_status_and_login(self, client):
        """TI.4 反例：未 setup 时 status configured=false；login → 401。"""
        r = client.get("/admin/auth/status")
        assert r.status_code == 200 and r.json()["configured"] is False
        r = client.post("/admin/auth/login", json=USER)
        assert r.status_code == 401
        # D2：清单约定中文文案，实现为英文 —— 按实际行为断言，统一时改 LOGIN_401。
        assert r.json()["detail"] == LOGIN_401
