"""Tests for workspace path management (100% coverage)."""

from __future__ import annotations

from pathlib import Path

from botflow.workspace import get_workspace_path, init_workspace


def test_get_workspace_custom():
    p = get_workspace_path("~/my-workspace")
    assert isinstance(p, Path)
    assert p == Path("~/my-workspace").expanduser().resolve()


def test_get_workspace_default():
    p = get_workspace_path()
    assert p == Path.cwd().resolve()


def test_get_workspace_none_implied():
    p = get_workspace_path(None)
    assert p == Path.cwd().resolve()


def test_init_workspace_creates_structure(tmp_path: Path):
    ws = tmp_path / "wf"
    result = init_workspace(ws)
    assert result == ws
    assert (ws / "data").is_dir()
    assert (ws / "logs").is_dir()


def test_init_workspace_idempotent(tmp_path: Path):
    ws = tmp_path / "wf"
    init_workspace(ws)
    # Second call must not raise even though dirs exist.
    init_workspace(ws)
    assert (ws / "data").is_dir()
