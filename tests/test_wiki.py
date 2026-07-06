"""Tests for botflow.wiki module — path safety, tools, skills, dream."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def wiki_dir(tmp_path: Path) -> Path:
    """Create a temporary MemWiki directory structure."""
    wiki = tmp_path / "MemWiki"
    wiki.mkdir()
    (wiki / "sources").mkdir()
    (wiki / "concepts").mkdir()
    (wiki / "entities").mkdir()
    (wiki / "syntheses").mkdir()
    (wiki / "index.md").write_text("# MemWiki Index\n", encoding="utf-8")
    (wiki / "log.md").write_text("# MemWiki Log\n", encoding="utf-8")
    return wiki


@pytest.fixture
def sample_concept(wiki_dir: Path) -> Path:
    """Create a sample concept file."""
    p = wiki_dir / "concepts" / "rag.md"
    p.write_text(
        "---\ntype: concept\ntitle: RAG\ndescription: Retrieval Augmented Generation\ntags: [rag, llm]\ntimestamp: 2026-07-03T10:00:00Z\n---\n\nRAG is a technique...\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def sample_source(wiki_dir: Path) -> Path:
    """Create a sample source file."""
    p = wiki_dir / "sources" / "attention-paper.md"
    p.write_text(
        "---\ntype: source\ntitle: Attention Is All You Need\ndescription: Transformer architecture paper\ntags: [transformer, attention]\ntimestamp: 2026-07-03T10:00:00Z\n---\n\nThe dominant sequence transduction models...\n",
        encoding="utf-8",
    )
    return p


# ---------------------------------------------------------------------------
# Path safety tests
# ---------------------------------------------------------------------------

class TestPathSafety:
    """Test _safe_path() sandbox enforcement."""

    def test_set_wiki_dir(self, wiki_dir: Path):
        from botflow.wiki.tools_impl import set_wiki_dir, _get_wiki_dir
        set_wiki_dir(wiki_dir)
        assert _get_wiki_dir() == wiki_dir

    def test_relative_path_allowed(self, wiki_dir: Path):
        from botflow.wiki.tools_impl import set_wiki_dir, _safe_path
        set_wiki_dir(wiki_dir)
        result = _safe_path("concepts/rag.md")
        assert result == wiki_dir / "concepts" / "rag.md"

    def test_absolute_path_rejected(self, wiki_dir: Path):
        from botflow.wiki.tools_impl import set_wiki_dir, _safe_path
        from botflow.common.exceptions import PathTraversalError
        set_wiki_dir(wiki_dir)
        with pytest.raises(PathTraversalError):
            _safe_path("/etc/passwd")

    def test_traversal_rejected(self, wiki_dir: Path):
        from botflow.wiki.tools_impl import set_wiki_dir, _safe_path
        from botflow.common.exceptions import PathTraversalError
        set_wiki_dir(wiki_dir)
        with pytest.raises(PathTraversalError):
            _safe_path("../../../etc/passwd")

    def test_dot_dot_rejected(self, wiki_dir: Path):
        from botflow.wiki.tools_impl import set_wiki_dir, _safe_path
        from botflow.common.exceptions import PathTraversalError
        set_wiki_dir(wiki_dir)
        with pytest.raises(PathTraversalError):
            _safe_path("concepts/../secrets.md")

    def test_index_md_allowed(self, wiki_dir: Path):
        from botflow.wiki.tools_impl import set_wiki_dir, _safe_path
        set_wiki_dir(wiki_dir)
        result = _safe_path("index.md")
        assert result == wiki_dir / "index.md"

    def test_nested_path_allowed(self, wiki_dir: Path):
        from botflow.wiki.tools_impl import set_wiki_dir, _safe_path
        set_wiki_dir(wiki_dir)
        result = _safe_path("sources/paper.md")
        assert result == wiki_dir / "sources" / "paper.md"


# ---------------------------------------------------------------------------
# Tool tests
# ---------------------------------------------------------------------------

class TestToolsImpl:
    """Test LangChain @tool implementations."""

    def test_read_file_exists(self, wiki_dir: Path, sample_concept: Path):
        from botflow.wiki.tools_impl import set_wiki_dir, read_file
        set_wiki_dir(wiki_dir)
        result = read_file.invoke({"path": "concepts/rag.md"})
        assert "RAG" in result
        assert "Retrieval Augmented Generation" in result

    def test_read_file_not_found(self, wiki_dir: Path):
        from botflow.wiki.tools_impl import set_wiki_dir, read_file
        set_wiki_dir(wiki_dir)
        result = read_file.invoke({"path": "concepts/nonexistent.md"})
        assert "not found" in result.lower()

    def test_read_file_traversal(self, wiki_dir: Path):
        from botflow.wiki.tools_impl import set_wiki_dir, read_file
        set_wiki_dir(wiki_dir)
        result = read_file.invoke({"path": "../data/botflow.db"})
        assert "Error" in result

    def test_write_file(self, wiki_dir: Path):
        from botflow.wiki.tools_impl import set_wiki_dir, write_file
        set_wiki_dir(wiki_dir)
        content = "---\ntype: concept\ntitle: Test\n---\n\nTest content"
        result = write_file.invoke({"path": "concepts/test.md", "content": content})
        assert "Written" in result
        assert (wiki_dir / "concepts" / "test.md").read_text(encoding="utf-8") == content

    def test_write_file_creates_dirs(self, wiki_dir: Path):
        from botflow.wiki.tools_impl import set_wiki_dir, write_file
        set_wiki_dir(wiki_dir)
        result = write_file.invoke({"path": "concepts/new/deep.md", "content": "deep"})
        assert "Written" in result
        assert (wiki_dir / "concepts" / "new" / "deep.md").exists()

    def test_write_file_traversal(self, wiki_dir: Path):
        from botflow.wiki.tools_impl import set_wiki_dir, write_file
        set_wiki_dir(wiki_dir)
        result = write_file.invoke({"path": "../evil.md", "content": "bad"})
        assert "Error" in result

    def test_wiki_ripgrep(self, wiki_dir: Path, sample_concept: Path):
        from botflow.wiki.tools_impl import set_wiki_dir, wiki_ripgrep
        set_wiki_dir(wiki_dir)
        result = wiki_ripgrep.invoke({"pattern": "RAG"})
        assert "rag.md" in result.lower() or "RAG" in result

    def test_wiki_ripgrep_no_match(self, wiki_dir: Path):
        from botflow.wiki.tools_impl import set_wiki_dir, wiki_ripgrep
        set_wiki_dir(wiki_dir)
        result = wiki_ripgrep.invoke({"pattern": "xyznonexistent"})
        assert "No matches" in result

    def test_wiki_ripgrep_with_path(self, wiki_dir: Path, sample_concept: Path, sample_source: Path):
        from botflow.wiki.tools_impl import set_wiki_dir, wiki_ripgrep
        set_wiki_dir(wiki_dir)
        result = wiki_ripgrep.invoke({"pattern": "Transformer", "path": "sources"})
        assert "Transformer" in result

    def test_wiki_glob(self, wiki_dir: Path, sample_concept: Path, sample_source: Path):
        from botflow.wiki.tools_impl import set_wiki_dir, wiki_glob
        set_wiki_dir(wiki_dir)
        result = wiki_glob.invoke({"pattern": "**/*.md"})
        assert "concepts/rag.md" in result
        assert "sources/attention-paper.md" in result

    def test_wiki_glob_no_match(self, wiki_dir: Path):
        from botflow.wiki.tools_impl import set_wiki_dir, wiki_glob
        set_wiki_dir(wiki_dir)
        result = wiki_glob.invoke({"pattern": "*.xyz"})
        assert "No matches" in result


# ---------------------------------------------------------------------------
# Skills tests
# ---------------------------------------------------------------------------

class TestSkills:
    """Test system prompt generation."""

    def test_get_skill_prompt_remember(self):
        from botflow.wiki.skills import get_skill_prompt
        prompt = get_skill_prompt("remember", "/tmp/wiki")
        assert "MemWiki" in prompt
        assert "/tmp/wiki" in prompt
        assert "write_file" in prompt

    def test_get_skill_prompt_recall(self):
        from botflow.wiki.skills import get_skill_prompt
        prompt = get_skill_prompt("recall", "/tmp/wiki")
        assert "read_file" in prompt
        assert "wiki_ripgrep" in prompt

    def test_get_skill_prompt_query(self):
        from botflow.wiki.skills import get_skill_prompt
        prompt = get_skill_prompt("query", "/tmp/wiki")
        assert "wiki_ripgrep" in prompt
        assert "全文搜索" in prompt

    def test_get_skill_prompt_learn(self):
        from botflow.wiki.skills import get_skill_prompt
        prompt = get_skill_prompt("learn", "/tmp/wiki")
        assert "source" in prompt
        assert "write_file" in prompt

    def test_get_skill_prompt_research(self):
        from botflow.wiki.skills import get_skill_prompt
        prompt = get_skill_prompt("research", "/tmp/wiki")
        assert "call_llm" in prompt
        assert "synthesis" in prompt

    def test_invalid_skill_raises(self):
        from botflow.wiki.skills import get_skill_prompt
        with pytest.raises(ValueError, match="Unknown skill"):
            get_skill_prompt("nonexistent", "/tmp/wiki")

    def test_all_skills_have_shared_prefix(self):
        from botflow.wiki.skills import SKILLS
        for name, prompt in SKILLS.items():
            assert "MemWiki" in prompt, f"Skill {name} missing shared prefix"
            assert "YAML frontmatter" in prompt, f"Skill {name} missing format spec"
            assert "路径规则" in prompt, f"Skill {name} missing path rules"


# ---------------------------------------------------------------------------
# Dream tests
# ---------------------------------------------------------------------------

class TestDream:
    """Test dream maintenance task."""

    def test_run_dream_creates_report(self, wiki_dir: Path, sample_concept: Path):
        from botflow.wiki.dream import run_dream
        import asyncio
        asyncio.run(run_dream(wiki_dir))
        log_content = (wiki_dir / "log.md").read_text(encoding="utf-8")
        assert "dream" in log_content.lower()
        assert "Dream Maintenance" in log_content

    def test_run_dream_detects_orphan(self, wiki_dir: Path):
        """An orphan page with no inbound links should be flagged."""
        from botflow.wiki.dream import run_dream
        orphan = wiki_dir / "concepts" / "orphan.md"
        orphan.write_text("---\ntype: concept\ntitle: Orphan\n---\n\nOrphan page\n", encoding="utf-8")
        import asyncio
        asyncio.run(run_dream(wiki_dir))
        log_content = (wiki_dir / "log.md").read_text(encoding="utf-8")
        assert "orphan" in log_content.lower()

    def test_run_dream_detects_broken_link(self, wiki_dir: Path):
        """A [[Link]] pointing to nonexistent page should be flagged."""
        from botflow.wiki.dream import run_dream
        page = wiki_dir / "concepts" / "linked.md"
        page.write_text("---\ntype: concept\ntitle: Linked\n---\n\nSee [[NonExistent]]\n", encoding="utf-8")
        import asyncio
        asyncio.run(run_dream(wiki_dir))
        log_content = (wiki_dir / "log.md").read_text(encoding="utf-8")
        assert "broken" in log_content.lower()

    def test_run_dream_refreshes_index(self, wiki_dir: Path, sample_concept: Path, sample_source: Path):
        """index.md should be regenerated with file entries."""
        from botflow.wiki.dream import run_dream
        import asyncio
        asyncio.run(run_dream(wiki_dir))
        index = (wiki_dir / "index.md").read_text(encoding="utf-8")
        assert "rag" in index.lower() or "RAG" in index
        assert "attention" in index.lower() or "Attention" in index

    def test_run_dream_handles_empty_wiki(self, wiki_dir: Path):
        """Dream should work on an empty wiki."""
        from botflow.wiki.dream import run_dream
        import asyncio
        asyncio.run(run_dream(wiki_dir))
        log_content = (wiki_dir / "log.md").read_text(encoding="utf-8")
        assert "Dream Maintenance" in log_content


# ---------------------------------------------------------------------------
# Workspace integration
# ---------------------------------------------------------------------------

class TestWorkspaceInit:
    """Test workspace MemWiki initialization."""

    def test_init_workspace_creates_memwiki(self, tmp_path: Path):
        from botflow.workspace import init_workspace
        init_workspace(tmp_path)
        assert (tmp_path / "MemWiki").is_dir()
        assert (tmp_path / "MemWiki" / "sources").is_dir()
        assert (tmp_path / "MemWiki" / "concepts").is_dir()
        assert (tmp_path / "MemWiki" / "entities").is_dir()
        assert (tmp_path / "MemWiki" / "syntheses").is_dir()
        assert (tmp_path / "MemWiki" / "index.md").is_file()
        assert (tmp_path / "MemWiki" / "log.md").is_file()

    def test_init_workspace_preserves_existing(self, tmp_path: Path):
        """Existing files should not be overwritten."""
        from botflow.workspace import init_workspace
        wiki = tmp_path / "MemWiki"
        wiki.mkdir()
        custom = wiki / "index.md"
        custom.write_text("custom", encoding="utf-8")
        init_workspace(tmp_path)
        assert custom.read_text(encoding="utf-8") == "custom"
