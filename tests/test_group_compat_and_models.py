"""Tests for _get_group_id backward compatibility and list_models format.

Covers:
  - _get_group_id: exact group name match, model-name lookup, fallback behavior
  - list_models: return format, filtering by provider/enabled
  - list_groups_with_models: batch format
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException

import botflow.core as core
from botflow.storage.db import Database
from botflow.storage.models import Model, ModelGroup, Provider


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    """Create a temporary database for each test."""
    f = Path(tempfile.mktemp(suffix=".db"))
    database = Database(f)
    await database.initialize()
    yield database
    await database.close()
    if f.exists():
        f.unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def _patch_core_db(db):
    """Temporarily set core._db for _get_group_id tests."""
    original = core._db
    core._db = db
    yield
    core._db = original


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _setup_group_with_model(
    db: Database,
    group_name: str = "primary",
    model_name: str = "gpt-4o",
    *,
    group_enabled: bool = True,
    model_enabled: bool = True,
) -> tuple[int, int, int]:
    """Create a provider, model, group, and associate them. Returns (pid, mid, gid)."""
    pid = await db.create_provider(
        Provider(name=f"prov-{group_name}", provider_type="openai")
    )
    mid = await db.create_model(
        Model(
            name=model_name,
            provider_id=pid,
            is_enabled=model_enabled,
        )
    )
    gid = await db.create_group(ModelGroup(name=group_name, is_enabled=group_enabled))
    await db.add_model_to_group(gid, mid, weight=1.0)
    return pid, mid, gid


# ===========================================================================
# _get_group_id backward compatibility tests
# ===========================================================================


class TestGetGroupId:
    """Test the 3-step resolution in _get_group_id."""

    @pytest.mark.asyncio
    async def test_exact_group_name_match(self, db):
        """Step 1: request model matches a group name exactly."""
        pid, mid, gid = await _setup_group_with_model(db, group_name="my-group")
        result = await core._get_group_id({"model": "my-group"})
        assert result == gid

    @pytest.mark.asyncio
    async def test_model_name_lookup(self, db):
        """Step 2: request model matches a model inside a group."""
        pid, mid, gid = await _setup_group_with_model(
            db, group_name="primary", model_name="gpt-4o"
        )
        result = await core._get_group_id({"model": "gpt-4o"})
        assert result == gid

    @pytest.mark.asyncio
    async def test_group_name_takes_precedence_over_model_name(self, db):
        """When a name matches both a group and a model, group name wins (step 1 before step 2)."""
        pid1, mid1, gid1 = await _setup_group_with_model(
            db, group_name="shared-name", model_name="model-x"
        )
        pid2, mid2, gid2 = await _setup_group_with_model(
            db, group_name="other-group", model_name="shared-name"
        )
        # "shared-name" matches group name in step 1 → gid1
        result = await core._get_group_id({"model": "shared-name"})
        assert result == gid1

    @pytest.mark.asyncio
    async def test_unknown_model_falls_back_to_first_group(self, db):
        """Step 3: unknown model name falls back to first enabled group (never 404)."""
        pid, mid, gid = await _setup_group_with_model(
            db, group_name="legacy-group", model_name="gpt-4o"
        )
        # Use loguru sink capture to verify deprecation warning
        import loguru
        messages = []
        sink_id = core.log.add(lambda m: messages.append(str(m)), format="{message}")
        try:
            result = await core._get_group_id({"model": "unknown-model"})
        finally:
            core.log.remove(sink_id)
        assert result == gid
        assert any("DEPRECATION" in m for m in messages)

    @pytest.mark.asyncio
    async def test_no_groups_raises_404(self, db):
        """When no groups exist, raises 404."""
        with pytest.raises(HTTPException) as exc_info:
            await core._get_group_id({"model": "anything"})
        assert exc_info.value.status_code == 404
        assert "No enabled groups" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_disabled_group_skipped_in_exact_match(self, db):
        """Disabled group is not matched in step 1."""
        pid, mid, gid = await _setup_group_with_model(
            db, group_name="disabled-group", group_enabled=False
        )
        # No enabled groups → should 404
        with pytest.raises(HTTPException) as exc_info:
            await core._get_group_id({"model": "disabled-group"})
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_disabled_model_falls_back(self, db):
        """Disabled model is not found in step 2, falls back to step 3."""
        pid, mid, gid = await _setup_group_with_model(
            db, group_name="primary", model_name="gpt-4o", model_enabled=False
        )
        # Step 2 won't find disabled model → falls back to step 3
        result = await core._get_group_id({"model": "gpt-4o"})
        # Falls back to the only enabled group
        assert result == gid

    @pytest.mark.asyncio
    async def test_missing_model_key_uses_empty_string(self, db):
        """When 'model' key is missing from request body, uses empty string → fallback."""
        pid, mid, gid = await _setup_group_with_model(db, group_name="default-group")
        result = await core._get_group_id({})
        assert result == gid

    @pytest.mark.asyncio
    async def test_group_name_match_before_model_lookup(self, db):
        """Verify step 1 fires before step 2: group name match prevents model lookup."""
        pid, mid, gid = await _setup_group_with_model(
            db, group_name="target-model", model_name="gpt-4o"
        )
        # "target-model" matches group name → step 1 succeeds, step 2 never needed
        result = await core._get_group_id({"model": "target-model"})
        assert result == gid

    @pytest.mark.asyncio
    async def test_empty_model_string_falls_back(self, db):
        """Empty model string falls back to first enabled group."""
        pid, mid, gid = await _setup_group_with_model(db, group_name="fallback-group")
        result = await core._get_group_id({"model": ""})
        assert result == gid


# ===========================================================================
# list_models return format tests
# ===========================================================================


class TestListModelsFormat:
    """Verify list_models returns proper Model objects with correct fields."""

    @pytest.mark.asyncio
    async def test_returns_model_objects(self, db):
        """list_models returns list[Model] instances with all fields populated."""
        pid = await db.create_provider(Provider(name="prov", provider_type="openai"))
        await db.create_model(
            Model(
                name="gpt-4o",
                provider_id=pid,
                display_name="GPT-4o",
                api_format="openai",
                max_retries=5,
                cooldown_seconds=120,
                extra_config={"proxy": "http://proxy"},
            )
        )
        models = await db.list_models()
        assert len(models) == 1
        m = models[0]
        assert isinstance(m, Model)
        assert m.name == "gpt-4o"
        assert m.display_name == "GPT-4o"
        assert m.api_format == "openai"
        assert m.max_retries == 5
        assert m.cooldown_seconds == 120
        assert m.extra_config == {"proxy": "http://proxy"}

    @pytest.mark.asyncio
    async def test_filter_by_provider_id(self, db):
        """list_models(provider_id=X) returns only models for that provider."""
        pid1 = await db.create_provider(Provider(name="p1", provider_type="openai"))
        pid2 = await db.create_provider(Provider(name="p2", provider_type="anthropic"))
        await db.create_model(Model(name="gpt-4o", provider_id=pid1))
        await db.create_model(Model(name="claude-3", provider_id=pid2))
        await db.create_model(Model(name="gpt-3.5", provider_id=pid1))

        p1_models = await db.list_models(provider_id=pid1)
        assert len(p1_models) == 2
        assert all(m.provider_id == pid1 for m in p1_models)

        p2_models = await db.list_models(provider_id=pid2)
        assert len(p2_models) == 1
        assert p2_models[0].name == "claude-3"

    @pytest.mark.asyncio
    async def test_filter_enabled_only(self, db):
        """list_models(enabled_only=True) returns only enabled models."""
        pid = await db.create_provider(Provider(name="prov", provider_type="openai"))
        await db.create_model(Model(name="enabled-model", provider_id=pid, is_enabled=True))
        await db.create_model(Model(name="disabled-model", provider_id=pid, is_enabled=False))

        enabled = await db.list_models(enabled_only=True)
        assert len(enabled) == 1
        assert enabled[0].name == "enabled-model"

    @pytest.mark.asyncio
    async def test_ordered_by_name(self, db):
        """list_models returns results ordered by name."""
        pid = await db.create_provider(Provider(name="prov", provider_type="openai"))
        await db.create_model(Model(name="zebra", provider_id=pid))
        await db.create_model(Model(name="alpha", provider_id=pid))
        await db.create_model(Model(name="mid", provider_id=pid))

        models = await db.list_models()
        names = [m.name for m in models]
        assert names == sorted(names)

    @pytest.mark.asyncio
    async def test_empty_result(self, db):
        """list_models on empty DB returns empty list."""
        assert await db.list_models() == []

    @pytest.mark.asyncio
    async def test_extra_config_default_empty(self, db):
        """Model with no extra_config gets empty dict."""
        pid = await db.create_provider(Provider(name="prov", provider_type="openai"))
        await db.create_model(Model(name="basic-model", provider_id=pid))
        models = await db.list_models()
        assert models[0].extra_config == {}

    @pytest.mark.asyncio
    async def test_combined_filters(self, db):
        """list_models with both provider_id and enabled_only."""
        pid1 = await db.create_provider(Provider(name="p1", provider_type="openai"))
        pid2 = await db.create_provider(Provider(name="p2", provider_type="anthropic"))
        await db.create_model(Model(name="m1", provider_id=pid1, is_enabled=True))
        await db.create_model(Model(name="m2", provider_id=pid1, is_enabled=False))
        await db.create_model(Model(name="m3", provider_id=pid2, is_enabled=True))

        result = await db.list_models(provider_id=pid1, enabled_only=True)
        assert len(result) == 1
        assert result[0].name == "m1"


# ===========================================================================
# list_groups_with_models format tests
# ===========================================================================


class TestListGroupsWithModels:
    """Verify the batch list_groups_with_models query returns correct format."""

    @pytest.mark.asyncio
    async def test_returns_list_of_dicts(self, db):
        """Each entry has group fields plus model_names list."""
        pid, mid, gid = await _setup_group_with_model(
            db, group_name="primary", model_name="gpt-4o"
        )
        result = await db.list_groups_with_models()
        assert len(result) == 1
        g = result[0]
        assert g["id"] == gid
        assert g["name"] == "primary"
        assert g["is_enabled"] is True
        assert "model_names" in g
        assert g["model_names"] == ["gpt-4o"]

    @pytest.mark.asyncio
    async def test_multiple_models_per_group(self, db):
        """Group with multiple models returns all model_names."""
        pid = await db.create_provider(Provider(name="prov", provider_type="openai"))
        m1 = await db.create_model(Model(name="gpt-4o", provider_id=pid))
        m2 = await db.create_model(Model(name="gpt-3.5", provider_id=pid))
        gid = await db.create_group(ModelGroup(name="multi"))
        await db.add_model_to_group(gid, m1, 1.0)
        await db.add_model_to_group(gid, m2, 1.0)

        result = await db.list_groups_with_models()
        assert len(result) == 1
        assert sorted(result[0]["model_names"]) == ["gpt-3.5", "gpt-4o"]

    @pytest.mark.asyncio
    async def test_disabled_groups_excluded(self, db):
        """Disabled groups are excluded by default."""
        pid, mid, gid = await _setup_group_with_model(
            db, group_name="active", group_enabled=True
        )
        pid2, mid2, gid2 = await _setup_group_with_model(
            db, group_name="inactive", group_enabled=False
        )
        result = await db.list_groups_with_models(enabled_only=True)
        assert len(result) == 1
        assert result[0]["name"] == "active"

    @pytest.mark.asyncio
    async def test_empty_groups_not_returned(self, db):
        """Groups with no models are not returned (JOIN eliminates them)."""
        await db.create_group(ModelGroup(name="empty-group"))
        result = await db.list_groups_with_models()
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_multiple_groups(self, db):
        """Multiple groups each with their models."""
        pid = await db.create_provider(Provider(name="prov", provider_type="openai"))
        m1 = await db.create_model(Model(name="model-a", provider_id=pid))
        m2 = await db.create_model(Model(name="model-b", provider_id=pid))

        g1 = await db.create_group(ModelGroup(name="group-a"))
        g2 = await db.create_group(ModelGroup(name="group-b"))
        await db.add_model_to_group(g1, m1, 1.0)
        await db.add_model_to_group(g2, m2, 1.0)

        result = await db.list_groups_with_models()
        assert len(result) == 2
        names = sorted([g["name"] for g in result])
        assert names == ["group-a", "group-b"]
