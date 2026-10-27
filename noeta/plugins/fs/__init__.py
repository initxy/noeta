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


class FileTools(Extension):
    name = FS

    def __init__(self, tools: Sequence[Tool] | None = None) -> None:
        self._tools: list[Tool] = list(tools) if tools is not None else [
            ReadTool(), WriteTool(), EditTool(), GrepTool(), GlobTool(),
        ]

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        return reduce_fs(state, kind, data)

    def tools(self, view: TaskView) -> Sequence[Tool]:
        return self._tools
