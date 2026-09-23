"""setup token 单测 T1.1–T6.1（契约：docs/tasks/setup_token_features.md §3，v2 已审核放行）。

42 条（37 条对表 + 5 条 mq3 覆盖缺口补测），编号/类型/期望与 §3 各表一一对应：
  F1 生成与文件原语  T1.1–T1.8（8）
  F2 四分支+接线     T2.1–T2.14（14，T2.12–T2.14 为 mq3 补测）
  F3 setup 校验切换  T3.1–T3.9（9，T3.9 为 mq3 补测）
  F4 成功双删        T4.1–T4.5（5）
  F5 不变项守卫      T5.1–T5.5（5）
  F6 默认路径分支    T6.1（1，mq3 补测）
集成 TI.1–TI.5 在 tests/test_setup_token_e2e.py。

落地手法（§3 指定）：
- 文件系统分支用 ``tmp_path`` —— conftest autouse fixture 已把
  ``BOTFLOW_SETUP_TOKEN_FILE`` 指向本用例 tmp_path（ZG-2 硬隔离）；
- KV 用既有 db fixture 模式（tests/test_admin_auth.py）；
- HTTP 层沿用 ``TestClient + app.dependency_overrides[dbmod.get_db]`` 模式；
- R6 双轨：Windows（本仓测试平台）T1.3/T1.4 spy ``os.fchmod`` 断言实参
  ``0o600``，POSIX 断 ``stat.S_IMODE == 0o600``；T2.11 用 monkeypatch
  ``os.unlink`` 抛 ``PermissionError``。
"""

from __future__ import annotations

import asyncio
import contextlib
import errno as errno_mod
import hashlib
import json
import os
import secrets
import stat
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger

import botflow
import botflow.core as core
from botflow import auth as auth_mod
from botflow.admin_api import admin_router
from botflow.auth import (
    SETUP_TOKEN_KEY,
    ensure_setup_token,
    generate_setup_token,
    write_setup_token_file,
)
from botflow.config import BotflowSettings, set_config
from botflow.storage import db as dbmod
from botflow.storage.db import Database

# setup 端点 401 detail 新文案（决策 3；Bearer 通道文案不动，见 T5.3）。
SETUP_401 = "Invalid setup token."
ADMIN_KEY = "admin-secret"
PW = "password123"
_HEX32 = set("0123456789abcdef")


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fast_pbkdf2(monkeypatch):
    # setup/login 各要跑一次哈希，600k 迭代单测吃不消（沿用既有模式）。
    monkeypatch.setattr(auth_mod, "PBKDF2_ITERATIONS", 1000)


@pytest.fixture
async def db(tmp_path):
    """直调用例的库。建库前重置 settings 单例：``setup_token_path`` 的字段支
    可能残留上一个用例的 tmp_path（跨文件单例泄漏），fresh 构造保证字段从
    当前 ``BOTFLOW_SETUP_TOKEN_FILE`` 取值，与本用例 ``_path()`` 一致。"""
    set_config(BotflowSettings())
    d = Database(str(tmp_path / "setup_token.db"))
    await d.initialize()
    yield d
    await d.close()
    set_config(None)


@pytest.fixture
def client(tmp_path):
    """HTTP 层 fixture：沿用 TestClient + dependency_overrides 模式（§3）。

    与 tests/test_admin_auth.py 不同：本文件**不**在 fixture 预置凭证 ——
    T3.6 需要「无 KV 记录」的原始环境，各用例按表自行 ``_preset_http``。
    """
    d = Database(str(tmp_path / "setup_token_http.db"))
    asyncio.new_event_loop().run_until_complete(d.initialize())
    set_config(BotflowSettings(admin_key=ADMIN_KEY))
    app = FastAPI()
    app.include_router(admin_router)
    app.dependency_overrides[dbmod.get_db] = lambda: d
    with TestClient(app) as c:
        yield c
    asyncio.new_event_loop().run_until_complete(d.close())
    set_config(None)


@pytest.fixture
def lifespan_env(tmp_path, monkeypatch):
    """T2.9/T2.10 的 lifespan 驱动环境：照 tests/test_core_runtime.py
    ``_lifespan_env`` 的模式保存/还原 core 全局。

    后台任务循环不桩掉也安全：各循环先 sleep 再干活，本文件只关心
    「进入 lifespan（启动段含 ensure 接线）→ 退出」，退出时 cancel 即干净
    收场（CallLogWriter.stop / 各 task 的 CancelledError 均有既有吞掉点）。
    """
    saved = (core._db, core._config, core._log_writer)
    monkeypatch.setenv("LLM_KEY", "sk-lifespan-test")
    yield
    core._db, core._config, core._log_writer = saved


def _path() -> Path:
    """当前用例的 setup token 文件路径（与 ensure_setup_token 同源解析）。"""
    return auth_mod.get_config().setup_token_path


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _db_of(client) -> Database:
    return client.app.dependency_overrides[dbmod.get_db]()


def _kv_value(token: str) -> str:
    """KV 记录 value：只存 sha256 哈希 + 生成时间（明文不进库，硬约束 2）。"""
    return json.dumps({
        "hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "created_at": time.time(),
    })


def _account_value(username: str = "alice") -> str:
    return json.dumps({"username": username, "pwd_hash": "pbkdf2_sha256$1000$00$00"})


def _is_hex32(content: str) -> bool:
    return len(content) == 32 and set(content) <= _HEX32


def _preset_http(db: Database) -> str:
    """HTTP 用例预置：写 `.setup_token` 文件 + 写 KV 哈希，返回明文 token。"""
    token = generate_setup_token()
    write_setup_token_file(_path(), token)
    _run(db.set_config(SETUP_TOKEN_KEY, _kv_value(token)))
    return token


def _setup(client, token: str, username: str = "alice", password: str = PW):
    return client.post("/admin/auth/setup", json={
        "token": token, "username": username, "password": password,
    })


@contextlib.contextmanager
def _admin_key(key: str):
    """临时改 BOTFLOW_ADMIN_KEY（setup 与它的解耦断言用）。"""
    set_config(BotflowSettings(admin_key=key))
    try:
        yield
    finally:
        set_config(BotflowSettings(admin_key=ADMIN_KEY))


@contextlib.contextmanager
def _capture_logs(level: str = "INFO"):
    """loguru sink 捕获：list.append 当 sink，format 只留 message 本体。"""
    sink: list[str] = []
    handler = logger.add(sink.append, level=level, format="{message}")
    try:
        yield sink
    finally:
        logger.remove(handler)


# ---------------------------------------------------------------------------
# F1 生成与文件原语（T1.1-T1.8）
# ---------------------------------------------------------------------------


class TestF1GenerationAndFilePrimitives:

    async def test_t1_1_fresh_state_generates_kv_and_file(self, db):
        # 前置：未配置全空（无 admin_account、无 KV、无文件）。
        assert await db.get_config("admin_account") is None
        assert await db.get_config(SETUP_TOKEN_KEY) is None
        assert not _path().exists()

        await ensure_setup_token(db)

        raw = await db.get_config(SETUP_TOKEN_KEY)
        assert raw is not None
        value = json.loads(raw)
        assert set(value) == {"hash", "created_at"}
        assert len(value["hash"]) == 64  # sha256 hex
        assert isinstance(value["created_at"], float)  # float epoch
        assert _path().exists()
        content = _path().read_text(encoding="utf-8")
        assert _is_hex32(content)  # 内容恰 32 hex

    async def test_t1_2_kv_stores_hash_not_plaintext(self, db):
        await ensure_setup_token(db)
        plaintext = _path().read_text(encoding="utf-8")
        raw = await db.get_config(SETUP_TOKEN_KEY)
        assert raw is not None

        # 遍历 KV value 的每一项：均不含生成的明文 token。
        value = json.loads(raw)
        for item in value.values():
            assert plaintext not in str(item)
        assert plaintext not in raw
        # sha256(文件明文) == value["hash"]。
        assert value["hash"] == hashlib.sha256(plaintext.encode("utf-8")).hexdigest()

    async def test_t1_3_file_mode_0600(self, db, monkeypatch):
        if sys.platform == "win32":
            # R6 双轨：win32 只保证只读位、stat 不可信 → spy os.fchmod 断言实参 0o600。
            calls: list[int] = []
            monkeypatch.setattr(
                os, "fchmod", lambda fd, mode: calls.append(mode), raising=False,
            )
            await ensure_setup_token(db)
            assert calls, "write_setup_token_file 必须无条件调用 os.fchmod"
            assert all(mode == 0o600 for mode in calls)
        else:
            await ensure_setup_token(db)
            assert stat.S_IMODE(_path().stat().st_mode) == 0o600
        # 权限断言落点即生成的 32 hex 文件。
        assert _path().exists()
        assert _is_hex32(_path().read_text(encoding="utf-8"))

    async def test_t1_4_existing_0644_tightened_content_unchanged(
        self, db, monkeypatch,
    ):
        token = generate_setup_token()
        path = _path()
        path.write_text(token, encoding="utf-8")
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass  # win32 chmod 只支持只读位，预置尽力而为（断言走 R6 双轨）
        await db.set_config(SETUP_TOKEN_KEY, _kv_value(token))  # 双在分支

        if sys.platform == "win32":
            # R6 双轨：spy os.fchmod 断言收紧动作实参 0o600。
            calls: list[int] = []
            monkeypatch.setattr(
                os, "fchmod", lambda fd, mode: calls.append(mode), raising=False,
            )
            await ensure_setup_token(db)
            assert any(
                mode == 0o600 for mode in calls
            ), "过宽旧权限收紧必须经 os.fchmod(fd, 0o600)（决策 1）"
        else:
            assert stat.S_IMODE(path.stat().st_mode) == 0o644  # 预置生效
            await ensure_setup_token(db)
            assert stat.S_IMODE(path.stat().st_mode) == 0o600  # 收紧到 0600

        # 收紧 ≠ 轮换：内容不变、KV 哈希不动。
        assert path.read_text(encoding="utf-8") == token
        value = json.loads(await db.get_config(SETUP_TOKEN_KEY))
        assert value["hash"] == hashlib.sha256(token.encode("utf-8")).hexdigest()

    async def test_t1_5_file_and_kv_hash_consistent(self, db):
        await ensure_setup_token(db)
        plaintext = _path().read_text(encoding="utf-8")
        value = json.loads(await db.get_config(SETUP_TOKEN_KEY))
        computed = hashlib.sha256(plaintext.encode("utf-8")).hexdigest().encode()
        stored = str(value["hash"]).encode()
        assert secrets.compare_digest(computed, stored)  # 双写可互证

    async def test_t1_6_token_shape_no_newline(self, db):
        await ensure_setup_token(db)
        raw_bytes = _path().read_bytes()
        token = raw_bytes.decode("utf-8")
        assert len(token) == 32
        assert set(token) <= _HEX32  # secrets.token_hex(16) 的产物形状
        assert len(bytes.fromhex(token)) == 16
        assert b"\n" not in raw_bytes and b"\r" not in raw_bytes  # 纯 32 hex、无换行
        assert _path().read_text(encoding="utf-8") == token  # read() == token

    async def test_t1_7_startup_log_contains_plaintext_once(self, db):
        with _capture_logs() as sink:
            await ensure_setup_token(db)
        token = _path().read_text(encoding="utf-8")
        hits = [msg for msg in sink if token in msg]
        assert len(hits) == 1  # 恰有一行含明文 token（启动日志双写出处）

    async def test_t1_8_orphan_file_overwritten_branch2(self, db):
        stale = "0" * 32  # 磁盘残留旧文件、KV 无记录 → 走分支②（R11）
        _path().write_text(stale, encoding="utf-8")

        await ensure_setup_token(db)

        fresh = _path().read_text(encoding="utf-8")
        assert fresh != stale  # O_TRUNC 换新，文件内容 ≠ 旧内容
        value = json.loads(await db.get_config(SETUP_TOKEN_KEY))
        assert value["hash"] == hashlib.sha256(fresh.encode("utf-8")).hexdigest()
        assert value["hash"] != hashlib.sha256(stale.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# F2 生命周期四分支 + 接线（T2.1-T2.14，T2.12-T2.14 为 mq3 缺口补测）
# ---------------------------------------------------------------------------


class TestF2LifecycleFourBranches:

    async def test_t2_1_startup_generation_branch2(self, db):
        await ensure_setup_token(db)
        value = json.loads(await db.get_config(SETUP_TOKEN_KEY))
        assert set(value) == {"hash", "created_at"}  # 同 T1.1 结果
        content = _path().read_text(encoding="utf-8")
        assert _is_hex32(content)

    async def test_t2_2_missing_or_invalid_file_rotates(self, db):
        path = _path()

        # 变体①：KV 有记录 + 文件缺 → 重新生成，日志 rotated: file missing。
        old = generate_setup_token()
        await db.set_config(SETUP_TOKEN_KEY, _kv_value(old))
        with _capture_logs() as sink:
            await ensure_setup_token(db)
        fresh = path.read_text(encoding="utf-8")
        assert fresh != old  # 新文件内容 ≠ 旧明文
        value = json.loads(await db.get_config(SETUP_TOKEN_KEY))
        assert value["hash"] == hashlib.sha256(fresh.encode("utf-8")).hexdigest()
        assert value["hash"] != hashlib.sha256(old.encode("utf-8")).hexdigest()
        assert any("rotated: file missing" in msg for msg in sink)

        # 变体②：文件 0 字节 → rotated: file invalid。
        path.write_text("", encoding="utf-8")
        with _capture_logs() as sink:
            await ensure_setup_token(db)
        assert any("rotated: file invalid" in msg for msg in sink)
        assert _is_hex32(path.read_text(encoding="utf-8"))

        # 变体③：文件截断（16 hex ≠ 32）→ rotated: file invalid。
        path.write_text("abcdef0123456789", encoding="utf-8")
        with _capture_logs() as sink:
            await ensure_setup_token(db)
        assert any("rotated: file invalid" in msg for msg in sink)

        # 变体④：文件污染（32 位非 hex）→ rotated: file invalid。
        path.write_text("Z" * 32, encoding="utf-8")
        with _capture_logs() as sink:
            await ensure_setup_token(db)
        assert any("rotated: file invalid" in msg for msg in sink)
        assert _is_hex32(path.read_text(encoding="utf-8"))

    async def test_t2_3_both_present_no_rotation(self, db):
        token = generate_setup_token()
        path = _path()
        path.write_text(token, encoding="utf-8")
        await db.set_config(SETUP_TOKEN_KEY, _kv_value(token))
        raw_before = await db.get_config(SETUP_TOKEN_KEY)

        await ensure_setup_token(db)  # 分支④：双在且内容合法 32 hex

        assert (await db.get_config(SETUP_TOKEN_KEY)) == raw_before  # hash 不变
        assert path.read_text(encoding="utf-8") == token  # 文件内容不变

    async def test_t2_4_configured_idempotent_cleanup(self, db):
        path = _path()
        await db.set_config("admin_account", _account_value())
        token = generate_setup_token()
        path.write_text(token, encoding="utf-8")
        await db.set_config(SETUP_TOKEN_KEY, _kv_value(token))

        await ensure_setup_token(db)  # 分支①：已配置 → 幂等清理

        assert await db.get_config(SETUP_TOKEN_KEY) is None  # KV 记录删
        assert not path.exists()  # 文件删
        account = json.loads(await db.get_config("admin_account"))
        assert account["username"] == "alice"  # admin_account 不动

    async def test_t2_5_configured_all_empty_no_generation(self, db):
        await db.set_config("admin_account", _account_value())

        await ensure_setup_token(db)  # 不抛（幂等）

        assert not _path().exists()  # 文件不被创建
        assert await db.get_config(SETUP_TOKEN_KEY) is None  # 不生成

    async def test_t2_6_configured_orphan_file_deleted(self, db):
        await db.set_config("admin_account", _account_value())
        _path().write_text(generate_setup_token(), encoding="utf-8")  # 孤儿：无 KV

        await ensure_setup_token(db)

        assert not _path().exists()  # 清理优先于生成
        assert await db.get_config(SETUP_TOKEN_KEY) is None

    async def test_t2_7_configured_kv_present_file_missing(self, db):
        await db.set_config("admin_account", _account_value())
        await db.set_config(SETUP_TOKEN_KEY, _kv_value(generate_setup_token()))

        await ensure_setup_token(db)

        assert await db.get_config(SETUP_TOKEN_KEY) is None  # 只删 KV
        assert not _path().exists()  # 不重建文件

    async def test_t2_8_two_consecutive_ensures_same_token(self, db):
        await ensure_setup_token(db)
        first_token = _path().read_text(encoding="utf-8")
        first_raw = await db.get_config(SETUP_TOKEN_KEY)

        await ensure_setup_token(db)  # 生成后立即重启模拟 → 第二次落分支④

        assert _path().read_text(encoding="utf-8") == first_token
        assert (await db.get_config(SETUP_TOKEN_KEY)) == first_raw  # token 不变

    async def test_t2_9_lifespan_wiring_awaits_ensure(
        self, db, tmp_path, monkeypatch, lifespan_env,
    ):
        # 照 tests/test_core_runtime.py 的 lifespan 模式真实驱动 core.lifespan。
        spy = AsyncMock()
        monkeypatch.setattr(core, "ensure_setup_token", spy)
        cfg = BotflowSettings(
            workspace=str(tmp_path), daily_summary_hour=3, model_sync_interval=0,
        )
        set_config(cfg)
        core._db = db
        core._config = cfg

        async with core.lifespan(core.app):
            # 被 await 且传入当前 db（core.py 接线行不落 UNCOVERED）。
            spy.assert_awaited_once_with(db)

    async def test_t2_10_write_failure_non_fatal(
        self, db, tmp_path, monkeypatch, lifespan_env,
    ):
        # ZG-1/R7：写文件任一步失败 → 仅 log.error（含路径与 errno），
        # 不抛、不中止启动、不写 KV。
        cfg = BotflowSettings(
            workspace=str(tmp_path), daily_summary_hour=3, model_sync_interval=0,
        )
        set_config(cfg)
        core._db = db
        core._config = cfg

        def _fail_write(fd, data):
            raise OSError(errno_mod.EACCES, "permission denied")

        monkeypatch.setattr(os, "write", _fail_write)

        # ① 直调 ensure：能走到这里 = 不抛；KV 未写入；log.error 可捕获。
        with _capture_logs(level="ERROR") as sink:
            await ensure_setup_token(db)
        assert await db.get_config(SETUP_TOKEN_KEY) is None  # 不写 KV
        errors = [
            msg for msg in sink
            if str(_path()) in msg and "errno" in msg.lower()  # 含路径与 errno
        ]
        assert errors, "写文件失败必须 log.error（含路径与 errno）"

        # ② lifespan/启动继续：写失败不阻断服务启动（F2 错误分支）。
        async with core.lifespan(core.app):
            assert await db.get_config(SETUP_TOKEN_KEY) is None

    async def test_t2_11_unlink_failure_non_fatal(self, db, monkeypatch):
        path = _path()
        await db.set_config("admin_account", _account_value())
        path.write_text(generate_setup_token(), encoding="utf-8")
        await db.set_config(SETUP_TOKEN_KEY, _kv_value(generate_setup_token()))

        def _fail_unlink(p):
            raise PermissionError(errno_mod.EACCES, "permission denied", str(p))

        monkeypatch.setattr(os, "unlink", _fail_unlink)

        with _capture_logs(level="ERROR") as sink:
            await ensure_setup_token(db)  # 分支①清理：不抛

        assert await db.get_config(SETUP_TOKEN_KEY) is None  # KV 照删
        assert path.exists()  # 文件删失败留场（下次启动分支①重试）
        errors = [
            msg for msg in sink
            if str(path) in msg and "errno" in msg.lower()  # loguru error 可捕获
        ]
        assert errors, "删文件失败必须 log.error（含路径与 errno）"

    async def test_t2_12_unreadable_file_rotates_file_invalid(
        self, db, monkeypatch,
    ):
        # mq3 缺口 auth.py:299-300（分支③）：read_text 抛非 FileNotFoundError
        # 的 OSError/UnicodeDecodeError → rotate_reason="file invalid" → 轮换。
        old = generate_setup_token()
        path = _path()
        path.write_text(old, encoding="utf-8")
        await db.set_config(SETUP_TOKEN_KEY, _kv_value(old))
        old_raw = await db.get_config(SETUP_TOKEN_KEY)

        def _fail_read(self, *args, **kwargs):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

        monkeypatch.setattr(Path, "read_text", _fail_read)

        with _capture_logs() as sink:
            await ensure_setup_token(db)  # 不抛（ZG-1/R7 降级契约）

        assert any("rotated: file invalid" in msg for msg in sink)
        # read_text 仍被 patch，走 bytes 旁路断言落盘内容。
        fresh = path.read_bytes().decode("utf-8")
        assert _is_hex32(fresh)  # 新 token 文件已写入
        assert fresh != old
        value = json.loads(await db.get_config(SETUP_TOKEN_KEY))
        assert value["hash"] == hashlib.sha256(fresh.encode("utf-8")).hexdigest()
        assert value["hash"] != json.loads(old_raw)["hash"]  # KV 哈希已换新

    async def test_t2_13_fchmod_failure_warning_only_no_rotation(
        self, db, monkeypatch,
    ):
        # mq3 缺口 auth.py:314-315（分支④收紧）：os.open/os.fchmod 抛 OSError
        # → 仅 warning 不抛、内容与 KV 均不轮换。
        token = generate_setup_token()
        path = _path()
        path.write_text(token, encoding="utf-8")
        await db.set_config(SETUP_TOKEN_KEY, _kv_value(token))
        raw_before = await db.get_config(SETUP_TOKEN_KEY)

        calls: list[int] = []

        def _fail_fchmod(fd, mode):
            calls.append(mode)
            raise PermissionError(errno_mod.EACCES, "permission denied")

        monkeypatch.setattr(os, "fchmod", _fail_fchmod, raising=False)

        with _capture_logs(level="WARNING") as sink:
            await ensure_setup_token(db)  # 分支④：不抛

        # R6 win32 轨 spy 断言：收紧动作确实经 os.fchmod(fd, 0o600) 发起。
        assert calls, "分支④必须调用 os.fchmod(fd, 0o600)"
        assert all(mode == 0o600 for mode in calls)
        assert any("chmod failed" in msg for msg in sink)
        assert path.read_text(encoding="utf-8") == token  # 文件内容原样未变
        assert (await db.get_config(SETUP_TOKEN_KEY)) == raw_before  # KV 未动

    async def test_t2_14_kv_write_failure_non_fatal(self, db, monkeypatch):
        # mq3 缺口 auth.py:340-343：写文件成功后 db.set_config 抛错 → 仅
        # error 不抛；KV 仍无记录（下次启动分支②自愈的契约前提）+ 文件已写入
        # （证明「先文件后 KV」动作序）。
        async def _fail_set(key, value):
            raise RuntimeError("kv down")

        monkeypatch.setattr(db, "set_config", _fail_set)

        with _capture_logs(level="ERROR") as sink:
            await ensure_setup_token(db)  # 分支②路径：不抛

        assert any("KV write failed" in msg for msg in sink)
        assert await db.get_config(SETUP_TOKEN_KEY) is None  # KV 仍无记录
        path = _path()
        assert path.exists()  # 文件先落盘
        assert _is_hex32(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# F3 setup 校验切换（T3.1-T3.9，HTTP 层）
# ---------------------------------------------------------------------------


class TestF3SetupValidation:

    def test_t3_1_valid_token_setup_200(self, client):
        db = _db_of(client)
        token = _preset_http(db)

        r = _setup(client, token)

        assert r.status_code == 200 and r.json() == {"success": True}
        account = json.loads(_run(db.get_config("admin_account")))
        assert account["username"] == "alice"  # admin_account 落库
        assert account["pwd_hash"].startswith("pbkdf2_sha256$")

    def test_t3_2_wrong_token_401_new_detail(self, client):
        db = _db_of(client)
        _preset_http(db)

        r = _setup(client, generate_setup_token())  # 等长随机 hex

        assert r.status_code == 401
        assert r.json()["detail"] == SETUP_401  # 恰为 "Invalid setup token."

    def test_t3_3_admin_key_cannot_setup(self, client):
        db = _db_of(client)
        _preset_http(db)  # 环境里有合法 setup token，但提交 admin key 仍必须被拒

        r = _setup(client, ADMIN_KEY)  # BOTFLOW_ADMIN_KEY

        assert r.status_code == 401
        assert r.json()["detail"] == SETUP_401  # 决策 3 核心守卫

    def test_t3_4_empty_token_short_circuits_before_hash(self, client, monkeypatch):
        db = _db_of(client)
        _preset_http(db)
        calls = []
        real_sha256 = hashlib.sha256

        def _spy(data=b"", *args, **kwargs):
            calls.append(data)
            return real_sha256(data, *args, **kwargs)

        monkeypatch.setattr(hashlib, "sha256", _spy)

        r = _setup(client, "")

        assert r.status_code == 401
        assert r.json()["detail"] == SETUP_401
        assert calls == []  # 空 token 短路：不进哈希比对

    def test_t3_5_missing_token_field_401_not_422(self, client):
        db = _db_of(client)
        _preset_http(db)

        r = client.post("/admin/auth/setup", json={"username": "alice", "password": PW})

        # AuthReq 缺省 "" → 与显式空串同一 401 短路；不是 422、不是 500。
        assert r.status_code == 401
        assert r.json()["detail"] == SETUP_401

    def test_t3_6_no_kv_record_401_not_500(self, client):
        # 不预置 KV（未走启动钩子的极端）→ 替代原 T6.3「500」语义。
        r1 = _setup(client, generate_setup_token())
        assert r1.status_code == 401  # 不是 500
        assert r1.json()["detail"] == SETUP_401

        # admin key 未配置也不再 500（admin key 与 setup 彻底解耦）。
        with _admin_key(""):
            r2 = _setup(client, generate_setup_token())
        assert r2.status_code == 401
        assert r2.json()["detail"] == SETUP_401

    def test_t3_7_non_ascii_token_401_not_500(self, client):
        db = _db_of(client)
        _preset_http(db)

        r = _setup(client, "中文token")

        # 先 sha256 后比 hex：任意字节可哈希，天然无 compare_digest TypeError。
        assert r.status_code == 401
        assert r.json()["detail"] == SETUP_401

    def test_t3_8_admin_key_unconfigured_still_200(self, client):
        db = _db_of(client)
        token = _preset_http(db)

        with _admin_key(""):  # BOTFLOW_ADMIN_KEY 未配置
            r = _setup(client, token)

        assert r.status_code == 200  # setup 与 BOTFLOW_ADMIN_KEY 彻底解耦

    def test_t3_9_corrupt_kv_row_401_no_repair(self, client):
        # mq3 缺口 admin_api.py:168-169：KV 行非 JSON → stored_hash="" →
        # 比对必不等 → 401，且不当场修数据（status 仍 configured=false）。
        db = _db_of(client)
        token = generate_setup_token()
        write_setup_token_file(_path(), token)  # 文件合法，损坏的只是 KV 行
        corrupt = "{not json"
        _run(db.set_config(SETUP_TOKEN_KEY, corrupt))

        r = _setup(client, token)  # 「正确」token 也比不中（stored_hash=""）

        assert r.status_code == 401
        assert r.json()["detail"] == SETUP_401  # 恰 "Invalid setup token."
        assert _run(db.get_config(SETUP_TOKEN_KEY)) == corrupt  # 不当场修数据
        status = client.get("/admin/auth/status").json()
        assert status["configured"] is False  # 未开通


# ---------------------------------------------------------------------------
# F4 setup 成功双删（T4.1-T4.5）
# ---------------------------------------------------------------------------


class TestF4DoubleDelete:

    def test_t4_1_setup_success_double_delete(self, client):
        db = _db_of(client)
        token = _preset_http(db)

        r = _setup(client, token)

        assert r.status_code == 200  # T3.1 断言的 200
        assert _run(db.get_config(SETUP_TOKEN_KEY)) is None  # KV 记录已删
        assert not _path().exists()  # .setup_token 文件已删

    def test_t4_2_reset_double_delete_and_purge(self, client):
        db = _db_of(client)
        token = _preset_http(db)
        assert _setup(client, token).status_code == 200
        old_session = client.post("/admin/auth/login", json={
            "username": "alice", "password": PW,
        }).json()["token"]

        # 已建号再 setup（重置）：成功双删已毁凭证 → 按同法重新预置。
        token = _preset_http(db)
        r = _setup(client, token, username="bob", password="newpassword")

        assert r.status_code == 200
        assert _run(db.get_config(SETUP_TOKEN_KEY)) is None  # 双删仍执行
        assert not _path().exists()
        # 旧会话 purge（沿用原 T6.9 断言）。
        r = client.get(
            "/admin/providers", headers={"Authorization": f"Bearer {old_session}"}
        )
        assert r.status_code == 401

    def test_t4_3_wrong_token_401_never_cleans(self, client):
        db = _db_of(client)
        _preset_http(db)
        raw_before = _run(db.get_config(SETUP_TOKEN_KEY))
        content_before = _path().read_text(encoding="utf-8")

        r = _setup(client, generate_setup_token())

        assert r.status_code == 401  # 失败路径绝不清理（防自锁）
        assert r.json()["detail"] == SETUP_401
        assert _run(db.get_config(SETUP_TOKEN_KEY)) == raw_before  # KV 原样
        assert _path().read_text(encoding="utf-8") == content_before  # 文件原样

    def test_t4_4_file_missing_still_200(self, client):
        db = _db_of(client)
        token = _preset_http(db)
        _path().unlink()  # 文件本就不存在（手工删）

        r = _setup(client, token)

        assert r.status_code == 200  # FileNotFoundError 吞掉（幂等）
        assert _run(db.get_config(SETUP_TOKEN_KEY)) is None

    def test_t4_5_ensure_after_setup_branch1_no_new_token(self, client):
        db = _db_of(client)
        token = _preset_http(db)
        assert _setup(client, token).status_code == 200

        # 串联：setup 成功 → 再跑一次 ensure_setup_token。
        _run(ensure_setup_token(db))

        assert not _path().exists()  # 落分支①（账号已配置 → 幂等清理）
        assert _run(db.get_config(SETUP_TOKEN_KEY)) is None  # 不生成新 token
        assert _run(db.get_config("admin_account")) is not None  # 账号仍在


# ---------------------------------------------------------------------------
# F5/F6 不变项与 Bearer 回归（T5.1-T5.5）
# ---------------------------------------------------------------------------


class TestF5InvariantsAndBearer:

    def test_t5_1_status_never_contains_token(self, client):
        db = _db_of(client)
        token = _preset_http(db)
        kv_hash = json.loads(_run(db.get_config(SETUP_TOKEN_KEY)))["hash"]

        r = client.get("/admin/auth/status")

        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"success", "configured", "username"}  # key 集合恰为此
        # P0 红线（R1）：响应全文不得出现 token / KV 哈希 / 记录名。
        assert "admin_setup_token" not in r.text
        assert kv_hash not in r.text
        assert token not in r.text

    def test_t5_2_bearer_admin_key_still_works(self, client):
        r = client.get(
            "/admin/providers", headers={"Authorization": f"Bearer {ADMIN_KEY}"}
        )
        assert r.status_code == 200  # 切换后脚本通道不回归

    def test_t5_3_forged_session_401_invalid_admin_key(self, client):
        r = client.get(
            "/admin/providers",
            headers={"Authorization": "Bearer forged-session-token"},
        )
        assert r.status_code == 401
        # verify_admin_key 文案不随 setup 新文案漂移（双文案各守其门）。
        assert r.json()["detail"] == "Invalid admin key."

    def test_t5_4_login_logout_semantics_unchanged(self, client):
        db = _db_of(client)
        token = _preset_http(db)
        assert _setup(client, token).status_code == 200

        r = client.post("/admin/auth/login", json={"username": "alice", "password": PW})
        assert r.status_code == 200
        sess = r.json()["token"]
        assert isinstance(sess, str) and len(sess) >= 40

        r = client.post(
            "/admin/auth/logout", headers={"Authorization": f"Bearer {sess}"}
        )
        assert r.status_code == 200 and r.json() == {"success": True}

    def test_t5_5_admin_api_no_require_admin_key_attr(self):
        import botflow.admin_api as m

        # import 清理锁死：admin_api 不再引用 _require_admin_key。
        assert not hasattr(m, "_require_admin_key")
        # 函数本体在 auth 模块仍可导出直调（F5：函数不删）。
        assert hasattr(auth_mod, "_require_admin_key")


# ---------------------------------------------------------------------------
# F6 默认路径分支（T6.1，config.py）
# ---------------------------------------------------------------------------


class TestF6ConfigDefaultPath:

    def test_t6_1_default_path_project_root(self, monkeypatch):
        # mq3 缺口 config.py:84：字段空且 env 空 → 项目根 / ".setup_token"。
        # conftest autouse fixture 恒设 env（掩盖该分支），用例内删掉才走到；
        # teardown 由 monkeypatch 自动还原，不污染其它用例。
        monkeypatch.delenv("BOTFLOW_SETUP_TOKEN_FILE", raising=False)
        settings = BotflowSettings()
        assert settings.setup_token_file == ""  # 字段亦空
        # 与 src 同源推导断言（config.py parents[2] 即项目根），不硬编码绝对路径。
        assert settings.setup_token_path == (
            Path(botflow.__file__).resolve().parents[2] / ".setup_token"
        )
