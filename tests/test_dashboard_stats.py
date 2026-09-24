"""dashboard_stats 单测 D1.1–D4.4 共 37 条（契约：docs/tasks/dashboard_stats_features.md §3）。

分组：D1×15 db 层（直调 ``list_group_trend`` / ``list_model_stats``）、
D2×8 range 解析（直调 ``_resolve_range`` 并注入固定 now）、
D3×10 端点（TestClient HTTP 层）、D4×4 兼容回归（HTTP 层）。
编号与契约 §3 用例表 1:1 对表，无缺号无多重。

行键契约（本任务新增两列）：``list_model_stats`` 行 = 既有 7 键
[model_id, model_name, total_calls, success_calls, error_calls,
total_cost, total_tokens] + avg_latency_ms + error_rate（共 9 键，
顺序固定）；顶层键集 ``{success, model_stats}`` 零新增。

seed 惯例（契约 §3）：需指定时刻的行用 ``execute_write`` 裸 SQL 直插 UTC 空格串
——``create_call_log`` 在 db.py:790 写死 ``datetime('now')`` 无法指定时刻；
其余造数用 ``create_call_log(CallLog(...))``。分组先行：2 组 + 2 型 +
group_models 关联（INNER JOIN 依赖）。

本机不跑 pytest（async 挂死），交付门槛 = ``python -m py_compile`` + 计数核对。
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botflow.admin_api import admin_router
from botflow.auth import create_session
from botflow.config import BotflowSettings, set_config
from botflow.storage import db as dbmod
from botflow.storage.db import Database
from botflow.storage.models import CallLog, Model, ModelGroup, Provider

UTC = timezone.utc
FMT = "%Y-%m-%d %H:%M:%S"
ADMIN_KEY = "admin-secret"
AUTH = {"Authorization": f"Bearer {ADMIN_KEY}"}


# ---------------------------------------------------------------------------
# 共享 fixture / 造数助手
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    """db 层 fixture：照 tests/test_db_full.py:11-16 的 async Database 模式。"""
    d = Database(str(tmp_path / "dashboard_stats.db"))
    await d.initialize()
    yield d
    await d.close()


@pytest.fixture
def client(tmp_path):
    """HTTP 层 fixture：照 tests/test_admin_api.py:22-35 的 TestClient 模式。"""
    d = Database(str(tmp_path / "dashboard_stats_http.db"))
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


def _now_str(delta: timedelta = timedelta(0)) -> str:
    return (datetime.now(UTC) + delta).strftime(FMT)


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
    api_key_id=None,
    duration_ms=None,
):
    """裸 SQL 直插可控 ``created_at`` 的 call_log 行。

    ``create_call_log`` 把 ``created_at`` 写死为 ``datetime('now')``
    （db.py:790），指定时刻的用例必须直插——契约 §3 seed 惯例明示；
    列与 call_logs DDL 对齐（db.py:96-117，total_tokens/duration_ms 列可空）。
    ``duration_ms`` 默认 None（既有调用点零修改），D1.12+ 延时用例显式传。
    """
    await db.execute_write(
        """INSERT INTO call_logs
           (api_key_id, group_id, model_id, status, total_tokens, cost,
            duration_ms, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (api_key_id, group_id, model_id, status, total_tokens, cost,
         duration_ms, created_at),
    )


# ---------------------------------------------------------------------------
# D1.x db 层（15 条）
# ---------------------------------------------------------------------------


class TestD1DbLayer:
    async def test_d1_1_trend_row_shape_and_aggregates(self, db):
        """D1.1 正例：两组 × 两天 → 键恰 5 个，calls/tokens/day 与造数一致。"""
        w = await _seed_world(db)
        await _insert_at(db, "2026-09-20 03:00:00", group_id=w["g1"], model_id=w["m1"], total_tokens=100)
        await _insert_at(db, "2026-09-20 05:00:00", group_id=w["g1"], model_id=w["m1"], total_tokens=50)
        await _insert_at(db, "2026-09-21 03:00:00", group_id=w["g1"], model_id=w["m1"], total_tokens=20)
        await _insert_at(db, "2026-09-21 10:00:00", group_id=w["g2"], model_id=w["m2"], total_tokens=70)
        rows = await db.list_group_trend("2026-09-19 00:00:00", "2026-09-22 00:00:00")
        # 行式 dict，键恰 day/group_id/group_name/calls/tokens（定档 B 形状）
        assert [set(r.keys()) for r in rows] == [
            {"day", "group_id", "group_name", "calls", "tokens"}
        ] * 3
        # day 为东八日期串（UTC 03:00/05:00/10:00 不跨 16:00 界 → 东八同日）
        assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", r["day"]) for r in rows)
        assert rows == [
            {"day": "2026-09-20", "group_id": w["g1"], "group_name": "g1", "calls": 2, "tokens": 150},
            {"day": "2026-09-21", "group_id": w["g1"], "group_name": "g1", "calls": 1, "tokens": 20},
            {"day": "2026-09-21", "group_id": w["g2"], "group_name": "g2", "calls": 1, "tokens": 70},
        ]

    async def test_d1_2_trend_null_tokens_coalesce(self, db):
        """D1.2 正例：total_tokens 为 NULL 的行不炸且计 0（R4 COALESCE）。"""
        w = await _seed_world(db)
        await _insert_at(db, "2026-09-20 03:00:00", group_id=w["g1"], model_id=w["m1"], total_tokens=100)
        await _insert_at(db, "2026-09-20 04:00:00", group_id=w["g1"], model_id=w["m1"], total_tokens=None)
        await _insert_at(db, "2026-09-20 05:00:00", group_id=w["g1"], model_id=w["m1"], total_tokens=50)
        await _insert_at(db, "2026-09-20 06:00:00", group_id=w["g2"], model_id=w["m2"], total_tokens=None)
        rows = await db.list_group_trend("2026-09-19 00:00:00", "2026-09-22 00:00:00")
        by_group = {r["group_id"]: r for r in rows}
        assert by_group[w["g1"]]["tokens"] == 150  # 100 + NULL(计0) + 50
        assert by_group[w["g1"]]["calls"] == 3
        assert by_group[w["g2"]]["tokens"] == 0  # 全 NULL → COALESCE 归 0，非 null
        assert by_group[w["g2"]]["calls"] == 1
        assert all(r["tokens"] is not None for r in rows)  # JSON 不出 null

    async def test_d1_3_trend_window_inclusive_both_ends(self, db):
        """D1.3 边界：== since / == until 两端含（>=/<=），窗外前后行不出现。

        本用例同时覆盖：>= / <= 两端含端 + 窗内跨 16:00Z 日界分两桶
        （+8 分桶与 UTC 窗口交互）—— 12:00:00Z 落东八 09-20 桶、
        18:00:00Z 落东八 09-21 桶，故返回 2 行是契约正确行为。
        """
        w = await _seed_world(db)
        await _insert_at(db, "2026-09-20 11:59:59", group_id=w["g1"], model_id=w["m1"], total_tokens=1)   # 窗外（前）
        await _insert_at(db, "2026-09-20 12:00:00", group_id=w["g1"], model_id=w["m1"], total_tokens=10)  # == since
        await _insert_at(db, "2026-09-20 18:00:00", group_id=w["g1"], model_id=w["m1"], total_tokens=20)  # == until
        await _insert_at(db, "2026-09-20 18:00:01", group_id=w["g1"], model_id=w["m1"], total_tokens=4)   # 窗外（后）
        rows = await db.list_group_trend("2026-09-20 12:00:00", "2026-09-20 18:00:00")
        rows = sorted(rows, key=lambda r: r["day"])
        # 窗内两行分落两个东八日桶：== since 行 → 09-20 桶，== until 行 → 09-21 桶
        assert len(rows) == 2
        assert all(set(r.keys()) == {"day", "group_id", "group_name", "calls", "tokens"} for r in rows)
        assert all(r["group_id"] == w["g1"] and r["group_name"] == "g1" for r in rows)
        # == since 行单独成桶：若 11:59:59 漏入 → tokens 11 / calls 2
        assert rows[0]["day"] == "2026-09-20"
        assert rows[0]["calls"] == 1 and rows[0]["tokens"] == 10
        # == until 行单独成桶：若 18:00:01 漏入 → tokens 24 / calls 2
        assert rows[1]["day"] == "2026-09-21"
        assert rows[1]["calls"] == 1 and rows[1]["tokens"] == 20

    async def test_d1_4_trend_excludes_null_group_id(self, db):
        """D1.4 反例：group_id = NULL 行被 INNER JOIN model_groups 排除。"""
        w = await _seed_world(db)
        await _insert_at(db, "2026-09-20 03:00:00", group_id=None, model_id=w["m1"], total_tokens=999)
        await _insert_at(db, "2026-09-20 04:00:00", group_id=w["g1"], model_id=w["m1"], total_tokens=10)
        rows = await db.list_group_trend("2026-09-19 00:00:00", "2026-09-22 00:00:00")
        assert len(rows) == 1
        assert rows[0]["group_id"] == w["g1"] and rows[0]["calls"] == 1 and rows[0]["tokens"] == 10
        assert all(r["group_id"] is not None for r in rows)

    async def test_d1_5_trend_empty_returns_list(self, db):
        """D1.5 反例：空表 / 窗口内无数据 → []，不抛。"""
        # 空表
        assert await db.list_group_trend("2020-01-01 00:00:00", "2030-01-01 00:00:00") == []
        # 有数据但窗口内无数据
        w = await _seed_world(db)
        await _insert_at(db, "2026-09-20 03:00:00", group_id=w["g1"], model_id=w["m1"], total_tokens=1)
        assert await db.list_group_trend("2027-01-01 00:00:00", "2027-01-02 00:00:00") == []

    async def test_d1_6_cross_utc_day_bucket_uses_cn_date(self, db):
        """D1.6 边界：跨 UTC 日界分桶 —— date(created_at,'+8 hours') 实测。

        行 A UTC 09-23 16:30（东八 09-24 00:30）与行 B UTC 09-24 00:30
        （东八 09-24 08:30）必须落同一东八日桶 "2026-09-24"（R3 核心守卫）。
        """
        w = await _seed_world(db)
        await _insert_at(db, "2026-09-23 16:30:00", group_id=w["g1"], model_id=w["m1"], total_tokens=1)
        await _insert_at(db, "2026-09-24 00:30:00", group_id=w["g1"], model_id=w["m1"], total_tokens=2)
        rows = await db.list_group_trend("2026-09-23 00:00:00", "2026-09-25 00:00:00")
        assert len(rows) == 1
        assert rows[0]["day"] == "2026-09-24"
        assert rows[0]["calls"] == 2 and rows[0]["tokens"] == 3

    async def test_d1_7_model_stats_window_filter(self, db):
        """D1.7 正例：list_model_stats(since, until) 只统计窗内行。"""
        w = await _seed_world(db)
        # 窗内两行
        await _insert_at(db, "2026-09-21 10:00:00", group_id=w["g1"], model_id=w["m1"],
                         status="success", total_tokens=100, cost=0.1)
        await _insert_at(db, "2026-09-21 11:00:00", group_id=w["g1"], model_id=w["m1"],
                         status="error", total_tokens=50, cost=0.2)
        # 窗外前后各一行：calls/tokens/cost 均不得计入
        await _insert_at(db, "2026-09-01 00:00:00", group_id=w["g1"], model_id=w["m1"],
                         status="success", total_tokens=9999, cost=9.9)
        await _insert_at(db, "2026-10-01 00:00:00", group_id=w["g1"], model_id=w["m1"],
                         status="success", total_tokens=9999, cost=9.9)
        rows = await db.list_model_stats(since_utc="2026-09-20 00:00:00",
                                         until_utc="2026-09-22 00:00:00")
        assert len(rows) == 1
        r = rows[0]
        assert r["model_id"] == w["m1"]
        assert (r["total_calls"], r["success_calls"], r["error_calls"]) == (2, 1, 1)
        assert r["total_tokens"] == 150
        assert r["total_cost"] == pytest.approx(0.3)

    async def test_d1_8_model_stats_total_tokens_coalesce(self, db):
        """D1.8 正例：行含新键 total_tokens，NULL tokens 行归 0 后累加正确。"""
        w = await _seed_world(db)
        await _insert_at(db, _now_str(), group_id=w["g1"], model_id=w["m1"], total_tokens=100)
        # 时间不敏感 → 按契约 seed 惯例走 create_call_log（NULL 列显式传入）
        await db.create_call_log(CallLog(model_id=w["m1"], group_id=w["g1"],
                                         status="success", total_tokens=None))
        rows = await db.list_model_stats()
        assert len(rows) == 1
        r = rows[0]
        assert "total_tokens" in r
        assert r["total_tokens"] == 100  # NULL 行 COALESCE 归 0 后累加
        assert r["total_calls"] == 2

    async def test_d1_9_model_stats_default_full_time_with_new_key(self, db):
        """D1.9 边界：缺省（不传两参）= 全时间聚合，行含新键 total_tokens（§0.1）。"""
        w = await _seed_world(db)
        await _insert_at(db, "2020-01-01 00:00:00", group_id=w["g1"], model_id=w["m1"],
                         status="success", total_tokens=7, cost=0.01)
        await db.create_call_log(CallLog(model_id=w["m1"], group_id=w["g1"],
                                         status="success", total_tokens=11, cost=0.02))
        rows = await db.list_model_stats()  # 不传两参 → 不加时间条件 = 全时间
        assert len(rows) == 1
        r = rows[0]
        # 行内 9 键恰集（7 旧键 + avg_latency_ms + error_rate，红线 §0.1）
        assert set(r.keys()) == {"model_id", "model_name", "total_calls", "success_calls",
                                 "error_calls", "total_cost", "total_tokens",
                                 "avg_latency_ms", "error_rate"}
        assert r["total_calls"] == 2  # 2020 老行 + 当前行都计入（全时间）
        assert r["total_tokens"] == 18
        assert r["total_cost"] == pytest.approx(0.03)

    async def test_d1_10_model_stats_order_and_limit(self, db):
        """D1.10 正例：ORDER BY total_calls DESC 与 LIMIT ? 生效（回归 db.py:1084）。"""
        w = await _seed_world(db)
        m3 = await db.create_model(Model(name="m3", provider_id=w["provider"]))
        counts = {w["m1"]: 5, m3: 3, w["m2"]: 1}
        for mid, n in counts.items():
            for _ in range(n):
                await db.create_call_log(CallLog(model_id=mid, status="success", total_tokens=1))
        rows = await db.list_model_stats()
        assert [r["model_id"] for r in rows] == [w["m1"], m3, w["m2"]]
        assert [r["total_calls"] for r in rows] == [5, 3, 1]
        top2 = await db.list_model_stats(limit=2)
        assert [r["model_id"] for r in top2] == [w["m1"], m3]

    async def test_d1_11_trend_order_day_then_group(self, db):
        """D1.11 边界：同 day 多组、多 day → ORDER BY day ASC, group_id ASC 稳定序。"""
        w = await _seed_world(db)
        # 乱序插入，验证排序而非插入序
        await _insert_at(db, "2026-09-22 03:00:00", group_id=w["g2"], model_id=w["m2"], total_tokens=1)
        await _insert_at(db, "2026-09-21 04:00:00", group_id=w["g2"], model_id=w["m2"], total_tokens=1)
        await _insert_at(db, "2026-09-22 05:00:00", group_id=w["g1"], model_id=w["m1"], total_tokens=1)
        await _insert_at(db, "2026-09-21 06:00:00", group_id=w["g1"], model_id=w["m1"], total_tokens=1)
        rows = await db.list_group_trend("2026-09-20 00:00:00", "2026-09-23 00:00:00")
        assert [(r["day"], r["group_id"]) for r in rows] == [
            ("2026-09-21", w["g1"]),
            ("2026-09-21", w["g2"]),
            ("2026-09-22", w["g1"]),
            ("2026-09-22", w["g2"]),
        ]

    async def test_d1_12_model_stats_avg_latency_excludes_error_and_error_rate(self, db):
        """D1.12 正例：混合 success(100ms)+error(999ms) → avg 排除 error 耗时、rate=0.5。

        同时钉死行键顺序 = 7 旧键 + avg_latency_ms + error_rate（共 9 键）。
        """
        w = await _seed_world(db)
        await _insert_at(db, "2026-09-21 10:00:00", group_id=w["g1"], model_id=w["m1"],
                         status="success", duration_ms=100)
        await _insert_at(db, "2026-09-21 11:00:00", group_id=w["g1"], model_id=w["m1"],
                         status="error", duration_ms=999)
        rows = await db.list_model_stats(since_utc="2026-09-20 00:00:00",
                                         until_utc="2026-09-22 00:00:00")
        assert len(rows) == 1
        r = rows[0]
        # 行键顺序 = 既有 7 键 + avg_latency_ms + error_rate（契约行键序）
        assert list(r.keys()) == ["model_id", "model_name", "total_calls",
                                  "success_calls", "error_calls", "total_cost",
                                  "total_tokens", "avg_latency_ms", "error_rate"]
        assert r["avg_latency_ms"] == 100.0  # error 行 999ms 不计入平均
        assert r["error_rate"] == 0.5        # 2 行中 1 行非 success

    async def test_d1_13_model_stats_only_error_rows_avg_null_rate_one(self, db):
        """D1.13 反例：仅 error 行（50ms）→ avg_latency_ms is None、error_rate==1.0。"""
        w = await _seed_world(db)
        await _insert_at(db, "2026-09-21 10:00:00", group_id=w["g1"], model_id=w["m1"],
                         status="error", duration_ms=50)
        rows = await db.list_model_stats(since_utc="2026-09-20 00:00:00",
                                         until_utc="2026-09-22 00:00:00")
        assert len(rows) == 1
        r = rows[0]
        assert r["avg_latency_ms"] is None  # 全组无 success 行 → NULL（JSON null）
        assert r["error_rate"] == 1.0

    async def test_d1_14_model_stats_all_non_success_statuses_counted(self, db):
        """D1.14 正例：timeout/cooldown/cancelled 各 1 + success 1 → error_rate==0.75。"""
        w = await _seed_world(db)
        for i, status in enumerate(("timeout", "cooldown", "cancelled")):
            await _insert_at(db, f"2026-09-21 1{i}:00:00", group_id=w["g1"],
                             model_id=w["m1"], status=status, duration_ms=700)
        await _insert_at(db, "2026-09-21 14:00:00", group_id=w["g1"], model_id=w["m1"],
                         status="success", duration_ms=200)
        rows = await db.list_model_stats(since_utc="2026-09-20 00:00:00",
                                         until_utc="2026-09-22 00:00:00")
        assert len(rows) == 1
        r = rows[0]
        assert (r["total_calls"], r["success_calls"]) == (4, 1)
        assert r["error_rate"] == 0.75  # 非 success 三态全算差错
        assert r["avg_latency_ms"] == 200.0  # 仅 success 行的 200ms

    async def test_d1_15_model_stats_all_success_rate_zero_avg_exact(self, db):
        """D1.15 正例：全 success → error_rate==0.0、avg 为 float 精确值 (100+300)/2。"""
        w = await _seed_world(db)
        await _insert_at(db, "2026-09-21 10:00:00", group_id=w["g1"], model_id=w["m1"],
                         status="success", duration_ms=100)
        await _insert_at(db, "2026-09-21 11:00:00", group_id=w["g1"], model_id=w["m1"],
                         status="success", duration_ms=300)
        rows = await db.list_model_stats(since_utc="2026-09-20 00:00:00",
                                         until_utc="2026-09-22 00:00:00")
        assert len(rows) == 1
        r = rows[0]
        assert r["error_rate"] == 0.0
        assert r["avg_latency_ms"] == 200.0
        assert isinstance(r["avg_latency_ms"], float)  # AVG() → REAL
        assert isinstance(r["error_rate"], float)      # CAST AS REAL


# ---------------------------------------------------------------------------
# D2.x range 解析（8 条，注入固定 now）
# ---------------------------------------------------------------------------


class TestD2ResolveRange:
    @pytest.fixture
    def resolve_range(self):
        """延迟导入：``_resolve_range`` 由 coder 并行落地（契约 F1）。

        放 fixture 层导入，让 D1/D3/D4 的收集不依赖 F1 落地顺序
        （本文件不跑 pytest，仅保证按契约 1:1 落码）。
        """
        from botflow.admin_api import _resolve_range

        return _resolve_range

    def test_d2_1_six_enums_fixed_now_format(self, resolve_range):
        """D2.1 正例：now=2026-09-24 03:00 UTC（东八 11:00 周四）六枚举逐一。"""
        now = datetime(2026, 9, 24, 3, 0, 0, tzinfo=UTC)
        expected = {
            "half_hour": "2026-09-24 02:30:00",
            "hour": "2026-09-24 02:00:00",
            "today": "2026-09-23 16:00:00",   # 东八今日 00:00 → 减 8h 转 UTC
            "week": "2026-09-20 16:00:00",    # 东八本周一（09-21）00:00 → UTC
            "month": "2026-08-31 16:00:00",   # 东八 09-01 00:00 → UTC
            "d90": "2026-06-26 03:00:00",     # now − 90 天（UTC 直算）
        }
        shape = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        for rv, since_exp in expected.items():
            since, until = resolve_range(rv, now=now)
            assert since == since_exp, rv
            assert until == "2026-09-24 03:00:00", rv
            # UTC 空格串：无 T、无微秒、无时区后缀
            assert shape.fullmatch(since), since
            assert shape.fullmatch(until), until

    def test_d2_2_today_crosses_utc_day_boundary(self, resolve_range):
        """D2.2 边界：today 跨 UTC 日界（东八自然日 ≠ UTC 日）+ 16:00 跳变。"""
        # 契约例：now=2026-09-23 16:30 UTC（东八已 09-24 00:30）
        now = datetime(2026, 9, 23, 16, 30, 0, tzinfo=UTC)
        assert resolve_range("today", now=now) == ("2026-09-23 16:00:00",
                                                   "2026-09-23 16:30:00")
        # UTC 16:00 前一瞬：东八仍在 09-23 → since 落在前一东八日 00:00 的 UTC 串
        before = datetime(2026, 9, 23, 15, 59, 59, tzinfo=UTC)
        assert resolve_range("today", now=before) == ("2026-09-22 16:00:00",
                                                      "2026-09-23 15:59:59")
        # UTC 16:00 整点：东八翻日 → since 一天跳变（边界跳变守卫）
        at = datetime(2026, 9, 23, 16, 0, 0, tzinfo=UTC)
        assert resolve_range("today", now=at) == ("2026-09-23 16:00:00",
                                                  "2026-09-23 16:00:00")

    def test_d2_3_week_starts_monday(self, resolve_range):
        """D2.3 边界：week 起于周一 —— 东八周一 00:30（UTC 前一日 16:30）。"""
        tz8 = timezone(timedelta(hours=8))
        now = datetime(2026, 9, 28, 0, 30, tzinfo=tz8).astimezone(UTC)
        assert now.strftime(FMT) == "2026-09-27 16:30:00"  # 2026-09-28 是周一
        since, until = resolve_range("week", now=now)
        assert since == "2026-09-27 16:00:00"  # 东八周一 00:00 转 UTC（前一 UTC 日 16:00）
        assert until == "2026-09-27 16:30:00"

    def test_d2_4_week_ends_sunday_back_to_monday(self, resolve_range):
        """D2.4 边界：week 止于周日 —— since 仍是本周一，窗口含整周至 now。"""
        tz8 = timezone(timedelta(hours=8))
        now = datetime(2026, 9, 27, 10, 0, tzinfo=tz8).astimezone(UTC)  # 东八周日 10:00
        assert now.strftime(FMT) == "2026-09-27 02:00:00"  # 2026-09-27 是周日
        since, until = resolve_range("week", now=now)
        # 东八本周一 2026-09-21 00:00 → UTC 2026-09-20 16:00
        assert since == "2026-09-20 16:00:00"
        assert until == "2026-09-27 02:00:00"
        assert since < until  # 窗口非空且含整周起点

    def test_d2_5_month_boundaries(self, resolve_range):
        """D2.5 边界：month 月初与月中 —— since 均为东八 10-01 00:00 → UTC。"""
        tz8 = timezone(timedelta(hours=8))
        # 东八 10-01 00:10 → UTC 09-30 16:10（月初）
        oct1 = datetime(2026, 10, 1, 0, 10, tzinfo=tz8).astimezone(UTC)
        # 东八 10-15 12:00 → UTC 10-15 04:00（月中）
        oct15 = datetime(2026, 10, 15, 12, 0, tzinfo=tz8).astimezone(UTC)
        s1, u1 = resolve_range("month", now=oct1)
        s2, u2 = resolve_range("month", now=oct15)
        # 两者 since 均 = 东八 10-01 00:00 → UTC 09-30 16:00（1 日是始，不越上月）
        assert s1 == "2026-09-30 16:00:00"
        assert s2 == "2026-09-30 16:00:00"
        assert u1 == "2026-09-30 16:10:00"
        assert u2 == "2026-10-15 04:00:00"
        assert s1 < u1  # 月初窗口不为空

    def test_d2_6_half_hour_hour_direct_utc(self, resolve_range):
        """D2.6 正例：half_hour/hour 注入 now 直接加减（UTC 直算，不经东八）。"""
        now = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)
        assert resolve_range("half_hour", now=now) == ("2026-09-24 11:30:00",
                                                       "2026-09-24 12:00:00")
        assert resolve_range("hour", now=now) == ("2026-09-24 11:00:00",
                                                  "2026-09-24 12:00:00")
        # 东八已翻日的 UTC 时刻：since 不被 +8 移日（证明没走东八换算）
        edge = datetime(2026, 9, 23, 17, 0, 0, tzinfo=UTC)
        assert resolve_range("half_hour", now=edge) == ("2026-09-23 16:30:00",
                                                        "2026-09-23 17:00:00")

    def test_d2_7_d90_sliding_90_days(self, resolve_range):
        """D2.7 正例：d90 = now − 90 天（UTC 直算），until = now。"""
        now = datetime(2026, 9, 24, 3, 0, 0, tzinfo=UTC)
        assert resolve_range("d90", now=now) == ("2026-06-26 03:00:00",
                                                 "2026-09-24 03:00:00")

    def test_d2_8_identity_since_le_until(self, resolve_range):
        """D2.8 边界：六档一律 since <= until 且 until == 注入 now（秒级截断）。"""
        now = datetime(2026, 9, 24, 3, 0, 7, tzinfo=UTC)
        until_exp = "2026-09-24 03:00:07"
        for rv in ("half_hour", "hour", "today", "week", "month", "d90"):
            since, until = resolve_range(rv, now=now)
            assert since <= until, rv
            assert until == until_exp, rv


# ---------------------------------------------------------------------------
# D3.x 端点（10 条，HTTP 层）
#
# AUTH 统一前置（契约 §3 D3 表）：除 D3.4 鉴权反例外，D3.1–D3.9 全部带
# AUTH —— FastAPI 先跑 Depends(verify_admin_key) 后校验 query，不带 AUTH
# 时 401 先于 422/200。
# ---------------------------------------------------------------------------


class TestD3Endpoints:
    def test_d3_1_trend_default_week_shape(self, client):
        """D3.1 正例：缺省 GET /admin/stats/trend → 键恰 3 个、range=week。"""
        db = _db_of(client)
        w = _run(_seed_world(db))
        # 造数行落在缺省 week 窗内（now 属本周，week since ≤ now ≤ 请求时刻 until）
        _run(_insert_at(db, _now_str(), group_id=w["g1"], model_id=w["m1"], total_tokens=5))
        r = client.get("/admin/stats/trend", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) == {"success", "range", "trend"}
        assert body["success"] is True
        assert body["range"] == "week"  # 缺省 week
        assert len(body["trend"]) >= 1  # trend 元素形状同定档 B
        for row in body["trend"]:
            assert set(row.keys()) == {"day", "group_id", "group_name", "calls", "tokens"}

    def test_d3_2_today_echo_and_cn_bucket(self, client):
        """D3.2 正例：?range=today 造今日数据 → 回显 today、行落东八今日桶。"""
        db = _db_of(client)
        w = _run(_seed_world(db))
        now = datetime.now(UTC)
        _run(_insert_at(db, now.strftime(FMT), group_id=w["g1"], model_id=w["m1"], total_tokens=5))
        r = client.get("/admin/stats/trend", params={"range": "today"}, headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["range"] == "today"  # range 回显恰等于请求档
        today_cn = (now + timedelta(hours=8)).strftime("%Y-%m-%d")
        # 行落在东八今日桶，且窗口（东八今日 00:00 起）包含刚造的行
        assert body["trend"] == [
            {"day": today_cn, "group_id": w["g1"], "group_name": "g1", "calls": 1, "tokens": 5}
        ]

    def test_d3_3_illegal_trend_range_422(self, client):
        """D3.3 反例：?range=fortnight（枚举外，带 AUTH）→ 422（Literal 自动）。"""
        r = client.get("/admin/stats/trend", params={"range": "fortnight"}, headers=AUTH)
        assert r.status_code == 422  # 非 400/500；detail 为 FastAPI 自动文案
        assert isinstance(r.json()["detail"], list)

    def test_d3_4_auth_rejections(self, client):
        """D3.4 反例：无头 / 假 key → 401，detail 恰 "Invalid admin key."。"""
        r1 = client.get("/admin/stats/trend")
        assert r1.status_code == 401
        assert r1.json()["detail"] == "Invalid admin key."  # 文案一字不差
        r2 = client.get("/admin/stats/models", params={"range": "hour"},
                        headers={"Authorization": "Bearer wrong-key"})
        assert r2.status_code == 401
        assert r2.json()["detail"] == "Invalid admin key."

    def test_d3_5_models_range_window_and_keyset(self, client):
        """D3.5 正例：?range=hour&limit=10 → 顶层键集恰 2 个、仅窗内行计入。"""
        db = _db_of(client)
        w = _run(_seed_world(db))
        _run(_insert_at(db, _now_str(timedelta(minutes=-5)), group_id=w["g1"],
                        model_id=w["m1"], total_tokens=42, cost=0.07))
        _run(_insert_at(db, _now_str(timedelta(days=-3)), group_id=w["g1"],
                        model_id=w["m1"], total_tokens=9999, cost=9.9))
        r = client.get("/admin/stats/models", params={"range": "hour", "limit": 10},
                       headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        # 顶层键集恰 {success, model_stats}：无 range 回显键（键集零新增）
        assert set(body.keys()) == {"success", "model_stats"}
        assert "range" not in body
        rows = body["model_stats"]
        assert len(rows) == 1  # 3 天前窗外行不计入
        # 行含既有 total_tokens + 新增 avg_latency_ms / error_rate 两键
        assert all({"total_tokens", "avg_latency_ms", "error_rate"} <= set(row)
                   for row in rows)
        assert rows[0]["total_calls"] == 1
        assert rows[0]["total_tokens"] == 42
        assert rows[0]["total_cost"] == pytest.approx(0.07)

    def test_d3_6_illegal_models_range_422(self, client):
        """D3.6 反例：?range=bogus → 422（Literal）。"""
        r = client.get("/admin/stats/models", params={"range": "bogus"}, headers=AUTH)
        assert r.status_code == 422
        assert isinstance(r.json()["detail"], list)

    def test_d3_7_trend_empty_db_200_empty_list(self, client):
        """D3.7 边界：空库调 /stats/trend → 200 + trend=[]（不是 404/500）。"""
        r = client.get("/admin/stats/trend", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body == {"success": True, "range": "week", "trend": []}

    def test_d3_8_models_default_limit_20(self, client):
        """D3.8 边界：/stats/models 不带 limit → 缺省 20 生效，最多 20 行。"""
        db = _db_of(client)
        pid = _run(db.create_provider(Provider(name="p", provider_type="openai")))
        mids = [_run(db.create_model(Model(name=f"m{i}", provider_id=pid))) for i in range(21)]
        for mid in mids:
            _run(db.create_call_log(CallLog(model_id=mid, status="success", total_tokens=1)))
        r = client.get("/admin/stats/models", headers=AUTH)
        assert r.status_code == 200
        rows = r.json()["model_stats"]
        assert len(rows) == 20  # 21 个模型 → LIMIT 20 截断

    def test_d3_9_session_token_channel(self, client):
        """D3.9 正例：session token 通道（login 签发的会话）调 /stats/trend → 200。"""
        db = _db_of(client)
        token = _run(create_session(db, "alice"))
        r = client.get("/admin/stats/trend",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) == {"success", "range", "trend"}
        assert body["success"] is True and body["range"] == "week"
        assert isinstance(body["trend"], list)

    def test_d3_10_models_avg_latency_and_error_rate_keys(self, client):
        """D3.10 正例：响应行含两新键、9 键序列正确，值与直调 db 一致（同库双查）。"""
        db = _db_of(client)
        w = _run(_seed_world(db))
        _run(_insert_at(db, _now_str(), group_id=w["g1"], model_id=w["m1"],
                        status="success", duration_ms=100))
        _run(_insert_at(db, _now_str(), group_id=w["g1"], model_id=w["m1"],
                        status="error", duration_ms=999))
        r = client.get("/admin/stats/models", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) == {"success", "model_stats"}  # 顶层键集零新增
        rows = body["model_stats"]
        assert len(rows) == 1
        row = rows[0]
        # 9 键序列 = 7 旧键 + avg_latency_ms + error_rate
        assert list(row.keys()) == ["model_id", "model_name", "total_calls",
                                    "success_calls", "error_calls", "total_cost",
                                    "total_tokens", "avg_latency_ms", "error_rate"]
        assert row["avg_latency_ms"] == 100.0  # error 耗时被排除
        assert row["error_rate"] == 0.5
        # 同库双查法：端点输出 == 直调 db.list_model_stats()（同数据）
        assert rows == _run(db.list_model_stats())


# ---------------------------------------------------------------------------
# D4.x 兼容回归（4 条，HTTP 层）
# ---------------------------------------------------------------------------


class TestD4CompatRegression:
    def test_d4_1_models_no_range_field_compat(self, client):
        """D4.1 正例：无 range 旧调用逐字段对账（固定夹具手算 + 同库双查）。"""
        db = _db_of(client)
        w = _run(_seed_world(db))
        # m1：2 成功（10/20 tokens，各 0.1）+ 1 失败（30，0.1）+ 2020 老行（50，0.2）
        _run(_insert_at(db, "2026-09-21 10:00:00", group_id=w["g1"], model_id=w["m1"],
                        status="success", total_tokens=10, cost=0.1))
        _run(_insert_at(db, "2026-09-21 11:00:00", group_id=w["g1"], model_id=w["m1"],
                        status="success", total_tokens=20, cost=0.1))
        _run(_insert_at(db, "2026-09-21 12:00:00", group_id=w["g1"], model_id=w["m1"],
                        status="error", total_tokens=30, cost=0.1))
        _run(_insert_at(db, "2020-01-01 00:00:00", group_id=w["g1"], model_id=w["m1"],
                        status="success", total_tokens=50, cost=0.2))
        # m2：1 成功（40，0.5）—— 时间不敏感走 create_call_log
        _run(db.create_call_log(CallLog(model_id=w["m2"], group_id=w["g2"],
                                        status="success", total_tokens=40, cost=0.5)))
        r = client.get("/admin/stats/models", headers=AUTH)  # 无 range = 旧调用
        assert r.status_code == 200
        body = r.json()
        # 顶层键集恰 {success, model_stats}，无 range 回显键（§0.1 红线）
        assert set(body.keys()) == {"success", "model_stats"}
        rows = body["model_stats"]
        old6 = ["model_id", "model_name", "total_calls", "success_calls",
                "error_calls", "total_cost"]
        for row in rows:
            # 既有 6 键键名/顺序不变 + total_tokens + avg_latency_ms + error_rate（9 键）
            assert list(row.keys()) == old6 + ["total_tokens", "avg_latency_ms",
                                               "error_rate"]
        # ORDER BY total_calls DESC（m1=4 > m2=1）
        assert [row["model_id"] for row in rows] == [w["m1"], w["m2"]]
        a = rows[0]
        assert a["model_name"] == "m1"
        assert (a["total_calls"], a["success_calls"], a["error_calls"]) == (4, 3, 1)
        assert a["total_cost"] == pytest.approx(0.5)
        assert a["total_tokens"] == 110  # 全时间聚合，含 2020 老行
        b = rows[1]
        assert (b["total_calls"], b["success_calls"], b["error_calls"]) == (1, 1, 0)
        assert b["total_cost"] == pytest.approx(0.5)
        assert b["total_tokens"] == 40
        # 同库双查法：端点输出 == 直调 db.list_model_stats()（同数据）
        assert rows == _run(db.list_model_stats())

    def test_d4_2_api_key_filter_without_range(self, client):
        """D4.2 正例：?api_key_id 过滤仍生效，且不带 range 时行为同旧。"""
        db = _db_of(client)
        w = _run(_seed_world(db))
        k1 = _run(db.create_api_key("key-one", "a"))
        k2 = _run(db.create_api_key("key-two", "b"))
        for _ in range(2):
            _run(db.create_call_log(CallLog(model_id=w["m1"], group_id=w["g1"],
                                            status="success", api_key_id=k1.id,
                                            total_tokens=5, cost=0.01)))
        _run(db.create_call_log(CallLog(model_id=w["m1"], group_id=w["g1"],
                                        status="success", api_key_id=k2.id,
                                        total_tokens=7, cost=0.02)))
        r = client.get("/admin/stats/models", params={"api_key_id": k1.id}, headers=AUTH)
        assert r.status_code == 200
        rows = r.json()["model_stats"]
        assert len(rows) == 1
        assert rows[0]["total_calls"] == 2  # k2 的 1 条被过滤掉
        # 不带 range 时过滤行为同旧：与直调 db.list_model_stats(api_key_id=...) 逐字段一致
        assert rows == _run(db.list_model_stats(api_key_id=k1.id))
        # 无过滤：全部 3 条计入
        rows_all = client.get("/admin/stats/models", headers=AUTH).json()["model_stats"]
        assert rows_all[0]["total_calls"] == 3

    def test_d4_3_groups_and_cost_untouched(self, client):
        """D4.3 边界：/stats/groups、/stats/cost 响应逐字段与改动前一致。"""
        db = _db_of(client)
        w = _run(_seed_world(db))
        _run(db.create_call_log(CallLog(model_id=w["m1"], group_id=w["g1"],
                                        status="success", total_tokens=9, cost=0.7)))
        # /stats/groups：顶层与行内键集零新增（不带 total_tokens——本任务零触碰）
        g = client.get("/admin/stats/groups", headers=AUTH)
        assert g.status_code == 200
        gbody = g.json()
        assert set(gbody.keys()) == {"success", "group_stats"}
        grow = gbody["group_stats"][0]
        assert set(grow.keys()) == {"group_id", "group_name", "total_calls",
                                    "success_calls", "error_calls", "total_cost"}
        assert grow["total_calls"] == 1 and grow["total_cost"] == pytest.approx(0.7)
        # /stats/cost：既有 4 列口径不动（DATE(created_at) UTC 日分桶）
        c = client.get("/admin/stats/cost", params={"days": 30}, headers=AUTH)
        assert c.status_code == 200
        cbody = c.json()
        assert set(cbody.keys()) == {"success", "cost_summary"}
        assert isinstance(cbody["cost_summary"], list) and cbody["cost_summary"]
        for row in cbody["cost_summary"]:
            assert set(row.keys()) == {"day", "total_calls", "total_cost", "total_tokens"}
        assert any(row["total_cost"] == pytest.approx(0.7)
                   for row in cbody["cost_summary"])

    def test_d4_4_existing_stats_usecases_still_pass(self, client):
        """D4.4 正例：既有用例原样通过（点名回归面）。

        复刻 tests/test_admin_api.py:148-171（stats 三端点）与
        tests/test_db_full.py:80-87（db 层 list_model_stats/list_group_stats）
        的断言——契约 §0.8/§1.3 不变项，同 PR 必须仍绿。
        """
        # —— test_admin_api.py TestStats.test_models ——
        pid = client.post("/admin/providers",
                          json={"req": {"name": "openai", "base_url": "https://x"}},
                          headers=AUTH).json()["provider_id"]
        client.post("/admin/models", json={"req": {"provider_id": pid, "name": "gpt-4"}},
                    headers=AUTH)
        r = client.get("/admin/stats/models", headers=AUTH)
        assert r.status_code == 200 and r.json()["success"] is True
        r2 = client.get("/admin/stats/models", params={"api_key_id": 1}, headers=AUTH)
        assert r2.status_code == 200
        # —— TestStats.test_groups ——
        client.post("/admin/groups", json={"req": {"name": "empty"}}, headers=AUTH)
        r = client.get("/admin/stats/groups", headers=AUTH)
        assert r.status_code == 200 and r.json()["success"] is True
        r2 = client.get("/admin/stats/groups", params={"api_key_id": 1}, headers=AUTH)
        assert r2.status_code == 200
        # —— TestStats.test_cost ——
        r = client.get("/admin/stats/cost", params={"days": 30}, headers=AUTH)
        assert r.status_code == 200 and r.json()["success"] is True
        assert isinstance(r.json()["cost_summary"], list)
        r2 = client.get("/admin/stats/cost", params={"api_key_id": 1}, headers=AUTH)
        assert r2.status_code == 200
        # —— test_db_full.py:80-87 test_stats_listing ——
        db = _db_of(client)
        w = _run(_seed_world(db))
        _run(db.create_call_log(CallLog(model_id=w["m1"], group_id=w["g1"],
                                        status="success", cost=0.1)))
        assert isinstance(_run(db.list_model_stats()), list)
        assert isinstance(_run(db.list_group_stats()), list)
