"""A memory directory's ``MEMORY.md`` index: its limits and how it is read."""

from __future__ import annotations

from pathlib import Path

from noeta.core.execenv import ExecEnv
from noeta.toolkit.context import read_task_text

#: The auto memory index a memory directory keeps, and how much of it is
#: loaded (Claude Code's limits). The memory plugin points the model at the
#: directory; the instructions plugin shows the index with the other
#: instruction files.
MEMORY_INDEX_FILE = "MEMORY.md"
MEMORY_INDEX_MAX_LINES = 200
MEMORY_INDEX_MAX_BYTES = 25_000
_INDEX_CUT = ("> Note: {name} is {size}; only part of it was loaded ({what}). Keep each index "
              "entry to one short line and move details into the memory files.")


def trim_memory_index(text: str) -> str:
    """``MEMORY.md`` within its line and byte limits, with a note when cut."""
    trimmed = text.strip()
    lines = trimmed.split("\n")
    size = len(trimmed.encode("utf-8"))
    over_lines = len(lines) > MEMORY_INDEX_MAX_LINES
    over_bytes = size > MEMORY_INDEX_MAX_BYTES
    if not over_lines and not over_bytes:
        return trimmed
    kept = "\n".join(lines[:MEMORY_INDEX_MAX_LINES])
    if len(kept.encode("utf-8")) > MEMORY_INDEX_MAX_BYTES:
        cut = kept.encode("utf-8")[:MEMORY_INDEX_MAX_BYTES].decode("utf-8", errors="ignore")
        newline = cut.rfind("\n")
        kept = cut[:newline] if newline > 0 else cut
    shown = kept.count("\n") + 1
    if over_lines and not over_bytes:
        size_text = f"{len(lines)} lines (limit {MEMORY_INDEX_MAX_LINES})"
    elif over_bytes and not over_lines:
        size_text = f"{size} bytes (limit {MEMORY_INDEX_MAX_BYTES})"
    else:
        size_text = f"{len(lines)} lines and {size} bytes"
    note = _INDEX_CUT.format(name=MEMORY_INDEX_FILE, size=size_text,
                             what=f"the first {shown} of {len(lines)} lines")
    return f"{kept}\n{note}"


async def read_memory_index(env: ExecEnv, directory: str) -> tuple[str, str] | None:
    """``(path, trimmed text)`` of the memory directory's index, or ``None``
    when it is missing, unreadable or blank.
    Read where the model's file tools write it
    (:func:`~noeta.toolkit.context.read_task_text`)."""
    path = Path(directory) / MEMORY_INDEX_FILE
    text = await read_task_text(env, str(path))
    if text is None or not text.strip():
        return None
    return str(path), trim_memory_index(text)
