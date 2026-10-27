"""The official sub-agents of the ``main`` preset: general-purpose, explore
and plan, with their 0.6 prompts and roster lines byte for byte.

Each is a one-shot ``Agent`` (it ends with its first turn's answer). Their
tool sets follow 0.6:

- general-purpose: the full file/shell set (Read, Write, Edit, Bash, Grep,
  Glob); in 0.6 also BashOutput/KillShell/Monitor, WebSearch/WebFetch, skills
  and MCP — pass those groups' extensions in ``extensions``.
- explore / plan: read-only — Read, Bash, Grep, Glob, no Write/Edit ("no
  writes" is held by the prompt, with shell approval as the backstop); in
  0.6 also BashOutput/KillShell/Monitor and WebSearch/WebFetch; plan also had
  AskUserQuestion. Pass those in ``extensions`` (read-only ones only).

None of them delegates: each carries ``Subtasks()`` with an empty roster,
which offers no ``Task`` tool but applies the child-side rules (a background
child that needs a human ends with an explanation).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from noeta.agent import Agent
from noeta.ext.base import Extension
from noeta.ext.fs import FileTools
from noeta.ext.subtasks.extension import SubagentSpec, Subtasks
from noeta.ext.subtasks.texts import load_prompt
from noeta.ext.workspace import Environment
from noeta.tools import BashTool, EditTool, GlobTool, GrepTool, ReadTool, WriteTool

GENERAL_PURPOSE_PROMPT = load_prompt("general-purpose")
EXPLORE_PROMPT = load_prompt("explore")
PLAN_PROMPT = load_prompt("plan")

# 0.6 presets/__init__.py:179-215 (the roster lines are part of main's Task
# schema, so they are byte-pinned too).
GENERAL_PURPOSE = SubagentSpec(
    name="general-purpose",
    description=(
        "General-purpose worker for self-contained coding tasks: "
        "search, read, write, edit, run shell commands, then return "
        "the result."
    ),
)
EXPLORE = SubagentSpec(
    name="explore",
    description=(
        "Read-only scout: fans out (glob/grep/read + read-only shell) "
        "to investigate the workspace and report facts (never edits)."
    ),
)
PLAN = SubagentSpec(
    name="plan",
    description=(
        "Architect: reads the code and returns a concrete ordered "
        "implementation plan (read-only — never writes any file)."
    ),
)
OFFICIAL_ROSTER: dict[str, SubagentSpec] = {s.name: s for s in (GENERAL_PURPOSE, EXPLORE, PLAN)}


def _agent(name: str, model: str, prompt: str, fs: FileTools,
           extensions: Sequence[Extension], kw: dict[str, Any]) -> Agent:
    return Agent(
        name=kw.pop("name", name),
        model=model,
        system_prompt=kw.pop("system_prompt", prompt),
        extensions=(fs, Environment(), Subtasks(), *extensions),
        one_shot=kw.pop("one_shot", True),
        **kw,
    )


def general_purpose_agent(model: str, *, extensions: Sequence[Extension] = (),
                          **kw: Any) -> Agent:
    fs = FileTools([ReadTool(), WriteTool(), EditTool(), BashTool(), GrepTool(), GlobTool()])
    return _agent(GENERAL_PURPOSE.name, model, GENERAL_PURPOSE_PROMPT, fs, extensions, kw)


def _read_only_fs() -> FileTools:
    return FileTools([ReadTool(), BashTool(), GrepTool(), GlobTool()])


def explore_agent(model: str, *, extensions: Sequence[Extension] = (), **kw: Any) -> Agent:
    return _agent(EXPLORE.name, model, EXPLORE_PROMPT, _read_only_fs(), extensions, kw)


def plan_agent(model: str, *, extensions: Sequence[Extension] = (), **kw: Any) -> Agent:
    return _agent(PLAN.name, model, PLAN_PROMPT, _read_only_fs(), extensions, kw)


def official_subagents(
    model: str,
    *,
    extensions: Sequence[Extension] = (),
    read_only_extensions: Sequence[Extension] | None = None,
) -> list[Agent]:
    """The three agents to register next to ``main``. ``extensions`` go to
    general-purpose; ``read_only_extensions`` (default: the same) to explore
    and plan."""
    ro = extensions if read_only_extensions is None else read_only_extensions
    return [
        general_purpose_agent(model, extensions=extensions),
        explore_agent(model, extensions=ro),
        plan_agent(model, extensions=ro),
    ]
