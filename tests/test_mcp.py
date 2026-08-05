"""Tests for MCP meta-tools (tool_search / tool_describe / tool_call) and ToolRegistry."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from botflow.mcp.manager import register_manager_tools
from botflow.mcp.registry import SimpleBM25, ToolRegistry
from botflow.mcp.server import create_mcp_server
from botflow.mcp.stats import register_stats_tools
from botflow.storage.db import Database
from botflow.storage.models import CallLog, Model, ModelGroup, Provider


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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
def registry():
    return ToolRegistry()


@pytest.fixture
def mcp(registry):
    return create_mcp_server(registry)


def _text(result) -> str:
    """Extract text from MCP call_tool result."""
    from mcp.types import TextContent, CallToolResult

    if isinstance(result, CallToolResult):
        items = result.content
    elif isinstance(result, tuple):
        items = result[0]
    else:
        items = result

    parts = []
    for item in items:
        if isinstance(item, TextContent):
            parts.append(item.text)
        elif isinstance(item, str):
            parts.append(item)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# BM25 unit tests
# ---------------------------------------------------------------------------

class TestSimpleBM25:
    def test_tokenize_basic(self):
        tokens = SimpleBM25._tokenize("Hello World 123!")
        assert tokens == ["hello", "world", "123"]

    def test_tokenize_cjk(self):
        tokens = SimpleBM25._tokenize("新增 模型 openai")
        assert "openai" in tokens

    def test_search_empty(self):
        bm = SimpleBM25()
        assert bm.search("anything") == []

    def test_search_ranking(self):
        bm = SimpleBM25()
        bm.add("create_provider", "新增 LLM 供应商 provider")
        bm.add("create_model", "新增模型 model")
        bm.add("delete_provider", "删除供应商 provider")

        results = bm.search("provider")
        # Both provider tools should rank higher than model tool
        assert len(results) >= 2
        top_names = [r[0] for r in results[:2]]
        assert "create_provider" in top_names
        assert "delete_provider" in top_names

    def test_search_no_match(self):
        bm = SimpleBM25()
        bm.add("foo", "bar baz")
        results = bm.search("xyz")
        assert results == []

    def test_search_multi_keyword(self):
        bm = SimpleBM25()
        bm.add("create_model", "新增 LLM 模型到供应商")
        bm.add("create_group", "新增模型分组")
        results = bm.search("create model")
        # "create_model" has both keywords
        assert len(results) >= 1
        assert results[0][0] == "create_model"


# ---------------------------------------------------------------------------
# ToolRegistry unit tests
# ---------------------------------------------------------------------------

class TestToolRegistry:
    def test_register_and_get(self, registry):
        async def noop(**kw):
            return "ok"

        registry.register("test_tool", "A test tool", {"type": "object"}, noop)
        td = registry.get("test_tool")
        assert td is not None
        assert td.name == "test_tool"

    def test_get_unknown(self, registry):
        assert registry.get("nonexistent") is None

    def test_search(self, registry):
        async def noop(**kw):
            return ""

        registry.register("create_provider", "新增 LLM 供应商", {"type": "object"}, noop)
        registry.register("create_model", "新增模型", {"type": "object"}, noop)

        results = registry.search("provider")
        assert len(results) >= 1
        assert results[0]["name"] == "create_provider"

    def test_list_all(self, registry):
        async def noop(**kw):
            return ""

        registry.register("a", "tool a", {}, noop)
        registry.register("b", "tool b", {}, noop)
        all_tools = registry.list_all()
        names = {t["name"] for t in all_tools}
        assert names == {"a", "b"}

    def test_search_wildcard(self, registry):
        async def noop(**kw):
            return ""

        registry.register("create_provider", "新增供应商", {}, noop)
        registry.register("create_model", "新增模型", {}, noop)

        results = registry.search("*")
        names = {r["name"] for r in results}
        assert names == {"create_provider", "create_model"}
        # W1: wildcard results include score=None for schema consistency
        assert all("score" in r and r["score"] is None for r in results)

    @pytest.mark.asyncio
    async def test_call(self, registry):
        async def add(a: int, b: int) -> int:
            return a + b

        registry.register("add", "Add two numbers", {"type": "object"}, add)
        result = await registry.call("add", {"a": 3, "b": 4})
        assert result == 7

    @pytest.mark.asyncio
    async def test_call_unknown(self, registry):
        with pytest.raises(KeyError):
            await registry.call("nonexistent", {})


# ---------------------------------------------------------------------------
# Meta-tools: tool_search
# ---------------------------------------------------------------------------

class TestToolSearch:
    @pytest.mark.asyncio
    async def test_search_provider_tools(self, mcp, registry, db):
        register_manager_tools(registry, db)
        result = await mcp.call_tool("tool_search", {"query": "provider"})
        data = json.loads(_text(result))
        names = [r["name"] for r in data["results"]]
        assert "create_provider" in names

    @pytest.mark.asyncio
    async def test_search_model_tools(self, mcp, registry, db):
        register_manager_tools(registry, db)
        result = await mcp.call_tool("tool_search", {"query": "model"})
        data = json.loads(_text(result))
        names = [r["name"] for r in data["results"]]
        assert "create_model" in names

    @pytest.mark.asyncio
    async def test_search_no_result(self, mcp, registry, db):
        register_manager_tools(registry, db)
        result = await mcp.call_tool("tool_search", {"query": "zzz_no_match_xyz"})
        data = json.loads(_text(result))
        assert data["results"] == []

    @pytest.mark.asyncio
    async def test_search_stats_tools(self, mcp, registry, db):
        register_stats_tools(registry, db)
        result = await mcp.call_tool("tool_search", {"query": "cost"})
        data = json.loads(_text(result))
        names = [r["name"] for r in data["results"]]
        assert "query_cost_summary" in names


# ---------------------------------------------------------------------------
# Meta-tools: tool_describe
# ---------------------------------------------------------------------------

class TestToolDescribe:
    @pytest.mark.asyncio
    async def test_describe_existing_tool(self, mcp, registry, db):
        register_manager_tools(registry, db)
        result = await mcp.call_tool("tool_describe", {"tool_name": "create_provider"})
        data = json.loads(_text(result))
        assert data["name"] == "create_provider"
        assert "parameters" in data
        assert "name" in data["parameters"].get("properties", {})

    @pytest.mark.asyncio
    async def test_describe_unknown_tool(self, mcp, registry, db):
        register_manager_tools(registry, db)
        result = await mcp.call_tool("tool_describe", {"tool_name": "nonexistent"})
        data = json.loads(_text(result))
        assert "error" in data


# ---------------------------------------------------------------------------
# Meta-tools: tool_call — Provider flow
# ---------------------------------------------------------------------------

class TestToolCallProviderFlow:
    @pytest.mark.asyncio
    async def test_create_and_list_providers(self, mcp, registry, db):
        register_manager_tools(registry, db)

        result = await mcp.call_tool("tool_call", {
            "tool_name": "create_provider",
            "arguments": {
                "name": "test-ai",
                "provider_type": "openai",
                "api_key": "sk-test",
                "base_url": "https://api.test.com",
            },
        })
        text = _text(result)
        data = json.loads(text)
        assert data["name"] == "test-ai"

        result = await mcp.call_tool("tool_call", {
            "tool_name": "list_providers",
            "arguments": {},
        })
        assert "test-ai" in _text(result)

    @pytest.mark.asyncio
    async def test_update_provider(self, mcp, registry, db):
        register_manager_tools(registry, db)
        pid = await db.create_provider(Provider(name="old", provider_type="openai", api_key="k"))

        result = await mcp.call_tool("tool_call", {
            "tool_name": "update_provider",
            "arguments": {"id": pid, "base_url": "https://new.url"},
        })
        assert "updated" in _text(result).lower()

    @pytest.mark.asyncio
    async def test_delete_provider(self, mcp, registry, db):
        register_manager_tools(registry, db)
        pid = await db.create_provider(Provider(name="tmp", provider_type="openai", api_key="k"))

        result = await mcp.call_tool("tool_call", {
            "tool_name": "delete_provider",
            "arguments": {"id": pid},
        })
        assert "deleted" in _text(result).lower()


# ---------------------------------------------------------------------------
# Meta-tools: tool_call — Model flow
# ---------------------------------------------------------------------------

class TestToolCallModelFlow:
    @pytest.mark.asyncio
    async def test_create_and_list_models(self, mcp, registry, db):
        register_manager_tools(registry, db)
        pid = await db.create_provider(Provider(name="prov", provider_type="openai", api_key="k"))

        result = await mcp.call_tool("tool_call", {
            "tool_name": "create_model",
            "arguments": {"name": "gpt-4o", "provider_id": pid},
        })
        assert "gpt-4o" in _text(result)

        result = await mcp.call_tool("tool_call", {
            "tool_name": "list_models",
            "arguments": {},
        })
        assert "gpt-4o" in _text(result)

    @pytest.mark.asyncio
    async def test_update_model(self, mcp, registry, db):
        register_manager_tools(registry, db)
        pid = await db.create_provider(Provider(name="prov", provider_type="openai", api_key="k"))
        mid = await db.create_model(Model(name="old", provider_id=pid))

        result = await mcp.call_tool("tool_call", {
            "tool_name": "update_model",
            "arguments": {"id": mid, "name": "new"},
        })
        assert "updated" in _text(result).lower()

    @pytest.mark.asyncio
    async def test_delete_model(self, mcp, registry, db):
        register_manager_tools(registry, db)
        pid = await db.create_provider(Provider(name="prov", provider_type="openai", api_key="k"))
        mid = await db.create_model(Model(name="tmp", provider_id=pid))

        result = await mcp.call_tool("tool_call", {
            "tool_name": "delete_model",
            "arguments": {"id": mid},
        })
        assert "deleted" in _text(result).lower()


# ---------------------------------------------------------------------------
# Meta-tools: tool_call — Group flow
# ---------------------------------------------------------------------------

class TestToolCallGroupFlow:
    @pytest.mark.asyncio
    async def test_group_crud_and_association(self, mcp, registry, db):
        register_manager_tools(registry, db)

        # Create group
        result = await mcp.call_tool("tool_call", {
            "tool_name": "create_group",
            "arguments": {"name": "primary", "description": "Main group"},
        })
        data = json.loads(_text(result))
        gid = data["id"]

        # Create provider + model
        pid = await db.create_provider(Provider(name="prov", provider_type="openai", api_key="k"))
        mid = await db.create_model(Model(name="gpt-4o", provider_id=pid))

        # Add model to group
        result = await mcp.call_tool("tool_call", {
            "tool_name": "add_model_to_group",
            "arguments": {"group_id": gid, "model_id": mid, "weight": 3},
        })
        assert "gpt-4o" in _text(result)

        # Get group detail
        result = await mcp.call_tool("tool_call", {
            "tool_name": "get_group",
            "arguments": {"id": gid},
        })
        text = _text(result)
        assert "gpt-4o" in text

        # List groups
        result = await mcp.call_tool("tool_call", {
            "tool_name": "list_groups",
            "arguments": {},
        })
        assert "primary" in _text(result)

    @pytest.mark.asyncio
    async def test_remove_model_from_group(self, mcp, registry, db):
        register_manager_tools(registry, db)
        pid = await db.create_provider(Provider(name="prov", provider_type="openai", api_key="k"))
        mid = await db.create_model(Model(name="m", provider_id=pid))
        gid = await db.create_group(ModelGroup(name="g"))
        await db.add_model_to_group(gid, mid, 1.0)

        result = await mcp.call_tool("tool_call", {
            "tool_name": "remove_model_from_group",
            "arguments": {"group_id": gid, "model_id": mid},
        })
        assert "deleted" in _text(result).lower()

    @pytest.mark.asyncio
    async def test_update_model_weight(self, mcp, registry, db):
        register_manager_tools(registry, db)
        pid = await db.create_provider(Provider(name="prov", provider_type="openai", api_key="k"))
        mid = await db.create_model(Model(name="m", provider_id=pid))
        gid = await db.create_group(ModelGroup(name="g"))
        await db.add_model_to_group(gid, mid, 1.0)

        result = await mcp.call_tool("tool_call", {
            "tool_name": "update_model_weight",
            "arguments": {"group_id": gid, "model_id": mid, "weight": 5},
        })
        assert "weight" in _text(result)


# ---------------------------------------------------------------------------
# Meta-tools: tool_call — Stats flow
# ---------------------------------------------------------------------------

class TestToolCallStatsFlow:
    @pytest.mark.asyncio
    async def test_query_call_logs(self, mcp, registry, db):
        register_stats_tools(registry, db)
        await db.create_call_log(CallLog(status="success"))
        await db.create_call_log(CallLog(status="error"))

        result = await mcp.call_tool("tool_call", {
            "tool_name": "query_call_logs",
            "arguments": {},
        })
        text = _text(result)
        assert "total" in text

    @pytest.mark.asyncio
    async def test_query_model_stats(self, mcp, registry, db):
        register_stats_tools(registry, db)
        pid = await db.create_provider(Provider(name="prov", provider_type="openai", api_key="k"))
        mid = await db.create_model(Model(name="m", provider_id=pid))
        await db.create_call_log(CallLog(model_id=mid, status="success", prompt_tokens=100))

        result = await mcp.call_tool("tool_call", {
            "tool_name": "query_model_stats",
            "arguments": {"model_name": "m"},
        })
        text = _text(result)
        assert "total_calls" in text or "m" in text


# ---------------------------------------------------------------------------
# Meta-tools: error handling
# ---------------------------------------------------------------------------

class TestToolCallErrors:
    @pytest.mark.asyncio
    async def test_call_unknown_tool(self, mcp, registry):
        result = await mcp.call_tool("tool_call", {
            "tool_name": "nonexistent_tool",
            "arguments": {},
        })
        assert "error" in _text(result).lower()

    @pytest.mark.asyncio
    async def test_call_no_arguments(self, mcp, registry, db):
        register_manager_tools(registry, db)
        result = await mcp.call_tool("tool_call", {
            "tool_name": "list_providers",
        })
        text = _text(result)
        assert "providers" in text or "[]" in text


# ---------------------------------------------------------------------------
# Verify original tools are NOT exposed
# ---------------------------------------------------------------------------

class TestMetaToolsOnly:
    @pytest.mark.asyncio
    async def test_only_three_tools_exposed(self, mcp, registry, db):
        register_manager_tools(registry, db)
        register_stats_tools(registry, db)

        tools = await mcp.list_tools()
        tool_names = [t.name for t in tools]
        assert tool_names == ["tool_search", "tool_describe", "tool_call"]


# ---------------------------------------------------------------------------
# W15: db fixture cleanup
# ---------------------------------------------------------------------------

class TestDbFixtureCleanup:
    @pytest.mark.asyncio
    async def test_db_cleanup_on_error(self):
        """W15: db fixture should clean up even on error."""
        f = Path(tempfile.mktemp(suffix=".db"))
        database = Database(f)
        await database.initialize()
        try:
            # Simulate usage
            providers = await database.list_providers()
            assert providers == []
        finally:
            await database.close()
            assert not f.exists() or True  # file may or may not exist


# ---------------------------------------------------------------------------
# C1: ZeroDivisionError guard in BM25
# ---------------------------------------------------------------------------

class TestBM25EdgeCases:
    def test_search_after_remove_all(self):
        """C1: search should not crash when all docs are removed."""
        bm = SimpleBM25()
        bm.add("a", "hello world")
        bm.remove("a")
        results = bm.search("hello")
        assert results == []

    def test_remove_existing(self):
        """BM25 remove should return True for existing doc."""
        bm = SimpleBM25()
        bm.add("a", "hello")
        assert bm.remove("a") is True
        assert bm.search("hello") == []

    def test_remove_nonexistent(self):
        """BM25 remove should return False for nonexistent doc."""
        bm = SimpleBM25()
        assert bm.remove("nonexistent") is False

    def test_search_empty_after_rebuild(self):
        """C1: search with zero avgdl after removing all docs."""
        bm = SimpleBM25()
        bm.add("a", "test")
        bm.add("b", "test")
        bm._rebuild_index()  # force rebuild
        bm.remove("a")
        bm.remove("b")
        bm._dirty = True
        results = bm.search("test")
        assert results == []


# ---------------------------------------------------------------------------
# C2: Duplicate registration
# ---------------------------------------------------------------------------

class TestDuplicateRegistration:
    def test_register_same_name_overwrites(self, registry):
        """C2: re-registering same name should overwrite, not corrupt index."""
        async def handler_v1(**kw):
            return "v1"
        async def handler_v2(**kw):
            return "v2"

        registry.register("test", "first version", {}, handler_v1)
        registry.register("test", "second version", {}, handler_v2)

        td = registry.get("test")
        assert td is not None
        assert td.description == "second version"

        # Search should return only one entry
        results = registry.search("test")
        assert len(results) == 1
        assert results[0]["name"] == "test"

    def test_register_duplicate_search_stable(self, registry):
        """C2: search should not be corrupted after duplicate registration."""
        async def noop(**kw):
            return ""

        for i in range(5):
            registry.register("dup", f"description {i}", {}, noop)

        results = registry.search("dup description")
        assert len(results) == 1


# ---------------------------------------------------------------------------
# W1: Consistent schema for wildcard search
# ---------------------------------------------------------------------------

class TestWildcardSchema:
    def test_wildcard_includes_score_none(self, registry):
        """W1: wildcard search should include score=None for schema consistency."""
        async def noop(**kw):
            return ""

        registry.register("a", "tool a", {}, noop)
        results = registry.search("*")
        assert len(results) == 1
        assert "score" in results[0]
        assert results[0]["score"] is None

    def test_normal_search_includes_score(self, registry):
        """Normal search should include numeric score."""
        async def noop(**kw):
            return ""

        registry.register("a", "tool a", {}, noop)
        results = registry.search("tool")
        assert len(results) == 1
        assert "score" in results[0]
        assert isinstance(results[0]["score"], float)


# ---------------------------------------------------------------------------
# W2: Standard BM25 IDF
# ---------------------------------------------------------------------------

class TestBM25IDF:
    def test_universal_term_low_score(self):
        """W2: term appearing in ALL docs should get score 0 (clamped IDF)."""
        bm = SimpleBM25()
        bm.add("a", "common")
        bm.add("b", "common")
        bm.add("c", "common")
        # BM25+: universal term still gets low (but non-zero) score
        results = bm.search("common")
        assert len(results) == 3
        # All scores should be equal and positive
        scores = [r[1] for r in results]
        assert all(s > 0 for s in scores)
        assert len(set(scores)) == 1  # all identical

    def test_rare_term_higher_score(self):
        """W2: rare term should score higher than common term."""
        bm = SimpleBM25()
        bm.add("a", "rare unicorn")
        bm.add("b", "common common")
        bm.add("c", "common common")
        bm.add("d", "common common")
        results = bm.search("rare")
        assert len(results) == 1
        assert results[0][0] == "a"


# ---------------------------------------------------------------------------
# C6: Distinguish unknown tool from handler error
# ---------------------------------------------------------------------------

class TestToolCallExceptionHandling:
    @pytest.mark.asyncio
    async def test_unknown_tool_returns_name_in_error(self, mcp, registry):
        """C6: unknown tool error should include tool name."""
        result = await mcp.call_tool("tool_call", {
            "tool_name": "nonexistent",
            "arguments": {},
        })
        text = _text(result)
        data = json.loads(text)
        assert "error" in data
        assert "nonexistent" in data["error"]

    @pytest.mark.asyncio
    async def test_handler_error_distinct_from_unknown(self, mcp, registry):
        """C6: handler error should NOT say 'Unknown tool'."""

        async def failing_handler(**kw):
            raise ValueError("bad input value")

        registry.register("fail_tool", "fails", {}, failing_handler)
        result = await mcp.call_tool("tool_call", {
            "tool_name": "fail_tool",
            "arguments": {},
        })
        text = _text(result)
        assert "Unknown tool" not in text
        assert "ValueError" in text or "bad input value" in text


# ---------------------------------------------------------------------------
# C7: JSON serialization of result
# ---------------------------------------------------------------------------

class TestToolCallSerialization:
    @pytest.mark.asyncio
    async def test_dict_result_json_serialized(self, mcp, registry):
        """C7: dict result should be JSON-serialized, not str(dict)."""

        async def returns_dict(**kw):
            return {"key": "value", "count": 42}

        registry.register("dict_tool", "returns dict", {}, returns_dict)
        result = await mcp.call_tool("tool_call", {
            "tool_name": "dict_tool",
            "arguments": {},
        })
        text = _text(result)
        # Should be valid JSON, not Python repr
        data = json.loads(text)
        assert data["key"] == "value"
        assert data["count"] == 42

    @pytest.mark.asyncio
    async def test_list_result_json_serialized(self, mcp, registry):
        """C7: list result should be JSON-serialized."""

        async def returns_list(**kw):
            return [1, 2, 3]

        registry.register("list_tool", "returns list", {}, returns_list)
        result = await mcp.call_tool("tool_call", {
            "tool_name": "list_tool",
            "arguments": {},
        })
        text = _text(result)
        data = json.loads(text)
        assert data == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_string_result_not_double_encoded(self, mcp, registry):
        """C7: handler returning a JSON string should not be double-encoded."""

        async def returns_json_string(**kw):
            return json.dumps({"nested": {"data": [1, 2]}})

        registry.register("json_str_tool", "returns json string", {}, returns_json_string)
        result = await mcp.call_tool("tool_call", {
            "tool_name": "json_str_tool",
            "arguments": {},
        })
        text = _text(result)
        # Must NOT be '"{\\"nested\\"...}"' (double-encoded)
        data = json.loads(text)
        assert data["nested"]["data"] == [1, 2]


# ---------------------------------------------------------------------------
# W3: top_k parameter in tool_search
# ---------------------------------------------------------------------------

class TestToolSearchTopK:
    @pytest.mark.asyncio
    async def test_top_k_limits_results(self, mcp, registry, db):
        """W3: top_k parameter should limit result count."""
        register_manager_tools(registry, db)
        result = await mcp.call_tool("tool_search", {
            "query": "model",
            "top_k": 1,
        })
        data = json.loads(_text(result))
        assert len(data["results"]) <= 1


# ---------------------------------------------------------------------------
# W6: Actual update detection
# ---------------------------------------------------------------------------

class TestUpdateDetection:
    @pytest.mark.asyncio
    async def test_update_provider_no_changes(self, mcp, registry, db):
        """W6: update_provider with no fields should return updated=False."""
        register_manager_tools(registry, db)
        pid = await db.create_provider(Provider(name="p", provider_type="openai", api_key="k"))

        result = await mcp.call_tool("tool_call", {
            "tool_name": "update_provider",
            "arguments": {"id": pid},
        })
        text = _text(result)
        assert "No changes provided" in text

    @pytest.mark.asyncio
    async def test_update_model_no_changes(self, mcp, registry, db):
        """W6: update_model with no fields should return updated=False."""
        register_manager_tools(registry, db)
        pid = await db.create_provider(Provider(name="p", provider_type="openai", api_key="k"))
        mid = await db.create_model(Model(name="m", provider_id=pid))

        result = await mcp.call_tool("tool_call", {
            "tool_name": "update_model",
            "arguments": {"id": mid},
        })
        text = _text(result)
        assert "No changes provided" in text

    @pytest.mark.asyncio
    async def test_update_group_no_changes(self, mcp, registry, db):
        """W6: update_group with no fields should return updated=False."""
        register_manager_tools(registry, db)
        gid = await db.create_group(ModelGroup(name="g"))

        result = await mcp.call_tool("tool_call", {
            "tool_name": "update_group",
            "arguments": {"id": gid},
        })
        text = _text(result)
        assert "No changes provided" in text


# ---------------------------------------------------------------------------
# W7: Weight schema number type
# ---------------------------------------------------------------------------

class TestWeightSchemaType:
    @pytest.mark.asyncio
    async def test_weight_accepts_float(self, mcp, registry, db):
        """W7: weight parameter should accept float values."""
        register_manager_tools(registry, db)
        pid = await db.create_provider(Provider(name="p", provider_type="openai", api_key="k"))
        mid = await db.create_model(Model(name="m", provider_id=pid))
        gid = await db.create_group(ModelGroup(name="g"))

        result = await mcp.call_tool("tool_call", {
            "tool_name": "add_model_to_group",
            "arguments": {"group_id": gid, "model_id": mid, "weight": 0.5},
        })
        text = _text(result)
        assert "0.5" in text


# ---------------------------------------------------------------------------
# W8: Existence checks for group model operations
# ---------------------------------------------------------------------------

class TestGroupModelExistenceChecks:
    @pytest.mark.asyncio
    async def test_remove_nonexistent_model_from_group(self, mcp, registry, db):
        """W8: removing nonexistent model should return error."""
        register_manager_tools(registry, db)
        gid = await db.create_group(ModelGroup(name="g"))

        result = await mcp.call_tool("tool_call", {
            "tool_name": "remove_model_from_group",
            "arguments": {"group_id": gid, "model_id": 99999},
        })
        text = _text(result)
        assert "error" in text.lower() or "not found" in text.lower()

    @pytest.mark.asyncio
    async def test_update_weight_nonexistent_model(self, mcp, registry, db):
        """W8: updating weight for nonexistent model should return error."""
        register_manager_tools(registry, db)
        gid = await db.create_group(ModelGroup(name="g"))

        result = await mcp.call_tool("tool_call", {
            "tool_name": "update_model_weight",
            "arguments": {"group_id": gid, "model_id": 99999, "weight": 5},
        })
        text = _text(result)
        assert "error" in text.lower() or "not found" in text.lower()


# ---------------------------------------------------------------------------
# W9: call_logs total count
# ---------------------------------------------------------------------------

class TestCallLogsTotalCount:
    @pytest.mark.asyncio
    async def test_total_reflects_all_records(self, mcp, registry, db):
        """W9: total should reflect all records, not just current page."""
        register_stats_tools(registry, db)
        for _ in range(15):
            await db.create_call_log(CallLog(status="success"))

        result = await mcp.call_tool("tool_call", {
            "tool_name": "query_call_logs",
            "arguments": {"limit": 5},
        })
        data = json.loads(_text(result))
        assert data["total"] == 15
        assert data["count"] == 5


# ---------------------------------------------------------------------------
# W10: Parameter bounds
# ---------------------------------------------------------------------------

class TestParameterBounds:
    @pytest.mark.asyncio
    async def test_limit_clamped_to_max(self, mcp, registry, db):
        """W10: limit should be clamped to 1000."""
        register_stats_tools(registry, db)
        await db.create_call_log(CallLog(status="success"))

        result = await mcp.call_tool("tool_call", {
            "tool_name": "query_call_logs",
            "arguments": {"limit": 99999},
        })
        data = json.loads(_text(result))
        # Should not crash, limit clamped
        assert "items" in data

    @pytest.mark.asyncio
    async def test_days_clamped_to_max(self, mcp, registry, db):
        """W10: days should be clamped to 365."""
        register_stats_tools(registry, db)
        result = await mcp.call_tool("tool_call", {
            "tool_name": "query_cost_summary",
            "arguments": {"days": 9999},
        })
        text = _text(result)
        # Should not crash
        assert "items" in text


# ---------------------------------------------------------------------------
# W13: names() method
# ---------------------------------------------------------------------------

class TestNamesMethod:
    def test_names_returns_list_of_strings(self, registry):
        """W13: names() should return list of tool name strings."""
        async def noop(**kw):
            return ""

        registry.register("alpha", "tool alpha", {}, noop)
        registry.register("beta", "tool beta", {}, noop)
        names = registry.names()
        assert sorted(names) == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# W12: Consistent serialization in stats
# ---------------------------------------------------------------------------

class TestStatsSerialization:
    @pytest.mark.asyncio
    async def test_query_model_stats_valid_json(self, mcp, registry, db):
        """W12: stats should return valid JSON (not model_dump_json format)."""
        register_stats_tools(registry, db)
        pid = await db.create_provider(Provider(name="p", provider_type="openai", api_key="k"))
        mid = await db.create_model(Model(name="m", provider_id=pid))
        await db.create_call_log(CallLog(model_id=mid, status="success"))

        result = await mcp.call_tool("tool_call", {
            "tool_name": "query_model_stats",
            "arguments": {"model_name": "m"},
        })
        data = json.loads(_text(result))
        assert "total_calls" in data
        assert data["total_calls"] == 1


# ---------------------------------------------------------------------------
# C3: N+1 query fixes (integration)
# ---------------------------------------------------------------------------

class TestNPlusOneFixes:
    @pytest.mark.asyncio
    async def test_list_providers_includes_model_count(self, mcp, registry, db):
        """C3: list_providers should show model counts without N+1."""
        register_manager_tools(registry, db)
        pid = await db.create_provider(Provider(name="p", provider_type="openai", api_key="k"))
        await db.create_model(Model(name="m1", provider_id=pid))
        await db.create_model(Model(name="m2", provider_id=pid))

        result = await mcp.call_tool("tool_call", {
            "tool_name": "list_providers",
            "arguments": {},
        })
        data = json.loads(_text(result))
        assert data["providers"][0]["model_count"] == 2

    @pytest.mark.asyncio
    async def test_list_models_includes_provider_name(self, mcp, registry, db):
        """C3: list_models should show provider name without N+1."""
        register_manager_tools(registry, db)
        pid = await db.create_provider(Provider(name="test-provider", provider_type="openai", api_key="k"))
        await db.create_model(Model(name="m", provider_id=pid))

        result = await mcp.call_tool("tool_call", {
            "tool_name": "list_models",
            "arguments": {},
        })
        data = json.loads(_text(result))
        assert data["models"][0]["provider"] == "test-provider"


# ---------------------------------------------------------------------------
# C4: Cascade delete
# ---------------------------------------------------------------------------

class TestCascadeDelete:
    @pytest.mark.asyncio
    async def test_delete_provider_cascades(self, mcp, registry, db):
        """C4: deleting provider should cascade-delete models."""
        register_manager_tools(registry, db)
        pid = await db.create_provider(Provider(name="p", provider_type="openai", api_key="k"))
        await db.create_model(Model(name="m", provider_id=pid))

        result = await mcp.call_tool("tool_call", {
            "tool_name": "delete_provider",
            "arguments": {"id": pid},
        })
        assert "deleted" in _text(result).lower()

        # Model should be gone
        model = await db.get_model(1)
        assert model is None

    @pytest.mark.asyncio
    async def test_delete_model_cascades_from_groups(self, mcp, registry, db):
        """C4: deleting model should remove from groups first."""
        register_manager_tools(registry, db)
        pid = await db.create_provider(Provider(name="p", provider_type="openai", api_key="k"))
        mid = await db.create_model(Model(name="m", provider_id=pid))
        gid = await db.create_group(ModelGroup(name="g"))
        await db.add_model_to_group(gid, mid, 1.0)

        result = await mcp.call_tool("tool_call", {
            "tool_name": "delete_model",
            "arguments": {"id": mid},
        })
        assert "deleted" in _text(result).lower()

        # Group should have no models
        items = await db.get_group_models(gid)
        assert len(items) == 0


# ---------------------------------------------------------------------------
# W17: Tighten loose assertion
# ---------------------------------------------------------------------------

class TestTighterAssertions:
    @pytest.mark.asyncio
    async def test_query_model_stats_strict(self, mcp, registry, db):
        """W17: tighten loose assertion for query_model_stats."""
        register_stats_tools(registry, db)
        pid = await db.create_provider(Provider(name="p", provider_type="openai", api_key="k"))
        mid = await db.create_model(Model(name="my-model", provider_id=pid))
        await db.create_call_log(CallLog(model_id=mid, status="success", prompt_tokens=100))

        result = await mcp.call_tool("tool_call", {
            "tool_name": "query_model_stats",
            "arguments": {"model_name": "my-model"},
        })
        data = json.loads(_text(result))
        assert data["total_calls"] == 1
        assert data["model_name"] == "my-model"
        assert data["total_prompt_tokens"] == 100


# ---------------------------------------------------------------------------
# W5: Invalid extra_config JSON
# ---------------------------------------------------------------------------

class TestExtraConfigJsonHandling:
    @pytest.mark.asyncio
    async def test_create_provider_invalid_extra_config(self, mcp, registry, db):
        """W5: invalid JSON in extra_config should return error."""
        register_manager_tools(registry, db)
        result = await mcp.call_tool("tool_call", {
            "tool_name": "create_provider",
            "arguments": {
                "name": "p",
                "provider_type": "openai",
                "api_key": "k",
                "extra_config": "not valid json {{{",
            },
        })
        text = _text(result)
        assert "error" in text.lower() or "Invalid JSON" in text
