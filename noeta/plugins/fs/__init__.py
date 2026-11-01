"""The file tools as one extension: Read, Write, Edit and NotebookEdit, the
fold of what they record (the files the model has read), and Claude Code's
reminder when one of those files changed on disk since.

The check runs before every model call (so right after the step whose
command changed the file), over every file the fold still remembers as read
(:data:`noeta.plugins.fs.state.MAX_FILES`, as Claude Code's read-file state),
all examined in one :func:`noeta.core.execenv.stat_many`: a file whose
modification time moved is hashed, and if its bytes are neither what the
model read nor what it was already told about, the model is told once, in a
system turn (the prompt ``reminder/file_changed``; ``{path}`` is the file),
with the changed lines when the model's last whole view of the file is still
in the conversation (:mod:`noeta.plugins.fs.changes`). A file last read with
an offset or a limit is not checked, as in Claude Code. With those lines shown
the file counts as read as it is now; otherwise ``Edit`` and ``Write`` refuse
until the model reads it again.

Files a ``Bash`` command showed (``cat``, ``head``, ``sed -n`` and the
like) count as read too, as in Claude Code (:mod:`noeta.plugins.fs.shell_reads`).

A tool's own note for the model (``notice``: the paging hint of a read cut
to fit the token ceiling) is sent as a reminder right after that call's
result, as Claude Code does. Files the person mentions with ``@path`` are
attached through ``Read`` (:mod:`noeta.plugins.fs.mentions`); images the person pastes
are also saved as files the model is told of (:mod:`noeta.plugins.fs.pasted`)."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from typing import Any

import msgspec

from noeta.core.events import Compacted, Recorded
from noeta.core.execenv import stat_many
from noeta.core.extension import Extension, Reminder, TaskView
from noeta.core.tool import Tool, ToolOutput
from noeta.core.types import ModelRequest, ToolUse
from noeta.plugins.fs._encoding import decode, not_text
from noeta.plugins.fs.changes import changed_lines, seen_text
from noeta.plugins.fs.edit import EditTool
from noeta.plugins.fs.mentions import attach_mentions, hoist_attachments
from noeta.plugins.fs.notebook import NotebookEditTool
from noeta.plugins.fs.pasted import remove_task_images, save_pasted_images
from noeta.plugins.fs.read import ReadTool
from noeta.plugins.fs.shell_reads import record_shell_reads
from noeta.plugins.fs.state import (
    FS,
    KIND_CHANGED,
    KIND_CHECKED,
    KIND_FORGET_RANGES,
    KIND_NOTICE_SENT,
    KIND_READ,
    FsState,
    reduce_fs,
)
from noeta.plugins.fs.write import WriteTool
from noeta.toolkit.paths import plans_dir

__all__ = ["EditTool", "FileTools", "NotebookEditTool", "ReadTool", "WriteTool"]


#: The note for a file changed outside the model's own edits (Claude Code's
#: meaning), followed by the changed lines or a pointer to ``Read``.
FILE_CHANGED_REMINDER = (
    "Note: {path} changed on disk since you last read it. A change like this is usually "
    "deliberate, so take the file as it is now rather than reverting it; if the change looks "
    "wrong, say so instead of undoing it yourself, and otherwise there is no need to mention "
    "it."
)
CHANGES_SHOWN = " These are the changed lines, numbered as in the file now:\n{snippet}"
CHANGES_NOT_SHOWN = " The changes are not shown here; read the file if you need what it holds now."
#: Its place in a joined system turn: after the quiet nudge, before the budget note.
ORDER_FILE_CHANGED = 70

_BY_NAME: dict[str, type[Tool]] = {
    "Read": ReadTool, "Write": WriteTool, "Edit": EditTool, "NotebookEdit": NotebookEditTool,
}


class FileTools(Extension):
    """``tools``: which file tools to offer, by name or as tool objects
    (default: all four, in the order Read, Write, Edit, NotebookEdit).
    ``write_roots``: directories outside the working directory the named
    tools may write (Claude Code's ``additionalDirectories``); plan mode's
    plan directory and the task's memory directory are always among them.
    ``mentions``: attach the files the person names with ``@path`` (default
    on). ``home``: the person's home directory, which ``@~/…`` names (a
    front end on the person's machine sets it; unset, as on a server, ``~``
    is not expanded)."""

    name = FS

    def __init__(self, tools: Sequence[str | Tool] | None = None, *,
                 write_roots: Sequence[str] = (), mentions: bool = True,
                 home: str | None = None) -> None:
        if tools is None:
            tools = list(_BY_NAME)
        unknown = [t for t in tools if isinstance(t, str) and t not in _BY_NAME]
        if unknown:
            raise ValueError(f"FileTools: unknown tools {unknown}; known: {list(_BY_NAME)}")
        extra = tuple(os.path.expanduser(d) for d in write_roots)
        self.mentions = mentions
        self.home = home

        def roots(ctx: Any) -> list[str]:
            return [*extra, str(plans_dir()), *memory_dir(ctx.state)]

        self._tools: list[Tool] = [
            (_BY_NAME[t](write_roots=roots) if t != "Read" else _BY_NAME[t]())  # type: ignore[call-arg]
            if isinstance(t, str) else t for t in tools]

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        return reduce_fs(state, kind, data)

    def tools(self, view: TaskView) -> Sequence[Tool]:
        return self._tools

    async def after_tool(self, view: TaskView, call: ToolUse, output: ToolOutput) -> ToolOutput:
        await record_shell_reads(view, call, output)
        return output

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        # The earlier Read results are gone: a re-read must return content.
        if isinstance(recorded.event, Compacted):
            view.record(self.name, KIND_FORGET_RANGES, {})
        await remove_task_images(view, recorded)

    async def before_model(self, view: TaskView, request: ModelRequest) -> ModelRequest:
        st = view.ext_state(self.name)
        if not isinstance(st, FsState) or not st.attached:
            return request
        messages = hoist_attachments(request.messages, frozenset(st.attached))
        return msgspec.structs.replace(request, messages=messages)

    async def reminders(self, view: TaskView) -> Sequence[Reminder]:
        if self.mentions:
            await attach_mentions(view, self.home)
        await save_pasted_images(view)
        st = view.ext_state(self.name)
        if not isinstance(st, FsState):
            return ()
        # A tool's note rides right after the results of the call that made it.
        out = [Reminder(text=text, kind=KIND_NOTICE_SENT, data={"text": text})
               for text in st.notices]
        if view.env is None:
            return out
        watched = [(path, read) for path, read in st.reads.items() if not read.window]
        try:
            stats = await stat_many(view.env, [path for path, _ in watched])
        except OSError:
            return out
        for (path, read), stat in zip(watched, stats, strict=True):
            if stat is None or stat.is_dir or stat.mtime in (read.mtime, st.checked.get(path)):
                continue
            try:
                raw = await view.env.read_bytes(path)
            except OSError:
                continue
            digest = hashlib.sha256(raw).hexdigest()
            if digest in (read.digest, st.noticed.get(path)):
                # Known bytes under a new time: not hashed again until it moves.
                view.record(FS, KIND_CHECKED, {"path": path, "mtime": stat.mtime})
                continue
            text = view.prompt("reminder/file_changed", FILE_CHANGED_REMINDER).replace(
                "{path}", path)
            before = seen_text(view.state.messages, path, view.cwd or view.env.cwd)
            snippet = None
            if before is not None and not_text(raw) in (None, "not_utf8"):
                snippet = changed_lines(before, decode(raw).removeprefix("\ufeff"))
            if snippet is not None:
                text += CHANGES_SHOWN.format(snippet=snippet)
                # The model now has the file as it is: an edit needs no re-read.
                view.record(FS, KIND_READ, {"path": path, "digest": digest,
                                            "mtime": stat.mtime, "whole": False})
            else:
                text += CHANGES_NOT_SHOWN
            out.append(Reminder(text=text, kind=KIND_CHANGED, role="system",
                                order=ORDER_FILE_CHANGED,
                                data={"path": path, "digest": digest,
                                      "mtime": stat.mtime}))
        return out


def memory_dir(state: Any) -> list[str]:
    """The memory plugin's directory for the task (its ``dir`` record), as
    a one-item list; empty when the task keeps no memory."""
    memory = state.get("memory") if isinstance(state, dict) else None
    directory = memory.get("dir") if isinstance(memory, dict) else None
    return [directory] if isinstance(directory, str) and directory else []
