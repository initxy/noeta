"""``@`` completion: the files and directories under the task's directory
whose path contains what was typed. Listing a tree is slow (a home
directory takes a while), so the app calls this off its event loop."""

from __future__ import annotations

import os
import subprocess
import time

from noeta.toolkit.shell import GIT_READ_ENV, GIT_READ_FLAGS

#: Directories never walked into.
SKIPPED = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
           ".pytest_cache", ".ruff_cache", "dist", "build"}
#: Paths looked at, at most (a huge tree is cut, not walked whole).
LIMIT = 5000
#: Paths a git repository lists at most (git lists them from its index:
#: fast, and only what is not ignored).
GIT_LIMIT = 50_000
#: Seconds ``git ls-files`` may take.
GIT_TIMEOUT = 2.0
#: Completions offered.
SHOWN = 15
#: Seconds a directory's listing is reused.
FRESH = 10.0

_cache: dict[str, tuple[float, list[str]]] = {}


def listing(root: str) -> list[str]:
    """Paths under ``root``, relative, directories ending in ``/``: a git
    repository's files (tracked and new, not ignored) and their
    directories, else what a walk finds first."""
    found = _cache.get(root)
    if found is not None and time.monotonic() - found[0] < FRESH:
        return found[1]
    out = _git_files(root)
    if out is None:
        out = _walk(root)
    _cache[root] = (time.monotonic(), out)
    return out


def _git_files(root: str) -> list[str] | None:
    """``None`` when ``root`` is not in a git work tree (or git is not
    here). Run as noeta runs git itself: nothing the repository's own
    config names (an fsmonitor hook, a fetch) may start."""
    try:
        done = subprocess.run(["git", *GIT_READ_FLAGS, "ls-files", "--cached", "--others",
                               "--exclude-standard", "-z"], cwd=root, capture_output=True,
                              timeout=GIT_TIMEOUT, check=False,
                              env={**os.environ, **GIT_READ_ENV})
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    names = [n for n in done.stdout.decode("utf-8", "replace").split("\0") if n][:GIT_LIMIT]
    dirs: dict[str, None] = {}
    for name in names:
        parts = name.split("/")[:-1]
        for n in range(1, len(parts) + 1):
            dirs.setdefault("/".join(parts[:n]) + "/")
    return [*dirs, *names]


def _walk(root: str) -> list[str]:
    out: list[str] = []
    for here, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIPPED and not d.startswith("."))
        rel = os.path.relpath(here, root)
        prefix = "" if rel == "." else rel + "/"
        out += [f"{prefix}{d}/" for d in dirs]
        out += [f"{prefix}{n}" for n in sorted(names)]
        if len(out) >= LIMIT:
            break
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
