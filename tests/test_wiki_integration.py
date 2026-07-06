"""Integration tests for MemWiki — requires real LLM API access.

These tests exercise the full pipeline:
  MCP Tools → Memory Agent → BotflowLLM → LLM API → file operations

Run with:
  pytest tests/test_wiki_integration.py -v --tb=short -m integration

Environment:
  STEPFUN_API_KEY must be set (or use the key below for local testing).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from botflow.common.exceptions import PathTraversalError
import botflow.core as core_mod
from botflow.router import CooldownManager, GroupRouter
from botflow.storage.db import Database
from botflow.storage.models import Model, ModelGroup, Provider
from botflow.wiki.agent import MemoryAgent
from botflow.wiki.skills import get_skill_prompt
from botflow.wiki.tools_impl import set_wiki_dir, read_file, write_file, wiki_ripgrep, wiki_glob

pytestmark = pytest.mark.integration

# stepfun API config
API_KEY = "4xXSZHnfTt8SYCgkplyNuXr9hiJ89TD7ykvByvDjNQb3NnsBe3iDpWqXumxCMZNbE"
BASE_URL = "https://api.stepfun.com/step_plan/v1"
MODEL_NAME = "step-3.7-flash"
GROUP_NAME = "fast"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def wiki_dir(tmp_path: Path) -> Path:
    """Create a temporary MemWiki directory."""
    wiki = tmp_path / "MemWiki"
    wiki.mkdir()
    for sub in ("sources", "concepts", "entities", "syntheses"):
        (wiki / sub).mkdir()
    (wiki / "index.md").write_text("# MemWiki Index\n", encoding="utf-8")
    (wiki / "log.md").write_text("# MemWiki Log\n", encoding="utf-8")
    return wiki


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    """Create and initialize a test database with stepfun provider + model + group."""
    db_path = tmp_path / "test.db"
    database = Database(db_path)
    await database.initialize()

    # Create provider
    provider = Provider(
        name="stepfun",
        provider_type="openai",
        api_key=API_KEY,
        base_url=BASE_URL,
    )
    provider_id = await database.create_provider(provider)

    # Create model
    model = Model(
        name=MODEL_NAME,
        provider_id=provider_id,
        display_name="Step 3.7 Flash",
    )
    model_id = await database.create_model(model)

    # Create model group
    group = ModelGroup(name=GROUP_NAME, description="Fast model group")
    group_id = await database.create_group(group)

    # Add model to group
    await database.add_model_to_group(group_id, model_id, weight=1.0)

    yield database
    core_mod._db = None
    await database.close()


@pytest.fixture
async def agent(db: Database, wiki_dir: Path) -> MemoryAgent:
    """Create a MemoryAgent with real LLM."""
    core_mod._db = db
    cooldown = CooldownManager()
    # Find the fast group ID
    groups = await db.list_groups()
    fast_group = next(g for g in groups if g.name == GROUP_NAME)
    router = GroupRouter(group_id=fast_group.id, db=db, cooldown_manager=cooldown)
    return MemoryAgent(wiki_dir=wiki_dir, model_group=GROUP_NAME, router=router)


# ---------------------------------------------------------------------------
# Path safety integration tests (no LLM needed)
# ---------------------------------------------------------------------------

class TestPathSafetyIntegration:
    """Path safety tests using real file system."""

    def test_safe_path_various(self, wiki_dir: Path):
        from botflow.wiki.tools_impl import _safe_path
        set_wiki_dir(wiki_dir)

        # Valid paths
        assert _safe_path("concepts/rag.md") == wiki_dir / "concepts" / "rag.md"
        assert _safe_path("index.md") == wiki_dir / "index.md"
        assert _safe_path("sources/paper.md") == wiki_dir / "sources" / "paper.md"

        # Invalid paths
        with pytest.raises(PathTraversalError):
            _safe_path("/etc/passwd")
        with pytest.raises(PathTraversalError):
            _safe_path("../../../secret")
        with pytest.raises(PathTraversalError):
            _safe_path("concepts/../../data/db.db")


class TestToolsIntegration:
    """Tool tests with real file system."""

    def test_read_write_roundtrip(self, wiki_dir: Path):
        set_wiki_dir(wiki_dir)
        content = "---\ntype: concept\ntitle: Test\ntags: [test]\n---\n\nHello world"
        write_file.invoke({"path": "concepts/test.md", "content": content})
        result = read_file.invoke({"path": "concepts/test.md"})
        assert "Hello world" in result
        assert "Test" in result

    def test_ripgrep_finds_content(self, wiki_dir: Path):
        set_wiki_dir(wiki_dir)
        write_file.invoke({"path": "concepts/ai.md", "content": "Artificial Intelligence is transforming everything"})
        write_file.invoke({"path": "concepts/ml.md", "content": "Machine Learning is a subset of AI"})
        result = wiki_ripgrep.invoke({"pattern": "AI"})
        assert "ai.md" in result or "ml.md" in result

    def test_glob_finds_files(self, wiki_dir: Path):
        set_wiki_dir(wiki_dir)
        write_file.invoke({"path": "concepts/a.md", "content": "a"})
        write_file.invoke({"path": "concepts/b.md", "content": "b"})
        write_file.invoke({"path": "sources/c.md", "content": "c"})
        result = wiki_glob.invoke({"pattern": "concepts/*.md"})
        assert "a.md" in result
        assert "b.md" in result
        assert "c.md" not in result


# ---------------------------------------------------------------------------
# LLM integration tests (require API key)
# ---------------------------------------------------------------------------

class TestMemoryAgentIntegration:
    """End-to-end Memory Agent tests with real LLM."""

    @pytest.mark.asyncio
    async def test_remember(self, agent: MemoryAgent):
        """remember: store a knowledge entry."""
        result = await agent.run(
            "remember",
            "title=RAG\ncontent=Retrieval Augmented Generation combines search with LLM generation.\ntags=rag,llm",
        )
        print(f"\n[remember] Result: {result}")

        # Verify file was created
        wiki = agent.wiki_dir
        concept_files = list((wiki / "concepts").glob("*.md"))
        assert len(concept_files) >= 1, f"No concept files created. Result: {result}"

        # Verify content
        content = concept_files[0].read_text(encoding="utf-8")
        assert "RAG" in content or "rag" in content.lower()

    @pytest.mark.asyncio
    async def test_recall(self, agent: MemoryAgent):
        """recall: retrieve a knowledge entry."""
        # First create something to recall
        wiki = agent.wiki_dir
        (wiki / "concepts" / "transformer.md").write_text(
            "---\ntype: concept\ntitle: Transformer\ntags: [transformer, attention]\n---\n\nA neural network architecture.",
            encoding="utf-8",
        )

        result = await agent.run("recall", "title=Transformer")
        print(f"\n[recall] Result: {result}")
        assert "Transformer" in result or "transformer" in result.lower()

    @pytest.mark.asyncio
    async def test_query(self, agent: MemoryAgent):
        """query: search the wiki."""
        wiki = agent.wiki_dir
        (wiki / "concepts" / "attention.md").write_text(
            "---\ntype: concept\ntitle: Attention\ntags: [attention]\n---\n\nSelf-attention mechanism.",
            encoding="utf-8",
        )

        result = await agent.run("query", "query=attention")
        print(f"\n[query] Result: {result}")
        assert "attention" in result.lower()

    @pytest.mark.asyncio
    async def test_learn(self, agent: MemoryAgent):
        """learn: ingest raw text content."""
        result = await agent.run(
            "learn",
            "content=The Transformer architecture was introduced in 'Attention Is All You Need' (Vaswani et al., 2017). It uses self-attention instead of recurrence.",
        )
        print(f"\n[learn] Result: {result}")

        # Verify source file was created
        source_files = list((agent.wiki_dir / "sources").glob("*.md"))
        assert len(source_files) >= 1, f"No source files created. Result: {result}"

    @pytest.mark.asyncio
    async def test_full_workflow(self, agent: MemoryAgent):
        """Full workflow: remember → recall → query."""
        # 1. Remember
        r1 = await agent.run(
            "remember",
            "title=LangChain\ncontent=LangChain is a framework for building LLM applications.\ntags=langchain,llm,framework",
        )
        print(f"\n[full-workflow] remember: {r1}")

        # 2. Recall
        r2 = await agent.run("recall", "title=LangChain")
        print(f"[full-workflow] recall: {r2}")
        assert "LangChain" in r2

        # 3. Query
        r3 = await agent.run("query", "query=LangChain")
        print(f"[full-workflow] query: {r3}")
        assert "LangChain" in r3.lower() or "langchain" in r3.lower()
