"""Files a ``Bash`` command showed count as read, as in Claude Code.

After a foreground ``Bash`` call whose command only shows files
(:func:`noeta.toolkit.shell.shell_reads`: ``cat``, ``head``, ``sed -n``,
a lone ``grep`` that found a match, and the like), each such file the
model has no read entry for is recorded as read (``fs`` / ``read``, durable
in the task's log like a ``Read``): the file is read through the exec env
right after the command, its digest and modification time recorded, and
the record made only when what the command printed holds the part of the
file it was meant to show (a ``grep``, which shows only matching lines, and
an output cut to fit, are recorded without that check, as views the model
does not have in full).

What the record says follows Claude Code's read-file entry: a whole-file
view (``cat``) is watched by the changed-file check and, when its text is
in the conversation, lets ``Edit`` say the file is in view; a view of some
lines (``head``, ``tail``, ``sed -n``) keeps its line range, so a ``Read``
of the same range is answered as unchanged, and is not watched. Every one
of them lets ``Edit`` and ``Write`` through the read-first check.
"""

from __future__ import annotations

import hashlib
import posixpath
from typing import Any

from noeta.core.extension import TaskView
from noeta.core.large_output import output_cut
from noeta.core.tool import ToolOutput
from noeta.core.types import Text, ToolUse
from noeta.plugins.fs._encoding import decode
from noeta.plugins.fs.state import FS, KIND_READ, current_fs_state
from noeta.toolkit.shell import ShellRead, shell_reads

__all__ = ["MAX_SHELL_READ_BYTES", "SHELL_TOOL", "record_shell_reads"]

SHELL_TOOL = "Bash"
#: A larger file is not recorded (Claude Code's 10 MB).
MAX_SHELL_READ_BYTES = 10 * 1024 * 1024


def _shell_cwd(view: TaskView) -> str:
    """Where the command ran: the directory the shell plugin's ``cwd``
    record left it in (read by name, as its state is not this plugin's),
    else the exec env's."""
    assert view.env is not None
    cwd = getattr(view.state.ext.get("shell"), "cwd", None)
    return cwd if isinstance(cwd, str) and cwd else view.env.cwd


async def record_shell_reads(view: TaskView, call: ToolUse, output: ToolOutput) -> None:
    """Record the files ``call`` (a ``Bash`` call) showed as read."""
    if call.name != SHELL_TOOL or view.env is None or call.input.get("run_in_background"):
        return
    command = call.input.get("command")
    reads = shell_reads(command) if isinstance(command, str) else []
    if not reads or output.meta.get("timed_out") \
            or not all(isinstance(b, Text) for b in output.content):
        return
    printed = "".join(b.text for b in output.content if isinstance(b, Text))
    cut = output_cut(printed)
    known = set(current_fs_state(view.state.ext).reads)
    cwd = _shell_cwd(view)
    for read in reads:
        if read.grep and output.meta.get("exit_code") != 0:
            continue
        try:
            path = await view.env.realpath(posixpath.join(cwd, read.path))
        except OSError:
            continue
        if path in known:
            continue
        data = await _entry(view, path, read, printed, in_context=not cut and not read.grep)
        if data is not None:
            known.add(path)
            view.record(FS, KIND_READ, data)


async def _entry(view: TaskView, path: str, read: ShellRead, printed: str, *,
                 in_context: bool) -> dict[str, Any] | None:
    """The ``read`` record for ``path``, or ``None`` when it is not to be
    recorded (gone, too large, or not what the command printed)."""
    assert view.env is not None
    try:
        st = await view.env.stat(path)
        if st is None or st.is_dir or st.size > MAX_SHELL_READ_BYTES:
            return None
        raw = await view.env.read_bytes(path)
    except OSError:
        return None
    text = decode(raw).removeprefix("﻿").replace("\r\n", "\n")
    shown = read.view(text)
    if shown is None:
        return None
    content, first, count = shown
    if not content.strip() and text.strip():
        return None
    if in_context and not read.shown_in(printed, content):
        return None
    # The whole file in view: Claude Code's test, a view from the top that
    # ends before its line count ran out.
    whole = in_context and (first == 0 or (
        first <= 1 and content != "" and content.count("\n") + 1 < count))
    data: dict[str, Any] = {"path": path, "digest": hashlib.sha256(raw).hexdigest(),
                            "mtime": st.mtime}
    if first:
        if in_context:
            data["range"] = [first, count]
        data["window"] = True
    if not whole:
        data["whole"] = False
    return data
