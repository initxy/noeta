"""What one plugin publishes for others to read, behind one typed reader
each, and the plugins' names, defined once.

A plugin's folded state is its own: another plugin reads it only through a
reader here, which checks the shape and gives the reader's default when the
plugin is absent or its state is not what the reader expects. Each record
reader takes the folded extension states by name (``view.state.ext``,
``TaskState.ext``, ``ToolContext.state``). The few methods one plugin calls
on another (the permissions plugin's ``mode_of``, ``plan_start_mode``,
``working_directories``, ``read_deny_rule``; the instructions plugin's
``rehang`` and ``managed_only``) are read here too, by the plugin's name, so a
replacement keeps them under the same name. ``tests/toolkit/
test_plugin_records.py`` holds each writer to its reader.

Who reads what:

* memory's ``dir`` — instructions, permissions, fs, compaction;
* shell's ``cwd`` — permissions, hooks, fs, compaction, project_status;
* permissions' ``mode`` (plan_mode writes its switches there) — hooks,
  shell, front ends; its ``classified`` records — hooks;
* plan_mode's ``plan`` — compaction;
* instructions' files — hooks, fs, compaction; its ``start`` and
  ``reread`` texts — permissions' auto-mode classifier;
* fs's ``read`` records — instructions (a ``Bash`` read), compaction;
* mcp's ``down`` and ``too_long`` — tool_search;
* the methods above — plan_mode (``mode_of``, ``plan_start_mode``),
  environment (``working_directories``), instructions (``read_deny_rule``),
  compaction (``rehang``), permissions (``managed_only``);
* checkpoints' ``snapshot``, memory's ``dir``, title's ``title``,
  compaction's ``threshold`` — front ends."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from noeta.core.events import ExtensionRecorded, Recorded
from noeta.core.extension import Extension, TaskView

__all__ = [
    "AUTO_COMPACT_KIND",
    "CHECKPOINTS",
    "COMPACTION",
    "FS",
    "HOOKS",
    "INSTRUCTIONS",
    "MCP",
    "MEMORY",
    "PERMISSIONS",
    "PLAN_MODE",
    "SHELL",
    "TITLE",
    "AutoCompact",
    "auto_compact",
    "files_shown_by_commands",
    "instruction_digests",
    "instructions_managed_only",
    "instructions_rehang",
    "mcp_down",
    "mcp_names_too_long",
    "memory_dir",
    "mode_of",
    "permission_mode",
    "plan_file",
    "plan_start_mode",
    "plugin",
    "read_deny_rule",
    "shell_cwd",
    "shown_instruction_files",
    "working_directories",
]

CHECKPOINTS = "checkpoints"
COMPACTION = "compaction"
FS = "fs"
HOOKS = "hooks"
INSTRUCTIONS = "instructions"
MCP = "mcp"
MEMORY = "memory"
PERMISSIONS = "permissions"
PLAN_MODE = "plan_mode"
SHELL = "shell"
TITLE = "title"


# ---------------------------------------------------------------- records


def permission_mode(ext: Mapping[str, Any]) -> str | None:
    """The permission mode the task last switched to (the permissions
    plugin's folded ``mode``; ``None``: none recorded)."""
    mode = getattr(ext.get(PERMISSIONS), "mode", None)
    return mode if isinstance(mode, str) and mode else None


def shell_cwd(ext: Mapping[str, Any]) -> str | None:
    """Where the shell's next command starts (the shell plugin's ``cwd``
    record; ``None``: where the task started)."""
    cwd = getattr(ext.get(SHELL), "cwd", None)
    return cwd if isinstance(cwd, str) and cwd else None


def memory_dir(ext: Mapping[str, Any]) -> str | None:
    """The memory plugin's directory (its ``dir`` record)."""
    memory = ext.get(MEMORY)
    directory = memory.get("dir") if isinstance(memory, Mapping) else None
    return directory if isinstance(directory, str) and directory else None


def shown_instruction_files(ext: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The instruction files the instructions plugin showed (its ``start``,
    ``nested`` and ``reread`` records): each ``{"path", "real", "mtime",
    "whole", ...}``."""
    state = ext.get(INSTRUCTIONS)
    if not isinstance(state, Mapping):
        return []
    return [e for e in [*(state.get("files") or ()), *(state.get("nested") or ())]
            if isinstance(e, Mapping)]


def instruction_digests(ext: Mapping[str, Any]) -> Mapping[str, str]:
    """Path -> digest of each file the instructions plugin gave the model
    (the memory index among them when it showed it)."""
    state = ext.get(INSTRUCTIONS)
    digests = state.get("digests") if isinstance(state, Mapping) else None
    return digests if isinstance(digests, Mapping) else {}


def plan_file(ext: Mapping[str, Any]) -> str | None:
    """The plan file of the task's plan (the plan-mode plugin's folded
    ``plan``, kept after planning ends)."""
    plan = getattr(ext.get(PLAN_MODE), "plan", None)
    path = plan.get("path") if isinstance(plan, Mapping) else None
    return path if isinstance(path, str) and path else None


def files_shown_by_commands(ext: Mapping[str, Any]) -> dict[str, list[str]]:
    """``Bash`` call id -> the resolved paths of the files that call showed
    (the file tools' ``read`` records with a ``call_id``)."""
    reads = getattr(ext.get(FS), "reads", None)
    out: dict[str, list[str]] = {}
    for path, read in (reads.items() if isinstance(reads, Mapping) else ()):
        call_id = getattr(read, "call_id", None)
        if isinstance(call_id, str) and isinstance(path, str):
            out.setdefault(call_id, []).append(path)
    return out


def mcp_down(ext: Mapping[str, Any]) -> dict[str, str]:
    """Alias -> why, for each configured MCP server that could not be
    reached (the MCP plugin's ``down`` records)."""
    state = ext.get(MCP)
    down = state.get("down") if isinstance(state, Mapping) else None
    return {str(k): str(v or "") for k, v in down.items()} if isinstance(down, Mapping) else {}


def mcp_names_too_long(ext: Mapping[str, Any]) -> dict[str, int]:
    """Alias -> how many of a server's tools were left out for their name's
    length (the MCP plugin's server entries, ``too_long``)."""
    state = ext.get(MCP)
    servers = state.get("servers") if isinstance(state, Mapping) else None
    return {str(e.get("alias")): e["too_long"] for e in servers or ()
            if isinstance(e, Mapping) and isinstance(e.get("too_long"), int)
            and not isinstance(e["too_long"], bool) and e["too_long"] > 0}


#: The compaction plugin's record of when it compacts.
AUTO_COMPACT_KIND = "threshold"


@dataclass(frozen=True)
class AutoCompact:
    #: Context tokens at which automatic compaction fires.
    threshold: int
    #: The window less the summary's reserve.
    window: int
    #: Whether it compacts automatically (``/compact`` only when not).
    auto: bool


def auto_compact(events: Iterable[Recorded]) -> AutoCompact | None:
    """When the task's compaction plugin last said it compacts (its
    ``threshold`` record; ``None`` before it has)."""
    found = None
    for rec in events:
        ev = rec.event
        if not (isinstance(ev, ExtensionRecorded) and ev.extension == COMPACTION
                and ev.kind == AUTO_COMPACT_KIND and isinstance(ev.data, Mapping)):
            continue
        threshold, window = ev.data.get("threshold"), ev.data.get("window")
        if isinstance(threshold, int) and isinstance(window, int) and threshold > 0 < window:
            found = AutoCompact(threshold=threshold, window=window,
                                auto=bool(ev.data.get("auto", True)))
    return found


# ---------------------------------------------------------------- methods


def plugin(view: TaskView, name: str) -> Extension | None:
    """The task's plugin called ``name``, if it runs one."""
    return next((e for e in view.extensions if e.name == name), None)


def mode_of(view: TaskView) -> str | None:
    """The task's current permission mode as the permissions plugin judges
    it (its starting mode before any switch); ``None`` without it."""
    fn = getattr(plugin(view, PERMISSIONS), "mode_of", None)
    mode = fn(view) if callable(fn) else None
    return mode if isinstance(mode, str) else None


def plan_start_mode(view: TaskView) -> str | None:
    """The mode a task that starts in plan mode plans from, as the
    permissions plugin allows it; ``None`` without it."""
    fn = getattr(plugin(view, PERMISSIONS), "plan_start_mode", None)
    mode = fn(view) if callable(fn) else None
    return mode if isinstance(mode, str) else None


def working_directories(view: TaskView) -> tuple[str, ...]:
    """The task's working directory, then the additional ones, as the
    permissions plugin keeps them; ``()`` without it."""
    fn = getattr(plugin(view, PERMISSIONS), "working_directories", None)
    dirs = fn(view) if callable(fn) else ()
    return tuple(str(d) for d in dirs)


def read_deny_rule(view: TaskView) -> Callable[[TaskView, str], Awaitable[str | None]] | None:
    """``await check(view, path)``: the ``Read`` deny rule that covers
    ``path`` (``None``: none), as the permissions plugin keeps the rules;
    ``None`` without it."""
    fn = getattr(plugin(view, PERMISSIONS), "read_deny_rule", None)
    return fn if callable(fn) else None


def instructions_rehang(view: TaskView) -> bool:
    """Whether the instructions plugin gives its files again after a
    compaction (``False`` without it)."""
    ext = plugin(view, INSTRUCTIONS)
    return ext is not None and bool(getattr(ext, "rehang", True))


def instructions_managed_only(view: TaskView) -> bool:
    """Whether the instructions plugin reads only the managed directory's
    files (Explore and Plan; ``False`` without it)."""
    return bool(getattr(plugin(view, INSTRUCTIONS), "managed_only", False))
