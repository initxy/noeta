"""The file tools as one extension: Read, Write, Edit, Grep and Glob, and
the fold of what they record (the files the model has read)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from noeta.core.extension import Extension, TaskView
from noeta.core.tool import Tool
from noeta.plugins.fs.edit import EditTool
from noeta.plugins.fs.glob import GlobTool
from noeta.plugins.fs.grep import GrepTool
from noeta.plugins.fs.read import ReadTool
from noeta.plugins.fs.state import FS, reduce_fs
from noeta.plugins.fs.write import WriteTool

__all__ = ["EditTool", "FileTools", "GlobTool", "GrepTool", "ReadTool", "WriteTool"]


_BY_NAME: dict[str, type[Tool]] = {
    "Read": ReadTool, "Write": WriteTool, "Edit": EditTool, "Grep": GrepTool, "Glob": GlobTool,
}


class FileTools(Extension):
    """``tools``: which file tools to offer, by name or as tool objects
    (default: all five, in the order Read, Write, Edit, Grep, Glob)."""

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
