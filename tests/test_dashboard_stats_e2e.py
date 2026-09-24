"""dashboard_stats 集成端到端 TI.1–TI.3（契约：docs/tasks/dashboard_stats_features.md §3）。

文件级 ``pytestmark = integration``：照抄 tests/test_setup_token_e2e.py 的模式。
pyproject 的 ``addopts = ["-m", "not integration"]`` 会把它们从默认全量跑里摘掉，
指名文件也逃不掉 —— 必须显式加 ``-m integration``：

    PYTHONPATH=src python -m pytest tests/test_dashboard_stats_e2e.py -m integration

（不用 ``pytest tests/ -m integration``：那会把打真实服务 127.0.0.1:4000 的
tests/test_integration.py 一并无差别收集，无实服务时被无关失败挡住。）
TestClient/ASGI 直连 app + 真实 Database，不起真端口、不碰 live 库。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botflow.admin_api import admin_router
from botflow.auth import create_session
from botflow.config import BotflowSettings, set_config
from botflow.storage import db as dbmod
from botflow.storage.db import Database
from botflow.storage.models import Model, ModelGroup, Provider

pytestmark = pytest.mark.integration

UTC = timezone.utc
FMT = "%Y-%m-%d %H:%M:%S"
ADMIN_KEY = "admin-secret-e2e"
AUTH = {"Authorization": f"Bearer {ADMIN_KEY}"}


@pytest.fixture
def client(tmp_path):
    """HTTP 层 fixture：照 test_setup_token_e2e 模式 —— 真实 Database +
    dependency_overrides 挂 admin_router，TestClient 起 lifespan。"""
    d = Database(str(tmp_path / "dashboard_stats_e2e.db"))
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


async def _seed_world(db) -> dict:
    """契约 §3 seed 惯例：2 组 + 2 型 + group_models 关联。"""
    pid = await db.create_provider(Provider(name="p", provider_type="openai"))
    m1 = await db.create_model(Model(name="m1", provider_id=pid))
    m2 = await db.create_model(Model(name="m2", provider_id=pid))
    g1 = await db.create_group(ModelGroup(name="g1"))
    g2 = await db.create_group(ModelGroup(name="g2"))
    await db.add_model_to_group(g1, m1, 1.0)
    await db.add_model_to_group(g2, m2, 1.0)
    return {"provider": pid, "m1": m1, "m2": m2, "g1": g1, "g2": g2}


async def _insert_at(
    db,
    created_at: str,
    *,
    group_id=None,
    model_id=None,
    status: str = "success",
    total_tokens=None,
    cost: float = 0.0,
):
    """裸 SQL 直插可控 ``created_at``（create_call_log 写死 datetime('now')，
    db.py:790 —— 契约 §3 seed 惯例明示指定时刻走直插）。"""
    await db.execute_write(
        """INSERT INTO call_logs
           (api_key_id, group_id, model_id, status, total_tokens, cost, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (None, group_id, model_id, status, total_tokens, cost, created_at),
    )


class TestDashboardStatsIntegration:
    def test_ti1_trend_models_sums_consistent(self, client):
        """TI.1 正例：全栈口径一致 —— 跨 3 天 × 2 组数据，d90 窗口下
        trend 全行 calls/tokens 总和 == models 全行 total_calls/total_tokens 总和。

        造数保证 group_id/model_id 均有效：两侧 INNER JOIN（trend JOIN
        model_groups / models JOIN models）口径差才仅剩 0。
        """
        db = _db_of(client)
        w = _run(_seed_world(db))
        for days_ago, tokens in [(1, 100), (2, None), (3, 300)]:
            at = (datetime.now(UTC) - timedelta(days=days_ago)).strftime(FMT)
            for g, m in ((w["g1"], w["m1"]), (w["g2"], w["m2"])):
                _run(_insert_at(db, at, group_id=g, model_id=m,
                                total_tokens=tokens, cost=0.01))
        t = client.get("/admin/stats/trend", params={"range": "d90"}, headers=AUTH)
        m = client.get("/admin/stats/models",
                       params={"range": "d90", "limit": 100}, headers=AUTH)
        assert t.status_code == 200 and m.status_code == 200
        trend = t.json()["trend"]
        stats = m.json()["model_stats"]
        assert len(trend) == 6  # 3 天 × 2 组，只回有数据行
        assert len(stats) == 2
        assert sum(r["calls"] for r in trend) == sum(r["total_calls"] for r in stats) == 6
        # NULL tokens 行两侧同被 COALESCE 归 0 → 总和仍相等
        assert sum(r["tokens"] for r in trend) == sum(r["total_tokens"] for r in stats) == 800

    def test_ti2_auth_triple_on_trend(self, client):
        """TI.2 反例：鉴权三连 —— 无头 401 / 假 key 401 / 正确 key 200。"""
        r = client.get("/admin/stats/trend")
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid admin key."  # 文案不漂移
        r = client.get("/admin/stats/trend",
                       headers={"Authorization": "Bearer fake-key-123"})
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid admin key."
        r = client.get("/admin/stats/trend", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) == {"success", "range", "trend"}  # 正确 range/trend 形状
        assert body["range"] == "week"
        assert isinstance(body["trend"], list)

    def test_ti3_models_no_range_superset_of_today(self, client):
        """TI.3 正例：兼容共存 —— 同会话先后查无 range 与 range=today，
        均 200；前者行集合 ⊇ 后者（全时间 ⊇ 今日），前者除 total_tokens
        外形状同旧。"""
        db = _db_of(client)
        w = _run(_seed_world(db))
        now = datetime.now(UTC)
        # m1：今天一条 + 40 天前一条；m2：仅 40 天前一条（制造真超集差异）
        _run(_insert_at(db, now.strftime(FMT), group_id=w["g1"], model_id=w["m1"],
                        total_tokens=10, cost=0.1))
        _run(_insert_at(db, (now - timedelta(days=40)).strftime(FMT),
                        group_id=w["g1"], model_id=w["m1"], total_tokens=20, cost=0.2))
        _run(_insert_at(db, (now - timedelta(days=40)).strftime(FMT),
                        group_id=w["g2"], model_id=w["m2"], total_tokens=30, cost=0.3))
        # 同一会话 token 先后两查
        sess = _run(create_session(db, "alice"))
        h = {"Authorization": f"Bearer {sess}"}
        r_all = client.get("/admin/stats/models", headers=h)
        r_today = client.get("/admin/stats/models", params={"range": "today"}, headers=h)
        assert r_all.status_code == 200 and r_today.status_code == 200
        ball, btd = r_all.json(), r_today.json()
        assert set(ball.keys()) == {"success", "model_stats"}
        # 带 range 也无回显键（models 顶层键集零新增）
        assert set(btd.keys()) == {"success", "model_stats"}
        old6 = {"model_id", "model_name", "total_calls", "success_calls",
                "error_calls", "total_cost"}
        for row in ball["model_stats"]:
            # 除新增第 7 键 total_tokens 外形状同旧
            assert set(row.keys()) == old6 | {"total_tokens"}
        ids_all = {r["model_id"] for r in ball["model_stats"]}
        ids_today = {r["model_id"] for r in btd["model_stats"]}
        assert ids_all == {w["m1"], w["m2"]}   # 全时间：两模型都有历史行
        assert ids_today == {w["m1"]}          # 今日：仅 m1 的今天行
        assert ids_all >= ids_today            # 全时间 ⊇ 今日
