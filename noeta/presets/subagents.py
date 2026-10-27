"""The official sub-agents of the ``main`` preset: general-purpose, explore
and plan.

Each is a one-shot ``Agent`` (it ends with its first turn's answer). Their
file tools:

- general-purpose: Read, Write, Edit, Grep, Glob, and Bash (a foreground
  one when the host passes no shell extension).
- explore: read-only — Read, Bash, Grep, Glob, no Write/Edit ("no writes"
  is held by the prompt, with shell approval as the backstop).
- plan: explore's file set.

The host passes those groups' extensions per agent (``extensions`` go before
the file tools, ``context`` after them and before the environment block).
0.6 recorded the workspace instructions and the environment block for every
session, so each sub-agent carries both. None of them delegates: each carries
``Subtasks()`` with an empty roster, which offers no ``Task`` tool but applies
the child-side rules (a background child that needs a human ends with an
explanation).
"""

from __future__ import annotations

from collections.abc import Sequence
from importlib import resources
from typing import Any

from noeta.core.agent import Agent
from noeta.core.extension import Extension
from noeta.plugins.environment import Environment
from noeta.plugins.fs import EditTool, FileTools, GlobTool, GrepTool, ReadTool, WriteTool
from noeta.plugins.instructions import Instructions
from noeta.plugins.shell import SHELL, Shell
from noeta.plugins.subtasks.extension import SubagentSpec, Subtasks


def load_prompt(name: str) -> str:
    """``noeta/presets/prompts/<name>.md`` byte for byte."""
    return (resources.files("noeta.presets") / "prompts" / f"{name}.md").read_text("utf-8")


GENERAL_PURPOSE_PROMPT = load_prompt("general-purpose")
EXPLORE_PROMPT = load_prompt("explore")
PLAN_PROMPT = load_prompt("plan")

# The roster lines are part of main's Task schema.
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
           extensions: Sequence[Extension], context: Sequence[Extension] | None,
           kw: dict[str, Any]) -> Agent:
    ctx = (Instructions(),) if context is None else tuple(context)
    if not any(e.name == SHELL for e in extensions):
        # Bash without background jobs when the host passes no shell.
        extensions = (*extensions, Shell(background=False))
    return Agent(
        name=kw.pop("name", name),
        model=model,
        system_prompt=kw.pop("system_prompt", prompt),
        # The host's extensions go first (compaction must see the request
        # first). Skills / instructions come before the environment block.
        extensions=(*extensions, fs, *ctx, Environment(model=model), Subtasks()),
        one_shot=kw.pop("one_shot", True),
        **kw,
    )


def general_purpose_agent(model: str, *, extensions: Sequence[Extension] = (),
                          context: Sequence[Extension] | None = None, **kw: Any) -> Agent:
    """``context``: the extensions recorded before the first message
    (default: ``Instructions()``)."""
    fs = FileTools([ReadTool(), WriteTool(), EditTool(), GrepTool(), GlobTool()])
    return _agent(GENERAL_PURPOSE.name, model, GENERAL_PURPOSE_PROMPT, fs, extensions, context,
                  kw)


def _read_only_fs() -> FileTools:
    return FileTools([ReadTool(), GrepTool(), GlobTool()])


def explore_agent(model: str, *, extensions: Sequence[Extension] = (),
                  context: Sequence[Extension] | None = None, **kw: Any) -> Agent:
    return _agent(EXPLORE.name, model, EXPLORE_PROMPT, _read_only_fs(), extensions, context, kw)


def plan_agent(model: str, *, extensions: Sequence[Extension] = (),
               context: Sequence[Extension] | None = None, **kw: Any) -> Agent:
    return _agent(PLAN.name, model, PLAN_PROMPT, _read_only_fs(), extensions, context, kw)


def official_subagents(
    model: str,
    *,
    extensions: Sequence[Extension] = (),
    read_only_extensions: Sequence[Extension] | None = None,
    plan_extensions: Sequence[Extension] | None = None,
    context: Sequence[Extension] | None = None,
    read_only_context: Sequence[Extension] | None = None,
    plan_context: Sequence[Extension] | None = None,
) -> list[Agent]:
    """The three agents to register next to ``main``. ``extensions`` /
    ``context`` go to general-purpose; ``read_only_*`` (default: the same)
    to explore; ``plan_*`` (default: explore's) to plan."""
    ro = extensions if read_only_extensions is None else read_only_extensions
    ro_ctx = context if read_only_context is None else read_only_context
    return [
        general_purpose_agent(model, extensions=extensions, context=context),
        explore_agent(model, extensions=ro, context=ro_ctx),
        plan_agent(model, extensions=ro if plan_extensions is None else plan_extensions,
                   context=ro_ctx if plan_context is None else plan_context),
    ]
