"""Workspace registry — host-side config store for selectable workspaces.

A
``Workspace = {id, display name, absolute path}`` lightweight registry, persisted
to a JSON file (default ``~/.noeta/workspaces.json``). Mirrors
:class:`~noeta.agent.host.mcp_registry.McpServerRegistry`: an in-memory map
written on every mutation, ``load()`` once at server startup.

This is the post-restructure home of the 0039 agent-layer registry (the original
``noeta.agent.host`` class was dropped when the backend collapsed to a single
workspace; see the ADR addendum). The codex-style frontend lists these as
"projects", creates a session in one, and groups the session pane by workspace.

Path authorization is **zero-whitelist** (0039 D3): noeta-agent is single-user,
locally trusted, so any absolute, existing, readable directory is accepted — the
same trust assumption as ``bypassPermissions``. A bad path raises
:class:`WorkspaceConfigError` (→ 400).

The **default workspace** is the host-fixed ``config.workspace_dir`` (the
"new conversation / scratch" bucket, ADR addendum revising 0039 D4). It is surfaced as a
registry entry (``is_default=True``), is never persisted to the JSON file, and
cannot be removed.

On-disk shape is a per-id record object::

    {
      "a1b2c3d4e5f6": {"name": "noeta", "path": "/Users/leo/Documents/noeta"},
      "f6e5d4c3b2a1": {"name": "blog", "path": "/Users/leo/src/blog"}
    }
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


__all__ = ["WorkspaceEntry", "WorkspaceRegistry", "WorkspaceConfigError"]


class WorkspaceConfigError(ValueError):
    """A workspace path is missing / not absolute / not a readable directory."""


def _workspace_id(canonical_path: str) -> str:
    """A stable id derived from the canonical absolute path (idempotent add)."""
    return hashlib.sha1(canonical_path.encode("utf-8")).hexdigest()[:12]


def _canonicalize(path: str) -> str:
    """Expand + absolutize a user-supplied path, validating it is a readable dir.

    Raises :class:`WorkspaceConfigError` (→ 400) on a non-absolute path, a
    missing path, or a non-directory. Symlinks are resolved so the stored path
    (welded into durable ``TaskHostBound``) is canonical.
    """
    raw = (path or "").strip()
    if not raw:
        raise WorkspaceConfigError("'path' is required")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise WorkspaceConfigError(f"path must be absolute: {raw!r}")
    if not p.exists():
        raise WorkspaceConfigError(f"path does not exist: {raw!r}")
    if not p.is_dir():
        raise WorkspaceConfigError(f"path is not a directory: {raw!r}")
    return str(p.resolve())


@dataclass(frozen=True)
class WorkspaceEntry:
    """One selectable workspace: a stable id, a display name, an absolute path."""

    id: str
    name: str
    path: str
    is_default: bool = False

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "path": self.path,
            "is_default": self.is_default,
        }


class WorkspaceRegistry:
    """In-memory workspace map persisted to a JSON file + a default entry.

    ``default_dir`` is the host-fixed workspace (``config.workspace_dir``); it is
    surfaced as ``is_default=True``, never written to disk, and undeletable.
    """

    def __init__(self, path: Path, *, default_dir: Path) -> None:
        self._path = Path(path).expanduser()
        self._default_path = str(Path(default_dir).expanduser().resolve())
        self._default_id = _workspace_id(self._default_path)
        # user-added entries only (the default is synthesized, never stored)
        self._entries: dict[str, WorkspaceEntry] = {}

    # -- persistence -------------------------------------------------------

    def load(self) -> None:
        """Read the JSON store once at startup (absent / malformed ⇒ empty)."""
        self._entries = {}
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        for wid, rec in raw.items():
            if not isinstance(rec, dict):
                continue
            path = rec.get("path")
            if not isinstance(path, str) or not path:
                continue
            # Skip a stored entry that collides with the (synthesized) default.
            if wid == self._default_id:
                continue
            name = rec.get("name")
            self._entries[str(wid)] = WorkspaceEntry(
                id=str(wid),
                name=str(name) if isinstance(name, str) and name else Path(path).name,
                path=path,
            )

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {e.id: {"name": e.name, "path": e.path} for e in self._entries.values()}
        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # -- default entry -----------------------------------------------------

    def _default_entry(self) -> WorkspaceEntry:
        return WorkspaceEntry(
            id=self._default_id,
            name=Path(self._default_path).name or "default",
            path=self._default_path,
            is_default=True,
        )

    # -- queries -----------------------------------------------------------

    def list_all(self) -> list[WorkspaceEntry]:
        """The default workspace first, then user entries sorted by name."""
        rest = sorted(self._entries.values(), key=lambda e: (e.name.lower(), e.path))
        return [self._default_entry(), *rest]

    def resolve(self, ref: Optional[str]) -> Optional[str]:
        """Resolve a workspace ``id`` (or a registered absolute path) → its path.

        ``None`` / unknown ⇒ ``None`` (the caller falls back to the host default
        workspace, byte-identical to the pre-feature single-workspace path).
        """
        if not ref:
            return None
        if ref == self._default_id or ref == self._default_path:
            return self._default_path
        entry = self._entries.get(ref)
        if entry is not None:
            return entry.path
        # Also accept a registered absolute path passed verbatim.
        for e in self._entries.values():
            if e.path == ref:
                return e.path
        return None

    def name_for_path(self, path: Optional[str]) -> Optional[str]:
        """The display name for an absolute ``path`` (``None`` ⇒ the default)."""
        if not path or path == self._default_path:
            return self._default_entry().name
        for e in self._entries.values():
            if e.path == path:
                return e.name
        return Path(path).name

    # -- mutations ---------------------------------------------------------

    def add(self, *, path: str, name: Optional[str] = None) -> WorkspaceEntry:
        """Validate + persist a new workspace; idempotent on the same path.

        Raises :class:`WorkspaceConfigError` on a bad path. Re-adding the default
        workspace, or an already-registered path, returns the existing entry
        (no duplicate). ``name`` defaults to the path's basename.
        """
        canonical = _canonicalize(path)
        wid = _workspace_id(canonical)
        if wid == self._default_id:
            return self._default_entry()
        display = (name or "").strip() or Path(canonical).name
        entry = WorkspaceEntry(id=wid, name=display, path=canonical)
        self._entries[wid] = entry
        self._save()
        return entry

    def remove(self, workspace_id: str) -> bool:
        """Remove a workspace entry (NOT its directory). ``True`` if removed.

        The default workspace cannot be removed (returns ``False``); an unknown
        id returns ``False``.
        """
        if workspace_id == self._default_id:
            return False
        if workspace_id not in self._entries:
            return False
        del self._entries[workspace_id]
        self._save()
        return True
