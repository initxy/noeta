"""The file tools as one extension: Read, Write and Edit, the
fold of what they record (the files the model has read), and Claude Code's
reminder when one of those files changed on disk since.

The check runs at the start of each turn, over the most recently read
files: a file whose modification time moved is hashed, and if its bytes are
neither what the model read nor what it was already told about, the model is
told once (the prompt ``reminder/file_changed``; ``{path}`` is the file).
``Edit`` and ``Write`` still refuse until it reads the file again."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

from noeta.core.events import Compacted, Recorded
from noeta.core.extension import Extension, Reminder, TaskView
from noeta.core.tool import Tool
from noeta.plugins.fs.edit import EditTool
from noeta.plugins.fs.read import ReadTool
from noeta.plugins.fs.state import FS, KIND_CHANGED, KIND_FORGET_RANGES, FsState, reduce_fs
from noeta.plugins.fs.write import WriteTool

__all__ = ["EditTool", "FileTools", "ReadTool", "WriteTool"]


#: How many of the most recently read files the turn-start check looks at.
CHANGED_FILES_CHECKED = 20
#: Claude Code's wording for a file changed outside the model's own edits, with
#: a re-read asked for instead of its snippet of the new contents.
FILE_CHANGED_REMINDER = (
    "Note: {path} was modified after you last read it, either by the user or by a linter. "
    "Don't tell the user this, since they are already aware. This change was intentional, "
    "so make sure to take it into account as you proceed (ie. don't revert it unless the "
    "user asks you to). Read the file again before you edit it."
)

_BY_NAME: dict[str, type[Tool]] = {
    "Read": ReadTool, "Write": WriteTool, "Edit": EditTool,
}


class FileTools(Extension):
    """``tools``: which file tools to offer, by name or as tool objects
    (default: all three, in the order Read, Write, Edit)."""

    name = FS

    def __init__(self, tools: Sequence[str | Tool] | None = None) -> None:
        if tools is None:
            tools = list(_BY_NAME)
        unknown = [t for t in tools if isinstance(t, str) and t not in _BY_NAME]
        if unknown:
            raise ValueError(f"FileTools: unknown tools {unknown}; known: {list(_BY_NAME)}")
        self._tools: list[Tool] = [_BY_NAME[t]() if isinstance(t, str) else t for t in tools]

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
        if view.state.steps_in_turn or view.env is None or not isinstance(st, FsState):
            return ()
        out: list[Reminder] = []
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
