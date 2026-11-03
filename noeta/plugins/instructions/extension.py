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
   reads the index from it; a sub-agent without the memory plugin is shown
   its caller's index (Claude Code's sub-agents get it, but no memory
   section).

A rule whose frontmatter has ``paths:`` is left out there. When a ``Read``,
``Edit``, ``Write`` or ``NotebookEdit`` touches a file inside the working
directory (or a ``Bash`` command shows one, ``cat`` or ``head`` and the like:
the file tools record it as read), the files that apply to it and were not
shown yet follow the tool's result, one message each (its path, no kind):
the instruction files of every directory
between the working directory and the file, and the ``paths:`` rules (the
user's, the working directory's and its ancestors', and those directories')
whose globs match it. A compaction puts them all, read again, ahead of its
summary. With each new message from the person, the files shown so far are
read again and those whose text changed are shown again (Claude Code does so
when a conversation is resumed; a durable task may resume with any message,
on any worker). See :mod:`noeta.plugins.instructions.files` for how a file is read.
"""

from __future__ import annotations

import hashlib
import logging
import posixpath
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from noeta.core.events import ExtensionRecorded, Recorded, ToolFinished
from noeta.core.execenv import ExecEnv
from noeta.core.extension import Extension, TaskView
from noeta.core.loop.state import TaskState
from noeta.core.models import model_limits
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
from noeta.toolkit.context import new_user_messages, read_text, task_env
from noeta.toolkit.memory_index import MEMORY_DIR_KEY, MEMORY_INDEX_FILE, read_memory_index
from noeta.toolkit.tokens import chars_per_token
from noeta.toolkit.tools import path_within

__all__ = ["DEFAULT_INSTRUCTIONS_FILENAMES", "Instructions", "render_file",
           "render_instructions"]

log = logging.getLogger(__name__)

#: Read in a directory with no ``NOETA.md`` / ``.noeta/NOETA.md``.
DEFAULT_INSTRUCTIONS_FILENAMES = FALLBACK_FILES

PREAMBLE = ("Below are instructions from the codebase and the user. Follow them: they "
            "override your default behavior and must be followed exactly as written.")
#: Heads the files shown again because their text changed, and those that
#: appeared since.
REREAD_HEAD = (
    "These instruction files are new, or changed since they were shown earlier in this "
    "conversation, and were read again; each text below replaces the one shown before, if "
    "any. The note that opened the instructions still applies.")
#: Told to the person (never the model) once per instruction file a ``Read``
#: deny rule keeps out of the conversation (Claude Code's
#: ``withheld_memory``, which it shows the person and sends the model nothing).
WITHHELD_NOTICE = "Instruction file not loaded (a Read deny rule covers it): {path}"
#: One line per file shown earlier that is gone now.
REMOVED_LINE = "This instruction file is gone and its instructions no longer apply: {path}"
#: One line per file shown earlier that now has nothing to show (emptied, or
#: only comments).
EMPTIED_LINE = "This instruction file is now empty and its instructions no longer apply: {path}"
LABELS = {
    MANAGED: "managed instructions set for this machine",
    USER: "the user's own instructions, for every project",
    PROJECT: "project instructions, part of the repository",
    LOCAL: "the user's private instructions for this project, not part of the repository",
    MEMORY: "your auto memory index for this project, kept across conversations",
}
#: The plugin whose recorded state names the memory directory.
MEMORY_PLUGIN = "memory"
#: A nested file (one a touched file brings) longer than the larger of
#: these is cut: characters, and the share of the model's window in
#: characters (Claude Code's limit).
NESTED_MAX_CHARS = 40_000
NESTED_WINDOW_SHARE = 0.05
#: Closes a nested file cut to its start.
NESTED_CUT_NOTE = ("> Only the start of this file is shown here; Read {path} from offset={offset} "
                   "for the rest.")
#: Tools whose touching a file brings the instructions that apply to it.
FILE_TOOLS = {"Read": "file_path", "Edit": "file_path", "Write": "file_path",
              "NotebookEdit": "notebook_path"}
#: The file tools' record of a file a ``Bash`` command showed (extension,
#: kind, its ``by``): it brings the file's instructions as a ``Read`` would,
#: as in Claude Code.
SHELL_READ = ("fs", "read", "Bash")


def render_file(path: str, kind: str, text: str) -> str:
    return f"{path} ({LABELS.get(kind, LABELS[PROJECT])}):\n\n{text}"


def render_nested(path: str, text: str) -> str:
    """A file a touched file brought: its path line names no kind (Claude
    Code's nested file reminder)."""
    return f"{path}:\n\n{text}"


def render_instructions(files: Sequence[tuple[str, str, str]]) -> str:
    """The start-of-conversation message: ``(path, kind, text)`` per file."""
    return "\n\n".join([PREAMBLE, *(render_file(*f) for f in files)])


def nested_max_chars(model: str) -> int:
    """The longest nested instruction file shown whole for ``model``."""
    window, _ = model_limits(model) if model else (0, 0)
    return max(NESTED_MAX_CHARS, round(window * NESTED_WINDOW_SHARE * chars_per_token(model)))


def cut_nested(path: str, text: str, limit: int) -> str:
    """``text`` within ``limit`` characters, cut at a line end, with a note
    saying where the rest starts."""
    if len(text) <= limit:
        return text
    kept = text[:limit]
    newline = kept.rfind("\n")
    kept = kept[:newline] if newline > 0 else kept
    offset = kept.count("\n") + 2
    return f"{kept}\n{NESTED_CUT_NOTE.format(path=path, offset=offset)}"


def _entry(f: InstructionFile, real: str, mtime: float | None = None, *,
           whole: bool = True) -> dict[str, Any]:
    """What the log keeps of a shown file (the hooks plugin reads ``path``,
    ``kind``, ``globs`` and ``parent`` for InstructionsLoaded; the file
    tools read ``real``, ``mtime`` and ``whole`` to answer a ``Read`` of a
    file shown whole and unchanged since with a short note, as Claude Code
    does)."""
    out: dict[str, Any] = {"path": f.path, "kind": f.kind, "real": real,
                           "digest": text_digest(f.text)}
    if mtime is not None:
        out["mtime"] = mtime
    if not whole:
        out["whole"] = False
    if f.globs:
        out["globs"] = list(f.globs)
    if f.parent:
        out["parent"] = f.parent
    return out


def text_digest(text: str) -> str:
    """What the task remembers of a shown file's text to see it changed."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


#: The digest of a shown file left with nothing to show.
EMPTY_DIGEST = text_digest("")


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
    * ``managed_only`` — read only the managed directory's files: no user,
      project or local ones, no memory index, nothing shown for a touched
      file (an agent file's ``omitClaudeMd``).
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
        managed_only: bool = False,
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
        self.managed_only = managed_only
        if managed_only:
            self.discovery = False

    # ------------------------------------------------------------ state

    def initial_state(self) -> Any:
        return {"files": [], "nested": [], "root": False, "names": [], "withheld": []}

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        state = {**self.initial_state(), **(state or {})}
        if kind == "start":
            return {**state, "files": list(data["files"]), "root": True,
                    "digests": {**_digests(data["files"]), **(data.get("memory") or {})}}
        if kind == "nested":
            return {**state, "nested": [*state["nested"], *data["files"]],
                    "digests": {**(state.get("digests") or {}), **_digests(data["files"])}}
        if kind in ("reread", "sent"):
            gone = set(data.get("removed") or ())
            digests = {p: d for p, d in {**(state.get("digests") or {}),
                                         **data["digests"]}.items() if p not in gone}
            if not gone and not data.get("files"):
                return {**state, "digests": digests}
            return {**state, "digests": digests,
                    "files": [*(e for e in state["files"] if e["path"] not in gone),
                              *(data.get("files") or ())],
                    "nested": [e for e in state["nested"] if e["path"] not in gone]}
        if kind == "withheld":
            return {**state, "withheld": [*state["withheld"], *data["paths"]]}
        if kind in ("root", "file"):  # a log written before the directory walk
            return {**state, "names": [*state["names"], data["name"]],
                    "root": state["root"] or kind == "root"}
        return state

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _model(view: TaskView) -> str:
        return str(view.state.config.get("model") or view.model or "")

    def _reader(self, view: TaskView) -> tuple[Reader, str]:
        env = task_env(view.env, view.state.config)
        cwd = view.cwd or env.cwd
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
        if self.managed_only:
            return out
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
        """The memory plugin's directory; for a sub-agent without that
        plugin, the one its caller passed on (``MEMORY_DIR_KEY``)."""
        state = view.folded({e.name: e.reduce for e in view.extensions}) \
            if pending else view.state
        memory = state.ext.get(MEMORY_PLUGIN)
        directory = memory.get("dir") if isinstance(memory, dict) else None
        if directory is None and not any(e.name == MEMORY_PLUGIN for e in view.extensions):
            directory = state.config.get(MEMORY_DIR_KEY)
        return directory if isinstance(directory, str) else None

    def inherit(self, state: TaskState) -> Mapping[str, Any]:
        """The memory directory this task was given, for its own sub-agents."""
        directory = state.config.get(MEMORY_DIR_KEY)
        return {MEMORY_DIR_KEY: directory} if isinstance(directory, str) else {}

    async def _memory_index(self, view: TaskView, *,
                            pending: bool) -> tuple[str, str, str] | None:
        directory = None if self.managed_only else self._memory_dir(view, pending=pending)
        env = task_env(view.env, view.state.config)
        found = await read_memory_index(env, directory) if directory else None
        if found is None or await self._withhold(view, [found[0]]):
            return None
        return (found[0], MEMORY, found[1])

    async def _withhold(self, view: TaskView, paths: Sequence[str]) -> set[str]:
        """Those of ``paths`` a ``Read`` deny rule covers (asked of the
        plugin that keeps the rules, ``read_deny_rule``; a check that fails
        withholds too). Each is told to the person once, never the model."""
        check = next((fn for e in view.extensions
                      if callable(fn := getattr(e, "read_deny_rule", None))), None)
        if check is None or not paths:
            return set()
        out: set[str] = set()
        for path in paths:
            try:
                rule = await check(view, path)
            except Exception:
                log.warning("instructions: could not check %s against the deny rules", path,
                            exc_info=True)
                rule = True
            if rule:
                out.add(path)
        told = set((view.ext_state(self.name) or {}).get("withheld") or ())
        new = sorted(out - told)
        if new:
            view.record(self.name, "withheld", {"paths": new})
            for path in new:
                view.notify(WITHHELD_NOTICE.format(path=path))
        return out

    async def _allowed(self, view: TaskView,
                       files: Sequence[InstructionFile]) -> list[InstructionFile]:
        """``files`` without those :meth:`_withhold` keeps out."""
        out = await self._withhold(view, [f.path for f in files])
        return [f for f in files if f.path not in out]

    # ------------------------------------------------------------ hooks

    async def on_start(self, view: TaskView) -> None:
        state = view.ext_state(self.name) or {}
        if not self.root or state.get("root"):
            return
        reader, cwd = self._reader(view)
        files = await self._allowed(view, await self._start_files(reader, cwd))
        rendered = [(f.path, f.kind, f.text) for f in files]
        memory = await self._memory_index(view, pending=True)
        if memory is not None:
            rendered.append(memory)
        if not rendered:
            return
        entries = [_entry(f, await _real(reader.env_for(f.kind), f.path),
                          await _mtime(reader.env_for(f.kind), f.path)) for f in files]
        data: dict[str, Any] = {"files": entries}
        if memory is not None:
            data["memory"] = {memory[0]: text_digest(memory[2])}
        view.record(self.name, "start", data,
                    message=user(render_instructions(rendered), origin=self.origin))

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        if new_user_messages(view.state, recorded.seq):
            await self._reread_changed(view)
        target = _touched(view, recorded.event) if self.discovery else None
        if target is None:
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
        limit = nested_max_chars(self._model(view))
        for f in await self._allowed(view, await self._nested_files(reader, root, resolved)):
            real = await _real(reader.env_for(f.kind), f.path)
            text = f.text if f.kind == MANAGED else cut_nested(f.path, f.text, limit)
            entry = _entry(f, real, await _mtime(reader.env_for(f.kind), f.path),
                           whole=text == f.text)
            view.record(self.name, "nested", {"files": [entry], "trigger": resolved},
                        message=user(render_nested(f.path, text), origin=self.origin))

    async def _reread_changed(self, view: TaskView) -> None:
        """The files shown so far, read again, and the conversation's start
        files looked for again: those whose text changed and those that
        appeared since are shown under :data:`REREAD_HEAD`, and each shown
        file that is gone is named (:data:`REMOVED_LINE`); the memory index
        too, also when it is new (Claude Code's re-read compares the whole
        set of files with what it showed, its auto-memory index included)."""
        state = view.ext_state(self.name) or {}
        digests: dict[str, str] = state.get("digests") or {}
        if not self.root:
            return
        reader, cwd = self._reader(view)
        limit = nested_max_chars(self._model(view))
        changed: list[tuple[str, str, str]] = []
        fresh: dict[str, str] = {}
        removed: list[str] = []
        emptied: list[str] = []
        shown = [*state.get("files", []), *state.get("nested", [])]
        known = {e["path"] for e in shown} | set(state.get("names", []))
        current = {f.path: f for f in await self._start_files(reader, cwd)}
        nested = {e["path"] for e in state.get("nested", [])}
        for e in shown:
            path, before = e["path"], digests.get(e["path"])
            if before is None or path in fresh or path in removed:
                continue
            text = current[path].text if path in current \
                else await _reread(reader.env_for(e["kind"]), path, empty="")
            if text is None:
                # Gone only when it is not there: a read that failed (a
                # remote env's hiccup) keeps what was shown.
                if await _absent(reader.env_for(e["kind"]), path):
                    removed.append(path)
                continue
            if text == "":
                # Left with nothing to show (emptied, or only comments):
                # its instructions no longer apply. Its digest becomes the
                # empty text's, so later checks pass it by until it changes.
                if before != EMPTY_DIGEST:
                    fresh[path] = EMPTY_DIGEST
                    emptied.append(path)
                continue
            if text_digest(text) == before:
                continue
            fresh[path] = text_digest(text)
            if path in nested and e["kind"] != MANAGED:
                text = cut_nested(path, text, limit)
            changed.append((path, e["kind"], text))
        added = await self._allowed(view, [f for p, f in current.items() if p not in known])
        entries = [_entry(f, await _real(reader.env_for(f.kind), f.path),
                          await _mtime(reader.env_for(f.kind), f.path)) for f in added]
        for f in added:
            fresh[f.path] = text_digest(f.text)
            changed.append((f.path, f.kind, f.text))
        memory = await self._memory_index(view, pending=False)
        if memory is not None and digests.get(memory[0]) != text_digest(memory[2]):
            fresh[memory[0]] = text_digest(memory[2])
            changed.append(memory)
        directory = None if self.managed_only else self._memory_dir(view, pending=False)
        index = str(Path(directory) / MEMORY_INDEX_FILE) if directory else None
        if memory is None and index in digests:
            removed.append(index)
        if not changed and not removed and not emptied:
            return
        body = "\n\n".join([REREAD_HEAD, *(render_file(*f) for f in changed)]) \
            if changed else ""
        lines = "\n".join([*(REMOVED_LINE.format(path=p) for p in removed),
                           *(EMPTIED_LINE.format(path=p) for p in emptied)])
        data: dict[str, Any] = {"digests": fresh}
        if entries:
            data["files"] = entries
        if removed:
            data["removed"] = removed
        view.record(self.name, "reread", data,
                    message=user("\n\n".join(t for t in (body, lines) if t),
                                 origin=self.origin))

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
        sent: dict[str, str] = {}
        for e in state.get("files", []):
            text = await _reread(reader.env_for(e["kind"]), e["path"])
            if text is not None:
                start.append((e["path"], e["kind"], text))
                sent[e["path"]] = text_digest(text)
        for name in state.get("names", []):
            source = self.file if "/" not in name and self.file is not None else name
            path = source if posixpath.isabs(source) else posixpath.join(cwd, source)
            text = await _reread(reader.env, path)
            if text is not None:
                start.append((path, PROJECT, text))
        memory = await self._memory_index(view, pending=False)
        if memory is not None:
            start.append(memory)
        out = [user(render_instructions(start), origin=self.origin)] if start else []
        index = {memory[0]: text_digest(memory[2])} if memory is not None else {}
        limit = nested_max_chars(self._model(view))
        for e in state.get("nested", []):
            text = await _reread(reader.env_for(e["kind"]), e["path"])
            if text is not None:
                sent[e["path"]] = text_digest(text)
                if e["kind"] != MANAGED:
                    text = cut_nested(e["path"], text, limit)
                out.append(user(render_nested(e["path"], text), origin=self.origin))
        known = state.get("digests") or {}
        sent = {p: d for p, d in sent.items() if p in known and known[p] != d}
        sent.update({p: d for p, d in index.items() if known.get(p) != d})
        if sent:
            view.record(self.name, "sent", {"digests": sent})
        return out


def _digests(entries: Sequence[dict[str, Any]]) -> dict[str, str]:
    """The digests of recorded entries (an entry recorded before digests
    were kept has none, and is never shown again)."""
    return {e["path"]: e["digest"] for e in entries if e.get("digest")}


async def _mtime(env: ExecEnv, path: str) -> float | None:
    try:
        st = await env.stat(path)
    except OSError:
        return None
    return st.mtime if st is not None else None


async def _real(env: ExecEnv, path: str) -> str:
    try:
        return await env.realpath(path)
    except OSError:
        return posixpath.normpath(path)


async def _reread(env: ExecEnv, path: str, *, empty: str | None = None) -> str | None:
    """The file's text to show; ``None`` when it cannot be read, ``empty``
    when it has nothing to show."""
    raw = await read_text(env, path)
    if raw is None:
        return None
    text, _globs = clean(raw)
    return text or empty


async def _absent(env: ExecEnv, path: str) -> bool:
    """Whether ``path`` is surely not there (a failed look says no)."""
    try:
        return await env.stat(path) is None
    except Exception:  # noqa: BLE001 — cannot tell: not gone
        return False


def _touched(view: TaskView, event: Any) -> str | None:
    """The file ``event`` shows the model touched: the path a successful
    file tool's call names, or one a ``Bash`` command showed (the file
    tools' ``read`` record of it, :data:`SHELL_READ`), unless a deny rule
    keeps that file from ``Read`` (Claude Code skips those too)."""
    if isinstance(event, ExtensionRecorded):
        data = event.data
        if (event.extension, event.kind) != SHELL_READ[:2] or not isinstance(data, dict) \
                or data.get("by") != SHELL_READ[2] or data.get("read_denied"):
            return None
        path = data.get("path")
        return path if isinstance(path, str) and path else None
    if not isinstance(event, ToolFinished) or event.result.is_error:
        return None
    call = _find_call(view, event.result.call_id)
    key = FILE_TOOLS.get(call.name) if call is not None else None
    target = call.input.get(key) if call is not None and key else None
    return target if isinstance(target, str) and target else None


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
