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
#: Child task config: the auto memory directory of the task that started
#: it, whose index a sub-agent is shown with its instruction files (Claude
#: Code's sub-agents get the session's ``MEMORY.md``, but no memory section).
MEMORY_DIR_KEY = "auto_memory_dir"
MEMORY_INDEX_MAX_LINES = 200
#: Characters, not UTF-8 bytes (Claude Code counts the string's length).
MEMORY_INDEX_MAX_CHARS = 25_000
_INDEX_CUT = ("> Note: {name} is {size}; only part of it was loaded ({what}). Keep each index "
              "entry to one short line and move details into the memory files.")


def trim_memory_index(text: str) -> str:
    """``MEMORY.md`` within its line and character limits, with a note when
    cut."""
    trimmed = text.strip()
    lines = trimmed.split("\n")
    size = len(trimmed)
    over_lines = len(lines) > MEMORY_INDEX_MAX_LINES
    over_chars = size > MEMORY_INDEX_MAX_CHARS
    if not over_lines and not over_chars:
        return trimmed
    kept = "\n".join(lines[:MEMORY_INDEX_MAX_LINES])
    if len(kept) > MEMORY_INDEX_MAX_CHARS:
        newline = kept.rfind("\n", 0, MEMORY_INDEX_MAX_CHARS + 1)
        kept = kept[:newline] if newline > 0 else kept[:MEMORY_INDEX_MAX_CHARS]
    shown = kept.count("\n") + 1
    if over_lines and not over_chars:
        size_text = f"{len(lines)} lines (limit {MEMORY_INDEX_MAX_LINES})"
    elif over_chars and not over_lines:
        size_text = f"{size} characters (limit {MEMORY_INDEX_MAX_CHARS})"
    else:
        size_text = f"{len(lines)} lines and {size} characters"
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
