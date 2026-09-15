# P0-1 测试用例清单：运行态缓存/限流单一事实源收敛 + Admin 配置失效

> 配套文档：`docs/tasks/P0-1_features.md`
> 覆盖功能点：F1~F10
> 覆盖率目标：100%（非平凡逻辑全部有可运行检查；平凡一行可标注 `# UNCOVERED`）
> 维度：每个用例标注 正例 / 反例 / 边界值

---

## 用例总览

| 编号 | 对应功能 | 类型 | 拟放置测试文件 / 函数名 |
|------|----------|------|--------------------------|
| TC-01 | F2 | 正例 | `test_p0_1_cache_convergence.py::test_endpoint_cache_identity` |
| TC-02 | F2 | 正例 | `test_p0_1_cache_convergence.py::test_provider_semaphores_identity` |
| TC-03 | F2 | 正例 | `test_p0_1_cache_convergence.py::test_provider_cache_identity` |
| TC-04 | F2 | 正例 | `test_p0_1_cache_convergence.py::test_reexport_symbols_are_same_objects` |
| TC-05 | F2 | 正例 | `test_p0_1_cache_convergence.py::test_patch_ensure_provider_semaphore_via_shared` |
| TC-06 | F2/F10 | 正例 | `test_p0_1_cache_convergence.py::test_clear_on_shared_object_propagates` |
| TC-07 | F4 | 正例 | `test_p0_1_cache_convergence.py::test_invalidate_endpoint_cache_pops` |
| TC-08 | F4 | 边界 | `test_p0_1_cache_convergence.py::test_invalidate_endpoint_cache_missing_no_error` |
| TC-09 | F4/F2 | 正例 | `test_p0_1_cache_convergence.py::test_invalidate_endpoint_cache_shared_via_either_module` |
| TC-10 | F5 | 正例 | `test_p0_1_cache_convergence.py::test_invalidate_provider_cache_none_clears_all` |
| TC-11 | F5 | 正例 | `test_p0_1_cache_convergence.py::test_invalidate_provider_cache_by_id` |
| TC-12 | F5 | 边界 | `test_p0_1_cache_convergence.py::test_invalidate_provider_cache_missing_id_no_error` |
| TC-13 | F5 | 边界 | `test_p0_1_cache_convergence.py::test_invalidate_provider_cache_same_provider_multiple_type_proxy` |
| TC-14 | F6 | 正例 | `test_p0_1_cache_convergence.py::test_invalidate_all_caches_clears_both` |
| TC-15 | F6/F7 | 边界 | `test_p0_1_cache_convergence.py::test_invalidate_all_caches_keeps_semaphores` |
| TC-16 | F7 | 边界 | `test_p0_1_cache_convergence.py::test_invalidate_provider_cache_keeps_semaphores` |
| TC-17 | F7 | 边界 | `test_p0_1_cache_convergence.py::test_invalidate_endpoint_cache_keeps_semaphores` |
| TC-18 | F7 | 边界 | `test_p0_1_cache_convergence.py::test_semaphore_identity_after_all_invalidate_calls` |
| TC-19 | F3/F10 | 正例 | `test_p0_1_cache_convergence.py::test_load_endpoints_writes_shared_cache_readable_by_router` |
| TC-20 | F3/F1 | 正例 | `test_p0_1_cache_convergence.py::test_shared_load_endpoints_is_router_function` |
| TC-21 | F8 | 正例 | `test_p0_1_admin_invalidation.py::test_add_model_to_group_invalidates_endpoint_cache` |
| TC-22 | F8 | 正例 | `test_p0_1_admin_invalidation.py::test_remove_model_from_group_invalidates_endpoint_cache` |
| TC-23 | F8 | 正例 | `test_p0_1_admin_invalidation.py::test_update_model_weight_invalidates_endpoint_cache` |
| TC-24 | F8 | 正例 | `test_p0_1_admin_invalidation.py::test_update_group_invalidates_endpoint_cache` |
| TC-25 | F8 | 正例 | `test_p0_1_admin_invalidation.py::test_delete_group_invalidates_endpoint_cache` |
| TC-26 | F9 | 正例 | `test_p0_1_admin_invalidation.py::test_update_provider_invalidates_all_caches` |
| TC-27 | F9 | 正例 | `test_p0_1_admin_invalidation.py::test_delete_provider_invalidates_all_caches` |
| TC-28 | F9 | 正例 | `test_p0_1_admin_invalidation.py::test_update_model_invalidates_all_caches` |
| TC-29 | F9 | 正例 | `test_p0_1_admin_invalidation.py::test_delete_model_invalidates_all_caches` |
| TC-30 | F8 | 反例 | `test_p0_1_admin_invalidation.py::test_update_group_404_does_not_invalidate` |
| TC-31 | F9 | 反例 | `test_p0_1_admin_invalidation.py::test_update_provider_404_does_not_invalidate` |
| TC-32 | F8/F9 | 反例 | `test_p0_1_admin_invalidation.py::test_get_group_does_not_invalidate` |
| TC-33 | F10 | 正例 | `test_p0_1_cache_convergence.py::test_existing_router_suite_contract`（回归契约） |
| TC-34 | F5 | 反例 | `test_p0_1_cache_convergence.py::test_invalidate_provider_cache_non_int_id_no_misdelete` |

---

## 一、对象同一性 & re-export（F1/F2/F3）

### TC-01 `_endpoint_cache` 对象同一性
- **类型**：正例
- **前置**：`botflow.router` 与 `botflow.pipeline._shared` 均已 import
- **步骤**：
  1. `import botflow.router as r; import botflow.pipeline._shared as s`
  2. `assert s._endpoint_cache is r._endpoint_cache`
- **期望**：断言通过（`_shared._endpoint_cache` 与 `router._endpoint_cache` 是同一 `dict` 对象）
- **文件/函数**：`test_p0_1_cache_convergence.py::test_endpoint_cache_identity`

### TC-02 `_provider_semaphores` 对象同一性
- **类型**：正例
- **前置**：同上
- **步骤**：`assert s._provider_semaphores is r._provider_semaphores`
- **期望**：断言通过（同一 `dict` 对象）
- **文件/函数**：`test_p0_1_cache_convergence.py::test_provider_semaphores_identity`

### TC-03 `_provider_cache` 对象同一性
- **类型**：正例
- **步骤**：`assert s._provider_cache is r._provider_cache`
- **期望**：断言通过
- **文件/函数**：`test_p0_1_cache_convergence.py::test_provider_cache_identity`

### TC-04 re-export 符号均为同一对象
- **类型**：正例
- **步骤**：逐一断言
  `s._get_cached_provider is r._get_cached_provider`、
  `s._ensure_provider_semaphore is r._ensure_provider_semaphore`、
  `s.load_endpoints is r.load_endpoints`、
  `s.invalidate_endpoint_cache is r.invalidate_endpoint_cache`、
  `s.invalidate_provider_cache is r.invalidate_provider_cache`、
  `s.invalidate_all_caches is r.invalidate_all_caches`、
  `s.PROVIDER_TYPE_MAP is r.PROVIDER_TYPE_MAP`、
  `s._ENDPOINT_CACHE_TTL == r._ENDPOINT_CACHE_TTL`、
  `s._PROVIDER_CACHE_TTL == r._PROVIDER_CACHE_TTL`
- **期望**：全部通过；常量值相等（60 / 300）
- **文件/函数**：`test_p0_1_cache_convergence.py::test_reexport_symbols_are_same_objects`

### TC-05 通过 `_shared` patch `_ensure_provider_semaphore` 生效
- **类型**：正例
- **前置**：`from unittest.mock import patch`
- **步骤**：
  1. 构造 `sample_endpoint`（带 mock provider，`provider_id=1`）
  2. `with patch("botflow.pipeline._shared._ensure_provider_semaphore", return_value=asyncio.Semaphore(2)), patch("botflow.pipeline._shared.get_config") as cfg: cfg.return_value=MagicMock(upstream_semaphore_size=0)`
  3. `await call_llm(sample_endpoint, messages=[], group_id=1, cooldown=CooldownManager())`
- **期望**：`call_llm` 成功返回且使用了被 patch 的信号量（不抛 `AttributeError`），证明 `_shared._ensure_provider_semaphore` 仍是 `_shared` 模块属性
- **文件/函数**：`test_p0_1_cache_convergence.py::test_patch_ensure_provider_semaphore_via_shared`

### TC-06 共享对象的 `.clear()` 跨模块传播
- **类型**：正例
- **步骤**：
  1. `from botflow.pipeline._shared import _endpoint_cache as s_ec`
  2. `s_ec[1] = (["fake"], time.time())`
  3. `from botflow.router import _endpoint_cache as r_ec; r_ec.clear()`
  4. `assert 1 not in s_ec` 且 `assert 1 not in r_ec`
- **期望**：`router` 侧 clear 清空了 `_shared` 注入的条目（同一对象）
- **文件/函数**：`test_p0_1_cache_convergence.py::test_clear_on_shared_object_propagates`

---

## 二、`invalidate_endpoint_cache`（F4）

### TC-07 正常 pop 单个 group
- **类型**：正例
- **前置**：`from botflow.router import _endpoint_cache, invalidate_endpoint_cache`
- **步骤**：
  1. `_endpoint_cache[42] = (["fake"], time.time())`
  2. `invalidate_endpoint_cache(42)`
  3. `assert 42 not in _endpoint_cache`
- **期望**：该 group 缓存被移除
- **文件/函数**：`test_p0_1_cache_convergence.py::test_invalidate_endpoint_cache_pops`

### TC-08 不存在的 group_id 不报错
- **类型**：边界
- **步骤**：`invalidate_endpoint_cache(99999)`（`_endpoint_cache` 为空）
- **期望**：不抛异常（等价于 `pop(default=None)`）
- **文件/函数**：`test_p0_1_cache_convergence.py::test_invalidate_endpoint_cache_missing_no_error`

### TC-09 经 `_shared` 或 `router` 调用作用于同一 dict
- **类型**：正例
- **前置**：`from botflow.pipeline._shared import invalidate_endpoint_cache as s_inv; from botflow.router import _endpoint_cache`
- **步骤**：
  1. `_endpoint_cache[7] = (["x"], time.time())`
  2. `s_inv(7)`
  3. `assert 7 not in _endpoint_cache`
- **期望**：通过 `_shared` 别名调用清掉的是 `router` 侧同一 `dict` 的条目
- **文件/函数**：`test_p0_1_cache_convergence.py::test_invalidate_endpoint_cache_shared_via_either_module`

---

## 三、`invalidate_provider_cache`（F5）

### TC-10 `provider_id=None` 清空整个 provider 缓存
- **类型**：正例
- **前置**：`from botflow.router import _provider_cache, invalidate_provider_cache`
- **步骤**：
  1. 注入 `_provider_cache[(1,"openai",""), (2,"openai","")]`
  2. `invalidate_provider_cache()`
  3. `assert _provider_cache == {}`
- **期望**：整个缓存清空
- **文件/函数**：`test_p0_1_cache_convergence.py::test_invalidate_provider_cache_none_clears_all`

### TC-11 按 provider_id 清掉仅属于该 provider 的条目
- **类型**：正例
- **步骤**：
  1. 注入 `{(1,"openai",""):v1, (1,"anthropic",""):v2, (2,"openai",""):v3}`
  2. `invalidate_provider_cache(1)`
  3. `assert (2,"openai","") in _provider_cache` 且 `(1,"openai","")`、`(1,"anthropic","")` 均不在
- **期望**：只清 `key[0]==1` 的条目，保留 provider 2
- **文件/函数**：`test_p0_1_cache_convergence.py::test_invalidate_provider_cache_by_id`

### TC-12 不存在的 provider_id 不报错且不动缓存
- **类型**：边界
- **步骤**：
  1. 注入 `{(5,"openai",""):v}`
  2. `invalidate_provider_cache(99999)`
  3. `assert (5,"openai","") in _provider_cache`
- **期望**：不抛异常，缓存不变
- **文件/函数**：`test_p0_1_cache_convergence.py::test_invalidate_provider_cache_missing_id_no_error`

### TC-13 同一 provider 多 type/proxy 组合全部被清
- **类型**：边界值
- **步骤**：
  1. 注入 `{(1,"openai",""):a, (1,"deepseek",""):b, (1,"openai","http://p"):c, (2,"openai",""):d}`
  2. `invalidate_provider_cache(1)`
  3. `assert all(k not in _provider_cache for k in [(1,"openai",""),(1,"deepseek",""),(1,"openai","http://p")])` 且 `(2,"openai","") in _provider_cache`
- **期望**：provider 1 的全部 (type,proxy) 变体被清，provider 2 保留
- **文件/函数**：`test_p0_1_cache_convergence.py::test_invalidate_provider_cache_same_provider_multiple_type_proxy`

---

## 四、`invalidate_all_caches` & 信号量保护（F6/F7）

### TC-14 `invalidate_all_caches` 清空 endpoint + provider 缓存
- **类型**：正例
- **前置**：`from botflow.router import _endpoint_cache, _provider_cache, invalidate_all_caches`
- **步骤**：
  1. 注入 `_endpoint_cache[1]` 与 `_provider_cache[(1,"openai","")]`
  2. `invalidate_all_caches()`
  3. `assert _endpoint_cache == {} and _provider_cache == {}`
- **期望**：两个缓存均空
- **文件/函数**：`test_p0_1_cache_convergence.py::test_invalidate_all_caches_clears_both`

### TC-15 `invalidate_all_caches` 不清信号量
- **类型**：边界（关键）
- **步骤**：
  1. `from botflow.router import _provider_semaphores, _ensure_provider_semaphore`
  2. `sem = _ensure_provider_semaphore(1, 5)`
  3. `invalidate_all_caches()`
  4. `assert _provider_semaphores == {1: sem}` 且 `_provider_semaphores[1] is sem`
- **期望**：信号量 dict 内容、对象身份均不变
- **文件/函数**：`test_p0_1_cache_convergence.py::test_invalidate_all_caches_keeps_semaphores`

### TC-16 `invalidate_provider_cache` 不清信号量
- **类型**：边界
- **步骤**：`sem=_ensure_provider_semaphore(1,5); invalidate_provider_cache(1); assert _provider_semaphores[1] is sem`
- **期望**：信号量保留
- **文件/函数**：`test_p0_1_cache_convergence.py::test_invalidate_provider_cache_keeps_semaphores`

### TC-17 `invalidate_endpoint_cache` 不清信号量
- **类型**：边界
- **步骤**：`sem=_ensure_provider_semaphore(1,5); invalidate_endpoint_cache(1); assert 1 in _provider_semaphores and _provider_semaphores[1] is sem`
- **期望**：信号量保留
- **文件/函数**：`test_p0_1_cache_convergence.py::test_invalidate_endpoint_cache_keeps_semaphores`

### TC-18 连续调用三个 invalidate 后信号量身份仍一致
- **类型**：边界
- **步骤**：
  1. `sem = _ensure_provider_semaphore(1, 5)`
  2. 依次 `invalidate_endpoint_cache(1)` → `invalidate_provider_cache(None)` → `invalidate_all_caches()`
  3. `assert _provider_semaphores[1] is sem`（三次后）
- **期望**：对象身份全程不变
- **文件/函数**：`test_p0_1_cache_convergence.py::test_semaphore_identity_after_all_invalidate_calls`

---

## 五、`load_endpoints` 跨路径共享（F3/F1/F10）

### TC-19 `load_endpoints` 写入的缓存 `router` 侧可读
- **类型**：正例（集成）
- **步骤**：
  1. 构造 `db`（mock `get_group_models` 返回 1 个 enabled model，`get_provider` 返回 enabled provider）
  2. `from botflow.pipeline._shared import load_endpoints; from botflow.router import _endpoint_cache, GroupRouter`
  3. `await load_endpoints(group_id=1, db=db)`
  4. 用同一 `db` 构造 `GroupRouter(1, db, CooldownManager())`，patch 其 `db.get_group_models` 计数
  5. `await router._load_endpoints()`；`assert db.get_group_models.call_count == 1`（仅 `load_endpoints` 调用过，router 命中 `_shared` 写入的同一缓存）
- **期望**：`GroupRouter._load_endpoints` 命中 `_shared.load_endpoints` 写入的共享缓存，不再查 DB
- **文件/函数**：`test_p0_1_cache_convergence.py::test_load_endpoints_writes_shared_cache_readable_by_router`

### TC-20 `_shared.load_endpoints` 是 `router` 模块函数
- **类型**：正例
- **步骤**：`from botflow.pipeline._shared import load_endpoints as s_le; from botflow.router import load_endpoints as r_le; assert s_le is r_le`
- **期望**：同一函数对象
- **文件/函数**：`test_p0_1_cache_convergence.py::test_shared_load_endpoints_is_router_function`

---

## 六、Admin 写接口失效接线（F8/F9）

> 测试手段：在 `test_p0_1_admin_invalidation.py` 中 monkeypatch `botflow.admin_api.get_db` 返回 `AsyncMock`，并 monkeypatch `botflow.admin_api.invalidate_endpoint_cache` / `invalidate_all_caches` 为 `MagicMock`，断言调用；404 分支让 raw 查询返回 `None`。

### TC-21 `add_model_to_group` 写成功 → invalidate_endpoint_cache(gid)
- **类型**：正例
- **前置**：patch `get_db` 返回 `db`（含 `get_group_raw`→group、`get_model_raw`→model、`add_model_to_group_raw`→None）；patch `invalidate_endpoint_cache` 为 MagicMock
- **步骤**：`await add_model_to_group(group_id=1, model_id=10, weight=1)`
- **期望**：`invalidate_endpoint_cache.assert_called_once_with(1)`
- **文件/函数**：`test_p0_1_admin_invalidation.py::test_add_model_to_group_invalidates_endpoint_cache`

### TC-22 `remove_model_from_group` 写成功 → invalidate_endpoint_cache(gid)
- **类型**：正例
- **步骤**：`await remove_model_from_group(group_id=2, model_id=20)`（db `remove_model_from_group_raw`→None）
- **期望**：`invalidate_endpoint_cache.assert_called_once_with(2)`
- **文件/函数**：`test_p0_1_admin_invalidation.py::test_remove_model_from_group_invalidates_endpoint_cache`

### TC-23 `update_model_weight` 写成功 → invalidate_endpoint_cache(gid)
- **类型**：正例
- **步骤**：`await update_model_weight(group_id=3, model_id=30, weight=5)`
- **期望**：`invalidate_endpoint_cache.assert_called_once_with(3)`
- **文件/函数**：`test_p0_1_admin_invalidation.py::test_update_model_weight_invalidates_endpoint_cache`

### TC-24 `update_group` 写成功 → invalidate_endpoint_cache(gid)
- **类型**：正例
- **步骤**：`get_group_raw`→group；`await update_group(group_id=4, name="new")`
- **期望**：`invalidate_endpoint_cache.assert_called_once_with(4)`
- **文件/函数**：`test_p0_1_admin_invalidation.py::test_update_group_invalidates_endpoint_cache`

### TC-25 `delete_group` 写成功 → invalidate_endpoint_cache(gid)
- **类型**：正例
- **步骤**：`delete_group_raw`→True；`await delete_group(group_id=5)`
- **期望**：`invalidate_endpoint_cache.assert_called_once_with(5)`
- **文件/函数**：`test_p0_1_admin_invalidation.py::test_delete_group_invalidates_endpoint_cache`

### TC-26 `update_provider` 写成功 → invalidate_all_caches()
- **类型**：正例
- **步骤**：`get_provider_raw`→provider；`await update_provider(provider_id=1, name="x")`
- **期望**：`invalidate_all_caches.assert_called_once_with()`
- **文件/函数**：`test_p0_1_admin_invalidation.py::test_update_provider_invalidates_all_caches`

### TC-27 `delete_provider` 写成功 → invalidate_all_caches()
- **类型**：正例
- **步骤**：`delete_provider_raw`→True；`await delete_provider(provider_id=1)`
- **期望**：`invalidate_all_caches.assert_called_once_with()`
- **文件/函数**：`test_p0_1_admin_invalidation.py::test_delete_provider_invalidates_all_caches`

### TC-28 `update_model` 写成功 → invalidate_all_caches()
- **类型**：正例
- **步骤**：`get_model_raw`→model；`await update_model(model_id=1, name="x")`
- **期望**：`invalidate_all_caches.assert_called_once_with()`
- **文件/函数**：`test_p0_1_admin_invalidation.py::test_update_model_invalidates_all_caches`

### TC-29 `delete_model` 写成功 → invalidate_all_caches()
- **类型**：正例
- **步骤**：`delete_model_raw`→True；`await delete_model(model_id=1)`
- **期望**：`invalidate_all_caches.assert_called_once_with()`
- **文件/函数**：`test_p0_1_admin_invalidation.py::test_delete_model_invalidates_all_caches`

### TC-30 `update_group` 找不到 group（404）不调用 invalidate（反例）
- **类型**：反例
- **步骤**：`get_group_raw`→None；断言 `pytest.raises(HTTPException)` 且 `invalidate_endpoint_cache` 调用次数为 0
- **期望**：抛 404 且 `invalidate_endpoint_cache` 未被调用（写未成功不失效）
- **文件/函数**：`test_p0_1_admin_invalidation.py::test_update_group_404_does_not_invalidate`

### TC-31 `update_provider` 找不到 provider（404）不调用 invalidate（反例）
- **类型**：反例
- **步骤**：`get_provider_raw`→None；`pytest.raises(HTTPException)` 且 `invalidate_all_caches` 调用次数 0
- **期望**：抛 404 且未失效
- **文件/函数**：`test_p0_1_admin_invalidation.py::test_update_provider_404_does_not_invalidate`

### TC-32 读接口（GET）不触发失效（反例）
- **类型**：反例
- **步骤**：调用 `get_group(group_id=1)`（读成功），断言 `invalidate_endpoint_cache` 与 `invalidate_all_caches` 均未被调用
- **期望**：读路径完全不动缓存
- **文件/函数**：`test_p0_1_admin_invalidation.py::test_get_group_does_not_invalidate`

---

## 七、回归契约（F10）

### TC-33 现有测试套件契约（对象同一性保证无污染）
- **类型**：正例
- **前置**：不修改 `test_router.py` / `test_router_full.py` / `test_pipeline_base.py`
- **步骤**（集成，手工/CI 执行，不在此文件落具体断言）：
  `PYTHONPATH=src python -m pytest tests/test_router.py tests/test_router_full.py tests/test_pipeline_base.py -m "not integration"`
- **期望**：全绿。重点验证：
  - `test_pipeline_base.py` 的 `_clear_caches` fixture（`_endpoint_cache.clear()` / `_provider_semaphores.clear()`）与 `test_router*.py` 的 `router._endpoint_cache.clear()` 作用于同一对象，互不残留污染。
  - `test_pipeline_base.py:544` 的 `patch("botflow.pipeline._shared._ensure_provider_semaphore")` 仍生效。
  - `test_router_full.py` 的 `monkeypatch.setattr(r, "_PROVIDER_CACHE_TTL", -1)` 仍命中单一事实源。
- **文件/函数**：`test_p0_1_cache_convergence.py::test_existing_router_suite_contract`（仅做轻量对象同一性 + `monkeypatch` 命中校验，作为单测可运行部分；完整套件绿为 CI 回归）

### TC-34 传非 int id 不误删（反例）
- **类型**：反例
- **前置**：`from botflow.router import _provider_cache, invalidate_provider_cache`
- **步骤**：
  1. 注入 `{(1,"openai",""): v}`（key[0] 为 int `1`）
  2. `invalidate_provider_cache("1")`（传入字符串）
  3. `assert (1,"openai","") in _provider_cache`
- **期望**：不抛异常，缓存不变。因运行时 key[0] 为 `int`，与字符串 `"1"` 不等，`key[0]=="1"` 为 False，故不误删（与 F5 设计说明一致；属懒人版，不引入强制类型校验）。
- **文件/函数**：`test_p0_1_cache_convergence.py::test_invalidate_provider_cache_non_int_id_no_misdelete`

---

## 覆盖率说明

- 新增函数（`invalidate_provider_cache`、`invalidate_all_caches`、迁移后的 `load_endpoints`/`invalidate_endpoint_cache`）均有对应 TC 覆盖正例/反例/边界值，目标 100%。其中 `invalidate_provider_cache` 覆盖：None 全清（TC-10）、按 id 删（TC-11）、不存在 id（TC-12）、同 provider 多 type/proxy（TC-13）、**非 int id 不误删（TC-34）**五类。
- `_shared` 的 re-export 别名本身（`from botflow.router import ...`）为平凡重绑定，无需单测逐行覆盖；其正确性由 TC-01~TC-06 的对象同一性 + patch 生效断言间接保证。
- 若个别异常分支（如 `get_db` 抛非预期异常）无法覆盖，在 `admin_api.py` 对应行标注 `# UNCOVERED: [原因]`。

---

## 落盘约定

- 新测试文件：`tests/test_p0_1_cache_convergence.py`、`tests/test_p0_1_admin_invalidation.py`
- 运行命令（验证子 agent 使用）：
  `PYTHONPATH=src python -m pytest tests/ --cov=botflow --cov-report=term -m "not integration"`
- 放行门槛：单元覆盖率 100%。
