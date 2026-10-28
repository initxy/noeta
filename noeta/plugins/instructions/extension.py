"""The ``Instructions`` extension: Claude Code's instruction-file layering,
in its order, over noeta's own files (``NOETA.md`` where Claude Code reads
``CLAUDE.md``, ``.noeta/`` where it reads ``.claude/``; Claude Code's files
are never read).

When a runner first picks the task up it reads, in this order, and records
as one message before the first prompt (a line saying these instructions
override default behavior, then each file's absolute path and kind, then
its text):

1. the machine's managed ``NOETA.md`` and ``.noeta/rules`` (``managed_dir``);
2. the user's ``NOETA.md`` and ``rules/`` (``user_dir``, ``~/.noeta``);
3. for every directory from the outermost ancestor of the working directory
   down to it: ``NOETA.md`` and ``.noeta/NOETA.md`` (or, in a directory
   with neither, its ``AGENTS.md``), the rules under ``.noeta/rules``, then
   ``NOETA.local.md``;
4. the auto memory index (``MEMORY.md``) when the agent runs the memory
   plugin before this one: that plugin records the directory, this one
   reads the index from it.

A rule whose frontmatter has ``paths:`` is left out there. When a ``Read``,
``Edit``, ``Write`` or ``NotebookEdit`` touches a file inside the working
directory, the files that apply to it and were not shown yet follow the
tool's result, one message each: the instruction files of every directory
between the working directory and the file, and the ``paths:`` rules (the
user's, the working directory's and its ancestors', and those directories')
whose globs match it. A compaction puts them all, read again, ahead of its
summary. See :mod:`noeta.plugins.instructions.files` for how a file is read.
"""

from __future__ import annotations

import posixpath
from collections.abc import Sequence
from typing import Any, Literal

from noeta.core.events import Recorded, ToolFinished
from noeta.core.execenv import ExecEnv
from noeta.core.extension import Extension, TaskView
from noeta.core.types import Message, ToolUse, user
from noeta.plugins.instructions.files import (
    FALLBACK_FILES,
    LOCAL,
    LOCAL_FILE,
    MANAGED,
    MEMORY,
    PROJECT,
    PROJECT_FILES,
    RULES_DIR,
    USER,
    InstructionFile,
    Reader,
    clean,
)
from noeta.toolkit.context import read_memory_index, read_text, simulated_state, task_env
from noeta.toolkit.tools import path_within

__all__ = ["DEFAULT_INSTRUCTIONS_FILENAMES", "Instructions", "render_file",
           "render_instructions"]

#: Read in a directory with no ``NOETA.md`` / ``.noeta/NOETA.md``.
DEFAULT_INSTRUCTIONS_FILENAMES = FALLBACK_FILES

PREAMBLE = ("Below are instructions from the codebase and the user. Follow them: they "
            "override your default behavior and must be followed exactly as written.")
LABELS = {
    MANAGED: "managed instructions set for this machine",
    USER: "the user's own instructions, for every project",
    PROJECT: "project instructions, part of the repository",
    LOCAL: "the user's private instructions for this project, not part of the repository",
    MEMORY: "your auto memory index for this project, kept across conversations",
}
#: The plugin whose recorded state names the memory directory.
MEMORY_PLUGIN = "memory"
#: Tools whose touching a file brings the instructions that apply to it.
FILE_TOOLS = {"Read": "file_path", "Edit": "file_path", "Write": "file_path",
              "NotebookEdit": "notebook_path"}


def render_file(path: str, kind: str, text: str) -> str:
    return f"{path} ({LABELS.get(kind, LABELS[PROJECT])}):\n\n{text}"


def render_instructions(files: Sequence[tuple[str, str, str]]) -> str:
    """The start-of-conversation message: ``(path, kind, text)`` per file."""
    return "\n\n".join([PREAMBLE, *(render_file(*f) for f in files)])


def _entry(f: InstructionFile, real: str) -> dict[str, str]:
    return {"path": f.path, "kind": f.kind, "real": real}


class Instructions(Extension):
    """Constructor arguments:

    * ``root`` — record the files of a conversation's start
      (``instructions_enabled``).
    * ``file`` — read this path instead of the repository's directory walk,
      relative to the workspace or absolute (``instructions_file``).
    * ``discovery`` — show the files that apply to a file a tool touches
      (``instructions_discovery``).
    * ``filenames`` — read, the first non-blank one, in a directory with no
      ``NOETA.md`` / ``.noeta/NOETA.md``.
    * ``user_dir`` — the user's directory (``~/.noeta``): its ``NOETA.md``
      and ``rules/``; ``None`` reads none.
    * ``managed_dir`` — the machine's managed directory (``NOETA.md`` and
      ``.noeta/rules``); ``None`` reads none.
    * ``local`` — read ``NOETA.local.md`` files.
    * ``walk`` — read the working directory's ancestors too, not only it.
    * ``rehang`` — put the files (read again) ahead of a compaction's
      summary (``lead_resident``).
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
        user_dir: str | None = None,
        managed_dir: str | None = None,
        local: bool = True,
        walk: bool = True,
        rehang: bool = True,
        origin: Literal["system"] | None = "system",
    ) -> None:
        if file is not None and not root:
            raise ValueError("Instructions: file is read only when root=True")
        self.root = root
        self.file = file
        self.discovery = discovery
        self.filenames = tuple(filenames)
        self.user_dir = posixpath.expanduser(user_dir) if user_dir else None
        self.managed_dir = managed_dir
        self.local = local
        self.walk = walk
        self.rehang = rehang
        self.origin = origin

    # ------------------------------------------------------------ state

    def initial_state(self) -> Any:
        return {"files": [], "nested": [], "root": False, "names": []}

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        state = {**self.initial_state(), **(state or {})}
        if kind == "start":
            return {**state, "files": list(data["files"]), "root": True}
        if kind == "nested":
            return {**state, "nested": [*state["nested"], *data["files"]]}
        if kind in ("root", "file"):  # a log written before the directory walk
            return {**state, "names": [*state["names"], data["name"]],
                    "root": state["root"] or kind == "root"}
        return state

    # ------------------------------------------------------------ helpers

    def _reader(self, view: TaskView) -> tuple[Reader, str]:
        env = task_env(view.env, view.state.config)
        cwd = str(view.state.config.get("cwd") or env.cwd)
        local = task_env(None, {"cwd": self.user_dir or "/"})
        return Reader(env=env, local=local, cwd=cwd), cwd

    def _dirs(self, cwd: str) -> list[str]:
        """The working directory's ancestors, outermost first (the
        filesystem root left out), then it."""
        if not self.walk:
            return [cwd]
        out: list[str] = []
        d = posixpath.normpath(cwd)
        while d != posixpath.dirname(d):
            out.insert(0, d)
            d = posixpath.dirname(d)
        return out or [cwd]

    async def _start_files(self, reader: Reader, cwd: str) -> list[InstructionFile]:
        out: list[InstructionFile] = []
        if self.managed_dir:
            out += await reader.file(posixpath.join(self.managed_dir, PROJECT_FILES[0]), MANAGED)
            out += await reader.rules(posixpath.join(self.managed_dir, RULES_DIR), MANAGED)
        if self.user_dir:
            out += await reader.file(posixpath.join(self.user_dir, PROJECT_FILES[0]), USER)
            out += await reader.rules(posixpath.join(self.user_dir, "rules"), USER)
        if self.file is not None:
            path = self.file if posixpath.isabs(self.file) else posixpath.join(cwd, self.file)
            return out + await reader.file(path, PROJECT)
        for d in self._dirs(cwd):
            out += await reader.directory(d, self.filenames)
            out += await reader.rules(posixpath.join(d, RULES_DIR), PROJECT)
            if self.local:
                out += await reader.file(posixpath.join(d, LOCAL_FILE), LOCAL)
        return out

    def _memory_dir(self, view: TaskView, *, pending: bool) -> str | None:
        state = simulated_state(view, {e.name: e.reduce for e in view.extensions}) \
            if pending else view.state
        memory = state.ext.get(MEMORY_PLUGIN)
        directory = memory.get("dir") if isinstance(memory, dict) else None
        return directory if isinstance(directory, str) else None

    def _memory_index(self, view: TaskView, *, pending: bool) -> tuple[str, str, str] | None:
        directory = self._memory_dir(view, pending=pending)
        found = read_memory_index(directory) if directory else None
        return (found[0], MEMORY, found[1]) if found else None

    # ------------------------------------------------------------ hooks

    async def on_start(self, view: TaskView) -> None:
        state = view.ext_state(self.name) or {}
        if not self.root or state.get("root"):
            return
        reader, cwd = self._reader(view)
        files = await self._start_files(reader, cwd)
        rendered = [(f.path, f.kind, f.text) for f in files]
        memory = self._memory_index(view, pending=True)
        if memory is not None:
            rendered.append(memory)
        if not rendered:
            return
        entries = [_entry(f, await _real(reader.env_for(f.kind), f.path)) for f in files]
        view.record(self.name, "start", {"files": entries},
                    message=user(render_instructions(rendered), origin=self.origin))

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        event = recorded.event
        if not self.discovery or not isinstance(event, ToolFinished) or event.result.is_error:
            return
        call = _find_call(view, event.result.call_id)
        key = FILE_TOOLS.get(call.name) if call is not None else None
        target = call.input.get(key) if call is not None and key else None
        if not isinstance(target, str) or not target:
            return
        reader, cwd = self._reader(view)
        env = reader.env
        try:
            root = await env.realpath(cwd)
            resolved = await env.realpath(target if posixpath.isabs(target)
                                          else posixpath.join(cwd, target))
        except OSError:
            return
        if not path_within(resolved, root) or resolved == root:
            return
        reader.cwd = root
        state = view.ext_state(self.name) or {}
        reader.seen.update(e["real"] for e in [*state.get("files", []), *state.get("nested", [])])
        reader.seen.update(posixpath.join(root, n) for n in state.get("names", []))
        for f in await self._nested_files(reader, root, resolved):
            real = await _real(reader.env_for(f.kind), f.path)
            view.record(self.name, "nested", {"files": [_entry(f, real)]},
                        message=user(render_file(f.path, f.kind, f.text), origin=self.origin))

    async def _nested_files(self, reader: Reader, root: str,
                            target: str) -> list[InstructionFile]:
        out: list[InstructionFile] = []
        if self.user_dir:
            out += await reader.rules(posixpath.join(self.user_dir, "rules"), USER,
                                      match=posixpath.relpath(target, root))
        for d in self._dirs(root):
            out += await reader.rules(posixpath.join(d, RULES_DIR), PROJECT,
                                      match=posixpath.relpath(target, d))
        d = root
        for part in posixpath.relpath(target, root).split("/")[:-1]:
            d = posixpath.join(d, part)
            out += await reader.directory(d, self.filenames)
            rules = posixpath.join(d, RULES_DIR)
            out += await reader.rules(rules, PROJECT)
            out += await reader.rules(rules, PROJECT, match=posixpath.relpath(target, d))
            if self.local:
                out += await reader.file(posixpath.join(d, LOCAL_FILE), LOCAL)
        return out

    async def lead_resident(self, view: TaskView) -> Sequence[Message]:
        """After a compaction, ahead of the summary: the files read again."""
        if not self.rehang:
            return ()
        reader, cwd = self._reader(view)
        state = view.ext_state(self.name) or {}
        start: list[tuple[str, str, str]] = []
        for e in state.get("files", []):
            text = await _reread(reader.env_for(e["kind"]), e["path"])
            if text is not None:
                start.append((e["path"], e["kind"], text))
        for name in state.get("names", []):
            source = self.file if "/" not in name and self.file is not None else name
            path = source if posixpath.isabs(source) else posixpath.join(cwd, source)
            text = await _reread(reader.env, path)
            if text is not None:
                start.append((path, PROJECT, text))
        memory = self._memory_index(view, pending=False)
        if memory is not None:
            start.append(memory)
        out = [user(render_instructions(start), origin=self.origin)] if start else []
        for e in state.get("nested", []):
            text = await _reread(reader.env_for(e["kind"]), e["path"])
            if text is not None:
                out.append(user(render_file(e["path"], e["kind"], text), origin=self.origin))
        return out


async def _real(env: ExecEnv, path: str) -> str:
    try:
        return await env.realpath(path)
    except OSError:
        return posixpath.normpath(path)


async def _reread(env: ExecEnv, path: str) -> str | None:
    raw = await read_text(env, path)
    if raw is None:
        return None
    text, _globs = clean(raw)
    return text or None


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
