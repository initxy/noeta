"""Ready-made agents. ``main`` is the general coding agent with Claude Code's
working style (``prompts/main.md`` + the memory policy, byte for byte from
0.6); it delegates to the official general-purpose / explore / plan
sub-agents.

``default_agents(model)`` is what the ``noeta`` command registers. The
permission mode is per task (``config["permission_mode"]``) and is inherited
by sub-agents. Extension order matters in two places: the background shell
offers its own ``Bash`` and so comes before the file tools; skills, memory
and project instructions come before the environment block, so the content
recorded before the first message is in 0.6's order.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from importlib import resources
from pathlib import Path
from typing import Any

from noeta.agent import Agent
from noeta.ext.ask_user import AskUser
from noeta.ext.background_shell import BackgroundShells
from noeta.ext.base import Extension
from noeta.ext.compaction import Compaction
from noeta.ext.fs import FileTools
from noeta.ext.instructions import Instructions
from noeta.ext.mcp import Mcp
from noeta.ext.memory import Memory, with_memory_policy
from noeta.ext.permissions import Permissions
from noeta.ext.skills import Skills
from noeta.ext.subtasks import Subtasks
from noeta.ext.subtasks.agents import OFFICIAL_ROSTER, official_subagents
from noeta.ext.todo import Todo
from noeta.ext.web import Web
from noeta.ext.workspace import Environment


def load_prompt(name: str) -> str:
    return (resources.files("noeta.presets") / "prompts" / f"{name}.md").read_text("utf-8")


MAIN_PROMPT = load_prompt("main")

#: Task config a sub-agent inherits from the task that spawned it.
INHERITED_CONFIG = ("cwd", "model", "effort", "permission_mode")


def default_agents(
    model: str,
    *,
    permissions: Permissions | None = None,
    memory_root: str | Path | Callable[..., Any] | None = None,
    skills: Skills | None = None,
    mcp: Mcp | None = None,
    compaction: Compaction | None = None,
    web: Web | None = None,
    extensions: Sequence[Extension] = (),
    **main_kw: Any,
) -> list[Agent]:
    """``main`` and the three official sub-agents. Without ``memory_root``
    the agent has no memory tools (and its prompt no memory policy)."""
    perms = permissions or Permissions()
    shells = BackgroundShells()
    compaction = compaction or Compaction()
    web = web or Web()
    skills = skills or Skills()
    memory = Memory(root=memory_root) if memory_root is not None else None
    knowledge: list[Extension] = [skills, *([memory] if memory else []), Instructions()]
    tail: list[Extension] = [*([mcp] if mcp else []), *extensions]

    main = Agent(
        name=main_kw.pop("name", "main"),
        model=model,
        system_prompt=main_kw.pop(
            "system_prompt",
            with_memory_policy(MAIN_PROMPT) if memory else MAIN_PROMPT),
        extensions=(
            compaction, perms, shells, FileTools(), *knowledge, Environment(),
            Subtasks(OFFICIAL_ROSTER, inherit_config=INHERITED_CONFIG),
            Todo(), AskUser(), web, *tail,
        ),
        **main_kw,
    )
    # Sub-agents get the shell, web, MCP and compaction; read-only ones the
    # same minus nothing that writes (their file tools are already read-only).
    shared = (compaction, perms, shells, web, *tail)
    return [main, *official_subagents(model, extensions=shared)]


def main_agent(model: str, **kw: Any) -> Agent:
    return default_agents(model, **kw)[0]
