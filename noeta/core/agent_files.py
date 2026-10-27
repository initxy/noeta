"""Agents as files: markdown whose YAML frontmatter says what the agent is
and whose body is its system prompt — Claude Code's agent file format, plus
``plugins``::

    ---
    name: explore
    description: Read-only scout that reports facts about the workspace.
    model: inherit                # or a model id (Claude Code's sonnet/opus/haiku inherit)
    plugins:                      # the extensions, in order: a name, or name: {arguments}
      - compaction
      - fs: {tools: [Read, Grep, Glob]}
      - instructions
    tools: Read, Grep, Glob       # optional: offer only these tools
    one_shot: true                # optional: end with the first turn's answer
    effort: high                  # optional
    ---
    You are a read-only scout...

A plugin name is an entry point of ``noeta.plugins`` (``noeta.core.registry``),
so a third-party plugin is used exactly like a built-in one. A file without
``plugins`` (a plain Claude Code agent file) gets the host's default set.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from noeta.core import registry
from noeta.core.agent import Agent
from noeta.core.extension import Extension

__all__ = [
    "AgentFile",
    "AgentFileError",
    "PluginEntry",
    "build_agents",
    "parse_agent_file",
    "read_agent_dir",
]

log = logging.getLogger("noeta.agents")

#: Model names that mean "the host's model".
_INHERIT = frozenset({"", "inherit", "sonnet", "opus", "haiku"})

PluginEntry = tuple[str, Mapping[str, Any]]


class AgentFileError(ValueError):
    """An agent file that cannot be read as one."""


@dataclass(frozen=True)
class AgentFile:
    name: str
    #: The system prompt, byte for byte.
    prompt: str
    description: str = ""
    #: A model id, or ``None`` for the host's model.
    model: str | None = None
    #: The extensions in order; ``None`` when the file names none.
    plugins: tuple[PluginEntry, ...] | None = None
    tools: tuple[str, ...] | None = None
    one_shot: bool = False
    effort: str | None = None
    source: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _names(value: Any, key: str, source: str) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(n.strip() for n in value.split(",") if n.strip())
    if isinstance(value, list) and all(isinstance(n, str) for n in value):
        return tuple(value)
    raise AgentFileError(f"{source}: '{key}' must be a list of names")


def _plugins(value: Any, source: str) -> tuple[PluginEntry, ...]:
    if not isinstance(value, list):
        raise AgentFileError(f"{source}: 'plugins' must be a list")
    out: list[PluginEntry] = []
    for item in value:
        if isinstance(item, str):
            out.append((item, {}))
        elif (isinstance(item, dict) and len(item) == 1
              and isinstance(next(iter(item)), str)):
            name, args = next(iter(item.items()))
            if args is None:
                args = {}
            if not isinstance(args, dict):
                raise AgentFileError(f"{source}: arguments of plugin {name!r} must be a mapping")
            out.append((name, args))
        else:
            raise AgentFileError(f"{source}: a plugin is a name or 'name: {{arguments}}'")
    return tuple(out)


def parse_agent_file(text: str, *, source: str = "", default_name: str = "") -> AgentFile:
    """Read one agent file. The body after the frontmatter is the system
    prompt, unchanged."""
    if text.startswith("﻿"):
        text = text[1:]
    if not text.startswith("---\n"):
        raise AgentFileError(f"{source}: an agent file starts with '---' frontmatter")
    end = text.find("\n---\n", 3)
    if end < 0:
        if not text.endswith("\n---") and not text.endswith("\n---\n"):
            raise AgentFileError(f"{source}: the frontmatter has no closing '---'")
        end = len(text) - 4
    front, body = text[4:end], text[end + 5:]
    try:
        data = yaml.safe_load(front) or {}
    except yaml.YAMLError as exc:
        raise AgentFileError(f"{source}: bad frontmatter: {exc}") from None
    if not isinstance(data, dict):
        raise AgentFileError(f"{source}: the frontmatter must be a mapping")
    name = data.pop("name", default_name)
    if not isinstance(name, str) or not name.strip():
        raise AgentFileError(f"{source}: 'name' is required")
    model = data.pop("model", None)
    if model is not None and not isinstance(model, str):
        raise AgentFileError(f"{source}: 'model' must be a string")
    plugins = data.pop("plugins", None)
    tools = data.pop("tools", None)
    effort = data.pop("effort", None)
    one_shot = data.pop("one_shot", False)
    description = data.pop("description", "")
    if not isinstance(description, str):
        raise AgentFileError(f"{source}: 'description' must be a string")
    if not isinstance(one_shot, bool):
        raise AgentFileError(f"{source}: 'one_shot' must be true or false")
    return AgentFile(
        name=name.strip(),
        prompt=body,
        description=description.strip(),
        model=None if model is None or model.strip() in _INHERIT else model.strip(),
        plugins=None if plugins is None else _plugins(plugins, source),
        tools=None if tools is None else _names(tools, "tools", source),
        one_shot=one_shot,
        effort=str(effort) if effort is not None else None,
        source=source,
        extra=data,
    )


def read_agent_dir(path: str | Path) -> list[AgentFile]:
    """Every ``*.md`` agent file in ``path`` (not recursive), by file name.
    A file that cannot be read is logged and skipped."""
    root = Path(path).expanduser()
    out: list[AgentFile] = []
    if not root.is_dir():
        return out
    for file in sorted(root.glob("*.md")):
        try:
            out.append(parse_agent_file(file.read_text("utf-8"), source=str(file),
                                        default_name=file.stem))
        except (OSError, AgentFileError) as exc:
            log.warning("skipping agent file %s: %s", file, exc)
    return out


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def build_agents(
    files: Iterable[AgentFile],
    *,
    model: str,
    config: Mapping[str, Mapping[str, Any]] | None = None,
    instances: Mapping[str, Extension] | None = None,
    disabled: Collection[str] = (),
    default_plugins: Sequence[PluginEntry] = (),
    extensions: Sequence[Extension] = (),
) -> list[Agent]:
    """The agents of ``files`` (a later file replaces an earlier one of the
    same name).

    A plugin is made once per set of arguments and shared by every agent
    that names it with those arguments: ``config`` holds the host's
    arguments per plugin name (laid under the file's), ``instances`` ready
    ones (used where a file gives no arguments). A ``disabled`` plugin is
    left out. ``extensions`` are added after every agent's own."""
    by_name: dict[str, AgentFile] = {}
    for f in files:
        by_name[f.name] = f
    config = config or {}
    instances = instances or {}
    made: dict[tuple[str, Any], Extension] = {}

    def plugin(name: str, args: Mapping[str, Any]) -> Extension:
        if not args and name in instances:
            return instances[name]
        key = (name, _freeze(args))
        if key not in made:
            factory = registry.load(registry.PLUGINS, name)
            made[key] = factory(**{**config.get(name, {}), **args})
        return made[key]

    agents: list[Agent] = []
    for f in by_name.values():
        entries = f.plugins if f.plugins is not None else tuple(default_plugins)
        exts = [plugin(name, args) for name, args in entries if name not in disabled]
        agents.append(Agent(
            name=f.name,
            model=f.model or model,
            system_prompt=f.prompt,
            description=f.description,
            extensions=(*exts, *extensions),
            effort=f.effort,  # type: ignore[arg-type]
            one_shot=f.one_shot,
            allowed_tools=f.tools,
        ))
    return agents
