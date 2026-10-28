"""The ``Memory`` extension: Claude Code's auto memory.

Memory is a directory of markdown files, one fact per file, under
``<noeta home>/projects/<workspace>/memory/`` (the workspace being the
repository's main checkout, else the working directory). The model writes
and edits the files with the ordinary ``Write`` / ``Edit`` tools, as the
memory section of the system prompt tells it. ``MEMORY.md``, the model's
index, is read when the task starts (its first 200 lines and 25,000 bytes)
and again after a compaction. With a ``recall_model``, each user message
asks that model which memory files bear on it and attaches up to five.
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

from noeta.core.events import Recorded
from noeta.core.extension import ContextPart, Extension, TaskView
from noeta.core.types import Message, ModelRequest, SystemPart, user
from noeta.toolkit.context import new_user_messages, noeta_home, project_dir, task_env

__all__ = ["INDEX_FILE", "MEMORY_SECTION", "Memory", "load_index", "memory_dir_for"]

log = logging.getLogger("noeta.memory")


def _load(name: str) -> str:
    return (resources.files("noeta.plugins.memory") / "prompts" / f"{name}.md").read_text(
        "utf-8").strip()


#: The system prompt's memory section (``memory/section``); ``{dir}`` is the
#: memory directory.
MEMORY_SECTION = _load("section")
#: The recall selector's instructions (``memory/recall``).
RECALL_PROMPT = _load("recall")

INDEX_FILE = "MEMORY.md"
#: How much of ``MEMORY.md`` is loaded (Claude Code's limits).
INDEX_MAX_LINES = 200
INDEX_MAX_BYTES = 25_000
#: The index as attached: its path, then its text.
INDEX_HEAD = ("Contents of {path} (your auto memory index for this project, kept across "
              "conversations):")
INDEX_CUT = ("> Note: {name} is {size}; only part of it was loaded ({what}). Keep each index "
             "entry to one short line and move details into the memory files.")

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
    trimmed = text.strip()
    lines = trimmed.split("\n")
    size = len(trimmed.encode("utf-8"))
    over_lines, over_bytes = len(lines) > INDEX_MAX_LINES, size > INDEX_MAX_BYTES
    if not over_lines and not over_bytes:
        return trimmed
    kept = "\n".join(lines[:INDEX_MAX_LINES])
    if len(kept.encode("utf-8")) > INDEX_MAX_BYTES:
        cut = kept.encode("utf-8")[:INDEX_MAX_BYTES].decode("utf-8", errors="ignore")
        newline = cut.rfind("\n")
        kept = cut[:newline] if newline > 0 else cut
    shown = kept.count("\n") + 1
    if over_lines and not over_bytes:
        size_text = f"{len(lines)} lines (limit {INDEX_MAX_LINES})"
    elif over_bytes and not over_lines:
        size_text = f"{size} bytes (limit {INDEX_MAX_BYTES})"
    else:
        size_text = f"{len(lines)} lines and {size} bytes"
    note = INDEX_CUT.format(name=INDEX_FILE, size=size_text,
                            what=f"the first {shown} of {len(lines)} lines")
    return f"{kept}\n{note}"


def memory_dir_for(root: str, home: Path | None = None) -> str:
    return str(project_dir(root, home or noeta_home()) / "memory") + "/"


_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(\n|\Z)", re.S)


def _field(head: str, key: str) -> str:
    m = re.search(rf"^\s*{key}:\s*(.+?)\s*$", head, re.M)
    return m.group(1).strip().strip("'\"") if m else ""


class Memory(Extension):
    """Constructor arguments:

    * ``home`` — noeta's home (default ``$NOETA_HOME`` or ``~/.noeta``); the
      memory directory is ``<home>/projects/<workspace>/memory/``.
    * ``directory`` — use this directory instead.
    * ``recall_model`` — the model asked, on each user message, which memory
      files to attach (off when unset).
    * ``rehang`` — read ``MEMORY.md`` again after a compaction.
    """

    name = "memory"

    def __init__(self, *, home: str | None = None, directory: str | None = None,
                 recall_model: str | None = None, rehang: bool = True) -> None:
        self.home = Path(home).expanduser() if home else None
        self.directory = directory
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
                    "bytes": state["bytes"] + int(data.get("bytes") or 0)}
        return state

    def _dir(self, view: TaskView) -> str | None:
        return (view.ext_state(self.name) or {}).get("dir")

    # ------------------------------------------------------------ prompt

    def context(self, view: TaskView) -> Sequence[ContextPart]:
        directory = self._dir(view)
        if directory is None:
            return ()
        text = view.prompt("memory/section", MEMORY_SECTION).replace("{dir}", directory)
        return (ContextPart(text=text),)

    # ------------------------------------------------------------ hooks

    async def on_start(self, view: TaskView) -> None:
        if self._dir(view) is not None:
            return
        directory = self.directory or memory_dir_for(await self._workspace(view), self.home)
        try:
            Path(directory).mkdir(parents=True, exist_ok=True)
        except OSError:
            log.warning("memory: could not create %s", directory)
        view.record(self.name, "dir", {"dir": directory})
        index = self._index(directory)
        if index is not None:
            view.record(self.name, "index", {}, message=user(index, origin="system"))

    async def lead_resident(self, view: TaskView) -> Sequence[Message]:
        directory = self._dir(view)
        if not self.rehang or directory is None:
            return ()
        index = self._index(directory)
        return (user(index, origin="system"),) if index is not None else ()

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        directory = self._dir(view)
        if not self.recall_model or view.complete is None or directory is None:
            return
        messages = new_user_messages(view.state, recorded.seq)
        if messages:
            await self._recall(view, directory, "\n\n".join(m.text for m in messages))

    # ------------------------------------------------------------ helpers

    async def _workspace(self, view: TaskView) -> str:
        """The repository's main checkout when the working directory is in
        one (so every worktree shares one memory), else the directory."""
        env = task_env(view.env, view.state.config)
        cwd = str(view.state.config.get("cwd") or env.cwd)
        try:
            out = await env.run(["git", "rev-parse", "--path-format=absolute",
                                 "--git-common-dir"], cwd=cwd, timeout=5, output_cap=4096)
        except Exception:  # noqa: BLE001 — not a repository, or no git
            return cwd
        common = out.stdout.decode("utf-8", errors="replace").strip()
        if out.exit_code != 0 or not common:
            return cwd
        return posixpath.dirname(common.rstrip("/")) if common.rstrip("/").endswith("/.git") \
            else cwd

    @staticmethod
    def _index(directory: str) -> str | None:
        path = Path(directory) / INDEX_FILE
        try:
            text = path.read_text("utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        if not text.strip():
            return None
        return f"{INDEX_HEAD.format(path=path)}\n\n{load_index(text)}"

    @staticmethod
    def _manifest(directory: str, skip: set[str]) -> list[tuple[str, str]]:
        """``(file name, manifest line)`` for every memory file not yet attached."""
        out: list[tuple[str, float, str]] = []
        for path in Path(directory).glob("*.md"):
            if path.name == INDEX_FILE or str(path) in skip:
                continue
            try:
                text = path.read_text("utf-8")
                mtime = path.stat().st_mtime
            except (OSError, UnicodeDecodeError):
                continue
            m = _FRONTMATTER.match(text)
            head = m.group(1) if m else ""
            kind, desc = _field(head, "type"), _field(head, "description")
            when = datetime.fromtimestamp(mtime, tz=UTC).isoformat(timespec="seconds")
            line = f"- {f'[{kind}] ' if kind else ''}{path.name} ({when})"
            out.append((path.name, mtime, f"{line}: {desc}" if desc else line))
        out.sort(key=lambda t: -t[1])
        return [(name, line) for name, _, line in out]

    async def _recall(self, view: TaskView, directory: str, query: str) -> None:
        state = view.ext_state(self.name) or {}
        if int(state.get("bytes") or 0) >= RECALL_TASK_BYTES:
            return
        manifest = self._manifest(directory, set(state.get("surfaced") or []))
        if not manifest:
            return
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
            return
        names = {name for name, _ in manifest}
        budget = RECALL_TASK_BYTES - int(state.get("bytes") or 0)
        parts: list[str] = []
        paths: list[str] = []
        used = 0
        for name in [p for p in picked if isinstance(p, str) and p in names][:RECALL_MAX_FILES]:
            path = Path(directory) / name
            try:
                body = path.read_text("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            size = len(body.encode("utf-8"))
            if used + size > budget:
                break
            used += size
            parts.append(f"Memory {path}:\n{body.strip()}")
            paths.append(str(path))
        if parts:
            view.record(self.name, "recalled", {"paths": paths, "bytes": used},
                        message=user(RECALL_HEAD + "\n\n" + "\n\n".join(parts), origin="memory"))
