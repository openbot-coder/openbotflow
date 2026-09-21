"""REST management API for botflow LLM Proxy.

Replaces the old MCP-based management tools with plain HTTP endpoints guarded
by the admin key (BOTFLOW_ADMIN_KEY). Each route maps 1:1 to a former MCP tool.
"""

from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel

from botflow.auth import verify_admin_key
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


@admin_router.get("/stats/models")
async def get_model_stats(
    limit: int = 20,
    api_key_id: Optional[int] = None,
    _=Depends(verify_admin_key),
):
    db = get_db()
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
