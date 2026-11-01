"""The note after a ``Bash`` command that rewrote files the model had read,
as in Claude Code.

A foreground ``Bash`` command that looks like it writes files in place (a
formatter or a fixer: ``--write``, ``--fix``, ``sed -i``, ``black``,
``ruff format``, ``cargo fmt`` and the like, :data:`WRITE_MARKERS`) has every
file the model has a read entry for examined (one batched
:func:`noeta.core.execenv.stat_many`) right after it; those whose
modification time moved past what the task last knew of them (the read
entry's time, or the time the changed-file check last found the file at)
are named in a note at the end of the call's result, the first
:data:`MAX_LISTED` of them, with a count of the rest. The note is part of
the result, so it is in the task's log for any worker. The read entries are
left as they are: ``Edit`` and ``Write`` still ask for a fresh read, and the
changed-file reminder before the next model call still shows what changed,
as Claude Code sends both.
"""

from __future__ import annotations

import posixpath
import re

import msgspec

from noeta.core.execenv import stat_many
from noeta.core.extension import TaskView
from noeta.core.tool import ToolOutput
from noeta.core.types import Text, ToolUse
from noeta.plugins.fs.shell_reads import SHELL_TOOL, shell_cwd
from noeta.plugins.fs.state import current_fs_state
from noeta.toolkit.shell import NO_OUTPUT

__all__ = ["MAX_LISTED", "MODIFIED_READS_NOTE", "WRITE_MARKERS", "note_modified_reads"]

#: A command that may rewrite files in place (Claude Code's list).
WRITE_MARKERS = re.compile("|".join([
    r"--write", r"--fix", r"--in-place", r"--auto-correct", r"\bg?sed\b.*\s-[a-zA-Z]*i",
    r"\brun\s+format\b", r"\brun\s+fix\b", r"\b(yarn|pnpm)\s+format\b", r"\blint:file\b",
    r"\blint:fix\b", r"\bblack\b", r"\bisort\b", r"\bruff\s+format\b",
    r"\bcargo\s+(fmt|fix)\b", r"\brustfmt\b", r"\bgo\s+fmt\b", r"\bterraform\s+fmt\b",
    r"\bdprint\s+fmt\b", r"\bswiftformat\b", r"\bphpcbf\b",
]))
#: Files named in the note at most (Claude Code's 5); the rest are counted.
MAX_LISTED = 5
#: ``{count}``, ``{files}`` (the word, singular or plural), ``{paths}``
#: (comma-separated, relative to the shell's directory) and ``{more}``.
MODIFIED_READS_NOTE = (
    "[Running this command changed {count} {files} you had read: {paths}{more}. "
    "Read the new contents with Read before you edit any of them.]")


async def note_modified_reads(view: TaskView, call: ToolUse,
                              output: ToolOutput) -> ToolOutput:
    """``output`` with the note when ``call`` (a ``Bash`` call) changed
    files the model had read; otherwise ``output`` unchanged."""
    if call.name != SHELL_TOOL or view.env is None or call.input.get("run_in_background") \
            or output.meta.get("timed_out") or not output.content \
            or not all(isinstance(b, Text) for b in output.content):
        return output
    command = call.input.get("command")
    if not isinstance(command, str) or not WRITE_MARKERS.search(command):
        return output
    state = current_fs_state(view.state.ext)
    paths = list(state.reads)
    if not paths:
        return output
    try:
        stats = await stat_many(view.env, paths)
    except OSError:
        return output
    changed = []
    for path, stat in zip(paths, stats, strict=True):
        if stat is None or stat.is_dir:
            continue
        read = state.reads[path]
        if stat.mtime > max(read.mtime, state.checked.get(path, read.mtime)):
            changed.append(path)
    if not changed:
        return output
    cwd = shell_cwd(view)
    shown = ", ".join(posixpath.relpath(p, cwd) for p in changed[:MAX_LISTED])
    more = f" and {len(changed) - MAX_LISTED} more" if len(changed) > MAX_LISTED else ""
    note = view.prompt("tool/bash_modified_reads", MODIFIED_READS_NOTE).format(
        count=len(changed), files="file" if len(changed) == 1 else "files",
        paths=shown, more=more)
    *head, last = output.content
    assert isinstance(last, Text)
    text = note if last.text in ("", NO_OUTPUT) and not head else f"{last.text}\n{note}"
    return msgspec.structs.replace(output, content=[*head, Text(text=text)])
