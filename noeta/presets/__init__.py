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
from pathlib import Path
from typing import Any

from noeta.core.agent import Agent
from noeta.core.extension import Extension
from noeta.plugins.ask_user import AskUser
from noeta.plugins.compaction import Compaction
from noeta.plugins.environment import Environment
from noeta.plugins.fs import FileTools
from noeta.plugins.instructions import Instructions
from noeta.plugins.mcp import Mcp
from noeta.plugins.memory import Memory, with_memory_policy
from noeta.plugins.permissions import Permissions
from noeta.plugins.shell import Shell
from noeta.plugins.skills import Skills
from noeta.plugins.subtasks import Subtasks
from noeta.plugins.todo import Todo
from noeta.plugins.web import Web
from noeta.presets.subagents import OFFICIAL_ROSTER, load_prompt, official_subagents

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
    shells = Shell()
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
            compaction, perms, shells, FileTools(), *knowledge, Environment(model=model),
            Subtasks(OFFICIAL_ROSTER, inherit_config=INHERITED_CONFIG),
            Todo(), AskUser(), web, *tail,
        ),
        **main_kw,
    )
    # 0.6's sub-agent plugin sets: general-purpose has skills and MCP,
    # explore skills (no MCP), plan AskUserQuestion (no skills, no MCP); all
    # three the background shells, web, project instructions and the
    # environment block. ``extensions`` (host extras) go to all three.
    head = (compaction, perms, shells)
    return [main, *official_subagents(
        model,
        extensions=(*head, web, *([mcp] if mcp else []), *extensions),
        read_only_extensions=(*head, web, *extensions),
        plan_extensions=(*head, AskUser(), web, *extensions),
        context=(skills, Instructions()),
        read_only_context=(skills, Instructions()),
        plan_context=(Instructions(),),
    )]


def main_agent(model: str, **kw: Any) -> Agent:
    return default_agents(model, **kw)[0]
