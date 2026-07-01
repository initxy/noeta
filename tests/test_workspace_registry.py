"""WorkspaceRegistry — the host-side workspace (project) config store.

The default workspace is synthesized from ``default_dir`` (never persisted,
undeletable); user-added projects validate (absolute / existing / dir),
persist to JSON, and resolve id ↔ path for the session-creation + sidebar paths.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from noeta.agent.host.workspace_registry import (
    WorkspaceConfigError,
    WorkspaceRegistry,
)


def _reg(tmp_path: Path) -> tuple[WorkspaceRegistry, Path, Path]:
    default = tmp_path / "default"
    default.mkdir()
    reg = WorkspaceRegistry(tmp_path / "workspaces.json", default_dir=default)
    reg.load()
    return reg, default, tmp_path


def test_default_entry_present_and_undeletable(tmp_path: Path) -> None:
    reg, default, _ = _reg(tmp_path)
    rows = reg.list_all()
    assert len(rows) == 1
    d = rows[0]
    assert d.is_default is True
    assert d.path == str(default.resolve())
    # The default cannot be removed.
    assert reg.remove(d.id) is False
    # Resolving the default id (and its path) returns the default path.
    assert reg.resolve(d.id) == str(default.resolve())


def test_add_lists_resolves_and_names(tmp_path: Path) -> None:
    reg, _default, root = _reg(tmp_path)
    proj = root / "myproj"
    proj.mkdir()
    entry = reg.add(path=str(proj))
    assert entry.name == "myproj"
    assert entry.path == str(proj.resolve())
    assert entry.is_default is False

    # Listed after the default (default first).
    rows = reg.list_all()
    assert [r.is_default for r in rows] == [True, False]
    assert rows[1].id == entry.id

    # Resolve by id and name lookup by path.
    assert reg.resolve(entry.id) == str(proj.resolve())
    assert reg.name_for_path(str(proj.resolve())) == "myproj"
    # An unknown ref resolves to None (caller falls back to host default).
    assert reg.resolve("nope") is None


def test_add_idempotent_on_same_path(tmp_path: Path) -> None:
    reg, _default, root = _reg(tmp_path)
    proj = root / "p"
    proj.mkdir()
    a = reg.add(path=str(proj), name="first")
    b = reg.add(path=str(proj), name="second")
    assert a.id == b.id  # same canonical path → same id
    assert len([r for r in reg.list_all() if not r.is_default]) == 1


def test_add_rejects_bad_paths(tmp_path: Path) -> None:
    reg, _default, root = _reg(tmp_path)
    with pytest.raises(WorkspaceConfigError):
        reg.add(path="")  # empty
    with pytest.raises(WorkspaceConfigError):
        reg.add(path="relative/dir")  # not absolute
    with pytest.raises(WorkspaceConfigError):
        reg.add(path=str(root / "does-not-exist"))  # missing
    afile = root / "afile.txt"
    afile.write_text("x", encoding="utf-8")
    with pytest.raises(WorkspaceConfigError):
        reg.add(path=str(afile))  # not a directory


def test_remove_and_persistence(tmp_path: Path) -> None:
    reg, default, root = _reg(tmp_path)
    proj = root / "persisted"
    proj.mkdir()
    entry = reg.add(path=str(proj))

    # A fresh registry over the same file reloads the user entry.
    reg2 = WorkspaceRegistry(tmp_path / "workspaces.json", default_dir=default)
    reg2.load()
    assert any(r.id == entry.id for r in reg2.list_all())

    # Remove persists too.
    assert reg2.remove(entry.id) is True
    reg3 = WorkspaceRegistry(tmp_path / "workspaces.json", default_dir=default)
    reg3.load()
    assert all(r.is_default for r in reg3.list_all())
    assert reg2.remove("unknown-id") is False
