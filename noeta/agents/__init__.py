"""The built-in agents, as agent files in ``files/``: ``main`` is the general
coding agent with Claude Code's working style; it delegates to the
general-purpose, Explore and Plan sub-agents.

``default_agents(model)`` is what the ``noeta`` command registers, plus any
agent files a host adds (``extra_files``, e.g. from ``~/.noeta/agents`` and
``<cwd>/.noeta/agents``; a file named like a built-in replaces it). The
permission mode is per task (``config["permission_mode"]``) and is inherited
by sub-agents.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from importlib import resources
from typing import Any

from noeta.agents.agent_files import (
    AgentFile,
    PluginEntry,
    PluginSetting,
    build_agents,
    parse_agent_file,
)
from noeta.core.agent import Agent
from noeta.core.extension import Extension

__all__ = [
    "DEFAULT_PLUGINS",
    "OFF_UNLESS_SET",
    "builtin_agent_files",
    "default_agents",
    "main_agent",
]

#: The extensions of an agent file that names none (a Claude Code agent
#: file): the general-purpose sub-agent's.
DEFAULT_PLUGINS: tuple[PluginEntry, ...] = (
    ("permissions", {}), ("plan_mode", {}), ("budget", {}), ("repetition_guard", {}),
    ("hooks", {}), ("shell", {}), ("web", {}), ("mcp", {}), ("fs", {}), ("skills", {}),
    ("instructions", {}), ("environment", {}), ("subtasks", {}),
    # Last: the summarize call reuses the request as the others left it.
    ("compaction", {}),
)


#: Plugins the built-in agents name that need the host's say-so: memory
#: writes under a directory, MCP starts the servers it is given, and the
#: repetition guard is not Claude Code's.
OFF_UNLESS_SET: frozenset[str] = frozenset({"memory", "mcp", "repetition_guard"})


def builtin_agent_files() -> list[AgentFile]:
    root = resources.files("noeta.agents") / "files"
    return [parse_agent_file(f.read_text("utf-8"), source=f"noeta.agents:{f.name}")
            for f in sorted(root.iterdir(), key=lambda f: (f.name != "main.md", f.name))
            if f.name.endswith(".md")]


def default_agents(
    model: str,
    *,
    plugins: Mapping[str, PluginSetting] | None = None,
    add: Mapping[str, Sequence[PluginEntry]] | None = None,
    extensions: Sequence[Extension] = (),
    extra_files: Iterable[AgentFile] = (),
    made: dict[Any, Extension] | None = None,
    **main_kw: Any,
) -> list[Agent]:
    """``main`` and the three sub-agents (and ``extra_files``' agents).

    ``plugins``, ``add`` and ``made`` are :func:`~noeta.agents.agent_files.build_agents`'s:
    per plugin name its arguments, a ready instance, or ``False``. The
    plugins in :data:`OFF_UNLESS_SET` (memory, MCP, the repetition guard) stay off until
    ``plugins`` has a setting for them. ``main_kw`` replaces fields of
    ``main`` (``name``, ``system_prompt``, ...)."""
    settings: dict[str, PluginSetting] = {name: False for name in OFF_UNLESS_SET}
    settings.update(plugins or {})
    agents = build_agents([*builtin_agent_files(), *extra_files], model=model,
                          plugins=settings, default_plugins=DEFAULT_PLUGINS, add=add,
                          extensions=extensions, made=made)
    if main_kw:
        agents = [dataclasses.replace(a, **main_kw) if a.name == "main" else a for a in agents]
    return agents


def main_agent(model: str, **kw: Any) -> Agent:
    return next(a for a in default_agents(model, **kw) if a.name == kw.get("name", "main"))
