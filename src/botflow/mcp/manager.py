"""MCP management tools for Provider/Model/Group CRUD operations."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from botflow.common.logger import get_logger
from botflow.common.exceptions import ConfigurationError
from botflow.storage.db import Database
from botflow.storage.models import Model, ModelGroup, Provider

log = get_logger("mcp.manager")


def register_manager_tools(mcp: FastMCP, db: Database) -> None:
    """Register all management tools with the provided MCP server."""

    # -----------------------------------------------------------------------
    # Provider management
    # -----------------------------------------------------------------------

    @mcp.tool()
    async def create_provider(
        name: str,
        provider_type: str,
        api_key: str = "",
        base_url: str = "",
        extra_config: str = "{}",
        is_enabled: bool = True,
    ) -> dict:
        """Create a new LLM provider."""
        provider = Provider(
            name=name,
            provider_type=provider_type,
            api_key=api_key,
            base_url=base_url,
            extra_config=_json_loads(extra_config),
            is_enabled=is_enabled,
        )
        try:
            provider_id = await db.create_provider(provider)
            log.info("Created provider '{}' (id={})", name, provider_id)
            return {"id": provider_id, "name": name, "provider_type": provider_type}
        except Exception as e:
            raise ConfigurationError(f"Failed to create provider '{name}': {e}")

    @mcp.tool()
    async def update_provider(
        provider_id: int,
        name: str | None = None,
        provider_type: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        extra_config: str | None = None,
        is_enabled: bool | None = None,
    ) -> dict:
        """Update an existing LLM provider."""
        updates: dict[str, Any] = {}
        if name is not None:
            updates["name"] = name
        if provider_type is not None:
            updates["provider_type"] = provider_type
        if api_key is not None:
            updates["api_key"] = api_key
        if base_url is not None:
            updates["base_url"] = base_url
        if extra_config is not None:
            updates["extra_config"] = _json_loads(extra_config)
        if is_enabled is not None:
            updates["is_enabled"] = int(is_enabled)

        if not updates:
            return {"updated": False, "message": "No updates provided."}

        await db.update_provider(provider_id, updates)
        log.info("Updated provider id={}", provider_id)
        return {"updated": True, "provider_id": provider_id, "changes": list(updates.keys())}

    @mcp.tool()
    async def delete_provider(provider_id: int) -> dict:
        """Delete an LLM provider and its associated models."""
        await db.delete_provider(provider_id)
        log.info("Deleted provider id={}", provider_id)
        return {"deleted": True, "provider_id": provider_id}

    @mcp.tool()
    async def get_provider(provider_id: int) -> dict:
        """Get details of a specific provider."""
        provider = await db.get_provider(provider_id)
        if provider is None:
            return {"found": False, "provider_id": provider_id}
        return _format_provider_dict(provider)

    @mcp.tool()
    async def list_providers(enabled_only: bool = False) -> dict:
        """List all LLM providers."""
        providers = await db.list_providers(enabled_only=enabled_only)
        if not providers:
            return {"providers": []}
        return {
            "providers": [
                {
                    "id": p.id,
                    "name": p.name,
                    "provider_type": p.provider_type,
                    "base_url": p.base_url,
                    "is_enabled": bool(p.is_enabled),
                }
                for p in providers
            ]
        }

    # -----------------------------------------------------------------------
    # Model management
    # -----------------------------------------------------------------------

    @mcp.tool()
    async def create_model(
        name: str,
        provider_id: int,
        display_name: str = "",
        max_retries: int = 3,
        cooldown_seconds: int = 60,
        cooldown_failure_threshold: int = 3,
        extra_config: str = "{}",
        is_enabled: bool = True,
    ) -> dict:
        """Create a new model under a provider."""
        model = Model(
            name=name,
            provider_id=provider_id,
            display_name=display_name,
            max_retries=max_retries,
            cooldown_seconds=cooldown_seconds,
            cooldown_failure_threshold=cooldown_failure_threshold,
            extra_config=_json_loads(extra_config),
            is_enabled=is_enabled,
        )
        try:
            model_id = await db.create_model(model)
            log.info("Created model '{}' (id={}) under provider id={}", name, model_id, provider_id)
            return {"id": model_id, "name": name, "provider_id": provider_id}
        except Exception as e:
            raise ConfigurationError(f"Failed to create model '{name}': {e}")

    @mcp.tool()
    async def update_model(
        model_id: int,
        name: str | None = None,
        display_name: str | None = None,
        max_retries: int | None = None,
        cooldown_seconds: int | None = None,
        cooldown_failure_threshold: int | None = None,
        extra_config: str | None = None,
        is_enabled: bool | None = None,
    ) -> dict:
        """Update an existing model."""
        updates: dict[str, Any] = {}
        if name is not None:
            updates["name"] = name
        if display_name is not None:
            updates["display_name"] = display_name
        if max_retries is not None:
            updates["max_retries"] = max_retries
        if cooldown_seconds is not None:
            updates["cooldown_seconds"] = cooldown_seconds
        if cooldown_failure_threshold is not None:
            updates["cooldown_failure_threshold"] = cooldown_failure_threshold
        if extra_config is not None:
            updates["extra_config"] = _json_loads(extra_config)
        if is_enabled is not None:
            updates["is_enabled"] = int(is_enabled)

        if not updates:
            return {"updated": False, "message": "No updates provided."}

        await db.update_model(model_id, updates)
        log.info("Updated model id={}", model_id)
        return {"updated": True, "model_id": model_id, "changes": list(updates.keys())}

    @mcp.tool()
    async def delete_model(model_id: int) -> dict:
        """Delete a model."""
        await db.delete_model(model_id)
        log.info("Deleted model id={}", model_id)
        return {"deleted": True, "model_id": model_id}

    @mcp.tool()
    async def list_models(enabled_only: bool = False) -> dict:
        """List all models."""
        models = await db.list_models(enabled_only=enabled_only)
        if not models:
            return {"models": []}
        return {
            "models": [
                {
                    "id": m.id,
                    "name": m.name,
                    "provider_id": m.provider_id,
                    "display_name": m.display_name,
                    "is_enabled": bool(m.is_enabled),
                }
                for m in models
            ]
        }

    # -----------------------------------------------------------------------
    # Group management
    # -----------------------------------------------------------------------

    @mcp.tool()
    async def create_group(
        name: str,
        description: str = "",
        is_enabled: bool = True,
    ) -> dict:
        """Create a new model group for weighted routing."""
        group = ModelGroup(name=name, description=description, is_enabled=is_enabled)
        try:
            group_id = await db.create_group(group)
            log.info("Created group '{}' (id={})", name, group_id)
            return {"id": group_id, "name": name}
        except Exception as e:
            raise ConfigurationError(f"Failed to create group '{name}': {e}")

    @mcp.tool()
    async def update_group(
        group_id: int,
        name: str | None = None,
        description: str | None = None,
        is_enabled: bool | None = None,
        fallback_group_id: int | None = None,
    ) -> dict:
        """Update an existing group."""
        updates: dict[str, Any] = {}
        if name is not None:
            updates["name"] = name
        if description is not None:
            updates["description"] = description
        if is_enabled is not None:
            updates["is_enabled"] = int(is_enabled)
        if fallback_group_id is not None:
            updates["fallback_group_id"] = int(fallback_group_id)

        if not updates:
            return {"updated": False, "message": "No updates provided."}

        await db.update_group(group_id, updates)
        log.info("Updated group id={}", group_id)
        return {"updated": True, "group_id": group_id, "changes": list(updates.keys())}

    @mcp.tool()
    async def delete_group(group_id: int) -> dict:
        """Delete a group."""
        await db.delete_group(group_id)
        log.info("Deleted group id={}", group_id)
        return {"deleted": True, "group_id": group_id}

    @mcp.tool()
    async def get_group(group_id: int) -> dict:
        """Get details of a specific group with its models."""
        group = await db.get_group(group_id)
        if group is None:
            return {"found": False, "group_id": group_id}

        models = await db.get_group_models(group_id)
        return {
            "id": group.id,
            "name": group.name,
            "description": group.description,
            "is_enabled": bool(group.is_enabled),
            "models": [
                {
                    "model_id": m.model_id,
                    "model_name": m.model_name,
                    "provider_name": m.provider_name,
                    "weight": m.weight,
                }
                for m in models
            ],
        }

    @mcp.tool()
    async def list_groups(enabled_only: bool = False) -> dict:
        """List all model groups."""
        groups = await db.list_groups(enabled_only=enabled_only)
        if not groups:
            return {"groups": []}
        return {
            "groups": [
                {
                    "id": g.id,
                    "name": g.name,
                    "description": g.description,
                    "is_enabled": bool(g.is_enabled),
                }
                for g in groups
            ]
        }

    # -----------------------------------------------------------------------
    # Group-Model association
    # -----------------------------------------------------------------------

    @mcp.tool()
    async def add_model_to_group(group_id: int, model_id: int, weight: float = 1.0) -> dict:
        """Add a model to a group with a weight."""
        await db.add_model_to_group(group_id, model_id, weight)
        log.info("Added model id={} to group id={} with weight={}", model_id, group_id, weight)
        return {"added": True, "group_id": group_id, "model_id": model_id, "weight": weight}

    @mcp.tool()
    async def remove_model_from_group(group_id: int, model_id: int) -> dict:
        """Remove a model from a group."""
        await db.remove_model_from_group(group_id, model_id)
        log.info("Removed model id={} from group id={}", model_id, group_id)
        return {"removed": True, "group_id": group_id, "model_id": model_id}

    @mcp.tool()
    async def update_model_weight(group_id: int, model_id: int, weight: float) -> dict:
        """Update the weight of a model in a group."""
        if weight <= 0:
            return {"updated": False, "message": "Weight must be greater than 0."}
        await db.update_model_weight(group_id, model_id, weight)
        log.info("Updated weight of model id={} in group id={} to {}", model_id, group_id, weight)
        return {"updated": True, "group_id": group_id, "model_id": model_id, "weight": weight}

    log.info("MCP manager tools registered.")


def _format_provider_dict(p: Provider) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "provider_type": p.provider_type,
        "base_url": p.base_url or "(default)",
        "api_key": "***" if p.api_key else "(none)",
        "is_enabled": bool(p.is_enabled),
    }


def _json_loads(s: str) -> dict:
    import json
    if not s or s.strip() == "":
        return {}
    return json.loads(s)