"""The file and shell tools as one extension: it offers Read, Write, Edit,
Bash, Grep and Glob, and folds what they record (files read, shell cwd)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from noeta.ext.base import Extension, TaskView
from noeta.tools import BashTool, EditTool, GlobTool, GrepTool, ReadTool, WriteTool
from noeta.tools.base import Tool
from noeta.tools.fs_state import FS, reduce_fs


class FileTools(Extension):
    name = FS

    def __init__(self, tools: Sequence[Tool] | None = None) -> None:
        self._tools: list[Tool] = list(tools) if tools is not None else [
            ReadTool(), WriteTool(), EditTool(), BashTool(), GrepTool(), GlobTool(),
        ]

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        return reduce_fs(state, kind, data)

    def tools(self, view: TaskView) -> Sequence[Tool]:
        return self._tools
