"""Phase 4 I1 — `WorkspaceRoot` path-containment regressions.

Covers the three escape classes the resolver must reject (absolute
paths, ``..``-rooted relatives, symlinks pointing outside) plus the
happy-path resolution / relative-display helpers.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from noeta.tools.fs._workspace import WorkspaceEscape, WorkspaceRoot


def _make_root(tmp_path: Path) -> WorkspaceRoot:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return WorkspaceRoot.from_path(workspace)


def test_from_path_canonicalises_and_keeps_display(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    given = str(workspace) + "/."  # trailing /. is a noop canonicalisation
    root = WorkspaceRoot.from_path(given)
    assert root.root == Path(os.path.realpath(workspace))
    assert root.display == given


def test_from_path_rejects_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceEscape):
        WorkspaceRoot.from_path(tmp_path / "nope")


def test_from_path_rejects_file(tmp_path: Path) -> None:
    f = tmp_path / "f.txt"
    f.write_text("x")
    with pytest.raises(WorkspaceEscape):
        WorkspaceRoot.from_path(f)


def test_resolve_relative_inside(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    (root.root / "sub").mkdir()
    target = root.resolve("sub")
    assert target == root.root / "sub"


def test_resolve_empty_path_rejected(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    with pytest.raises(WorkspaceEscape):
        root.resolve("")


def test_resolve_absolute_outside_rejected(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    with pytest.raises(WorkspaceEscape):
        root.resolve("/etc/passwd")


def test_resolve_dotdot_rejected(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    (tmp_path / "outside.txt").write_text("secret")
    with pytest.raises(WorkspaceEscape):
        root.resolve("../outside.txt")


def test_resolve_symlink_to_outside_rejected(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    link = root.root / "link"
    link.symlink_to(outside)
    with pytest.raises(WorkspaceEscape):
        root.resolve("link")


def test_resolve_symlink_to_inside_allowed(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    target = root.root / "inside.txt"
    target.write_text("hello")
    link = root.root / "link"
    link.symlink_to(target)
    assert root.resolve("link") == target


def test_resolve_root_itself(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    assert root.resolve(".") == root.root


def test_resolve_absolute_inside_allowed(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    (root.root / "ok.txt").write_text("x")
    inside_abs = str(root.root / "ok.txt")
    assert root.resolve(inside_abs) == root.root / "ok.txt"


def test_relative_renders_root_as_dot(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    assert root.relative(root.root) == "."


def test_relative_renders_nested_posix(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    (root.root / "a" / "b").mkdir(parents=True)
    nested = root.root / "a" / "b"
    assert root.relative(nested) == "a/b"


def test_resolve_rejects_non_string(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    with pytest.raises(WorkspaceEscape):
        root.resolve(None)  # type: ignore[arg-type]
