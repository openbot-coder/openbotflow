"""Tests for MCP management and stats tools."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

from botflow.mcp.manager import register_manager_tools
from botflow.mcp.stats import register_stats_tools
from botflow.storage.db import Database
from botflow.storage.models import CallLog, Model, ModelGroup, Provider


def _get_text(result) -> str:
    """Extract text from MCP call_tool result (returns (content_list, meta_dict))."""
    from mcp.types import TextContent
    content_list, meta = result
    parts = []
    for item in content_list:
        if isinstance(item, TextContent):
            parts.append(item.text)
        elif isinstance(item, str):
            parts.append(item)
    return "\n".join(parts)


@pytest.fixture
async def db():
    f = Path(tempfile.mktemp(suffix=".db"))
    database = Database(f)
    await database.initialize()
    yield database
    await database.close()
    if f.exists():
        f.unlink(missing_ok=True)


@pytest.fixture
def mcp():
    return FastMCP("test")


# ---------------------------------------------------------------------------
# Provider tools
# ---------------------------------------------------------------------------

class TestProviderTools:
    @pytest.mark.asyncio
    async def test_create_and_list(self, mcp, db):
        register_manager_tools(mcp, db)

        result = await mcp.call_tool("create_provider", {
            "name": "test-ai",
            "provider_type": "openai",
            "api_key": "sk-test",
            "base_url": "https://api.test.com",
        })
        assert "created with id=" in _get_text(result)

        result = await mcp.call_tool("list_providers", {})
        assert "test-ai" in _get_text(result)

    @pytest.mark.asyncio
    async def test_update_and_get(self, mcp, db):
        register_manager_tools(mcp, db)
        pid = await db.create_provider(Provider(name="old", provider_type="openai"))

        await mcp.call_tool("update_provider", {"provider_id": pid, "name": "new-name"})
        result = await mcp.call_tool("get_provider", {"provider_id": pid})
        assert "new-name" in _get_text(result)

    @pytest.mark.asyncio
    async def test_delete(self, mcp, db):
        register_manager_tools(mcp, db)
        pid = await db.create_provider(Provider(name="temp", provider_type="openai"))
        await mcp.call_tool("delete_provider", {"provider_id": pid})
        assert await db.get_provider(pid) is None


# ---------------------------------------------------------------------------
# Model tools
# ---------------------------------------------------------------------------

class TestModelTools:
    @pytest.mark.asyncio
    async def test_create_model_with_full_config(self, mcp, db):
        register_manager_tools(mcp, db)
        pid = await db.create_provider(Provider(name="prov", provider_type="openai"))

        result = await mcp.call_tool("create_model", {
            "name": "gpt-4o",
            "provider_id": pid,
            "display_name": "GPT-4o",
            "max_retries": 5,
            "cooldown_seconds": 120,
            "cooldown_failure_threshold": 2,
        })
        assert "created with id=" in _get_text(result)

        fetched = await db.get_model(1)
        assert fetched is not None
        assert fetched.cooldown_seconds == 120
        assert fetched.cooldown_failure_threshold == 2

    @pytest.mark.asyncio
    async def test_list_models(self, mcp, db):
        register_manager_tools(mcp, db)
        pid = await db.create_provider(Provider(name="prov", provider_type="openai"))
        await db.create_model(Model(name="gpt-4o", provider_id=pid))
        await db.create_model(Model(name="claude-3", provider_id=pid))

        result = await mcp.call_tool("list_models", {})
        text = _get_text(result)
        assert "gpt-4o" in text
        assert "claude-3" in text

    @pytest.mark.asyncio
    async def test_create_model_nonexistent_provider(self, mcp, db):
        register_manager_tools(mcp, db)
        with pytest.raises(Exception):
            await mcp.call_tool("create_model", {"name": "bad", "provider_id": 999})

    @pytest.mark.asyncio
    async def test_update_model(self, mcp, db):
        """Test update_model MCP tool with various fields."""
        register_manager_tools(mcp, db)
        pid = await db.create_provider(Provider(name="prov", provider_type="openai"))
        mid = await db.create_model(Model(name="old-name", provider_id=pid))

        # Update name + cooldown
        result = await mcp.call_tool("update_model", {
            "model_id": mid,
            "name": "new-name",
            "display_name": "New Display",
            "cooldown_seconds": 300,
        })
        assert "updated" in _get_text(result).lower()

        fetched = await db.get_model(mid)
        assert fetched.name == "new-name"
        assert fetched.display_name == "New Display"
        assert fetched.cooldown_seconds == 300

    @pytest.mark.asyncio
    async def test_update_model_noop(self, mcp, db):
        """Test update_model with no fields returns no-op."""
        register_manager_tools(mcp, db)
        pid = await db.create_provider(Provider(name="prov", provider_type="openai"))
        mid = await db.create_model(Model(name="m", provider_id=pid))

        result = await mcp.call_tool("update_model", {"model_id": mid})
        assert "No updates" in _get_text(result)

    @pytest.mark.asyncio
    async def test_update_model_disable(self, mcp, db):
        """Test update_model is_enabled=False disables the model."""
        register_manager_tools(mcp, db)
        pid = await db.create_provider(Provider(name="prov", provider_type="openai"))
        mid = await db.create_model(Model(name="m", provider_id=pid, is_enabled=True))

        await mcp.call_tool("update_model", {
            "model_id": mid,
            "is_enabled": False,
        })

        # Verify via list_models
        result = await mcp.call_tool("list_models", {})
        text = _get_text(result)
        assert "disabled" in text

    @pytest.mark.asyncio
    async def test_delete_nonexistent_model(self, mcp, db):
        register_manager_tools(mcp, db)
        # Deleting non-existent model should succeed gracefully (idempotent)
        result = await mcp.call_tool("delete_model", {"model_id": 999})
        assert "deleted" in _get_text(result).lower()


# ---------------------------------------------------------------------------
# Group tools
# ---------------------------------------------------------------------------

class TestGroupTools:
    @pytest.mark.asyncio
    async def test_create_group_direct(self, mcp, db):
        """Test create_group MCP tool directly."""
        register_manager_tools(mcp, db)

        result = await mcp.call_tool("create_group", {
            "name": "test-group",
            "description": "A test group",
            "is_enabled": True,
        })
        text = _get_text(result)
        assert "created with id=" in text
        assert "test-group" in text

        # Verify via get_group
        gid = int(text.split("id=")[1])
        result = await mcp.call_tool("get_group", {"group_id": gid})
        assert "test-group" in _get_text(result)
        assert "A test group" in _get_text(result)

    @pytest.mark.asyncio
    async def test_create_group_disabled(self, mcp, db):
        """Test create_group with is_enabled=False."""
        register_manager_tools(mcp, db)

        result = await mcp.call_tool("create_group", {
            "name": "disabled-group",
            "description": "Disabled",
            "is_enabled": False,
        })
        text = _get_text(result)
        assert "created with id=" in text

    @pytest.mark.asyncio
    async def test_create_and_add_models(self, mcp, db):
        register_manager_tools(mcp, db)
        pid = await db.create_provider(Provider(name="prov", provider_type="openai"))
        mid = await db.create_model(Model(name="gpt-4o", provider_id=pid))
        gid = await db.create_group(ModelGroup(name="primary"))

        result = await mcp.call_tool("add_model_to_group", {
            "group_id": gid,
            "model_id": mid,
            "weight": 2.0,
        })
        assert "added" in _get_text(result).lower()

        result = await mcp.call_tool("get_group", {"group_id": gid})
        assert "gpt-4o" in _get_text(result)

    @pytest.mark.asyncio
    async def test_list_groups(self, mcp, db):
        register_manager_tools(mcp, db)
        await db.create_group(ModelGroup(name="group-a"))
        await db.create_group(ModelGroup(name="group-b"))

        result = await mcp.call_tool("list_groups", {})
        text = _get_text(result)
        assert "group-a" in text
        assert "group-b" in text

    @pytest.mark.asyncio
    async def test_update_model_weight(self, mcp, db):
        register_manager_tools(mcp, db)
        pid = await db.create_provider(Provider(name="prov", provider_type="openai"))
        mid = await db.create_model(Model(name="gpt-4o", provider_id=pid))
        gid = await db.create_group(ModelGroup(name="primary"))
        await db.add_model_to_group(gid, mid, 1.0)

        await mcp.call_tool("update_model_weight", {"group_id": gid, "model_id": mid, "weight": 5.0})
        models = await db.get_group_models(gid)
        assert models[0].weight == 5.0

    @pytest.mark.asyncio
    async def test_update_group(self, mcp, db):
        """Test update_group MCP tool."""
        register_manager_tools(mcp, db)
        gid = await db.create_group(ModelGroup(name="old-group", description="old desc"))

        result = await mcp.call_tool("update_group", {
            "group_id": gid,
            "name": "new-group",
            "description": "new desc",
        })
        assert "updated" in _get_text(result).lower()

        result = await mcp.call_tool("get_group", {"group_id": gid})
        text = _get_text(result)
        assert "new-group" in text
        assert "new desc" in text

    @pytest.mark.asyncio
    async def test_update_group_noop(self, mcp, db):
        """Test update_group with no fields returns no-op."""
        register_manager_tools(mcp, db)
        gid = await db.create_group(ModelGroup(name="g"))

        result = await mcp.call_tool("update_group", {"group_id": gid})
        assert "No updates" in _get_text(result)

    @pytest.mark.asyncio
    async def test_update_group_disable(self, mcp, db):
        """Test update_group is_enabled=False disables the group."""
        register_manager_tools(mcp, db)
        gid = await db.create_group(ModelGroup(name="g", is_enabled=True))

        await mcp.call_tool("update_group", {
            "group_id": gid,
            "is_enabled": False,
        })

        # Verify in list output
        result = await mcp.call_tool("list_groups", {})
        text = _get_text(result)
        assert "disabled" in text

    @pytest.mark.asyncio
    async def test_delete_nonexistent_group(self, mcp, db):
        register_manager_tools(mcp, db)
        result = await mcp.call_tool("delete_group", {"group_id": 999})
        assert "deleted" in _get_text(result).lower()


# ---------------------------------------------------------------------------
# Stats tools
# ---------------------------------------------------------------------------

class TestStatsTools:
    @pytest.mark.asyncio
    async def test_query_model_stats_empty(self, mcp, db):
        register_stats_tools(mcp, db)
        result = await mcp.call_tool("query_model_stats", {"model_id": 999})
        assert "No stats found" in _get_text(result)

    @pytest.mark.asyncio
    async def test_query_group_stats_empty(self, mcp, db):
        register_stats_tools(mcp, db)
        result = await mcp.call_tool("query_group_stats", {"group_id": 999})
        assert "No stats found" in _get_text(result)

    @pytest.mark.asyncio
    async def test_query_group_stats_with_data(self, mcp, db):
        register_stats_tools(mcp, db)
        pid = await db.create_provider(Provider(name="prov", provider_type="openai"))
        mid = await db.create_model(Model(name="m", provider_id=pid))
        gid = await db.create_group(ModelGroup(name="g"))
        await db.add_model_to_group(gid, mid, 1.0)
        await db.create_call_log(CallLog(model_id=mid, group_id=gid, status="success"))

        result = await mcp.call_tool("query_group_stats", {"group_id": gid})
        text = _get_text(result)
        assert "Total calls" in text
        assert "Group [" in text
        assert "Total cost" in text

    @pytest.mark.asyncio
    async def test_query_messages(self, mcp, db):
        register_stats_tools(mcp, db)
        await db.create_call_log(CallLog(status="success"))
        await db.create_call_log(CallLog(status="error"))

        result = await mcp.call_tool("query_messages", {"limit": 10})
        text = _get_text(result)
        assert "Found 2" in text or "2 message" in text

    @pytest.mark.asyncio
    async def test_query_messages_filtered(self, mcp, db):
        register_stats_tools(mcp, db)
        await db.create_call_log(CallLog(status="success"))
        await db.create_call_log(CallLog(status="error"))

        result = await mcp.call_tool("query_messages", {"status": "error"})
        text = _get_text(result)
        assert "error" in text or "Found" in text

    @pytest.mark.asyncio
    async def test_query_messages_empty(self, mcp, db):
        register_stats_tools(mcp, db)
        result = await mcp.call_tool("query_messages", {"model_id": 999})
        assert "No messages found" in _get_text(result)

    @pytest.mark.asyncio
    async def test_query_cost_summary(self, mcp, db):
        register_stats_tools(mcp, db)
        await db.create_call_log(CallLog(status="success", prompt_tokens=100, completion_tokens=50, total_tokens=150))
        result = await mcp.call_tool("query_cost_summary", {"days": 30})
        assert "Total calls" in _get_text(result)
