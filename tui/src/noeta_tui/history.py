"""The prompts typed in a project, across sessions, as Claude Code keeps
its history file: ``<home>/projects/<project>/history.jsonl``, one
``{"text", "at"}`` per line, oldest first. Up and Ctrl+R step through and
search it; at most :data:`HISTORY_MAX` prompts are kept."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from noeta.toolkit.paths import project_dir

log = logging.getLogger("noeta_tui")

HISTORY_FILE = "history.jsonl"
#: Prompts kept per project (the file is cut back to this many once it
#: holds twice as many).
HISTORY_MAX = 100


def history_file(home: str, cwd: str) -> Path | None:
    """The history file of the project at ``cwd`` (``None`` without a home:
    the history stays in memory)."""
    return project_dir(cwd, Path(home)) / HISTORY_FILE if home and cwd else None


def _read(path: Path) -> list[str]:
    try:
        lines = path.read_text("utf-8").splitlines()
    except FileNotFoundError:
        return []
    out: list[str] = []
    for line in lines:
        try:
            text = json.loads(line).get("text")
        except (ValueError, AttributeError):
            continue  # a line cut short by a crash
        if isinstance(text, str) and text:
            out.append(text)
    return out


def load(path: Path | None) -> list[str]:
    """The project's prompts, oldest first (the newest :data:`HISTORY_MAX`)."""
    if path is None:
        return []
    try:
        return _read(path)[-HISTORY_MAX:]
    except OSError:
        log.warning("reading the prompt history %s failed", path, exc_info=True)
        return []


def append(path: Path | None, text: str) -> None:
    """Add ``text`` to the project's history; a failure is logged, never
    raised (the prompt is sent all the same)."""
    if path is None:
        return
    line = json.dumps({"text": text, "at": time.time()}, ensure_ascii=False) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(line)
        lines = path.read_text("utf-8").splitlines(keepends=True)
        if len(lines) > 2 * HISTORY_MAX:
            tmp = path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("".join(lines[-HISTORY_MAX:]))
            tmp.replace(path)
    except OSError:
        log.warning("writing the prompt history %s failed", path, exc_info=True)
