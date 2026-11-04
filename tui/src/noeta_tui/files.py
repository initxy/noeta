"""``@`` completion: the files and directories under the task's directory
whose path contains what was typed."""

from __future__ import annotations

import os
import time

#: Directories never walked into.
SKIPPED = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
           ".pytest_cache", ".ruff_cache", "dist", "build"}
#: Paths looked at, at most (a huge tree is cut, not walked whole).
LIMIT = 5000
#: Completions offered.
SHOWN = 15
#: Seconds a directory's listing is reused.
FRESH = 10.0

_cache: dict[str, tuple[float, list[str]]] = {}


def listing(root: str) -> list[str]:
    """Paths under ``root``, relative, directories ending in ``/``."""
    found = _cache.get(root)
    if found is not None and time.monotonic() - found[0] < FRESH:
        return found[1]
    out: list[str] = []
    for here, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIPPED and not d.startswith("."))
        rel = os.path.relpath(here, root)
        prefix = "" if rel == "." else rel + "/"
        out += [f"{prefix}{d}/" for d in dirs]
        out += [f"{prefix}{n}" for n in sorted(names)]
        if len(out) >= LIMIT:
            break
    _cache[root] = (time.monotonic(), out[:LIMIT])
    return out[:LIMIT]


def matches(root: str, typed: str) -> list[str]:
    """The paths to offer for ``@typed``: a name starting with it first,
    then paths containing it, shorter first."""
    if not root or not os.path.isdir(root):
        return []
    want = typed.lower()
    hits = [p for p in listing(root) if want in p.lower()]

    def rank(path: str) -> tuple[int, int, str]:
        name = path.rstrip("/").rsplit("/", 1)[-1].lower()
        return (0 if name.startswith(want) else 1, len(path), path)

    return sorted(hits, key=rank)[:SHOWN]
