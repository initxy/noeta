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


# ---------------------------------------------------------------------------
# Authorized write roots — the host's widening seam (extra_roots)
# ---------------------------------------------------------------------------


def _neighbour(tmp_path: Path) -> Path:
    other = tmp_path / "neighbour"
    other.mkdir()
    return Path(os.path.realpath(other))


def test_extra_root_admits_the_authorized_directory(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    other = _neighbour(tmp_path)
    with pytest.raises(WorkspaceEscape):
        root.resolve(str(other / "a.txt"))
    widened = root.with_extra_roots([other])
    assert widened.resolve(str(other / "a.txt")) == other / "a.txt"


def test_extra_root_covers_subdirectories(tmp_path: Path) -> None:
    # Authorizing a directory authorizes everything beneath it — the whole
    # point of ruling on a directory instead of on each file.
    root = _make_root(tmp_path).with_extra_roots([_neighbour(tmp_path)])
    deep = tmp_path / "neighbour" / "src" / "nested" / "a.py"
    assert root.resolve(str(deep)) == Path(os.path.realpath(deep.parent.parent.parent)) / "src" / "nested" / "a.py"


def test_extra_root_does_not_admit_a_string_prefix_sibling(tmp_path: Path) -> None:
    # ``/…/neighbour-old`` starts with ``/…/neighbour`` as a STRING but is a
    # different directory. A prefix check would leak it; containment is
    # component-wise.
    root = _make_root(tmp_path).with_extra_roots([_neighbour(tmp_path)])
    sibling = tmp_path / "neighbour-old"
    sibling.mkdir()
    with pytest.raises(WorkspaceEscape):
        root.resolve(str(sibling / "a.txt"))


def test_extra_root_does_not_admit_an_unrelated_directory(tmp_path: Path) -> None:
    root = _make_root(tmp_path).with_extra_roots([_neighbour(tmp_path)])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with pytest.raises(WorkspaceEscape):
        root.resolve(str(elsewhere / "a.txt"))


def test_extra_root_symlink_into_it_is_resolved_then_admitted(tmp_path: Path) -> None:
    # Containment is checked AFTER realpath, so a link is judged by where it
    # lands, not by where it sits — in both directions.
    root = _make_root(tmp_path)
    other = _neighbour(tmp_path)
    (other / "a.txt").write_text("x")
    os.symlink(other / "a.txt", root.root / "link.txt")
    with pytest.raises(WorkspaceEscape):
        root.resolve("link.txt")
    assert root.with_extra_roots([other]).resolve("link.txt") == other / "a.txt"


def test_relative_display_falls_back_to_absolute_outside_the_workspace(
    tmp_path: Path,
) -> None:
    root = _make_root(tmp_path)
    other = _neighbour(tmp_path)
    assert root.relative(root.root / "a.txt") == "a.txt"
    assert root.relative(other / "a.txt") == (other / "a.txt").as_posix()
