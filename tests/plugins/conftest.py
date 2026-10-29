"""A tiny fake ``ToolContext``: a ``LocalExecEnv`` over a temp workspace, and
a ``record`` that folds ``fs`` entries through ``reduce_fs`` at once — what the
runner does between calls."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.core.tool import Tool, ToolContext, ToolOutput
from noeta.core.types import Image, Text
from noeta.plugins.fs.state import FS, FsState, reduce_fs
from noeta.plugins.shell.bash import SHELL, ShellState, reduce_shell, shell_state


@dataclass
class Harness:
    env: LocalExecEnv
    state: dict[str, Any] = field(default_factory=dict)
    records: list[tuple[str, str, Any]] = field(default_factory=list)
    spill_dir: str | None = None
    task_id: str = "task-1"
    calls: int = 0
    #: Calls run as if a person allowed them at an approval prompt.
    approved: bool = False
    #: Calls run as if a permission layer allowed them by its settings.
    allowed_outright: bool = False
    model: str = ""

    @property
    def root(self) -> Path:
        return Path(self.env.cwd)

    @property
    def fs(self) -> FsState:
        value = self.state.get(FS)
        return value if isinstance(value, FsState) else FsState()

    @property
    def shell(self) -> ShellState:
        return shell_state(self.state)

    def record(self, ext: str, kind: str, data: Any) -> None:
        self.records.append((ext, kind, data))
        if ext == FS:
            self.state[FS] = reduce_fs(self.state.get(FS), kind, data)
        elif ext == SHELL:
            self.state[SHELL] = reduce_shell(self.state.get(SHELL), kind, data)

    def ctx(self, call_id: str | None = None) -> ToolContext:
        self.calls += 1
        return ToolContext(
            task_id=self.task_id,
            call_id=call_id or f"call-{self.calls}",
            env=self.env,
            state=self.state,
            record=self.record,
            spill_dir=self.spill_dir,
            approved=self.approved,
            allowed_outright=self.allowed_outright,
            model=self.model,
        )

    async def call(self, tool: Tool, **input: Any) -> ToolOutput:
        return await tool.run(input, self.ctx())


def text_of(out: ToolOutput) -> str:
    return "".join(b.text for b in out.content if isinstance(b, Text))


def images_of(out: ToolOutput) -> list[Image]:
    return [b for b in out.content if isinstance(b, Image)]


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    return Path(os.path.realpath(root))


@pytest.fixture
def h(ws: Path) -> Harness:
    return Harness(env=LocalExecEnv(str(ws)))
