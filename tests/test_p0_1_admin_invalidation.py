"""P0-1 Admin 写接口失效接线测试（F8/F9）。

覆盖 TC-21~TC-32（见 docs/tasks/P0-1_tests.md）。
只新建测试文件，不改动任何 src/ 源码。

手段：monkeypatch botflow.admin_api.get_db 返回 AsyncMock；
monkeypatch botflow.admin_api.invalidate_endpoint_cache / invalidate_all_caches
为 MagicMock，断言调用。404 分支让 raw 查询返回 None 并 pytest.raises(HTTPException)。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

import botflow.admin_api as admin
import botflow.pipeline._shared as shared
from botflow.admin_api import (
    UpdateGroupReq,
    UpdateModelReq,
    UpdateProviderReq,
    add_model_to_group,
    delete_group,
    delete_model,
    delete_provider,
    get_group,
    remove_model_from_group,
    update_group,
    update_model,
    update_model_weight,
    update_provider,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_caches():
    """清理共享运行态缓存，避免用例互相污染。"""
    shared._endpoint_cache.clear()
    shared._provider_cache.clear()
    shared._provider_semaphores.clear()
    yield
    shared._endpoint_cache.clear()
    shared._provider_cache.clear()
    shared._provider_semaphores.clear()


@pytest.fixture(autouse=True)
def _patch_invalidate(monkeypatch):
    """把两个失效函数替换为 MagicMock，供用例断言调用。"""
    inv_ep = MagicMock()
    inv_all = MagicMock()
    monkeypatch.setattr(admin, "invalidate_endpoint_cache", inv_ep)
    monkeypatch.setattr(admin, "invalidate_all_caches", inv_all)
    return {"ep": inv_ep, "all": inv_all}


@pytest.fixture
def db_mock(monkeypatch):
    """get_db 返回 AsyncMock，db 各 raw 方法按需覆盖。"""
    db = AsyncMock()
    monkeypatch.setattr(admin, "get_db", lambda: db)
    return db


# ---------------------------------------------------------------------------
# F8：group/model 写接口 → invalidate_endpoint_cache(gid)
# ---------------------------------------------------------------------------


# TC-21
@pytest.mark.asyncio
async def test_add_model_to_group_invalidates_endpoint_cache(db_mock, _patch_invalidate):
    db_mock.get_group_raw = AsyncMock(return_value=MagicMock())
    db_mock.get_model_raw = AsyncMock(return_value=MagicMock())
    db_mock.add_model_to_group_raw = AsyncMock(return_value=None)
    await add_model_to_group(group_id=1, model_id=10, weight=1)
    _patch_invalidate["ep"].assert_called_once_with(1)


# TC-22
@pytest.mark.asyncio
async def test_remove_model_from_group_invalidates_endpoint_cache(db_mock, _patch_invalidate):
    db_mock.remove_model_from_group_raw = AsyncMock(return_value=None)
    await remove_model_from_group(group_id=2, model_id=20)
    _patch_invalidate["ep"].assert_called_once_with(2)


# TC-23
@pytest.mark.asyncio
async def test_update_model_weight_invalidates_endpoint_cache(db_mock, _patch_invalidate):
    db_mock.update_model_weight_raw = AsyncMock(return_value=None)
    await update_model_weight(group_id=3, model_id=30, weight=5)
    _patch_invalidate["ep"].assert_called_once_with(3)


# TC-24
@pytest.mark.asyncio
async def test_update_group_invalidates_endpoint_cache(db_mock, _patch_invalidate):
    db_mock.get_group_raw = AsyncMock(return_value=MagicMock())
    db_mock.update_group_raw = AsyncMock(return_value=None)
    await update_group(group_id=4, req=UpdateGroupReq(name="new"))
    _patch_invalidate["ep"].assert_called_once_with(4)


# TC-25
@pytest.mark.asyncio
async def test_delete_group_invalidates_endpoint_cache(db_mock, _patch_invalidate):
    db_mock.delete_group_raw = AsyncMock(return_value=True)
    await delete_group(group_id=5)
    _patch_invalidate["ep"].assert_called_once_with(5)


# ---------------------------------------------------------------------------
# F9：provider/model 写接口 → invalidate_all_caches()
# ---------------------------------------------------------------------------


# TC-26
@pytest.mark.asyncio
async def test_update_provider_invalidates_all_caches(db_mock, _patch_invalidate):
    db_mock.get_provider_raw = AsyncMock(return_value=MagicMock())
    db_mock.update_provider_raw = AsyncMock(return_value=None)
    await update_provider(provider_id=1, req=UpdateProviderReq(name="x"))
    _patch_invalidate["all"].assert_called_once_with()


# TC-27
@pytest.mark.asyncio
async def test_delete_provider_invalidates_all_caches(db_mock, _patch_invalidate):
    db_mock.delete_provider_raw = AsyncMock(return_value=True)
    await delete_provider(provider_id=1)
    _patch_invalidate["all"].assert_called_once_with()


# TC-28
@pytest.mark.asyncio
async def test_update_model_invalidates_all_caches(db_mock, _patch_invalidate):
    db_mock.get_model_raw = AsyncMock(return_value=MagicMock())
    db_mock.update_model_raw = AsyncMock(return_value=None)
    await update_model(model_id=1, req=UpdateModelReq(name="x"))
    _patch_invalidate["all"].assert_called_once_with()


# TC-29
@pytest.mark.asyncio
async def test_delete_model_invalidates_all_caches(db_mock, _patch_invalidate):
    db_mock.delete_model_raw = AsyncMock(return_value=True)
    await delete_model(model_id=1)
    _patch_invalidate["all"].assert_called_once_with()


# ---------------------------------------------------------------------------
# 反例：404 分支 / 读接口不触发失效
# ---------------------------------------------------------------------------


# TC-30
@pytest.mark.asyncio
async def test_update_group_404_does_not_invalidate(db_mock, _patch_invalidate):
    db_mock.get_group_raw = AsyncMock(return_value=None)
    with pytest.raises(HTTPException):
        await update_group(group_id=4, req=UpdateGroupReq(name="new"))
    _patch_invalidate["ep"].assert_not_called()


# TC-31
@pytest.mark.asyncio
async def test_update_provider_404_does_not_invalidate(db_mock, _patch_invalidate):
    db_mock.get_provider_raw = AsyncMock(return_value=None)
    with pytest.raises(HTTPException):
        await update_provider(provider_id=1, req=UpdateProviderReq(name="x"))
    _patch_invalidate["all"].assert_not_called()


# TC-32
@pytest.mark.asyncio
async def test_get_group_does_not_invalidate(db_mock, _patch_invalidate):
    db_mock.get_group_raw = AsyncMock(return_value=MagicMock())
    await get_group(group_id=1)
    _patch_invalidate["ep"].assert_not_called()
    _patch_invalidate["all"].assert_not_called()
