"""The file tools as one extension: Read, Write, Edit and NotebookEdit, the
fold of what they record (the files the model has read), and Claude Code's
reminder when one of those files changed on disk since.

The check runs at the start of each turn, over the most recently read
files: a file whose modification time moved is hashed, and if its bytes are
neither what the model read nor what it was already told about, the model is
told once (the prompt ``reminder/file_changed``; ``{path}`` is the file).
``Edit`` and ``Write`` still refuse until it reads the file again.

A tool's own note for the model (``notice``: the paging hint of a read cut
to fit the token ceiling) is sent as a reminder right after that call's
result, as Claude Code does."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from typing import Any

from noeta.core.events import Compacted, Recorded
from noeta.core.extension import Extension, Reminder, TaskView
from noeta.core.tool import Tool
from noeta.plugins.fs.edit import EditTool
from noeta.plugins.fs.notebook import NotebookEditTool
from noeta.plugins.fs.read import ReadTool
from noeta.plugins.fs.state import (
    FS,
    KIND_CHANGED,
    KIND_FORGET_RANGES,
    KIND_NOTICE_SENT,
    FsState,
    reduce_fs,
)
from noeta.plugins.fs.write import WriteTool
from noeta.toolkit.paths import plans_dir

__all__ = ["EditTool", "FileTools", "NotebookEditTool", "ReadTool", "WriteTool"]


#: How many of the most recently read files the turn-start check looks at.
CHANGED_FILES_CHECKED = 20
#: The note for a file changed outside the model's own edits (Claude Code's
#: meaning), with a re-read asked for instead of a snippet of the new contents.
FILE_CHANGED_REMINDER = (
    "Note: {path} changed after you last read it, edited by the user or by a tool such as "
    "a formatter. The user knows and meant it, so do not mention it and do not undo it "
    "unless they ask; take it into account from here on. Read the file again before you "
    "edit it."
)

_BY_NAME: dict[str, type[Tool]] = {
    "Read": ReadTool, "Write": WriteTool, "Edit": EditTool, "NotebookEdit": NotebookEditTool,
}


class FileTools(Extension):
    """``tools``: which file tools to offer, by name or as tool objects
    (default: all four, in the order Read, Write, Edit, NotebookEdit).
    ``write_roots``: directories outside the working directory the named
    tools may write (Claude Code's ``additionalDirectories``); plan mode's
    plan directory is always one."""

    name = FS

    def __init__(self, tools: Sequence[str | Tool] | None = None, *,
                 write_roots: Sequence[str] = ()) -> None:
        if tools is None:
            tools = list(_BY_NAME)
        unknown = [t for t in tools if isinstance(t, str) and t not in _BY_NAME]
        if unknown:
            raise ValueError(f"FileTools: unknown tools {unknown}; known: {list(_BY_NAME)}")
        extra = tuple(os.path.expanduser(d) for d in write_roots)

        def roots(_ctx: Any) -> list[str]:
            return [*extra, str(plans_dir())]

        self._tools: list[Tool] = [
            (_BY_NAME[t](write_roots=roots) if t != "Read" else _BY_NAME[t]())  # type: ignore[call-arg]
            if isinstance(t, str) else t for t in tools]

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        return reduce_fs(state, kind, data)

    def tools(self, view: TaskView) -> Sequence[Tool]:
        return self._tools

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        # The earlier Read results are gone: a re-read must return content.
        if isinstance(recorded.event, Compacted):
            view.record(self.name, KIND_FORGET_RANGES, {})

    async def reminders(self, view: TaskView) -> Sequence[Reminder]:
        st = view.ext_state(self.name)
        if not isinstance(st, FsState):
            return ()
        # A tool's note rides right after the results of the call that made it.
        out = [Reminder(text=text, kind=KIND_NOTICE_SENT, data={"text": text})
               for text in st.notices]
        if view.state.steps_in_turn or view.env is None:
            return out
        for path, read in list(st.reads.items())[-CHANGED_FILES_CHECKED:]:
            try:
                stat = await view.env.stat(path)
                if stat is None or stat.is_dir or stat.mtime == read.mtime:
                    continue
                digest = hashlib.sha256(await view.env.read_bytes(path)).hexdigest()
            except OSError:
                continue
            if digest in (read.digest, st.noticed.get(path)):
                continue
            text = view.prompt("reminder/file_changed", FILE_CHANGED_REMINDER)
            out.append(Reminder(text=text.replace("{path}", path), kind=KIND_CHANGED,
                                data={"path": path, "digest": digest}))
        return out
