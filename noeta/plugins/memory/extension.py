"""The ``Memory`` extension: Claude Code's auto memory.

Memory is a directory of markdown files, one fact per file, under
``<noeta home>/projects/<workspace>/memory/`` (the workspace being the
repository's main checkout, else the working directory). The model writes
and edits the files with the ordinary ``Write`` / ``Edit`` tools, as the
memory section of the system prompt tells it. ``MEMORY.md``, the model's
index, is read when the task starts (its first 200 lines and 25,000 characters)
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
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import msgspec

from noeta.core import records
from noeta.core.errors import ModelError
from noeta.core.execenv import ExecEnv
from noeta.core.extension import ContextPart, Extension, Reminder, TaskView
from noeta.core.loop.state import TaskState
from noeta.core.types import Message, ModelRequest, SystemPart, ToolResult, user
from noeta.toolkit.context import read_task_text, task_env, task_files
from noeta.toolkit.local_env import is_local
from noeta.toolkit.memory_index import (
    MEMORY_DIR_KEY,
    MEMORY_INDEX_FILE,
    MEMORY_INDEX_MAX_CHARS,
    MEMORY_INDEX_MAX_LINES,
    read_memory_index,
    trim_memory_index,
)
from noeta.toolkit.paths import noeta_home, project_dir
from noeta.toolkit.tokens import has_cjk

__all__ = ["INDEX_FILE", "MEMORY_SECTION", "Memory", "load_index", "memory_dir_for"]

log = logging.getLogger("noeta.memory")

#: Recall models whose provider refused ``thinking: disabled``.
_THINKING_REFUSED: set[str] = set()
#: Selector failures already logged (once per process and reason).
_RECALL_WARNED: set[str] = set()
_NAME = re.compile(r"[^\s\"'`,\[\]()]+")


def selected_names(text: str, names: set[str]) -> list[str]:
    """The files the selector picked: its JSON's ``selected_memories``, or,
    when the reply is not that JSON (a provider that ignores the output
    schema), the listed names that appear in the text, in order."""
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("selected_memories"), list):
        return [p for p in data["selected_memories"] if isinstance(p, str)]
    found = [w.strip(".:;") for w in _NAME.findall(text)]
    return list(dict.fromkeys(w for w in found if w in names))


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
INDEX_MAX_CHARS = MEMORY_INDEX_MAX_CHARS
#: The index as attached when the agent has no instructions plugin to show
#: it: its path, then its text.
INDEX_HEAD = ("Contents of {path} (your auto memory index for this project, kept across "
              "conversations):")
#: The plugin that shows the index with the instruction files when it runs
#: after this one.
INSTRUCTIONS = "instructions"

#: Recall: memories attached per message, the selector's output ceiling, and
#: the characters attached over a whole task (Claude Code's budget counts
#: the attached text's length).
RECALL_MAX_FILES = 5
RECALL_MAX_TOKENS = 512
RECALL_TASK_CHARS = 61_440
#: Recalls kept so a hook's erase of their prompt can take them back (a
#: prompt is blocked before the model answers it: the newest few).
ERASABLE_RECALLS = 8
#: Erased prompt seqs remembered, for a recall recorded after the erase.
ERASED_SEQS_KEEP = 64
#: A recalled file shows at most its first lines and UTF-8 bytes, then
#: :data:`RECALL_CUT_NOTE` (Claude Code's limits: its file read counts
#: bytes here).
RECALL_FILE_MAX_LINES = 200
RECALL_FILE_MAX_BYTES = 4096
RECALL_CUT_NOTE = "> Only {what} of this memory are shown; Read {path} for the whole file."
#: Put ahead of a recalled file last changed more than a day ago.
RECALL_STALE_NOTE = (
    "This memory was last changed {days} days ago. A memory records what was true when it was "
    "written, not what is true now: what it says about the code, and any file:line it cites, "
    "may be out of date. Check against the current code before stating it as fact.")
RECALL_HEAD = ("Memories that may be relevant to this message; use one only if it really "
               "applies to what the user asked.")
_RECALL_SCHEMA = {
    "type": "object",
    "properties": {"selected_memories": {"type": "array", "items": {"type": "string"}}},
    "required": ["selected_memories"],
    "additionalProperties": False,
}


def load_index(text: str) -> str:
    """``MEMORY.md`` within its line and character limits, with a note when
    cut."""
    return trim_memory_index(text)


def memory_dir_for(root: str, home: Path | None = None) -> str:
    return str(project_dir(root, home or noeta_home()) / "memory") + "/"


_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(\n|\Z)", re.S)


def _field(head: str, key: str) -> str:
    m = re.search(rf"^\s*{key}:\s*(.+?)\s*$", head, re.M)
    return m.group(1).strip().strip("'\"") if m else ""


def recalled_text(path: str, text: str) -> str:
    """A recalled file's text within :data:`RECALL_FILE_MAX_LINES` lines and
    :data:`RECALL_FILE_MAX_BYTES` bytes, with a note saying where the rest
    is when it was cut."""
    what = ""
    lines = text.split("\n")
    if len(lines) > RECALL_FILE_MAX_LINES:
        text, what = "\n".join(lines[:RECALL_FILE_MAX_LINES]), \
            f"the first {RECALL_FILE_MAX_LINES} lines"
    encoded = text.encode("utf-8")
    if len(encoded) > RECALL_FILE_MAX_BYTES:
        kept = encoded[:RECALL_FILE_MAX_BYTES].decode("utf-8", errors="ignore")
        newline = kept.rfind("\n")
        text, what = (kept[:newline] if newline > 0 else kept), \
            f"the first {RECALL_FILE_MAX_BYTES} bytes"
    return f"{text}\n{RECALL_CUT_NOTE.format(what=what, path=path)}" if what else text


def _stale_note(mtime: float, now: float | None = None) -> str:
    """The note ahead of a memory changed more than a day ago (``""``
    otherwise), on its own line."""
    days = int(max(0.0, (now if now is not None else time.time()) - mtime) // 86_400)
    return RECALL_STALE_NOTE.format(days=days) + "\n" if days > 1 else ""


def _read_files(view: TaskView) -> set[str]:
    """The files the task's ``Read`` calls named: a memory already read is
    not recalled again."""
    out: set[str] = set()
    for m in view.state.history:
        for use in m.tool_uses if m.role == "assistant" else ():
            path = use.input.get("file_path") if use.name == "Read" else None
            if isinstance(path, str) and path:
                out.add(posixpath.normpath(path))
    return out


def _instructions_show_index(view: TaskView, name: str) -> bool:
    """Whether the instructions plugin runs after this one: it then shows
    ``MEMORY.md`` among the instruction files, as Claude Code does, reading
    the directory this plugin records (its ``dir`` record)."""
    names = [e.name for e in view.extensions]
    return name in names and INSTRUCTIONS in names[names.index(name) + 1:]


class Memory(Extension):
    """Constructor arguments:

    * ``home`` — noeta's home (default ``$NOETA_HOME`` or ``~/.noeta``); the
      memory directory is ``<home>/projects/<workspace>/memory/``.
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
            through = data.get("through") or state.get("through")
            if through is not None and through in (state.get("erased") or ()):
                # Recalled for a prompt a hook blocked (one listed after
                # this plugin): taken out of the conversation next round.
                return {**state, "through": through,
                        "voided": [*(state.get("voided") or ()), through]}
            recent = [*(state.get("recent") or ()),
                      {"through": through, "paths": data["paths"],
                       "bytes": int(data.get("bytes") or 0)}]
            return {**state, "surfaced": [*state["surfaced"], *data["paths"]],
                    "bytes": state["bytes"] + int(data.get("bytes") or 0),
                    "through": through, "recent": recent[-ERASABLE_RECALLS:]}
        if kind == "checked":
            return {**state, "through": data["through"]}
        if kind == records.ERASED_KIND:
            seqs = set(data["seqs"])
            state = {**state, "erased": [*(state.get("erased") or ()),
                                         *sorted(seqs)][-ERASED_SEQS_KEEP:]}
            for recall in [r for r in state.get("recent") or () if r["through"] in seqs]:
                gone = set(recall["paths"])
                state = {**state, "surfaced": [p for p in state["surfaced"] if p not in gone],
                         "bytes": max(0, state["bytes"] - recall["bytes"]),
                         "recent": [r for r in state["recent"] if r is not recall],
                         "voided": [*(state.get("voided") or ()), recall["through"]]}
            return state
        if kind == "unrecalled":
            return {**state, "voided": [t for t in state.get("voided") or ()
                                        if t != data["through"]]}
        return state

    def _dir(self, view: TaskView) -> str | None:
        return (view.ext_state(self.name) or {}).get("dir")

    def inherit(self, state: TaskState) -> Mapping[str, Any]:
        """The project's memory directory, for the sub-agents this task
        starts (an agent's own memory is not passed on)."""
        own = state.ext.get(self.name)
        directory = own.get("dir") if isinstance(own, dict) else None
        if self.agent_scope is not None or not isinstance(directory, str):
            return {}
        return {MEMORY_DIR_KEY: directory}

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
                or memory_dir_for(workspace, self.home)
        env = task_env(view.env, view.state.config)
        try:
            if is_local(env):
                Path(directory).mkdir(parents=True, exist_ok=True)
            else:  # the model's file tools write it there
                await env.run(["mkdir", "-p", directory], timeout=10)
        except Exception:  # noqa: BLE001 — written into by the first save anyway
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
        self._drop_voided(view)
        directory = self._dir(view)
        # A sub-agent recalls nothing: its caller chose its prompt.
        if not self.recall_model or view.complete is None or directory is None \
                or view.state.parent_id:
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
        query = "\n\n".join(m.text for _, m in new).strip()
        # A one-word message (Latin script) gives the selector nothing to go on.
        one_word = not any(c.isspace() for c in query) and not has_cjk(query)
        found = None if one_word else await self._recall(view, directory, query)
        if found is None:
            view.record(self.name, "checked", {"through": through})
            return ()
        text, paths, used = found
        return (Reminder(text=text, kind="recalled", origin="memory",
                         data={"paths": paths, "bytes": used, "through": through}),)

    def _drop_voided(self, view: TaskView) -> None:
        """Erase the recalls made for prompts a hook blocked: a hook listed
        after this plugin blocks a prompt after its recall was taken, so the
        recall is recorded behind the erase and outlives it."""
        voided = (view.ext_state(self.name) or {}).get("voided") or ()
        if not voided:
            return
        state = view.state
        for through in voided:
            # The recall's message is the first recalled memory after the
            # prompt it was made for (none: compacted away).
            seq = next((s for s, m in zip(state.message_seqs, state.messages, strict=False)
                        if s > through and m.origin == "memory"), None)
            if seq is not None:
                view.erase_messages([seq])
            view.record(self.name, "unrecalled", {"through": through})

    # ------------------------------------------------------------ helpers

    def _agent_dir(self, view: TaskView, workspace: str) -> str | None:
        """The agent's own memory: under noeta's home (``user``) or under
        ``workspace``, the main checkout, so an agent in a temporary
        worktree keeps it."""
        if self.agent_scope is None or self.agent_name is None:
            return None
        if self.agent_scope == "user":
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
            out = await env.run(["git", "rev-parse", "--path-format=absolute",
                                 "--git-common-dir"], cwd=cwd, timeout=5, output_cap=4096)
        except Exception:  # noqa: BLE001 — not a repository, or no git
            return cwd
        common = out.stdout.decode("utf-8", errors="replace").strip()
        if out.exit_code != 0 or not common or not common.rstrip("/").endswith("/.git"):
            return cwd
        main = posixpath.dirname(common.rstrip("/"))
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
                        skip: set[str]) -> list[tuple[str, float, str]]:
        """``(file name, modification time, manifest line)`` for every memory
        file not yet attached, newest first. Read where the model's file
        tools write them."""
        out: list[tuple[str, float, str]] = []
        for name, mtime in await task_files(env, directory, ".md"):
            path = Path(directory) / name
            if path.name == INDEX_FILE or str(path) in skip:
                continue
            text = await read_task_text(env, str(path))
            if text is None:
                continue
            m = _FRONTMATTER.match(text)
            head = m.group(1) if m else ""
            kind, desc = _field(head, "type"), _field(head, "description")
            when = datetime.fromtimestamp(mtime, tz=UTC).isoformat(timespec="seconds")
            line = f"- {f'[{kind}] ' if kind else ''}{path.name} ({when})"
            out.append((path.name, mtime, f"{line}: {desc}" if desc else line))
        out.sort(key=lambda t: -t[1])
        return out

    async def _recall(self, view: TaskView, directory: str,
                      query: str) -> tuple[str, list[str], int] | None:
        """The recalled text, the files attached and their characters (the
        state's ``bytes``, its name from before it counted characters);
        ``None`` when nothing is attached."""
        state = view.ext_state(self.name) or {}
        if int(state.get("bytes") or 0) >= RECALL_TASK_CHARS:
            return None
        env = task_env(view.env, view.state.config)
        skip = {*(state.get("surfaced") or ()), *_read_files(view)}
        manifest = await self._manifest(env, directory, skip)
        if not manifest:
            return None
        assert view.complete is not None and self.recall_model is not None
        listing = "\n".join(line for _, _, line in manifest)
        request = ModelRequest(
            model=self.recall_model,
            system=[SystemPart(text=view.prompt("memory/recall", RECALL_PROMPT), cache=True)],
            messages=[user(f"Memory files:\n{listing}\n\nPick the memories relevant to:\n"
                           f"{query}")],
            max_tokens=RECALL_MAX_TOKENS,
            thinking=None if self.recall_model in _THINKING_REFUSED else False,
            output_schema=_RECALL_SCHEMA)
        names = {name for name, _, _ in manifest}
        mtimes = {name: mtime for name, mtime, _ in manifest}
        try:
            try:
                response = await view.complete(request)
            except ModelError as exc:
                # Some gateways refuse ``thinking: disabled`` (HTTP 400): ask
                # again without the field, and from then on for this model.
                if exc.status != 400 or request.thinking is not False:
                    raise
                response = await view.complete(msgspec.structs.replace(request, thinking=None))
                _THINKING_REFUSED.add(self.recall_model)
            picked = selected_names(response.message.text, names)
        except Exception as exc:  # noqa: BLE001 — recall is a nicety; the turn goes on
            reason = f"{type(exc).__name__}: {exc}"[:300]
            if reason not in _RECALL_WARNED:
                _RECALL_WARNED.add(reason)
                log.warning("memory: recall selector failed (%s); no memory recalled", reason)
            return None
        budget = RECALL_TASK_CHARS - int(state.get("bytes") or 0)
        parts: list[str] = []
        paths: list[str] = []
        used = 0
        for name in [p for p in picked if isinstance(p, str) and p in mtimes][:RECALL_MAX_FILES]:
            path = Path(directory) / name
            body = await read_task_text(env, str(path))
            if body is None:
                continue
            shown = recalled_text(str(path), body.strip())
            size = len(shown)
            if used + size > budget:
                break
            used += size
            parts.append(f"{_stale_note(mtimes[name])}Memory {path}:\n{shown}")
            paths.append(str(path))
        if not parts:
            return None
        return RECALL_HEAD + "\n\n" + "\n\n".join(parts), paths, used
