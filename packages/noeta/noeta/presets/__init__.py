"""Ready-made agents. ``main`` is the general coding agent with Claude Code's
working style (``prompts/main.md``, byte for byte from 0.6); it delegates to
the official general-purpose / explore / plan sub-agents.

``default_agents(model)`` is what the ``noeta`` command registers: ``main``
plus its sub-agents. The permission mode is per task
(``config["permission_mode"]``) and is inherited by sub-agents.
"""

from __future__ import annotations

from collections.abc import Sequence
from importlib import resources
from typing import Any

from noeta.agent import Agent
from noeta.ext.ask_user import AskUser
from noeta.ext.base import Extension
from noeta.ext.fs import FileTools
from noeta.ext.permissions import Permissions
from noeta.ext.subtasks import Subtasks
from noeta.ext.subtasks.agents import OFFICIAL_ROSTER, official_subagents
from noeta.ext.workspace import Environment


def load_prompt(name: str) -> str:
    return (resources.files("noeta.presets") / "prompts" / f"{name}.md").read_text("utf-8")


MAIN_PROMPT = load_prompt("main")

#: Task config a sub-agent inherits from the task that spawned it.
INHERITED_CONFIG = ("cwd", "model", "effort", "permission_mode")


def main_agent(model: str, *, extensions: Sequence[Extension] = (),
               permissions: Permissions | None = None, **kw: Any) -> Agent:
    return Agent(
        name=kw.pop("name", "main"),
        model=model,
        system_prompt=kw.pop("system_prompt", MAIN_PROMPT),
        extensions=(
            FileTools(), Environment(), permissions or Permissions(),
            Subtasks(OFFICIAL_ROSTER, inherit_config=INHERITED_CONFIG), AskUser(),
            *extensions,
        ),
        **kw,
    )


def default_agents(model: str, *, extensions: Sequence[Extension] = (),
                   permissions: Permissions | None = None, **kw: Any) -> list[Agent]:
    """``main`` and the three official sub-agents, sharing one permissions
    policy (each task reads its own mode from its config)."""
    perms = permissions or Permissions()
    return [
        main_agent(model, extensions=extensions, permissions=perms, **kw),
        *official_subagents(model, extensions=(perms, *extensions)),
    ]
