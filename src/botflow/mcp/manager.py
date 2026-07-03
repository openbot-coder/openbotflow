"""MCP management tools for Provider/Model/Group CRUD operations."""

from __future__ import annotations

from typing import Any

from loguru import logger
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
    ) -> str:
        """Create a new LLM provider.

        Args:
            name: Provider name (must be unique).
            provider_type: Provider type (openai, azure, anthropic, google, ollama, vllm).
            api_key: API key for the provider.
            base_url: Base URL for the provider API.
            extra_config: JSON string of extra configuration.
            is_enabled: Whether the provider is enabled.
        """
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
            return f"Provider '{name}' created with id={provider_id}"
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
    ) -> str:
        """Update an existing LLM provider.

        Args:
            provider_id: ID of the provider to update.
            name: New provider name.
            provider_type: New provider type.
            api_key: New API key.
            base_url: New base URL.
            extra_config: JSON string of extra configuration.
            is_enabled: Whether the provider is enabled.
        """
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
            return "No updates provided."

        await db.update_provider(provider_id, updates)
        log.info("Updated provider id={}", provider_id)
        return f"Provider id={provider_id} updated."

    @mcp.tool()
    async def delete_provider(provider_id: int) -> str:
        """Delete an LLM provider and its associated models.

        Args:
            provider_id: ID of the provider to delete.
        """
        await db.delete_provider(provider_id)
        log.info("Deleted provider id={}", provider_id)
        return f"Provider id={provider_id} deleted."

    @mcp.tool()
    async def get_provider(provider_id: int) -> str:
        """Get details of a specific provider.

        Args:
            provider_id: ID of the provider.
        """
        provider = await db.get_provider(provider_id)
        if provider is None:
            return f"Provider id={provider_id} not found."
        return _format_provider(provider)

    @mcp.tool()
    async def list_providers(enabled_only: bool = False) -> str:
        """List all LLM providers.

        Args:
            enabled_only: If True, only list enabled providers.
        """
        providers = await db.list_providers(enabled_only=enabled_only)
        if not providers:
            return "No providers found."
        lines = [f"Found {len(providers)} provider(s):"]
        for p in providers:
            lines.append(f"  [{p.id}] {p.name} ({p.provider_type}) - {'enabled' if p.is_enabled else 'disabled'}")
        return "\n".join(lines)

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
    ) -> str:
        """Create a new model under a provider.

        Args:
            name: Model name (e.g., "gpt-4o", "claude-sonnet-4-20250514").
            provider_id: ID of the provider that hosts this model.
            display_name: Human-readable display name.
            max_retries: Maximum number of retry attempts on failure.
            cooldown_seconds: Cooldown duration in seconds after reaching failure threshold.
            cooldown_failure_threshold: Number of consecutive failures before entering cooldown.
            extra_config: JSON string of extra configuration.
            is_enabled: Whether the model is enabled.
        """
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
            return f"Model '{name}' created with id={model_id}"
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
    ) -> str:
        """Update an existing model.

        Args:
            model_id: ID of the model to update.
            name: New model name.
            display_name: New display name.
            max_retries: New max retry count.
            cooldown_seconds: New cooldown seconds.
            cooldown_failure_threshold: New failure threshold before cooldown.
            extra_config: JSON string of extra configuration.
            is_enabled: Whether the model is enabled.
        """
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
            return "No updates provided."

        await db.update_model(model_id, updates)
        log.info("Updated model id={}", model_id)
        return f"Model id={model_id} updated."

    @mcp.tool()
    async def delete_model(model_id: int) -> str:
        """Delete a model.

        Args:
            model_id: ID of the model to delete.
        """
        await db.delete_model(model_id)
        log.info("Deleted model id={}", model_id)
        return f"Model id={model_id} deleted."

    @mcp.tool()
    async def list_models(enabled_only: bool = False) -> str:
        """List all models.

        Args:
            enabled_only: If True, only list enabled models.
        """
        models = await db.list_models(enabled_only=enabled_only)
        if not models:
            return "No models found."
        lines = [f"Found {len(models)} model(s):"]
        for m in models:
            status = "enabled" if m.is_enabled else "disabled"
            lines.append(f"  [{m.id}] {m.name} (provider_id={m.provider_id}) - {status}")
        return "\n".join(lines)

    # -----------------------------------------------------------------------
    # Group management
    # -----------------------------------------------------------------------

    @mcp.tool()
    async def create_group(
        name: str,
        description: str = "",
        is_enabled: bool = True,
    ) -> str:
        """Create a new model group for weighted routing.

        Args:
            name: Group name (must be unique).
            description: Description of the group.
            is_enabled: Whether the group is enabled.
        """
        group = ModelGroup(name=name, description=description, is_enabled=is_enabled)
        try:
            group_id = await db.create_group(group)
            log.info("Created group '{}' (id={})", name, group_id)
            return f"Group '{name}' created with id={group_id}"
        except Exception as e:
            raise ConfigurationError(f"Failed to create group '{name}': {e}")

    @mcp.tool()
    async def update_group(
        group_id: int,
        name: str | None = None,
        description: str | None = None,
        is_enabled: bool | None = None,
    ) -> str:
        """Update an existing group.

        Args:
            group_id: ID of the group to update.
            name: New group name.
            description: New description.
            is_enabled: Whether the group is enabled.
        """
        updates: dict[str, Any] = {}
        if name is not None:
            updates["name"] = name
        if description is not None:
            updates["description"] = description
        if is_enabled is not None:
            updates["is_enabled"] = int(is_enabled)

        if not updates:
            return "No updates provided."

        await db.update_group(group_id, updates)
        log.info("Updated group id={}", group_id)
        return f"Group id={group_id} updated."

    @mcp.tool()
    async def delete_group(group_id: int) -> str:
        """Delete a group.

        Args:
            group_id: ID of the group to delete.
        """
        await db.delete_group(group_id)
        log.info("Deleted group id={}", group_id)
        return f"Group id={group_id} deleted."

    @mcp.tool()
    async def get_group(group_id: int) -> str:
        """Get details of a specific group with its models.

        Args:
            group_id: ID of the group.
        """
        group = await db.get_group(group_id)
        if group is None:
            return f"Group id={group_id} not found."

        models = await db.get_group_models(group_id)
        lines = [
            f"Group [{group.id}] {group.name}",
            f"  Description: {group.description}",
            f"  Enabled: {group.is_enabled}",
            f"  Models ({len(models)}):",
        ]
        for m in models:
            lines.append(f"    - [{m.model_id}] {m.model_name} (weight={m.weight}, provider={m.provider_name})")
        return "\n".join(lines)

    @mcp.tool()
    async def list_groups(enabled_only: bool = False) -> str:
        """List all model groups.

        Args:
            enabled_only: If True, only list enabled groups.
        """
        groups = await db.list_groups(enabled_only=enabled_only)
        if not groups:
            return "No groups found."
        lines = [f"Found {len(groups)} group(s):"]
        for g in groups:
            status = "enabled" if g.is_enabled else "disabled"
            lines.append(f"  [{g.id}] {g.name} - {status}")
        return "\n".join(lines)

    # -----------------------------------------------------------------------
    # Group-Model association
    # -----------------------------------------------------------------------

    @mcp.tool()
    async def add_model_to_group(group_id: int, model_id: int, weight: float = 1.0) -> str:
        """Add a model to a group with a weight.

        Args:
            group_id: ID of the group.
            model_id: ID of the model to add.
            weight: Weight for weighted random selection (higher = more likely chosen).
        """
        await db.add_model_to_group(group_id, model_id, weight)
        log.info("Added model id={} to group id={} with weight={}", model_id, group_id, weight)
        return f"Model id={model_id} added to group id={group_id} with weight={weight}."

    @mcp.tool()
    async def remove_model_from_group(group_id: int, model_id: int) -> str:
        """Remove a model from a group.

        Args:
            group_id: ID of the group.
            model_id: ID of the model to remove.
        """
        await db.remove_model_from_group(group_id, model_id)
        log.info("Removed model id={} from group id={}", model_id, group_id)
        return f"Model id={model_id} removed from group id={group_id}."

    @mcp.tool()
    async def update_model_weight(group_id: int, model_id: int, weight: float) -> str:
        """Update the weight of a model in a group.

        Args:
            group_id: ID of the group.
            model_id: ID of the model.
            weight: New weight value.
        """
        if weight <= 0:
            return "Weight must be greater than 0."
        await db.update_model_weight(group_id, model_id, weight)
        log.info("Updated weight of model id={} in group id={} to {}", model_id, group_id, weight)
        return f"Weight updated to {weight}."

    log.info("MCP manager tools registered.")


def _format_provider(p: Provider) -> str:
    return (
        f"Provider [{p.id}] {p.name}\n"
        f"  Type: {p.provider_type}\n"
        f"  Base URL: {p.base_url or '(default)'}\n"
        f"  API Key: {'***' if p.api_key else '(none)'}\n"
        f"  Enabled: {p.is_enabled}\n"
    )


def _json_loads(s: str) -> dict:
    import json
    if not s or s.strip() == "":
        return {}
    return json.loads(s)
