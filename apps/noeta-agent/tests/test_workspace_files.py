"""Session-workspace file surface: host directory traversal / hidden-entry
pruning / top-level exclusion / escape prevention."""
from __future__ import annotations

import os
from pathlib import Path

from noeta.agent.host.workspace_files import (
    WorkspaceFileEntry,
    list_files,
    resolve_within,
)


def _make_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "session-1"
    ws.mkdir()
    (ws / "result.txt").write_text("hello", encoding="utf-8")
    (ws / "sub").mkdir()
    (ws / "sub" / "b.md").write_text("world", encoding="utf-8")
    # hidden: noeta runtime metadata, must not show up in the file surface
    (ws / ".noeta" / "skills").mkdir(parents=True)
    (ws / ".noeta" / "meta.json").write_text("{}", encoding="utf-8")
    (ws / ".hidden.txt").write_text("x", encoding="utf-8")
    return ws


def test_list_files_lists_products_only(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    files = list_files(ws)
    paths = [f.path for f in files]
    assert paths == ["result.txt", "sub/b.md"]  # sorted, includes subdirs, skips hidden
    assert all(isinstance(f, WorkspaceFileEntry) for f in files)
    r = next(f for f in files if f.path == "result.txt")
    assert r.size == len("hello") and r.mtime > 0


def test_list_files_excludes_top_level_and_knowledge_symlink(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    # knowledge is a symlink to an external directory (mounted knowledge
    # base) — it must not be traversed
    external = tmp_path / "kb"
    (external / "space").mkdir(parents=True)
    (external / "space" / "INDEX.md").write_text("idx", encoding="utf-8")
    os.symlink(external / "space", ws / "knowledge")
    files = list_files(ws, exclude_top_level={"knowledge"})
    assert [f.path for f in files] == ["result.txt", "sub/b.md"]
    assert not any("knowledge" in f.path for f in files)


def test_list_files_missing_dir_is_empty(tmp_path: Path) -> None:
    assert list_files(tmp_path / "nope") == []


def test_resolve_within_accepts_inside(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    assert resolve_within(ws, "result.txt") == (ws / "result.txt").resolve()
    assert resolve_within(ws, "sub/b.md") == (ws / "sub" / "b.md").resolve()


def test_resolve_within_rejects_escape(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    assert resolve_within(ws, "../session-1/../../etc/passwd") is None
    assert resolve_within(ws, "/etc/passwd") is None  # absolute path
    assert resolve_within(ws, "") is None
    # a symlink inside the workspace pointing outside: containment is judged
    # after realpath normalization → rejected (prevents unauthorized reads)
    outside = tmp_path / "secret.txt"
    outside.write_text("s", encoding="utf-8")
    os.symlink(outside, ws / "leak")
    assert resolve_within(ws, "leak") is None
