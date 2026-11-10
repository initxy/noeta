"""The projects a front end lists down its side: the directories a person
saved, and the directories of their sessions (the root tasks not archived),
each with how many sessions it has, then the sessions run in scratch
directories as one entry; and the subdirectories of a directory, for a
folder picker."""

from __future__ import annotations

import contextlib
import json
import os
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

__all__ = ["SavedProjects", "list_dirs", "projects"]


class SavedProjects:
    """The saved directories, in the order they were saved, kept in a JSON
    file (``{"projects": [path, ...]}``) read again on every call, so two
    servers on one home share it; only in memory without a ``path``."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._memory: list[str] = []
        self._lock = threading.Lock()

    def paths(self) -> list[str]:
        if self._path is None:
            return list(self._memory)
        try:
            data = json.loads(self._path.read_text("utf-8"))
        except (OSError, ValueError):
            return []
        found = data.get("projects") if isinstance(data, dict) else None
        return [p for p in found if isinstance(p, str) and p] if isinstance(found, list) else []

    def add(self, path: str) -> None:
        with self._lock:
            saved = self.paths()
            if path not in saved:
                self._write([*saved, path])

    def remove(self, path: str) -> None:
        with self._lock:
            saved = self.paths()
            real = os.path.realpath(path)
            kept = [p for p in saved if p != path and p != real]
            if kept != saved:
                self._write(kept)

    def _write(self, paths: list[str]) -> None:
        if self._path is None:
            self._memory = paths
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fresh = self._path.with_name(f".{self._path.name}-{os.getpid()}")
        with contextlib.suppress(FileNotFoundError):
            fresh.unlink()
        fd = os.open(fresh, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"projects": paths}, fh, indent=2)
            fh.write("\n")
        os.replace(fresh, self._path)


def _project(path: str, saved: bool, sessions: int, *, scratch: bool = False,
             name: str = "") -> dict[str, Any]:
    return {"path": path, "name": name or os.path.basename(path.rstrip("/")) or path,
            "saved": saved, "sessions": sessions, "scratch": scratch}


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def projects(saved: Iterable[str], summaries: Iterable[dict[str, Any]],
             scratch: str | None = None) -> list[dict[str, Any]]:
    """The saved directories in the order saved, then the other directories
    of ``summaries`` (root task summaries, most recent first, the archived
    left out), the most recently used first; each ``{path, name, saved,
    sessions, scratch}``. Directories compare by their real path, which
    ``path`` is. No directory under ``scratch`` (a real path: where tasks
    created without a directory run) is a project: their sessions are one
    last entry, ``{path: scratch, name: "Scratch", scratch: true}``, when
    there are any."""
    counts: dict[str, int] = {}
    in_scratch = 0
    for summary in summaries:
        cwd = str(summary.get("cwd") or "")
        if cwd:
            real = os.path.realpath(cwd)
            if scratch is not None and _within(real, scratch):
                in_scratch += 1
            else:
                counts[real] = counts.get(real, 0) + 1
    out: list[dict[str, Any]] = []
    listed: set[str] = set()
    for path in saved:
        real = os.path.realpath(path)
        if real not in listed and not (scratch is not None and _within(real, scratch)):
            listed.add(real)
            out.append(_project(real, True, counts.get(real, 0)))
    out += [_project(p, False, n) for p, n in counts.items() if p not in listed]
    if scratch is not None and in_scratch:
        out.append(_project(scratch, False, in_scratch, scratch=True, name="Scratch"))
    return out


def list_dirs(path: str) -> list[dict[str, str]]:
    """The subdirectories of ``path`` (a real, absolute path) by name,
    hidden ones (a leading dot) left out, each ``{name, path}``. Raises
    ``ValueError`` when ``path`` is not a directory that can be read."""
    try:
        with os.scandir(path) as entries:
            names = [e.name for e in entries if not e.name.startswith(".") and _is_dir(e)]
    except NotADirectoryError:
        raise ValueError(f"{path} is not a directory") from None
    except FileNotFoundError:
        raise ValueError(f"{path} does not exist") from None
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc.strerror or exc}") from None
    return [{"name": n, "path": os.path.join(path, n)}
            for n in sorted(names, key=lambda n: (n.casefold(), n))]


def _is_dir(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_dir()
    except OSError:
        return False
