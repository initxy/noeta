"""Session-workspace file surface: reads the host-side `workspaces/<session_id>/`.

In sandbox mode this directory is bind-mounted into the container at
`/workspace`, where the agent's deliverables land; the frontend file panel
reads the host directory directly (no longer proxied through the container).
Deep module: a small interface (list_files / resolve_within) hiding the
os.walk traversal, hidden-entry pruning, and escape prevention.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class WorkspaceFileEntry:
    """One file inside the session directory (path relative to the session
    directory)."""

    path: str
    size: int
    mtime: float


def list_files(
    root: Path, exclude_top_level: Optional[set[str]] = None
) -> list[WorkspaceFileEntry]:
    """Recursively list files under root (excluding directories, hidden
    entries, and exclude_top_level top-level names).

    - Skips hidden directories / files at any depth (`.` prefix, including
      the noeta runtime's `.noeta`).
    - Skips top-level names in exclude_top_level (e.g. `knowledge` — a
      symlink pointing at the mounted knowledge base, potentially hundreds of
      thousands of files). os.walk with the default followlinks=False never
      descends into symlinked directories anyway; pruning them from the walk
      as well is a double safeguard.
    - Missing directory (empty session) → empty list.
    """
    if not root.is_dir():
        return []
    exclude = exclude_top_level or set()
    entries: list[WorkspaceFileEntry] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = Path(dirpath).relative_to(root)
        at_top = rel_dir == Path(".")
        # Prune before descending: hidden directories (any depth) + top-level
        # excluded names.
        dirnames[:] = [
            d
            for d in dirnames
            if not d.startswith(".") and not (at_top and d in exclude)
        ]
        for fn in filenames:
            if fn.startswith("."):
                continue
            full = Path(dirpath) / fn
            try:
                st = full.stat()
            except OSError:
                # Concurrent deletion from the container / dangling symlink:
                # skip the single file instead of failing the whole listing.
                continue
            entries.append(
                WorkspaceFileEntry(
                    path=str(full.relative_to(root)),
                    size=st.st_size,
                    mtime=st.st_mtime,
                )
            )
    entries.sort(key=lambda e: e.path)
    return entries


def resolve_within(root: Path, rel: str) -> Optional[Path]:
    """Resolve a user-supplied relative path into an absolute path inside
    root; escapes / absolute paths return None.

    Normalize with realpath (following symlinks) before the containment
    check: this blocks both `../` escapes and reads through
    workspace-internal symlinks pointing outside (preventing unauthorized
    reads of arbitrary host files).
    """
    p = (rel or "").strip()
    if not p or os.path.isabs(p):
        return None
    try:
        resolved = Path(os.path.realpath(root / p))
        root_real = Path(os.path.realpath(root))
    except OSError:
        return None
    if resolved != root_real and root_real not in resolved.parents:
        return None
    return resolved
