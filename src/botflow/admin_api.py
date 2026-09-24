"""REST management API for botflow LLM Proxy.

Replaces the old MCP-based management tools with plain HTTP endpoints guarded
by the admin key (BOTFLOW_ADMIN_KEY). Each route maps 1:1 to a former MCP tool.
"""

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from botflow.auth import (
    SESSION_TTL_SECONDS,
    SETUP_TOKEN_KEY,
    _extract_token,
    create_session,
    hash_password,
    session_config_key,
    verify_admin_key,
    verify_password,
)
from botflow.config import get_config
from botflow.router import invalidate_endpoint_cache, invalidate_all_caches
from botflow.storage.db import get_db
from botflow.storage.models import ApiKey

admin_router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Request body models (replacing raw query params for create endpoints)
#
# Create endpoints declare ``Body(embed=True)`` so the payload stays wrapped as
# ``{"req": {...}}``; that is the shape the shipped admin SPA sends, so keeping
# it avoids a breaking change. PATCH never had a working body path at all
# (its payload was silently ignored), so it uses the natural flat object.
# ---------------------------------------------------------------------------

class CreateProviderReq(BaseModel):
    name: str
    base_url: str
    api_key: str = ""
    type: str = "openai"
    is_enabled: bool = True

class CreateModelReq(BaseModel):
    provider_id: int
    name: str
    type: str = "openai"
    context_window: int = 0
    api_format: str = ""
    is_enabled: bool = True

class CreateGroupReq(BaseModel):
    name: str
    description: str = ""
    is_enabled: bool = True
    fallback_group_id: Optional[int] = None
    type: str = "random_weights"
    params: Optional[dict] = None

class CreateApiKeyReq(BaseModel):
    raw_key: str
    label: str = ""


# Update bodies. Every field is optional: a PATCH only touches the keys the
# client actually sent (``None`` means "leave as is"). They must be explicit
# Pydantic models — bare scalar parameters are classified as *query* params by
# FastAPI, so a client sending a JSON body would have it ignored (the request
# then returns 200 without changing anything).
class UpdateProviderReq(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    type: Optional[str] = None
    is_enabled: Optional[bool] = None

class UpdateModelReq(BaseModel):
    name: Optional[str] = None
    context_window: Optional[int] = None
    display_name: Optional[str] = None
    api_format: Optional[str] = None
    max_retries: Optional[int] = None
    cooldown_seconds: Optional[int] = None
    cooldown_failure_threshold: Optional[int] = None
    is_enabled: Optional[bool] = None

class UpdateGroupReq(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_enabled: Optional[bool] = None
    fallback_group_id: Optional[int] = None
    type: Optional[str] = None
    params: Optional[dict] = None

class UpdateApiKeyReq(BaseModel):
    is_enabled: bool


# ---------------------------------------------------------------------------
# Auth（账号开通 / 登录 / 注销）
#
# setup 与 login 共用一个 body 模型：字段和校验完全一样，setup 只是多一个可选
# token（引导期唯一的准入凭据）。校验放在模型层是刻意的 —— pbkdf2 在端点里才跑，
# 422 必须拦在它之前，超长密码才不会进入哈希计算（P1-6）。
# ---------------------------------------------------------------------------
class AuthReq(BaseModel):
    # 缺省空串：token 缺失与 token 错误统一走端点内的 401 "Invalid setup token."，
    # 不让"没带 token"以 422 的形式漏出不同的失败形态。
    token: str = ""
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=8, max_length=128)

    @field_validator("username", mode="before")
    @classmethod
    def _strip_username(cls, v):
        # 先 strip 再过 Field 约束：" admin " 与 "admin" 必须是同一个账号，
        # 纯空格按 min_length=1 判 422。
        return v.strip() if isinstance(v, str) else v


class LogoutReq(BaseModel):
    token: str = ""


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------


@admin_router.get("/auth/status")
async def auth_status():
    """账号开通状态。免鉴权：面板冷启动时还没有任何会话，要靠它决定显示开通
    表单还是登录表单。只回 configured / username，绝不回 pwd_hash 或 token。"""
    db = get_db()
    raw = await db.get_config("admin_account")
    username = ""
    if raw:
        try:
            username = str(json.loads(raw).get("username", ""))
        except (ValueError, AttributeError):
            username = ""  # 损坏行不修数据：仍报已开通，登录时自然 401
    return {"success": True, "configured": bool(raw), "username": username}


@admin_router.post("/auth/setup")
async def auth_setup(req: AuthReq):
    """开通 / 重置管理账号。不挂 verify_admin_key —— 准入凭据只有 token 字段
    （必须等于启动时自动生成的 setup token，KV 中只存它的 sha256），错一律 401。

    BOTFLOW_ADMIN_KEY 已退出 setup 流程（它只守 Bearer 通道），所以这里
    **不再存在 500 分支**：无 KV 记录也回 401，与 token 错误同文案。
    """
    if not req.token:
        # 空/缺失 token 短路（AuthReq 缺省 "" 与显式空串同路径），不进哈希比对。
        raise HTTPException(status_code=401, detail="Invalid setup token.")
    db = get_db()
    raw = await db.get_config(SETUP_TOKEN_KEY)
    if not raw:
        # 无 KV 记录（未走启动钩子 / 已开通双删）→ 401；原「admin key 未配置 → 500」废除。
        raise HTTPException(status_code=401, detail="Invalid setup token.")
    try:
        stored_hash = str(json.loads(raw).get("hash", ""))
    except (ValueError, AttributeError, TypeError):
        stored_hash = ""  # KV 行损坏 → 比对必不等 → 401，不当场修数据
    # 先哈希再比 hex：任意字节都可哈希，非 ASCII token 天然 401 而非 TypeError 500；
    # compare_digest 常数时间比对，不因耗时泄漏。
    if not secrets.compare_digest(
        hashlib.sha256(req.token.encode("utf-8")).hexdigest().encode(),
        stored_hash.encode("utf-8"),
    ):
        raise HTTPException(status_code=401, detail="Invalid setup token.")
    await db.set_config(
        "admin_account",
        json.dumps({"username": req.username, "pwd_hash": hash_password(req.password)}),
    )
    # 重置即全量吊销：cleanup_config_by_prefix 按 updated_at 只删过期行、删不干净
    # （R3），这里必须 execute_write + LIKE 字面量把所有会话一次清空。
    # P0-2：LIKE 模式必须带 % —— 不带通配符删 0 行，重置后旧会话仍能进门。
    await db.execute_write("DELETE FROM config WHERE key LIKE 'admin_sess:%'")
    # 成功双删（用毕即焚）：只在 200 路径执行；401 失败路径绝不清理（防自锁）。
    await db.execute_write("DELETE FROM config WHERE key = ?", (SETUP_TOKEN_KEY,))
    try:
        get_config().setup_token_path.unlink()  # FileNotFoundError 幂等吞掉（T4.4）
    except FileNotFoundError:
        pass
    return {"success": True}


@admin_router.post("/auth/login")
async def auth_login(req: AuthReq):
    """账号密码登录，换取会话 token。所有失败路径（账号不存在 / 用户名不符 /
    密码错）共用一条 401 文案，响应上不可区分，不泄露账号是否存在。"""
    db = get_db()
    raw = await db.get_config("admin_account")
    account = None
    if raw:
        try:
            account = json.loads(raw)
        except ValueError:
            account = None
    pwd_ok = False
    user_ok = False
    if isinstance(account, dict):
        # 两个检查都无条件执行：密码错也照样跑完 pbkdf2，不靠耗时区分失败原因。
        pwd_ok = verify_password(req.password, str(account.get("pwd_hash", "")))
        user_ok = secrets.compare_digest(
            req.username.encode("utf-8"), str(account.get("username", "")).encode("utf-8")
        )
    if not (pwd_ok and user_ok):
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    # 成功路径：先清过期会话再签发。P0-2 —— 前缀必须带 %，否则删 0 行、过期行无限堆积。
    await db.cleanup_config_by_prefix("admin_sess:%", SESSION_TTL_SECONDS)
    token = await create_session(db, str(account["username"]))
    return {"success": True, "token": token}


@admin_router.post("/auth/logout")
async def auth_logout(
    authorization: Optional[str] = Header(default=None),
    req: Optional[LogoutReq] = None,
):
    """注销会话（甲案）：带任意非空 token 即 200，按 key 直删、不解析有效性。

    已注销与伪造 token 删库后状态相同，先 resolve 再删会让重复登出从 200 变 401
    （幂等换鉴别力，选幂等）；token 是否有效本来就由后续请求的 401 兜底。
    """
    token = _extract_token(authorization)
    if not token and req is not None:
        token = req.token  # 允许 body 带 token：Authorization 头不是唯一发法
    if not token:
        raise HTTPException(status_code=401, detail="Missing session token.")
    await get_db().execute_write(
        "DELETE FROM config WHERE key = ?", (session_config_key(token),)
    )
    return {"success": True}


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


@admin_router.post("/providers")
async def create_provider(
    req: CreateProviderReq = Body(embed=True),
    _=Depends(verify_admin_key),
):
    db = get_db()
    pid = await db.create_provider_raw(
        name=req.name, type=req.type, base_url=req.base_url,
        api_key=req.api_key, is_enabled=req.is_enabled,
    )
    return {"success": True, "provider_id": pid, "name": req.name}


@admin_router.get("/providers")
async def list_providers(_=Depends(verify_admin_key)):
    db = get_db()
    providers = await db.list_providers_raw()
    return {"success": True, "providers": [p.model_dump() for p in providers]}


@admin_router.get("/providers/{provider_id}")
async def get_provider(provider_id: int, _=Depends(verify_admin_key)):
    db = get_db()
    provider = await db.get_provider_raw(provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail={"success": False, "error": f"Provider {provider_id} not found"})
    return {"success": True, "provider": provider.model_dump()}


@admin_router.patch("/providers/{provider_id}")
async def update_provider(
    provider_id: int,
    req: UpdateProviderReq,
    _=Depends(verify_admin_key),
):
    db = get_db()
    provider = await db.get_provider_raw(provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail={"success": False, "error": f"Provider {provider_id} not found"})
    await db.update_provider_raw(
        provider_id,
        name=req.name if req.name is not None else provider.name,
        base_url=req.base_url if req.base_url is not None else provider.base_url,
        api_key=req.api_key if req.api_key is not None else provider.api_key,
        type=req.type if req.type is not None else provider.provider_type,
        is_enabled=req.is_enabled if req.is_enabled is not None else provider.is_enabled,
    )
    invalidate_all_caches()
    return {"success": True, "provider_id": provider_id}


@admin_router.delete("/providers/{provider_id}")
async def delete_provider(provider_id: int, _=Depends(verify_admin_key)):
    db = get_db()
    # on-delete cascade handles models referencing this provider.
    ok = await db.delete_provider_raw(provider_id)
    if not ok:
        raise HTTPException(status_code=404, detail={"success": False, "error": f"Provider {provider_id} not found"})
    invalidate_all_caches()
    return {"success": True, "provider_id": provider_id}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@admin_router.post("/models")
async def create_model(
    req: CreateModelReq = Body(embed=True),
    _=Depends(verify_admin_key),
):
    db = get_db()
    provider = await db.get_provider_raw(req.provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail={"success": False, "error": f"Provider {req.provider_id} not found"})
    mid = await db.create_model_raw(
        provider_id=req.provider_id, name=req.name,
        context_window=req.context_window, api_format=req.api_format, is_enabled=req.is_enabled,
    )
    return {"success": True, "model_id": mid, "name": req.name}


@admin_router.get("/models")
async def list_models(
    provider_id: Optional[int] = Query(default=None),
    enabled_only: bool = False,
    _=Depends(verify_admin_key),
):
    db = get_db()
    models = await db.list_models_raw(provider_id=provider_id, enabled_only=enabled_only)
    return {"success": True, "models": [m.model_dump() for m in models]}


@admin_router.get("/models/{model_id}")
async def get_model(model_id: int, _=Depends(verify_admin_key)):
    db = get_db()
    model = await db.get_model_raw(model_id)
    if not model:
        raise HTTPException(status_code=404, detail={"success": False, "error": f"Model {model_id} not found"})
    return {"success": True, "model": model.model_dump()}


@admin_router.patch("/models/{model_id}")
async def update_model(
    model_id: int,
    req: UpdateModelReq,
    _=Depends(verify_admin_key),
):
    db = get_db()
    model = await db.get_model_raw(model_id)
    if not model:
        raise HTTPException(status_code=404, detail={"success": False, "error": f"Model {model_id} not found"})
    await db.update_model_raw(
        model_id,
        name=req.name if req.name is not None else model.name,
        context_window=req.context_window if req.context_window is not None else model.context_window,
        display_name=req.display_name if req.display_name is not None else model.display_name,
        api_format=req.api_format if req.api_format is not None else model.api_format,
        max_retries=req.max_retries if req.max_retries is not None else model.max_retries,
        cooldown_seconds=req.cooldown_seconds if req.cooldown_seconds is not None else model.cooldown_seconds,
        cooldown_failure_threshold=req.cooldown_failure_threshold if req.cooldown_failure_threshold is not None else model.cooldown_failure_threshold,
        is_enabled=req.is_enabled if req.is_enabled is not None else model.is_enabled,
    )
    invalidate_all_caches()
    return {"success": True, "model_id": model_id}


@admin_router.delete("/models/{model_id}")
async def delete_model(model_id: int, _=Depends(verify_admin_key)):
    db = get_db()
    ok = await db.delete_model_raw(model_id)
    if not ok:
        raise HTTPException(status_code=404, detail={"success": False, "error": f"Model {model_id} not found"})
    invalidate_all_caches()
    return {"success": True, "model_id": model_id}


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


@admin_router.post("/groups")
async def create_group(
    req: CreateGroupReq = Body(embed=True),
    _=Depends(verify_admin_key),
):
    db = get_db()
    gid = await db.create_group_raw(
        name=req.name, description=req.description, is_enabled=req.is_enabled,
        fallback_group_id=req.fallback_group_id, type=req.type, params=req.params,
    )
    return {"success": True, "group_id": gid, "name": req.name}


@admin_router.get("/groups")
async def list_groups(enabled_only: bool = False, _=Depends(verify_admin_key)):
    db = get_db()
    groups = await db.list_groups_raw(enabled_only=enabled_only)
    return {"success": True, "groups": [g.model_dump() for g in groups]}


@admin_router.get("/groups/{group_id}")
async def get_group(group_id: int, _=Depends(verify_admin_key)):
    db = get_db()
    group = await db.get_group_raw(group_id)
    if not group:
        raise HTTPException(status_code=404, detail={"success": False, "error": f"Group {group_id} not found"})
    return {"success": True, "group": group.model_dump()}


@admin_router.patch("/groups/{group_id}")
async def update_group(
    group_id: int,
    req: UpdateGroupReq,
    _=Depends(verify_admin_key),
):
    db = get_db()
    group = await db.get_group_raw(group_id)
    if not group:
        raise HTTPException(status_code=404, detail={"success": False, "error": f"Group {group_id} not found"})
    await db.update_group_raw(
        group_id,
        name=req.name if req.name is not None else group.name,
        description=req.description if req.description is not None else group.description,
        is_enabled=req.is_enabled if req.is_enabled is not None else group.is_enabled,
        fallback_group_id=req.fallback_group_id if req.fallback_group_id is not None else group.fallback_group_id,
        type=req.type if req.type is not None else group.type,
        params=req.params if req.params is not None else group.params,
    )
    invalidate_endpoint_cache(group_id)
    return {"success": True, "group_id": group_id}


@admin_router.delete("/groups/{group_id}")
async def delete_group(group_id: int, _=Depends(verify_admin_key)):
    db = get_db()
    ok = await db.delete_group_raw(group_id)
    if not ok:
        raise HTTPException(status_code=404, detail={"success": False, "error": f"Group {group_id} not found"})
    invalidate_endpoint_cache(group_id)
    return {"success": True, "group_id": group_id}


@admin_router.post("/groups/{group_id}/models")
async def add_model_to_group(
    group_id: int, model_id: int, weight: int = 1,
    _=Depends(verify_admin_key),
):
    db = get_db()
    group = await db.get_group_raw(group_id)
    if not group:
        raise HTTPException(status_code=404, detail={"success": False, "error": f"Group {group_id} not found"})
    model = await db.get_model_raw(model_id)
    if not model:
        raise HTTPException(status_code=404, detail={"success": False, "error": f"Model {model_id} not found"})
    await db.add_model_to_group_raw(group_id, model_id, weight=weight)
    invalidate_endpoint_cache(group_id)
    return {"success": True, "group_id": group_id, "model_id": model_id}


@admin_router.delete("/groups/{group_id}/models/{model_id}")
async def remove_model_from_group(group_id: int, model_id: int, _=Depends(verify_admin_key)):
    db = get_db()
    await db.remove_model_from_group_raw(group_id, model_id)
    invalidate_endpoint_cache(group_id)
    return {"success": True, "group_id": group_id, "model_id": model_id}


@admin_router.patch("/groups/{group_id}/models/{model_id}")
async def update_model_weight(
    group_id: int, model_id: int,
    weight: Optional[int] = None,
    _=Depends(verify_admin_key),
):
    db = get_db()
    await db.update_model_weight_raw(group_id, model_id, weight=weight)
    invalidate_endpoint_cache(group_id)
    return {"success": True, "group_id": group_id, "model_id": model_id}


@admin_router.get("/groups/{group_id}/details")
async def get_group_details(group_id: int, _=Depends(verify_admin_key)):
    db = get_db()
    group = await db.get_group_raw(group_id)
    if not group:
        raise HTTPException(status_code=404, detail={"success": False, "error": f"Group {group_id} not found"})
    models = await db.get_group_models_raw(group_id)
    return {
        "success": True,
        "group": group.model_dump(),
        "models": [m.model_dump() for m in models],
    }


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


@admin_router.get("/strategies")
async def list_strategies(_=Depends(verify_admin_key)):
    """Return registered strategy types."""
    from botflow.pipeline.base import STRATEGY_REGISTRY
    strategies = sorted(STRATEGY_REGISTRY.keys())
    return {"success": True, "strategies": strategies}


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

# Asia/Shanghai 固定 +8（1991 年后无夏令时）——用 stdlib timezone 而非
# 时区数据库方案：Windows 无时区数据、需第三方包，零依赖硬约束下不可用。
CN_TZ = timezone(timedelta(hours=8))
RANGE_VALUES = ("half_hour", "hour", "today", "week", "month", "d90")
RANGE_DEFAULT = "week"


def _resolve_range(range: str, now: datetime | None = None) -> tuple[str, str]:
    """把六档 range 预设解析为 UTC 窗口 (since_utc, until_utc)。

    返回 UTC 空格格式 ``YYYY-MM-DD HH:MM:SS``（与 call_logs.created_at 一致，
    字典序可比）。``now`` 仅测试注入用：生产调用不传、走
    ``datetime.now(timezone.utc)``；注入时须为 aware UTC（不加运行时校验）。

    六档语义：half_hour/hour/d90 为相对滑动窗口（UTC 直算）；today/week/month
    为东八自然边界（now 转 +8 求边界再转回 UTC）。非法值已在端点层被
    ``Literal[RANGE_VALUES]`` 422 拦截，本函数不设兜底分支。
    """
    if now is None:
        now = datetime.now(timezone.utc)
    until = now.strftime("%Y-%m-%d %H:%M:%S")
    if range in ("today", "week", "month"):
        # 东八自然边界：now 转 +8 取当日 00:00，再转回 UTC（created_at 存 UTC）
        local = now.astimezone(CN_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        if range == "week":
            local -= timedelta(days=local.weekday())  # 周一为一周之始（weekday: 周一=0）
        elif range == "month":
            local = local.replace(day=1)
        since = local.astimezone(timezone.utc)
    elif range == "half_hour":
        since = now - timedelta(minutes=30)
    elif range == "hour":
        since = now - timedelta(minutes=60)
    elif range == "d90":
        since = now - timedelta(days=90)
    return since.strftime("%Y-%m-%d %H:%M:%S"), until


@admin_router.get("/stats/models")
async def get_model_stats(
    limit: int = 20,
    api_key_id: Optional[int] = None,
    range: Optional[Literal[RANGE_VALUES]] = None,
    _=Depends(verify_admin_key),
):
    db = get_db()
    if range is not None:
        since, until = _resolve_range(range)
        stats = await db.list_model_stats(
            limit=limit, api_key_id=api_key_id, since_utc=since, until_utc=until
        )
    else:
        stats = await db.list_model_stats(limit=limit, api_key_id=api_key_id)
    return {"success": True, "model_stats": stats}


@admin_router.get("/stats/groups")
async def get_group_stats(
    limit: int = 20,
    api_key_id: Optional[int] = None,
    _=Depends(verify_admin_key),
):
    db = get_db()
    stats = await db.list_group_stats(limit=limit, api_key_id=api_key_id)
    return {"success": True, "group_stats": stats}


@admin_router.get("/stats/cost")
async def get_cost_summary(
    days: int = 30,
    api_key_id: Optional[int] = None,
    _=Depends(verify_admin_key),
):
    db = get_db()
    summary = await db.get_cost_summary(days=days, api_key_id=api_key_id)
    return {"success": True, "cost_summary": summary}


@admin_router.get("/stats/trend")
async def get_call_trend(
    range: Literal[RANGE_VALUES] = RANGE_DEFAULT,
    _=Depends(verify_admin_key),
):
    """分组按日调用趋势（东八日期分桶，range 六档窗口）。

    行式响应，只含有数据的 (day, group) 行（空窗由前端 pivot 补 0）；
    ``range`` 回显仅供调试/验收核对，前端竞态守卫不依赖它。
    """
    db = get_db()
    since, until = _resolve_range(range)
    rows = await db.list_group_trend(since, until)
    return {"success": True, "range": range, "trend": rows}


@admin_router.get("/logs")
async def query_logs(
    group_id: Optional[int] = None,
    model_id: Optional[int] = None,
    api_key_id: Optional[int] = None,
    status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    _=Depends(verify_admin_key),
):
    db = get_db()
    logs = await db.query_call_logs(
        group_id=group_id, model_id=model_id, api_key_id=api_key_id,
        status=status, limit=limit, offset=offset,
    )
    return {"success": True, "logs": [l.model_dump() for l in logs]}


@admin_router.get("/summaries/{day}")
async def get_summary(day: str, _=Depends(verify_admin_key)):
    db = get_db()
    summary = await db.get_daily_summary(day)
    if not summary:
        raise HTTPException(status_code=404, detail={"success": False, "error": f"No summary for {day}"})
    return {"success": True, "summary": summary.model_dump()}


@admin_router.get("/attempts")
async def query_attempts(
    request_id: Optional[str] = None,
    model_id: Optional[int] = None,
    provider_id: Optional[int] = None,
    group_id: Optional[int] = None,
    error_type: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    _=Depends(verify_admin_key),
):
    """Read-only query of failed-attempt rows (SG-0 observability).

    Returns 200 with an (possibly empty) ``attempts`` list on no match — never
    404, so callers can poll without special-casing "nothing yet".
    """
    db = get_db()
    rows = await db.query_call_attempts(
        request_id=request_id,
        model_id=model_id,
        provider_id=provider_id,
        group_id=group_id,
        error_type=error_type,
        limit=limit,
        offset=offset,
    )
    return {"success": True, "attempts": [a.model_dump() for a in rows]}


# ---------------------------------------------------------------------------
# Client API keys (multi-tenant)
# ---------------------------------------------------------------------------


@admin_router.post("/apikeys")
async def create_api_key(
    req: CreateApiKeyReq = Body(embed=True),
    _=Depends(verify_admin_key),
):
    db = get_db()
    key = await db.create_api_key(req.raw_key, label=req.label)
    # Return only a hash prefix — never the raw key after creation.
    return {
        "success": True,
        "id": key.id,
        "key_hash_prefix": key.key_hash[:8] + "…",
        "label": key.label,
        "is_enabled": key.is_enabled,
    }


@admin_router.get("/apikeys")
async def list_api_keys(_=Depends(verify_admin_key)):
    db = get_db()
    keys = await db.list_api_keys()
    return {
        "success": True,
        "api_keys": [
            {
                "id": k.id,
                "key_hash_prefix": k.key_hash[:8] + "…",
                "label": k.label,
                "is_enabled": k.is_enabled,
                "created_at": k.created_at,
            }
            for k in keys
        ],
    }


@admin_router.patch("/apikeys/{key_id}")
async def set_api_key_enabled(
    key_id: int,
    req: UpdateApiKeyReq,
    _=Depends(verify_admin_key),
):
    db = get_db()
    ok = await db.set_api_key_enabled(key_id, req.is_enabled)
    if not ok:
        raise HTTPException(status_code=404, detail={"success": False, "error": f"API key {key_id} not found"})
    return {"success": True, "key_id": key_id, "is_enabled": req.is_enabled}


@admin_router.delete("/apikeys/{key_id}")
async def delete_api_key(key_id: int, _=Depends(verify_admin_key)):
    db = get_db()
    ok = await db.delete_api_key(key_id)
    if not ok:
        raise HTTPException(status_code=404, detail={"success": False, "error": f"API key {key_id} not found"})
    return {"success": True, "key_id": key_id}
