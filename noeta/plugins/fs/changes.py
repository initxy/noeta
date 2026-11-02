"""What changed in a file the model read, for the note that tells it so:
the changed lines of the file as it is now, numbered as ``Read`` numbers
them, worked out against the file as the model last saw it whole (its last
full ``Read`` result in the conversation, or the output of a ``Bash``
command that showed just that whole file, such as ``cat``, when no edit of
the model's came after it). Nothing is kept outside the conversation, so a task on another
worker finds the same; when that view is gone (compacted, partial, edited
since) there is no snippet. A file the model read only in part (an offset
or a limit) gets no note at all, as in Claude Code (``FileRead.window``)."""

from __future__ import annotations

import difflib
import posixpath
import re
from collections.abc import Sequence

from noeta.core.large_output import output_cut
from noeta.core.types import Message, ToolResult, ToolUse
from noeta.toolkit.shell import NO_OUTPUT, shell_reads

__all__ = ["CONTEXT_LINES", "MAX_SNIPPET_CHARS", "changed_lines", "seen_text"]

#: Unchanged lines shown around each change (Claude Code's 8).
CONTEXT_LINES = 8
#: A snippet longer than this is cut at a line end, with a line saying how
#: many lines were left out (Claude Code's 8,192 characters).
MAX_SNIPPET_CHARS = 8192
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
            if use.name == "Bash":
                shown = _shown_by_command(use, results.get(use.id), cwd, path)
                if shown is not None:
                    return shown
                continue
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


def _shown_by_command(use: ToolUse, result: ToolResult | None, cwd: str,
                      path: str) -> str | None:
    """The file at ``path`` as a ``Bash`` command that showed only that whole
    file printed it, or ``None``."""
    command = use.input.get("command")
    reads = shell_reads(command) if isinstance(command, str) else []
    if len(reads) != 1 or result is None or result.is_error:
        return None
    read = reads[0]
    if read.grep or read.start is not None or read.tail is not None \
            or posixpath.normpath(posixpath.join(cwd, read.path)) != path:
        return None
    printed = result.text
    if output_cut(printed):
        return None
    if printed == NO_OUTPUT:
        return ""
    # The shell's result lost the file's last line break; most files end
    # with one (one without shows its last line as changed).
    return read.file_text(printed) + "\n"


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
    shows its neighbours), numbered, one block per change, cut to
    :data:`MAX_SNIPPET_CHARS`; ``None`` when nothing differs."""
    old, new = before.split("\n"), after.split("\n")
    parts: list[str] = []
    for group in difflib.SequenceMatcher(None, old, new, autojunk=False).get_grouped_opcodes(
            CONTEXT_LINES):
        first, last = group[0][3], group[-1][4]
        parts += [f"{n + 1}\t{new[n]}" for n in range(first, last)]
    snippet = "\n".join(parts)
    if not snippet:
        return None
    if len(snippet) <= MAX_SNIPPET_CHARS:
        return snippet
    end = snippet.rfind("\n", 0, MAX_SNIPPET_CHARS + 1)
    kept = snippet[:end] if end > 0 else snippet[:MAX_SNIPPET_CHARS]
    left_out = snippet[len(kept) + 1:].count("\n") + 1
    return f"{kept}\n... [{left_out} lines truncated] ..."
