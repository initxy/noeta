"""The ``Memory`` extension: Claude Code's auto memory.

Memory is a directory of markdown files, one fact per file, under
``<noeta home>/projects/<workspace>/memory/`` (the workspace being the
repository's main checkout, else the working directory). The model writes
and edits the files with the ordinary ``Write`` / ``Edit`` tools, as the
memory section of the system prompt tells it. ``MEMORY.md``, the model's
index, is read when the task starts (its first 200 lines and 25,000 bytes)
and again after a compaction; when the agent runs the instructions plugin
after this one, that plugin shows it with the instruction files (Claude
Code's place for it), reading the directory this plugin records. With a
``recall_model``, each user message asks that model which memory files bear
on it and attaches up to five.
"""

from __future__ import annotations

import json
import logging
import posixpath
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from noeta.core.events import DEFAULT_TENANT
from noeta.core.execenv import ExecEnv
from noeta.core.extension import ContextPart, Extension, Reminder, TaskView
from noeta.core.types import Message, ModelRequest, SystemPart, ToolResult, user
from noeta.toolkit.context import fence_refuses, read_local_text, task_env
from noeta.toolkit.memory_index import (
    MEMORY_INDEX_FILE,
    MEMORY_INDEX_MAX_BYTES,
    MEMORY_INDEX_MAX_LINES,
    read_memory_index,
    trim_memory_index,
)
from noeta.toolkit.paths import noeta_home, project_dir
from noeta.toolkit.sandbox import run_in_tenant_sandbox

__all__ = ["INDEX_FILE", "MEMORY_SECTION", "Memory", "load_index", "memory_dir_for"]

log = logging.getLogger("noeta.memory")


def _load(name: str) -> str:
    return (resources.files("noeta.plugins.memory") / "prompts" / f"{name}.md").read_text(
        "utf-8").strip()


#: The system prompt's memory section (``memory/section``); ``{dir}`` is the
#: memory directory.
MEMORY_SECTION = _load("section")
#: The section goes ahead of the agent prompt's environment section, as
#: Claude Code places it (after the session-specific guidance).
ENVIRONMENT_HEADING = "# Environment"
#: The recall selector's instructions (``memory/recall``).
RECALL_PROMPT = _load("recall")

#: Claude Code's agent memory scopes (an agent file's ``memory``).
AGENT_SCOPES = ("user", "project", "local")

INDEX_FILE = MEMORY_INDEX_FILE
INDEX_MAX_LINES = MEMORY_INDEX_MAX_LINES
INDEX_MAX_BYTES = MEMORY_INDEX_MAX_BYTES
#: The index as attached when the agent has no instructions plugin to show
#: it: its path, then its text.
INDEX_HEAD = ("Contents of {path} (your auto memory index for this project, kept across "
              "conversations):")
#: The plugin that shows the index with the instruction files when it runs
#: after this one.
INSTRUCTIONS = "instructions"

#: Recall: memories attached per message, the selector's output ceiling, and
#: the bytes attached over a whole task.
RECALL_MAX_FILES = 5
RECALL_MAX_TOKENS = 512
RECALL_TASK_BYTES = 61_440
RECALL_HEAD = ("Memories that may be relevant to this message; use one only if it really "
               "applies to what the user asked.")
_RECALL_SCHEMA = {
    "type": "object",
    "properties": {"selected_memories": {"type": "array", "items": {"type": "string"}}},
    "required": ["selected_memories"],
    "additionalProperties": False,
}


def load_index(text: str) -> str:
    """``MEMORY.md`` within its line and byte limits, with a note when cut."""
    return trim_memory_index(text)


def memory_dir_for(root: str, home: Path | None = None, tenant: str = DEFAULT_TENANT) -> str:
    return str(project_dir(root, home or noeta_home(), tenant) / "memory") + "/"


_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(\n|\Z)", re.S)


def _field(head: str, key: str) -> str:
    m = re.search(rf"^\s*{key}:\s*(.+?)\s*$", head, re.M)
    return m.group(1).strip().strip("'\"") if m else ""


def _instructions_show_index(view: TaskView, name: str) -> bool:
    """Whether the instructions plugin runs after this one: it then shows
    ``MEMORY.md`` among the instruction files, as Claude Code does, reading
    the directory this plugin records (its ``dir`` record)."""
    names = [e.name for e in view.extensions]
    return name in names and INSTRUCTIONS in names[names.index(name) + 1:]


class Memory(Extension):
    """Constructor arguments:

    * ``home`` — noeta's home (default ``$NOETA_HOME`` or ``~/.noeta``); the
      memory directory is ``<home>/projects/<workspace>/memory/`` (a tenant
      other than the default one: ``<home>/projects/<tenant>/<workspace>/memory/``).
    * ``directory`` — use this directory instead.
    * ``recall_model`` — the model asked, on each user message, which memory
      files to attach (off when unset).
    * ``rehang`` — read ``MEMORY.md`` again after a compaction.
    * ``agent_scope`` / ``agent_name`` — an agent file's ``memory`` field
      (Claude Code's agent memory): the directory is the agent's own,
      ``<home>/agent-memory/<agent>/`` (``user``),
      ``<cwd>/.noeta/agent-memory/<agent>/`` (``project``) or
      ``<cwd>/.noeta/agent-memory-local/<agent>/`` (``local``).
    """

    name = "memory"

    def __init__(self, *, home: str | None = None, directory: str | None = None,
                 recall_model: str | None = None, rehang: bool = True,
                 agent_scope: str | None = None, agent_name: str | None = None) -> None:
        if agent_scope is not None and (agent_scope not in AGENT_SCOPES or not agent_name):
            raise ValueError(f"Memory: agent_scope is one of {', '.join(AGENT_SCOPES)}, "
                             "with an agent_name")
        self.home = Path(home).expanduser() if home else None
        self.directory = directory
        self.agent_scope = agent_scope
        self.agent_name = agent_name
        self.recall_model = recall_model
        self.rehang = rehang

    # ------------------------------------------------------------ state

    def initial_state(self) -> Any:
        return {"dir": None, "surfaced": [], "bytes": 0}

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        state = state or self.initial_state()
        if kind == "dir":
            return {**state, "dir": data["dir"]}
        if kind == "recalled":
            return {**state, "surfaced": [*state["surfaced"], *data["paths"]],
                    "bytes": state["bytes"] + int(data.get("bytes") or 0),
                    "through": data.get("through") or state.get("through")}
        if kind == "checked":
            return {**state, "through": data["through"]}
        return state

    def _dir(self, view: TaskView) -> str | None:
        return (view.ext_state(self.name) or {}).get("dir")

    # ------------------------------------------------------------ prompt

    def context(self, view: TaskView) -> Sequence[ContextPart]:
        directory = self._dir(view)
        if directory is None:
            return ()
        text = view.prompt("memory/section", MEMORY_SECTION).replace("{dir}", directory)
        return (ContextPart(text=text, before=ENVIRONMENT_HEADING),)

    # ------------------------------------------------------------ hooks

    async def on_start(self, view: TaskView) -> None:
        if self._dir(view) is not None:
            return
        directory = self.directory
        if not directory:
            workspace = await self._workspace(view)
            directory = self._agent_dir(view, workspace) \
                or memory_dir_for(workspace, self.home, view.state.tenant)
        try:
            # Made by the worker: not where the task's tenant may not write
            # (a link on the way, or a repository's shared directory outside
            # the workspace).
            if fence_refuses(task_env(view.env, view.state.config), directory, write=True):
                raise OSError("the tenant fence refuses it")
            Path(directory).mkdir(parents=True, exist_ok=True)
        except OSError:
            log.warning("memory: could not create %s", directory)
        view.record(self.name, "dir", {"dir": directory})
        index = None if _instructions_show_index(view, self.name) \
            else await self._index(view, directory)
        if index is not None:
            view.record(self.name, "index", {}, message=user(index, origin="system"))

    async def lead_resident(self, view: TaskView) -> Sequence[Message]:
        directory = self._dir(view)
        if not self.rehang or directory is None or _instructions_show_index(view, self.name):
            return ()
        index = await self._index(view, directory)
        return (user(index, origin="system"),) if index is not None else ()

    async def reminders(self, view: TaskView) -> Sequence[Reminder]:
        """Recall for the person's messages the model has not answered yet,
        before the call that first carries them. Run as a reminder, not when
        a message arrives, so a ``UserPromptSubmit`` hook listed before this
        plugin has blocked (and erased) a prompt before its text reaches the
        recall model."""
        directory = self._dir(view)
        if not self.recall_model or view.complete is None or directory is None:
            return ()
        state = view.state
        after = int((view.ext_state(self.name) or {}).get("through") or 0)
        new: list[tuple[int, Message]] = []
        for seq, m in zip(state.message_seqs, state.messages, strict=False):
            if m.role == "assistant":
                new = []
            elif seq > after and m.from_person and m.text \
                    and not any(isinstance(b, ToolResult) for b in m.content):
                new.append((seq, m))
        if not new:
            return ()
        through = new[-1][0]
        found = await self._recall(view, directory, "\n\n".join(m.text for _, m in new))
        if found is None:
            view.record(self.name, "checked", {"through": through})
            return ()
        text, paths, used = found
        return (Reminder(text=text, kind="recalled", origin="memory",
                         data={"paths": paths, "bytes": used, "through": through}),)

    # ------------------------------------------------------------ helpers

    def _agent_dir(self, view: TaskView, workspace: str) -> str | None:
        """The agent's own memory: under noeta's home (``user``) or under
        ``workspace``, the main checkout, so an agent in a temporary
        worktree keeps it."""
        if self.agent_scope is None or self.agent_name is None:
            return None
        # "user" memory under noeta's home is shared by every tenant: a task
        # of a tenant other than the default one keeps it in its working
        # directory instead.
        if self.agent_scope == "user" and getattr(view.state, "tenant",
                                                  DEFAULT_TENANT) == DEFAULT_TENANT:
            base = self.home or noeta_home()
            return str(base / "agent-memory" / self.agent_name) + "/"
        folder = "agent-memory-local" if self.agent_scope == "local" else "agent-memory"
        return posixpath.join(workspace, ".noeta", folder, self.agent_name) + "/"

    async def _workspace(self, view: TaskView) -> str:
        """The repository's main checkout when the working directory is in
        one (so every worktree shares one memory), else the directory."""
        env = task_env(view.env, view.state.config)
        cwd = view.cwd or env.cwd
        try:
            out = await run_in_tenant_sandbox(
                env, ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                cwd=cwd, timeout=5, output_cap=4096)
        except Exception:  # noqa: BLE001 — not a repository, or no git
            return cwd
        common = out.stdout.decode("utf-8", errors="replace").strip()
        if out.exit_code != 0 or not common or not common.rstrip("/").endswith("/.git"):
            return cwd
        main = posixpath.dirname(common.rstrip("/"))
        # A ``.git`` file may name any repository; another tenant's task
        # shares a main checkout's memory only from inside it.
        if view.state.tenant != DEFAULT_TENANT and not (
                cwd == main or cwd.startswith(main.rstrip("/") + "/")):
            return cwd
        return main

    @staticmethod
    async def _index(view: TaskView, directory: str) -> str | None:
        found = await read_memory_index(task_env(view.env, view.state.config), directory)
        if found is None:
            return None
        path, text = found
        return f"{INDEX_HEAD.format(path=path)}\n\n{text}"

    @staticmethod
    async def _manifest(env: ExecEnv, directory: str,
                        skip: set[str]) -> list[tuple[str, str]]:
        """``(file name, manifest line)`` for every memory file not yet
        attached. Read on this machine, behind the task's tenant fence (an
        agent's memory may be in the tenant's workspace)."""
        if fence_refuses(env, directory):
            return []
        out: list[tuple[str, float, str]] = []
        for path in Path(directory).glob("*.md"):
            if path.name == INDEX_FILE or str(path) in skip:
                continue
            text = await read_local_text(env, str(path))
            if text is None:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            m = _FRONTMATTER.match(text)
            head = m.group(1) if m else ""
            kind, desc = _field(head, "type"), _field(head, "description")
            when = datetime.fromtimestamp(mtime, tz=UTC).isoformat(timespec="seconds")
            line = f"- {f'[{kind}] ' if kind else ''}{path.name} ({when})"
            out.append((path.name, mtime, f"{line}: {desc}" if desc else line))
        out.sort(key=lambda t: -t[1])
        return [(name, line) for name, _, line in out]

    async def _recall(self, view: TaskView, directory: str,
                      query: str) -> tuple[str, list[str], int] | None:
        """The recalled text, the files attached and their bytes; ``None``
        when nothing is attached."""
        state = view.ext_state(self.name) or {}
        if int(state.get("bytes") or 0) >= RECALL_TASK_BYTES:
            return None
        env = task_env(view.env, view.state.config)
        manifest = await self._manifest(env, directory, set(state.get("surfaced") or []))
        if not manifest:
            return None
        assert view.complete is not None and self.recall_model is not None
        listing = "\n".join(line for _, line in manifest)
        request = ModelRequest(
            model=self.recall_model,
            system=[SystemPart(text=view.prompt("memory/recall", RECALL_PROMPT), cache=True)],
            messages=[user(f"Memory files:\n{listing}\n\nPick the memories relevant to:\n"
                           f"{query}")],
            max_tokens=RECALL_MAX_TOKENS, thinking=False, output_schema=_RECALL_SCHEMA)
        try:
            response = await view.complete(request)
            picked = json.loads(response.message.text).get("selected_memories") or []
        except Exception:
            log.info("memory: recall selection failed", exc_info=True)
            return None
        names = {name for name, _ in manifest}
        budget = RECALL_TASK_BYTES - int(state.get("bytes") or 0)
        parts: list[str] = []
        paths: list[str] = []
        used = 0
        for name in [p for p in picked if isinstance(p, str) and p in names][:RECALL_MAX_FILES]:
            path = Path(directory) / name
            body = await read_local_text(env, str(path))
            if body is None:
                continue
            size = len(body.encode("utf-8"))
            if used + size > budget:
                break
            used += size
            parts.append(f"Memory {path}:\n{body.strip()}")
            paths.append(str(path))
        if not parts:
            return None
        return RECALL_HEAD + "\n\n" + "\n\n".join(parts), paths, used
