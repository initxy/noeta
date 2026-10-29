"""What changed in a file the model read, for the note that tells it so:
the changed lines of the file as it is now, numbered as ``Read`` numbers
them, worked out against the file as the model last saw it whole (its last
full ``Read`` result in the conversation, when no edit of the model's came
after it). Nothing is kept outside the conversation, so a task on another
worker finds the same; when that view is gone (compacted, partial, edited
since) there is no snippet."""

from __future__ import annotations

import difflib
import posixpath
import re
from collections.abc import Sequence

from noeta.core.types import Message, ToolResult

__all__ = ["CONTEXT_LINES", "MAX_SNIPPET_CHARS", "changed_lines", "seen_text"]

#: Unchanged lines shown around each change.
CONTEXT_LINES = 3
#: A snippet longer than this is not shown (the model reads the file instead).
MAX_SNIPPET_CHARS = 4000
_WRITERS = frozenset({"Edit", "Write", "NotebookEdit"})
_NUMBERED = re.compile(r"^(\d+)\t(.*)$")
_EMPTY = "Warning: the file is there but holds no content."


def _names(call_input: object, cwd: str, path: str) -> bool:
    if not isinstance(call_input, dict):
        return False
    given = call_input.get("file_path") or call_input.get("notebook_path")
    return isinstance(given, str) and posixpath.normpath(posixpath.join(cwd, given)) == path


def seen_text(messages: Sequence[Message], path: str, cwd: str) -> str | None:
    """The file at ``path`` as the model last saw it whole, from its last
    full ``Read`` of it, or ``None`` (not read whole, or written since)."""
    results = {b.call_id: b for m in messages if m.role == "user"
               for b in m.content if isinstance(b, ToolResult)}
    for m in reversed(messages):
        if m.role != "assistant":
            continue
        for use in reversed(m.tool_uses):
            if not _names(use.input, cwd, path):
                continue
            if use.name in _WRITERS:
                return None
            if use.name != "Read" or use.input.get("limit") or use.input.get("pages") \
                    or (use.input.get("offset") or 1) != 1:
                continue
            result = results.get(use.id)
            if result is None or result.is_error:
                continue
            parsed = _unnumbered(result.text)
            if parsed is not None:
                return parsed
    return None


def _unnumbered(text: str) -> str | None:
    if text == _EMPTY:
        return ""
    lines = []
    for n, line in enumerate(text.split("\n"), 1):
        m = _NUMBERED.match(line)
        if m is None or int(m.group(1)) != n:
            return None
        lines.append(m.group(2))
    return "\n".join(lines)


def changed_lines(before: str, after: str) -> str | None:
    """The changed lines of ``after`` with a little context (a removal
    shows its neighbours), numbered; ``None`` when nothing differs or it is
    too long to show."""
    old, new = before.split("\n"), after.split("\n")
    parts: list[str] = []
    for group in difflib.SequenceMatcher(None, old, new, autojunk=False).get_grouped_opcodes(
            CONTEXT_LINES):
        first, last = group[0][3], group[-1][4]
        if first == last:
            continue
        if parts:
            parts.append("...")
        parts += [f"{n + 1}\t{new[n]}" for n in range(first, last)]
    snippet = "\n".join(parts)
    if not snippet or len(snippet) > MAX_SNIPPET_CHARS:
        return None
    return snippet
