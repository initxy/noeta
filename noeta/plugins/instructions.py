"""Workspace instructions: the root ``NOETA.md`` / ``AGENTS.md`` /
``CLAUDE.md``, and the nested ones a ``Read`` walks past.

The root file is read through the task's ExecEnv when a runner picks the task
up and recorded once, before the first message, as Claude Code shows a
``CLAUDE.md``: a line saying these instructions override default behavior,
then the file's absolute path and kind, then its text. A successful ``Read`` of a file
inside the workspace walks the directories between the root and the file
(shallowest first); each directory's first non-empty candidate is recorded
once, right after the read's results, under its workspace-relative path.
Discovery is fenced to the workspace even though reads are not.
"""

from __future__ import annotations

import posixpath
from collections.abc import Sequence
from typing import Any, Literal

from noeta.core.events import Recorded, ToolFinished
from noeta.core.execenv import ExecEnv
from noeta.core.extension import Extension, TaskView
from noeta.core.types import Message, ToolUse, user
from noeta.toolkit.context import read_text, task_env
from noeta.toolkit.tools import path_within

__all__ = ["DEFAULT_INSTRUCTIONS_FILENAMES", "Instructions", "render_instructions"]

#: Search order: NOETA.md is canonical, AGENTS.md the cross-vendor
#: convention, CLAUDE.md last (a repo with both conventionally makes
#: CLAUDE.md include AGENTS.md).
DEFAULT_INSTRUCTIONS_FILENAMES = ("NOETA.md", "AGENTS.md", "CLAUDE.md")


def render_instructions(path: str, text: str) -> str:
    """``path`` is the file's absolute path."""
    return ("Below are instructions from the codebase and the user. Follow them: they "
            "override your default behavior and must be followed exactly as written.\n\n"
            f"{path} (project instructions, part of the repository):\n\n{text}")


async def _read_one(env: ExecEnv, path: str) -> str | None:
    """The file's text, or ``None`` when missing, unreadable or blank."""
    text = await read_text(env, path)
    return text if text is not None and text.strip() else None


class Instructions(Extension):
    """Constructor arguments (0.6 ``HostConfig``):

    * ``root`` — record the workspace-root file (``instructions_enabled``).
    * ``file`` — read this path instead of searching (``instructions_file``),
      relative to the workspace or absolute.
    * ``discovery`` — record nested files found after a ``Read``
      (``instructions_discovery``).
    * ``filenames`` — the search order.
    * ``rehang`` — put the files (read again) right after a compaction's
      summary (``resident``).
    * ``origin`` — origin of the recorded messages (``"system"``).
    """

    name = "instructions"

    def __init__(
        self,
        *,
        root: bool = True,
        file: str | None = None,
        discovery: bool = True,
        filenames: Sequence[str] = DEFAULT_INSTRUCTIONS_FILENAMES,
        rehang: bool = True,
        origin: Literal["system"] | None = "system",
    ) -> None:
        if file is not None and not root:
            raise ValueError("Instructions: file is read only when root=True")
        self.root = root
        self.file = file
        self.discovery = discovery
        self.filenames = tuple(filenames)
        self.rehang = rehang
        self.origin = origin

    def initial_state(self) -> Any:
        return {"names": [], "root": False}

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        state = state or self.initial_state()
        if kind in ("root", "file"):
            return {"names": [*state["names"], data["name"]],
                    "root": state["root"] or kind == "root"}
        return state

    def _record(self, view: TaskView, kind: str, name: str, path: str, text: str) -> None:
        view.record(self.name, kind, {"name": name},
                    message=user(render_instructions(path, text), origin=self.origin))

    async def on_start(self, view: TaskView) -> None:
        state = view.ext_state(self.name) or {}
        if not self.root or state.get("root"):
            return
        env = task_env(view.env, view.state.config)
        candidates = [self.file] if self.file is not None else list(self.filenames)
        for candidate in candidates:
            path = candidate if posixpath.isabs(candidate) else posixpath.join(env.cwd, candidate)
            text = await _read_one(env, path)
            if text is not None:
                self._record(view, "root", posixpath.basename(path), path, text)
                return

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        event = recorded.event
        if not self.discovery or not isinstance(event, ToolFinished) or event.result.is_error:
            return
        call = _find_call(view, event.result.call_id)
        if call is None or call.name != "Read":
            return
        target = call.input.get("file_path")
        if not isinstance(target, str) or not target:
            return
        env = task_env(view.env, view.state.config)
        try:
            root = await env.realpath(env.cwd)
            resolved = await env.realpath(target)
        except OSError:
            return
        if not path_within(resolved, root) or resolved == root:
            return
        active = set((view.ext_state(self.name) or {}).get("names", []))
        parts = posixpath.relpath(resolved, root).split("/")[:-1]
        directory = root
        rel = ""
        for part in parts:
            directory = posixpath.join(directory, part)
            rel = posixpath.join(rel, part) if rel else part
            names = [posixpath.join(rel, f) for f in self.filenames]
            if any(n in active for n in names):
                continue
            for filename, name in zip(self.filenames, names, strict=True):
                path = posixpath.join(directory, filename)
                text = await _read_one(env, path)
                if text is not None:
                    self._record(view, "file", name, path, text)
                    active.add(name)
                    break

    async def resident(self, view: TaskView) -> Sequence[Message]:
        if not self.rehang:
            return ()
        env = task_env(view.env, view.state.config)
        out: list[Message] = []
        for name in (view.ext_state(self.name) or {}).get("names", []):
            source = self.file if "/" not in name and self.file is not None else name
            path = source if posixpath.isabs(source) else posixpath.join(env.cwd, source)
            text = await _read_one(env, path)
            if text is not None:
                out.append(user(render_instructions(path, text), origin=self.origin))
        return out


def _find_call(view: TaskView, call_id: str) -> ToolUse | None:
    """The call a result answers: in the open batch, else in the latest
    assistant messages."""
    for c in view.state.calls:
        if c.use.id == call_id:
            return c.use
    for message in reversed(view.state.messages):
        if message.role == "assistant":
            for use in message.tool_uses:
                if use.id == call_id:
                    return use
    return None
