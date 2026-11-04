"""The files under a task's directory a prompt's ``@`` mention can name."""

from __future__ import annotations

import os
import subprocess

__all__ = ["file_matches"]

#: Files read from a directory before matching (a huge tree is cut there).
FILE_SCAN = 20_000
_SKIPPED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}


def _listed(root: str) -> list[str]:
    """The files under ``root``, relative: git's (tracked and untracked, not
    ignored) in a repository, else a walk skipping dot and build directories."""
    try:
        done = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=root,
                              capture_output=True, text=True, timeout=5, check=False)
        if done.returncode == 0:
            return done.stdout.splitlines()[:FILE_SCAN]
    except (OSError, subprocess.SubprocessError):
        pass
    found: list[str] = []
    for here, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _SKIPPED_DIRS and not d.startswith("."))
        rel = os.path.relpath(here, root)
        found += [f if rel == "." else os.path.join(rel, f) for f in sorted(files)]
        if len(found) >= FILE_SCAN:
            break
    return found[:FILE_SCAN]


def file_matches(root: str, query: str, limit: int = 30) -> list[str]:
    """Paths under ``root`` whose path holds ``query`` (case ignored): a file
    whose name starts with it first, then one whose name holds it, then the
    rest, shorter paths first in each."""
    if not root or not os.path.isdir(root):
        return []
    q = query.lower()

    def rank(path: str) -> tuple[int, int, str]:
        name = os.path.basename(path).lower()
        return (0 if name.startswith(q) else 1 if q in name else 2, len(path), path)

    return sorted((p for p in _listed(root) if q in p.lower()), key=rank)[:limit]
