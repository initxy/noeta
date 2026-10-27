"""Ready-made agents, as agent files in ``agents/``: ``main`` is the general
coding agent with Claude Code's working style; it delegates to the
general-purpose, explore and plan sub-agents.

``default_agents(model)`` is what the ``noeta`` command registers, plus any
agent files a host adds (``extra_files``, e.g. from ``~/.noeta/agents`` and
``<cwd>/.noeta/agents``; a file named like a built-in replaces it). The
permission mode is per task (``config["permission_mode"]``) and is inherited
by sub-agents.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable, Sequence
from importlib import resources
from pathlib import Path
from typing import Any

from noeta.core.agent import Agent
from noeta.core.agent_files import AgentFile, PluginEntry, build_agents, parse_agent_file
from noeta.core.extension import Extension

__all__ = ["DEFAULT_PLUGINS", "builtin_agent_files", "default_agents", "main_agent"]

#: The extensions of an agent file that names none (a Claude Code agent
#: file): the general-purpose sub-agent's.
DEFAULT_PLUGINS: tuple[PluginEntry, ...] = (
    ("compaction", {}), ("permissions", {}), ("shell", {}), ("web", {}), ("mcp", {}),
    ("fs", {}), ("skills", {}), ("instructions", {}), ("environment", {}), ("subtasks", {}),
)


def builtin_agent_files() -> list[AgentFile]:
    root = resources.files("noeta.presets") / "agents"
    return [parse_agent_file(f.read_text("utf-8"), source=f"noeta.presets:{f.name}")
            for f in sorted(root.iterdir(), key=lambda f: (f.name != "main.md", f.name))
            if f.name.endswith(".md")]


def default_agents(
    model: str,
    *,
    permissions: Extension | None = None,
    memory_root: str | Path | Callable[..., Any] | None = None,
    skills: Extension | None = None,
    mcp: Extension | None = None,
    compaction: Extension | None = None,
    web: Extension | None = None,
    extensions: Sequence[Extension] = (),
    extra_files: Iterable[AgentFile] = (),
    plugin_config: dict[str, dict[str, Any]] | None = None,
    **main_kw: Any,
) -> list[Agent]:
    """``main`` and the three sub-agents (and ``extra_files``' agents).

    The keyword extensions are shared by every agent that lists them.
    Without ``memory_root`` there is no memory (no tools, no policy); without
    ``mcp`` no MCP. ``main_kw`` replaces fields of ``main`` (``name``,
    ``system_prompt``, ...)."""
    instances: dict[str, Extension] = {
        name: ext for name, ext in (("permissions", permissions), ("skills", skills),
                                    ("compaction", compaction), ("web", web), ("mcp", mcp))
        if ext is not None
    }
    config = {k: dict(v) for k, v in (plugin_config or {}).items()}
    disabled = set()
    if memory_root is None:
        disabled.add("memory")
    else:
        config.setdefault("memory", {})["root"] = memory_root
    if mcp is None:
        disabled.add("mcp")
    agents = build_agents([*builtin_agent_files(), *extra_files], model=model, config=config,
                          instances=instances, disabled=disabled,
                          default_plugins=DEFAULT_PLUGINS, extensions=extensions)
    if main_kw:
        agents = [dataclasses.replace(a, **main_kw) if a.name == "main" else a for a in agents]
    return agents


def main_agent(model: str, **kw: Any) -> Agent:
    return next(a for a in default_agents(model, **kw) if a.name == kw.get("name", "main"))
