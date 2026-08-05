"""MCP Tools for Provider / Model / Group management.

Tools registered via ToolRegistry (not directly on FastMCP).
Uses the flat Database API: db.create_provider(Provider(...)) etc.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from botflow.storage.db import Database
from botflow.storage.models import Model, ModelGroup, Provider

if TYPE_CHECKING:
    from botflow.mcp.registry import ToolRegistry


def register_manager_tools(registry: ToolRegistry, db: Database) -> None:
    """Register all management tools into the internal ToolRegistry."""

    # ── Provider CRUD ──

    async def create_provider(
        name: str, provider_type: str, api_key: str,
        base_url: str | None = None, extra_config: str | None = None,
    ) -> str:
        # W5: safely parse extra_config JSON
        parsed_extra: dict[str, Any] = {}
        if extra_config:
            try:
                parsed_extra = json.loads(extra_config)
            except (json.JSONDecodeError, TypeError):
                return json.dumps({"error": f"Invalid JSON in extra_config: {extra_config[:200]}"})
        provider = Provider(
            name=name, provider_type=provider_type, api_key=api_key,
            base_url=base_url or "",
            extra_config=parsed_extra,
        )
        pid = await db.create_provider(provider)
        return json.dumps({"id": pid, "name": name, "provider_type": provider_type})

    registry.register(
        name="create_provider",
        description="新增 LLM 供应商。provider_type 支持 openai/anthropic/moonshot/dashscope/openai_compat。api_key 支持 ${VAR} 环境变量引用。",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "供应商名称，唯一标识"},
                "provider_type": {"type": "string", "enum": ["openai", "anthropic", "moonshot", "dashscope", "openai_compat"], "description": "供应商类型"},
                "api_key": {"type": "string", "description": "API Key，支持 ${VAR} 引用环境变量"},
                "base_url": {"type": "string", "description": "API 基础 URL（可选）"},
                "extra_config": {"type": "string", "description": "扩展配置 JSON 字符串（可选）"},
            },
            "required": ["name", "provider_type", "api_key"],
        },
        handler=create_provider,
    )

    async def update_provider(
        id: int, api_key: str | None = None, base_url: str | None = None,
        is_enabled: bool | None = None, extra_config: str | None = None,
    ) -> str:
        provider = await db.get_provider(id)
        if provider is None:
            return json.dumps({"error": f"Provider {id} not found"})
        updates: dict[str, Any] = {}
        if api_key is not None:
            updates["api_key"] = api_key
        if base_url is not None:
            updates["base_url"] = base_url
        if is_enabled is not None:
            updates["is_enabled"] = is_enabled
        if extra_config is not None:
            # W5: safely parse extra_config JSON
            try:
                updates["extra_config"] = json.loads(extra_config)
            except (json.JSONDecodeError, TypeError):
                return json.dumps({"error": f"Invalid JSON in extra_config: {extra_config[:200]}"})
        # W6: only report updated if something actually changed
        if updates:
            await db.update_provider(id, updates)
            return json.dumps({"id": id, "name": provider.name, "updated": True})
        return json.dumps({"id": id, "name": provider.name, "updated": False, "message": "No changes provided"})

    registry.register(
        name="update_provider",
        description="更新供应商配置（API Key、URL、启用状态等）",
        parameters={
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "供应商 ID"},
                "api_key": {"type": "string", "description": "新的 API Key"},
                "base_url": {"type": "string", "description": "新的基础 URL"},
                "is_enabled": {"type": "boolean", "description": "启用/禁用"},
                "extra_config": {"type": "string", "description": "扩展配置 JSON"},
            },
            "required": ["id"],
        },
        handler=update_provider,
    )

    async def delete_provider(id: int) -> str:
        provider = await db.get_provider(id)
        if provider is None:
            return json.dumps({"error": f"Provider {id} not found"})
        # C4: cascade — remove all models from groups, then delete models, then provider
        all_models = await db.list_models()
        provider_models = [m for m in all_models if m.provider_id == id]
        groups = await db.list_groups()
        for m in provider_models:
            for g in groups:
                await db.remove_model_from_group(g.id, m.id)
            await db.delete_model(m.id)
        await db.delete_provider(id)
        return json.dumps({"id": id, "deleted": True})

    registry.register(
        name="delete_provider",
        description="删除供应商及其所有模型",
        parameters={
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "供应商 ID"}},
            "required": ["id"],
        },
        handler=delete_provider,
    )

    async def list_providers() -> str:
        providers = await db.list_providers()
        # C3: fetch all models once, group in-memory to avoid N+1
        all_models = await db.list_models()
        models_by_provider: dict[int, int] = {}
        for m in all_models:
            models_by_provider[m.provider_id] = models_by_provider.get(m.provider_id, 0) + 1
        result = []
        for p in providers:
            result.append({
                "id": p.id, "name": p.name, "provider_type": p.provider_type,
                "model_count": models_by_provider.get(p.id, 0), "is_enabled": p.is_enabled,
            })
        return json.dumps({"providers": result})

    registry.register(
        name="list_providers",
        description="列出所有已配置的供应商及状态",
        parameters={"type": "object", "properties": {}},
        handler=list_providers,
    )

    async def get_provider(id: int) -> str:
        provider = await db.get_provider(id)
        if provider is None:
            return json.dumps({"error": f"Provider {id} not found"})
        all_models = await db.list_models()
        p_models = [m for m in all_models if m.provider_id == id]
        return json.dumps({
            "id": provider.id, "name": provider.name,
            "provider_type": provider.provider_type,
            "base_url": provider.base_url,
            "models": [{"id": m.id, "name": m.name} for m in p_models],
        })

    registry.register(
        name="get_provider",
        description="查看供应商详情及关联模型列表",
        parameters={
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "供应商 ID"}},
            "required": ["id"],
        },
        handler=get_provider,
    )

    # ── Model CRUD ──

    async def create_model(
        name: str, provider_id: int, display_name: str | None = None,
        max_retries: int | None = None, cooldown_seconds: int | None = None,
        cooldown_failure_threshold: int | None = None,
    ) -> str:
        provider = await db.get_provider(provider_id)
        if provider is None:
            return json.dumps({"error": f"Provider {provider_id} not found"})
        model = Model(
            name=name, provider_id=provider_id,
            display_name=display_name or "",
            max_retries=max_retries if max_retries is not None else 3,
            cooldown_seconds=cooldown_seconds if cooldown_seconds is not None else 60,
            cooldown_failure_threshold=cooldown_failure_threshold if cooldown_failure_threshold is not None else 3,
        )
        mid = await db.create_model(model)
        return json.dumps({"id": mid, "name": name, "provider_id": provider_id})

    registry.register(
        name="create_model",
        description="新增模型。需先有 provider，通过 provider_id 关联。",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "模型名称（如 gpt-4o）"},
                "provider_id": {"type": "integer", "description": "所属供应商 ID"},
                "display_name": {"type": "string", "description": "显示名称"},
                "max_retries": {"type": "integer", "description": "最大重试次数"},
                "cooldown_seconds": {"type": "integer", "description": "冷却时长（秒）"},
                "cooldown_failure_threshold": {"type": "integer", "description": "触发冷却的连续失败次数"},
            },
            "required": ["name", "provider_id"],
        },
        handler=create_model,
    )

    async def update_model(
        id: int, name: str | None = None, max_retries: int | None = None,
        cooldown_seconds: int | None = None,
        cooldown_failure_threshold: int | None = None,
        is_enabled: bool | None = None,
    ) -> str:
        model = await db.get_model(id)
        if model is None:
            return json.dumps({"error": f"Model {id} not found"})
        updates: dict[str, Any] = {}
        if name is not None:
            updates["name"] = name
        if max_retries is not None:
            updates["max_retries"] = max_retries
        if cooldown_seconds is not None:
            updates["cooldown_seconds"] = cooldown_seconds
        if cooldown_failure_threshold is not None:
            updates["cooldown_failure_threshold"] = cooldown_failure_threshold
        if is_enabled is not None:
            updates["is_enabled"] = is_enabled
        # W6: only report updated if something actually changed
        if updates:
            await db.update_model(id, updates)
            return json.dumps({"id": id, "updated": True})
        return json.dumps({"id": id, "updated": False, "message": "No changes provided"})

    registry.register(
        name="update_model",
        description="更新模型配置",
        parameters={
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "模型 ID"},
                "name": {"type": "string", "description": "新名称"},
                "max_retries": {"type": "integer", "description": "最大重试次数"},
                "cooldown_seconds": {"type": "integer", "description": "冷却时长（秒）"},
                "cooldown_failure_threshold": {"type": "integer", "description": "触发冷却的连续失败次数"},
                "is_enabled": {"type": "boolean", "description": "启用/禁用"},
            },
            "required": ["id"],
        },
        handler=update_model,
    )

    async def delete_model(id: int) -> str:
        model = await db.get_model(id)
        if model is None:
            return json.dumps({"error": f"Model {id} not found"})
        # C4: cascade — remove model from all groups before deleting
        groups = await db.list_groups()
        for g in groups:
            await db.remove_model_from_group(g.id, id)
        await db.delete_model(id)
        return json.dumps({"id": id, "deleted": True})

    registry.register(
        name="delete_model",
        description="删除模型",
        parameters={
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "模型 ID"}},
            "required": ["id"],
        },
        handler=delete_model,
    )

    async def list_models() -> str:
        models = await db.list_models()
        # C3: fetch all providers once, lookup in-memory to avoid N+1
        all_providers = await db.list_providers()
        provider_map = {p.id: p.name for p in all_providers}
        result = []
        for m in models:
            result.append({
                "id": m.id, "name": m.name,
                "provider": provider_map.get(m.provider_id, "?"),
                "is_enabled": m.is_enabled,
            })
        return json.dumps({"models": result})

    registry.register(
        name="list_models",
        description="列出所有可用模型",
        parameters={"type": "object", "properties": {}},
        handler=list_models,
    )

    # ── Group CRUD ──

    async def create_group(name: str, description: str | None = None) -> str:
        group = ModelGroup(name=name, description=description or "")
        gid = await db.create_group(group)
        return json.dumps({"id": gid, "name": name})

    registry.register(
        name="create_group",
        description="新增模型分组",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "分组名称"},
                "description": {"type": "string", "description": "分组描述"},
            },
            "required": ["name"],
        },
        handler=create_group,
    )

    async def update_group(
        id: int, name: str | None = None,
        description: str | None = None, is_enabled: bool | None = None,
    ) -> str:
        group = await db.get_group(id)
        if group is None:
            return json.dumps({"error": f"Group {id} not found"})
        updates: dict[str, Any] = {}
        if name is not None:
            updates["name"] = name
        if description is not None:
            updates["description"] = description
        if is_enabled is not None:
            updates["is_enabled"] = is_enabled
        # W6: only report updated if something actually changed
        if updates:
            await db.update_group(id, updates)
            return json.dumps({"id": id, "updated": True})
        return json.dumps({"id": id, "updated": False, "message": "No changes provided"})

    registry.register(
        name="update_group",
        description="更新分组信息",
        parameters={
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "分组 ID"},
                "name": {"type": "string", "description": "新名称"},
                "description": {"type": "string", "description": "新描述"},
                "is_enabled": {"type": "boolean", "description": "启用/禁用"},
            },
            "required": ["id"],
        },
        handler=update_group,
    )

    async def delete_group(id: int) -> str:
        group = await db.get_group(id)
        if group is None:
            return json.dumps({"error": f"Group {id} not found"})
        await db.delete_group(id)
        return json.dumps({"id": id, "deleted": True})

    registry.register(
        name="delete_group",
        description="删除分组",
        parameters={
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "分组 ID"}},
            "required": ["id"],
        },
        handler=delete_group,
    )

    async def list_groups() -> str:
        groups = await db.list_groups()
        result = []
        for g in groups:
            items = await db.get_group_models(g.id, enabled_only=False)
            enabled = sum(1 for gm in items if gm.is_enabled)
            result.append({
                "id": g.id, "name": g.name, "description": g.description,
                "model_count": len(items), "enabled_count": enabled,
            })
        return json.dumps({"groups": result})

    registry.register(
        name="list_groups",
        description="列出所有模型分组及状态",
        parameters={"type": "object", "properties": {}},
        handler=list_groups,
    )

    async def get_group(id: int) -> str:
        group = await db.get_group(id)
        if group is None:
            return json.dumps({"error": f"Group {id} not found"})
        items = await db.get_group_models(id, enabled_only=False)
        models = []
        for gm in items:
            models.append({
                "model_id": gm.model_id, "name": gm.model_name,
                "provider": gm.provider_name,
                "weight": gm.weight,
            })
        return json.dumps({
            "id": group.id, "name": group.name,
            "description": group.description, "models": models,
        })

    registry.register(
        name="get_group",
        description="查看分组详情及包含的模型和权重",
        parameters={
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "分组 ID"}},
            "required": ["id"],
        },
        handler=get_group,
    )

    # ── Group-Model association ──

    async def add_model_to_group(group_id: int, model_id: int, weight: float = 1.0) -> str:
        group = await db.get_group(group_id)
        if group is None:
            return json.dumps({"error": f"Group {group_id} not found"})
        model = await db.get_model(model_id)
        if model is None:
            return json.dumps({"error": f"Model {model_id} not found"})
        gm_id = await db.add_model_to_group(group_id, model_id, float(weight))
        return json.dumps({
            "id": gm_id, "group_id": group_id, "model_id": model_id,
            "group_name": group.name, "model_name": model.name, "weight": weight,
        })

    registry.register(
        name="add_model_to_group",
        description="将模型添加到分组，weight 为权重（越大越优先）",
        parameters={
            "type": "object",
            "properties": {
                "group_id": {"type": "integer", "description": "分组 ID"},
                "model_id": {"type": "integer", "description": "模型 ID"},
                "weight": {"type": "number", "description": "权重，默认 1.0"},
            },
            "required": ["group_id", "model_id"],
        },
        handler=add_model_to_group,
    )

    # W8: existence checks for remove/update operations
    async def remove_model_from_group(group_id: int, model_id: int) -> str:
        items = await db.get_group_models(group_id, enabled_only=False)
        if not any(gm.model_id == model_id for gm in items):
            return json.dumps({"error": f"Model {model_id} not found in group {group_id}"})
        await db.remove_model_from_group(group_id, model_id)
        return json.dumps({"group_id": group_id, "model_id": model_id, "deleted": True})

    registry.register(
        name="remove_model_from_group",
        description="从分组中移除模型",
        parameters={
            "type": "object",
            "properties": {
                "group_id": {"type": "integer", "description": "分组 ID"},
                "model_id": {"type": "integer", "description": "模型 ID"},
            },
            "required": ["group_id", "model_id"],
        },
        handler=remove_model_from_group,
    )

    # W8: existence check + W7: weight as number
    async def update_model_weight(group_id: int, model_id: int, weight: float) -> str:
        items = await db.get_group_models(group_id, enabled_only=False)
        if not any(gm.model_id == model_id for gm in items):
            return json.dumps({"error": f"Model {model_id} not found in group {group_id}"})
        await db.update_model_weight(group_id, model_id, float(weight))
        return json.dumps({"group_id": group_id, "model_id": model_id, "weight": weight, "updated": True})

    registry.register(
        name="update_model_weight",
        description="修改模型在分组中的权重",
        parameters={
            "type": "object",
            "properties": {
                "group_id": {"type": "integer", "description": "分组 ID"},
                "model_id": {"type": "integer", "description": "模型 ID"},
                "weight": {"type": "number", "description": "新权重"},
            },
            "required": ["group_id", "model_id", "weight"],
        },
        handler=update_model_weight,
    )
