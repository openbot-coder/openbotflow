# P0-1 功能点文档：运行态缓存/限流单一事实源收敛 + Admin 配置失效

> 阶段：P0（地基修复，阻塞 P3 流式迁移）
> 依赖：无（独立地基问题，需先于 P3）
> 设计原则：懒人工程师式「收敛删除」——不新建 cache.py 之类模块，单一事实源放在 `router.py`，`pipeline/_shared.py` 改为 re-export。

---

## 概述

旧 `GroupRouter`（流式）与新 `PipelineEngine` + 策略系统（非流式）各自保留了一份全局运行态缓存（`_provider_semaphores` / `_endpoint_cache` / `_provider_cache` 及 `load_endpoints` / `_get_cached_provider` / `_ensure_provider_semaphore`），导致：

1. **跨路径缓存/限流不共享**：同一 provider 的并发信号量被拆成两份 → `upstream_semaphore_size` 上限不可预期；endpoint/provider 缓存两条路径各记各的 → Admin 改配置只有一条路径生效。
2. **`invalidate_endpoint_cache` 从未被调用**：全仓 grep 只在定义处命中，`admin_api.py` 所有写操作（改 group、增删改 group 内 model、改 provider/model）都没有失效缓存 → Admin 改完配置后 60s（`_ENDPOINT_CACHE_TTL`）/ 300s（`_PROVIDER_CACHE_TTL`）内请求仍读旧配置。

本任务把全局状态收敛到 `router.py` 单一事实源，并补齐 Admin 写后失效能力。

**硬约束**（来自测试耦合，不可破坏）：

- `_shared._endpoint_cache` 必须与 `router._endpoint_cache` 是**同一个 dict 对象**；`_shared._provider_semaphores` 同理（`test_pipeline_base.py` 与 `test_router*.py` 各自 `from botflow.pipeline._shared import ...` / `from botflow.router import ...` 并 `.clear()`，两处 `.clear()` 必须作用于同一对象）。
- `_shared._ensure_provider_semaphore` 必须仍是 `_shared` 模块属性（`test_pipeline_base.py:544` 用 `patch("botflow.pipeline._shared._ensure_provider_semaphore", ...)`）。
- **禁止清空 `_provider_semaphores`**（信号量对象身份不能被打断，否则并发上限被重置绕过）。

---

## 功能点清单

### F1: 全局缓存/信号量/缓存相关可调用符号的单一事实源落在 `router.py`

**文件**：`src/botflow/router.py`（现有全局状态保留 + 新增）

**描述**：

`router.py` 继续持有以下模块级全局状态与函数（现状已存在，本任务仅作为单一事实源确认并补充缺失项）：

- `_provider_semaphores: dict[int, asyncio.Semaphore | None]`（L35）
- `_provider_cache`：类型注解为 `dict[tuple[int, str], tuple[BaseProvider, float]]`（L184，注解里 key 写成 2-tuple），但**运行时实际 key** 是 `(provider_id, resolved_type, proxy)` 三元组（见 `router.py:209` 的 `cache_key = (provider_id, resolved_type, proxy)`）。注解与运行时不匹配属历史遗留，本任务不改动类型注解；F5 的过滤逻辑以运行时三元组 `key[0]==provider_id` 为准。
- `_endpoint_cache: dict[int, tuple[list[ModelEndpoint], float]]`（L188）
- `_ENDPOINT_CACHE_TTL = 60`（L189）、`_PROVIDER_CACHE_TTL = 300`（L185）
- `PROVIDER_TYPE_MAP`（L173）
- `_ensure_provider_semaphore`（L38）、`_get_cached_provider`（L192）

本任务在 `router.py` **新增**：

- 模块级 `load_endpoints(group_id: int, db: Database) -> list[ModelEndpoint]`（从 `_shared.py` 迁移，逻辑 1:1 不变）。
- `invalidate_endpoint_cache(group_id: int)`（从 `_shared.py` 迁移，语义 1:1 不变：pop 单 group）。
- `invalidate_provider_cache(provider_id: int | None = None)`（新增）。
- `invalidate_all_caches()`（新增）。

`router.py` 不 import `pipeline._shared`，因此不存在循环依赖。

**验收标准**：

- `router.py` 中存在上述全部符号且可用。
- 全仓不存在 `router` 与 `_shared` 各定义一份同一语义全局对象的情况（grep 校验）。

**边界情况**：

- 正例：`from botflow.router import _endpoint_cache, _provider_semaphores, _provider_cache, load_endpoints, _get_cached_provider, _ensure_provider_semaphore, invalidate_endpoint_cache, invalidate_provider_cache, invalidate_all_caches, PROVIDER_TYPE_MAP, _ENDPOINT_CACHE_TTL, _PROVIDER_CACHE_TTL` 全部成功。
- 反例：若 `_shared.py` 仍保留自己的 `_endpoint_cache` 定义，则 F2 的对象同一性断言失败（禁止）。
- 边界：`load_endpoints` 在 `router.py` 中定义为模块函数（非 `GroupRouter` 方法），与 `GroupRouter._load_endpoints` 并存不冲突。

---

### F2: `pipeline/_shared.py` 删除重复定义，改为从 `router` re-export

**文件**：`src/botflow/pipeline/_shared.py`

**描述**：

`_shared.py` 删除以下自身的重复定义（现状 L36–L84、L87–L118、L144–L154、L286–L288）：

- `_provider_semaphores`、`_endpoint_cache`、`_ENDPOINT_CACHE_TTL`、`_provider_cache`、`_PROVIDER_CACHE_TTL`、`PROVIDER_TYPE_MAP`、`_get_cached_provider`、`_ensure_provider_semaphore`、`load_endpoints`、`invalidate_endpoint_cache`

改为从 `router` 引入（re-export），保持同名模块属性：

```python
from botflow.router import (
    _endpoint_cache,
    _provider_semaphores,
    _provider_cache,
    _ENDPOINT_CACHE_TTL,
    _PROVIDER_CACHE_TTL,
    PROVIDER_TYPE_MAP,
    _ensure_provider_semaphore,
    _get_cached_provider,
    load_endpoints,
    invalidate_endpoint_cache,
    invalidate_provider_cache,
    invalidate_all_caches,
)
```

**关键实现约束**：必须用 `from botflow.router import <name>`（绑定到同一对象），**不能**用 `import botflow.router as router` 后写成 `router._endpoint_cache` 的间接引用——否则 `test_pipeline_base.py` 的 `from botflow.pipeline._shared import _endpoint_cache` 拿到的不是 `dict` 本身，`is` 同一性断言失败。

**验收标准**：

```python
import botflow.pipeline._shared as s
import botflow.router as r
assert s._endpoint_cache is r._endpoint_cache
assert s._provider_semaphores is r._provider_semaphores
assert s._provider_cache is r._provider_cache
assert s._get_cached_provider is r._get_cached_provider
assert s._ensure_provider_semaphore is r._ensure_provider_semaphore
assert s.load_endpoints is r.load_endpoints
assert s.invalidate_endpoint_cache is r.invalidate_endpoint_cache
assert s.invalidate_provider_cache is r.invalidate_provider_cache
assert s.invalidate_all_caches is r.invalidate_all_caches
assert s.PROVIDER_TYPE_MAP is r.PROVIDER_TYPE_MAP
assert s._ENDPOINT_CACHE_TTL == r._ENDPOINT_CACHE_TTL
assert s._PROVIDER_CACHE_TTL == r._PROVIDER_CACHE_TTL
```

**边界情况**：

- 正例：现有 `test_pipeline_base.py:21-32` 的所有 `from botflow.pipeline._shared import (... _endpoint_cache, _provider_semaphores, _ENDPOINT_CACHE_TTL, _ensure_provider_semaphore, load_endpoints, invalidate_endpoint_cache, ...)` 导入仍可用，无需改动测试。
- 反例：若误用 `import botflow.router as _rt` 并在 `_shared` 内以 `_rt._endpoint_cache` 方式引用，则 `s._endpoint_cache` 不是模块属性、re-export 失效、`is` 断言失败（禁止）。
- 边界：`_ensure_provider_semaphore` 作为 `_shared` 模块属性存在，`test_pipeline_base.py:544` 的 `patch("botflow.pipeline._shared._ensure_provider_semaphore", return_value=sem)` 仍生效。

---

### F3: `load_endpoints` 迁移到 `router` 并 re-export（行为不变）

**文件**：`src/botflow/router.py`（新增）、`src/botflow/pipeline/_shared.py`（删除原函数、re-export）

**描述**：

将现状 `pipeline/_shared.py:87` 的 `load_endpoints` 迁移为 `router.py` 模块级函数，逻辑 1:1（查 `_endpoint_cache` → 命中且未过期返回 → 否则 `db.get_group_models(enabled_only=True)` + `db.get_provider` 重建 endpoint → 写入 `_endpoint_cache`）。迁移后用 `_shared.load_endpoints` 调用的是同一函数对象（`F2` 同一性）。

**验收标准**：

- `_shared.load_endpoints is router.load_endpoints`。
- 行为等价：现有 `test_pipeline_base.py` TC-14~TC-18（正常加载 / 缓存命中 / 缓存过期 / 空 / provider disabled 跳过）继续通过。

**边界情况**：

- 正例：命中共享 `_endpoint_cache`（`_shared` 注入的缓存 `router` 侧也可见）。
- 边界：`_ENDPOINT_CACHE_TTL` 过期（注入 `time.time()-61`）触发重载。
- 反例：provider `is_enabled=False` 时该 model 被跳过返回空。

---

### F4: `invalidate_endpoint_cache(group_id)` 保持 pop 单 group 语义，迁移到 `router`

**文件**：`src/botflow/router.py`（新增）、`src/botflow/pipeline/_shared.py`（删除原函数、re-export）

**描述**：

```python
def invalidate_endpoint_cache(group_id: int) -> None:
    """Admin 修改 group_models 后调用，清除缓存。"""
    _endpoint_cache.pop(group_id, None)
```

语义与现状 `pipeline/_shared.py:286` 完全一致（pop 单 key，缺失不报错）。迁移后 `router` 与 `_shared` 是同一函数对象。

**验收标准**：

- `router.invalidate_endpoint_cache is _shared.invalidate_endpoint_cache`。
- 调用后 `_endpoint_cache` 中该 `group_id` 被移除；缺失 key 不抛异常。

**边界情况**：

- 正例：`group_id` 存在 → pop 成功，`group_id not in _endpoint_cache`。
- 边界：`group_id` 不存在（如 `99999`）→ `pop(default=None)` 不抛异常。
- 正例（跨路径同一性）：`from botflow.pipeline._shared import invalidate_endpoint_cache` 调用等价于 `from botflow.router import invalidate_endpoint_cache` 调用，作用于同一 `dict`。

---

### F5: `invalidate_provider_cache(provider_id: int | None = None)` 新增于 `router`

**文件**：`src/botflow/router.py`

**描述**：

```python
def invalidate_provider_cache(provider_id: int | None = None) -> None:
    """清除 provider 实例缓存。

    provider_id=None 时清空整个 _provider_cache；否则只清掉
    key[0] == provider_id 的所有条目（同 provider 跨 type/proxy 的所有实例）。
    """
    if provider_id is None:
        _provider_cache.clear()
        return
    for key in list(_provider_cache.keys()):
        if key[0] == provider_id:
            _provider_cache.pop(key, None)
```

`_provider_cache` 的 key 为 `(provider_id, resolved_type, proxy)`（见 `router.py:209`），故 `key[0]` 是 provider_id。

**验收标准**：

- `provider_id=None` → 整个 `_provider_cache` 被清空。
- `provider_id` 给定 → 只清 `key[0]==provider_id` 的条目，其他 provider 的条目保留。

**边界情况**：

- 正例（None 分支）：`_provider_cache` 非空，`invalidate_provider_cache()` → `_provider_cache == {}`。
- 正例（指定 id）：`_provider_cache` 含 `{(1,"openai",""):..., (1,"anthropic",""):..., (2,"openai",""):...}`，`invalidate_provider_cache(1)` 后仅剩 `(2,"openai","")`。
- 边界（不存在的 id）：`invalidate_provider_cache(99999)` → 不报错，缓存不变。
- 边界（同 provider 多 type/proxy）：`(1,"openai","")`、`(1,"deepseek","")`、`(1,"openai","http://p")` 三种组合在 `invalidate_provider_cache(1)` 后全部被清（key[0] 均为 1）。
- 反例/防御：传非 int（如 `provider_id="1"`）→ `key[0]=="1"` 与 `int` 不等，天然不误删；不作强制类型校验（属懒人版，符合「不引入可避免的校验」）。

---

### F6: `invalidate_all_caches()` 新增于 `router`：清空 endpoint+provider 缓存，不清信号量

**文件**：`src/botflow/router.py`

**描述**：

```python
def invalidate_all_caches() -> None:
    """Admin 改动 provider/model（波及多 group）后调用。

    清空 _endpoint_cache 与 _provider_cache，但**不**清空 _provider_semaphores
    （信号量对象身份不能打断，否则并发上限被绕过）。
    """
    _endpoint_cache.clear()
    _provider_cache.clear()
```

**验收标准**：

- 调用后 `_endpoint_cache == {}` 且 `_provider_cache == {}`。
- 调用后 `_provider_semaphores` 内容不变（见 F7）。

**边界情况**：

- 正例：`_endpoint_cache` 与 `_provider_cache` 均被清空。
- 关键边界：`_provider_semaphores` 在调用前后对象身份与内容完全一致（信号量绝不被清）。

---

### F7: 三大 invalidate 永不触碰 `_provider_semaphores`（信号量身份保护）

**文件**：`src/botflow/router.py`

**描述**：

`invalidate_endpoint_cache` / `invalidate_provider_cache` / `invalidate_all_caches` 三个函数只操作 `_endpoint_cache` 与 `_provider_cache`，**绝不** `.clear()` 或重赋值 `_provider_semaphores`。

理由：信号量控制「同一 provider 跨 group」的并发上限；若被清空，`_ensure_provider_semaphore` 会在下次调用时新建 `asyncio.Semaphore`，已建立的并发计数被重置 → 限流失效。

**验收标准**：

- 三个 invalidate 执行后，`router._provider_semaphores` 的 `id()` 与内容（含已创建的 `Semaphore` 实例）与前一致。
- 已通过 `_ensure_provider_semaphore(1, 5)` 创建的 `Semaphore`，在任意 invalidate 调用后 `is` 同一对象、计数不被重置。

**边界情况**：

- 正例：先 `_ensure_provider_semaphore(1,5)` 拿到 `sem`，再依次调用三个 invalidate，`router._provider_semaphores[1] is sem`。
- 边界：高并发下信号量计数非零时调用 invalidate，计数不应归零。

---

### F8: Admin group/model 写接口 → `invalidate_endpoint_cache(gid)`

**文件**：`src/botflow/admin_api.py`

**描述**：

`admin_api.py` 在顶部 `from botflow.router import invalidate_endpoint_cache, invalidate_all_caches`。在以下写操作 **DB 写成功后** 调用 `invalidate_endpoint_cache(gid)`：

| 路由 | 函数 | 操作 | gid 来源 |
|------|------|------|----------|
| `POST /groups/{group_id}/models` | `add_model_to_group` | 组内 model 增 | `group_id` |
| `DELETE /groups/{group_id}/models/{model_id}` | `remove_model_from_group` | 组内 model 删 | `group_id` |
| `PATCH /groups/{group_id}/models/{model_id}` | `update_model_weight` | 组内 model 改权重 | `group_id` |
| `PATCH /groups/{group_id}` | `update_group` | 改 group 元信息 | `group_id` |
| `DELETE /groups/{group_id}` | `delete_group` | 删 group | `group_id` |

调用时机：**仅在 404 guard 之后、DB raw 写方法成功返回的位置**调用，写未成功绝不可失效。

- `update_group`（PATCH `/groups/{gid}`）：先 `get_group_raw(gid)` 判空，空则抛 `HTTPException(404)`；guard 之后才 `update_group_raw(...)` 并调用 `invalidate_endpoint_cache(gid)`。
- `add_model_to_group` / `remove_model_from_group` / `update_model_weight`（POST/DELETE/PATCH `/groups/{gid}/models/...`）：先各自 `get_group_raw` / `get_model_raw` 判空抛 404；写成功后调用 `invalidate_endpoint_cache(gid)`。
- `delete_group`（DELETE `/groups/{gid}`）：**无前置 `get_*_raw` 判空**，直接 `delete_group_raw(gid)` 据返回值（True/False）判空后决定是否抛 404；`invalidate_endpoint_cache(gid)` 必须置于该返回值判断、确认删除成功后调用。

**硬约束**：`invalidate_endpoint_cache(gid)` 的调用语句必须位于上述 404 guard **之后**（写成功分支内），禁止插到 guard 之前或守卫分支中，否则「写未成功却失效缓存」会误清有效缓存。

**验收标准**：

- 上述 5 个写接口成功路径各调用 `invalidate_endpoint_cache(gid)` 恰好一次。
- 404 / 异常分支**不**调用。

**边界情况**：

- 正例：group 存在，写成功 → `invalidate_endpoint_cache(gid)` 被调用。
- 反例（关键）：`update_group` 中 `get_group_raw(gid)` 返回 None → 抛 `HTTPException(404)`，**不**调用 `invalidate_endpoint_cache`。
- 边界：`delete_group` 写成功（即便 gid 此前无缓存） → `invalidate_endpoint_cache(gid)` 调用且不报错。
- **已确认的有意省略**：`POST /groups`（create_group）不接入失效。理由：新建 group 时 endpoint 缓存为空，`invalidate_endpoint_cache(gid)` 是 no-op（pop 不存在的 key 不报错、无副作用）；且 group 尚无任何 model，无缓存可失效。加入 model 是独立的 `POST /groups/{gid}/models` 调用，会触发本表首行的 `invalidate_endpoint_cache(gid)`。

---

### F9: Admin provider/model 写接口 → `invalidate_all_caches()`

**文件**：`src/botflow/admin_api.py`

**描述**：

在以下写操作 **DB 写成功后** 调用 `invalidate_all_caches()`（provider/model 变更会波及多个 group 的 endpoint，用最懒且正确的「全清」）：

| 路由 | 函数 | 操作 |
|------|------|------|
| `PATCH /providers/{provider_id}` | `update_provider` | 改 provider |
| `DELETE /providers/{provider_id}` | `delete_provider` | 删 provider |
| `PATCH /models/{model_id}` | `update_model` | 改 model |
| `DELETE /models/{model_id}` | `delete_model` | 删 model |

调用时机：**仅在 404 guard 之后、DB raw 写方法成功返回的位置**调用，写未成功绝不可失效。

- `update_provider`（PATCH `/providers/{pid}`）：先 `get_provider_raw(pid)` 判空，空则抛 `HTTPException(404)`；guard 之后才 `update_provider_raw(...)` 并调用 `invalidate_all_caches()`。
- `update_model`（PATCH `/models/{mid}`）：先 `get_model_raw(mid)` 判空抛 404；写成功后调用 `invalidate_all_caches()`。
- `delete_provider`（DELETE `/providers/{pid}`）/** `delete_model`（DELETE `/models/{mid}`）：**无前置 `get_*_raw` 判空**，直接 `delete_*_raw(...)` 据返回值（True/False）判空后决定是否抛 404；`invalidate_all_caches()` 必须置于返回值判断、确认删除成功后调用。

**硬约束**：`invalidate_all_caches()` 的调用语句必须位于上述 404 guard **之后**，禁止插到 guard 之前或守卫分支中。

**验收标准**：

- 上述 4 个写接口成功路径各调用 `invalidate_all_caches()` 恰好一次。
- 404 / 异常分支**不**调用。

**边界情况**：

- 正例：provider/model 存在，写成功 → `invalidate_all_caches()` 被调用，两个缓存均清空、`_provider_semaphores` 不变。
- 反例（关键）：`update_provider` 中 `get_provider_raw(pid)` 返回 None → 抛 `HTTPException(404)`，**不**调用 `invalidate_all_caches`。
- **已确认的有意省略**：`POST /providers`(create_provider) 与 `POST /models`(create_model) 不接入失效。理由：新建 provider/model 实体尚未被任何 group 引用（没有被使用的缓存条目），无缓存可失效；该 provider/model 真正进入某个 group 的 endpoint 缓存，是后续的「加入 group」调用（`POST /groups/{gid}/models`）触发，彼时会按需命中或重建缓存。故 create 阶段无需、也不应调用 `invalidate_all_caches()`。

---

### F10: 回归——现有 `test_router*.py` / `test_pipeline_base.py` 全绿

**文件**：`tests/test_router.py`、`tests/test_router_full.py`、`tests/test_pipeline_base.py`

**描述**：

因 `_shared` 与 `router` 的缓存对象同一（F2），两套测试各自的 `.clear()` fixture 作用于同一对象，互不污染；所有既有断言（缓存命中/过期、`_get_cached_provider` 缓存、`call_llm` 限流、patch `_ensure_provider_semaphore` 等）应继续通过。

**验收标准**：

- `PYTHONPATH=src python -m pytest tests/test_router.py tests/test_router_full.py tests/test_pipeline_base.py -m "not integration"` 全绿。
- 新增 `tests/test_p0_1_cache_convergence.py`、`tests/test_p0_1_admin_invalidation.py` 也全绿（见 `P0-1_tests.md`）。

**边界情况**：

- 正例：先 `from botflow.pipeline._shared import _endpoint_cache` 注入 `_endpoint_cache[1]=...`，再 `from botflow.router import _endpoint_cache as r_ec; r_ec.clear()`，随后 `_shared` 侧 `1 not in _endpoint_cache`（同一对象，共享清空心智）。
- 反例预警：若 F2 误用 `import botflow.router as _rt` 间接引用，则 `test_pipeline_base.py` 的 `_endpoint_cache.clear()` 只清 `_shared` 本地别名而 `router` 侧残留 → 测试互相污染、回归失败。

---

## 文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `src/botflow/router.py` | **修改** | 新增 `load_endpoints`、`invalidate_endpoint_cache`、`invalidate_provider_cache`、`invalidate_all_caches`；保留既有全局状态为单一事实源 |
| `src/botflow/pipeline/_shared.py` | **修改** | 删除重复的全局变量与函数定义，改为从 `router` re-export |
| `src/botflow/admin_api.py` | **修改** | 写接口成功后接入 `invalidate_endpoint_cache` / `invalidate_all_caches` |
| `tests/test_p0_1_cache_convergence.py` | **新建** | 对象同一性 + 3 个 invalidate 函数行为（见 `P0-1_tests.md`） |
| `tests/test_p0_1_admin_invalidation.py` | **新建** | Admin 各写接口失效接线（见 `P0-1_tests.md`） |

**不修改的文件**：

- `src/botflow/router.py` 中 `GroupRouter` 类及 `_load_endpoints` 方法（保留，流式路径仍用）。
- `src/botflow/pipeline/base.py`、`strategies.py`、`engine.py`（策略层不变）。
- `tests/test_router.py`、`test_router_full.py`、`test_pipeline_base.py`（仅因对象同一性而回归通过，无需改）。

---

## 向后兼容

| 场景 | 行为 |
|------|------|
| `from botflow.pipeline._shared import _endpoint_cache, _provider_semaphores, _ensure_provider_semaphore, load_endpoints, invalidate_endpoint_cache, _ENDPOINT_CACHE_TTL, PROVIDER_TYPE_MAP` | 仍可用（re-export），值与 `router` 侧同一对象 |
| 流式路径（`GroupRouter`） | 用 `router._endpoint_cache` / `router._provider_semaphores` 共享对象 |
| 非流式路径（`PipelineEngine` → `load_endpoints`） | 用同一 `_endpoint_cache` / `_provider_semaphores` |
| Admin 改配置 | 写成功后即时失效，60s/300s 陈旧窗口消除 |

---

## 与侦察结论不符 / 已确认的有意省略

1. **「增改删」与端点列举不一致（F9，已拍板）**：任务文字写 provider/model「增改删」，但显式列出的失效端点只有 `PATCH`/`DELETE`。**决策已拍板**：`POST /providers`(create_provider) 与 `POST /models`(create_model) 为**有意省略**——新建实体尚未被任何 group 引用，无缓存可失效；真正进入缓存是后续「加入 group」调用。故本方案只覆盖 PATCH/DELETE。
2. **`POST /groups`(create_group) 未列入（F8，已拍板）**：**有意省略**——新建 group 时 endpoint 缓存为空，`invalidate_endpoint_cache(gid)` 是 no-op；group 尚无 model，无缓存可失效。加入 model 由独立 `POST /groups/{gid}/models` 触发失效。
3. **`router.py` 当前无 `invalidate_endpoint_cache`**：现状该函数在 `_shared.py:286`，任务要求「保持现有语义」——本方案将其作为单一事实源**迁移**到 `router.py`，`_shared` 改为 re-export（与「值来自 router」一致）。
4. **`load_endpoints` 现状只在 `_shared.py`**：任务写「值来自 router」隐含其应迁移；本方案将 `load_endpoints` 迁移为 `router.py` 模块级函数，`_shared` re-export，确保非流式 `load_endpoints` 写入的缓存与流式 `GroupRouter._load_endpoints` 是同一 `dict`。
