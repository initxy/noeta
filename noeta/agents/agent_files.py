"""Agents as files: markdown whose YAML frontmatter says what the agent is
and whose body is its system prompt — Claude Code's agent file format, plus
``plugins`` and ``one_shot``::

    ---
    name: Explore
    description: Read-only search agent for wide searches.
    model: inherit                # inherit, an alias (sonnet, opus, haiku, fable) or a model id
    plugins:                      # the extensions, in order: a name, or name: {arguments}
      - compaction
      - fs
      - instructions
    tools: Read, Bash             # optional: offer only these tools ("*": all)
    disallowedTools: Edit, Write  # optional: never offer these
    permissionMode: plan          # optional: the permission mode it runs in
    maxTurns: 50                  # optional: model calls per turn
    omitClaudeMd: true            # optional: no project instructions
    one_shot: true                # optional: end with the first turn's answer
    effort: high                  # optional
    ---
    You are a search agent...

Claude Code's other fields (``skills``, ``mcpServers``, ``hooks``, ``memory``,
``isolation``, ``background``, ``initialPrompt``, ``color``, ...) are read
and kept in ``extra``; a field neither knows is logged as a warning.

A plugin name is an entry point of ``noeta.plugins`` (``noeta.core.registry``),
so a third-party plugin is used exactly like a built-in one. A file without
``plugins`` (a plain Claude Code agent file) gets the host's default set.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from noeta.core import models, registry
from noeta.core.agent import Agent
from noeta.core.extension import Extension

__all__ = [
    "AgentFile",
    "AgentFileError",
    "PluginEntry",
    "PluginSetting",
    "build_agents",
    "parse_agent_file",
    "parse_plugin_list",
    "read_agent_dir",
]

log = logging.getLogger("noeta.agents")

#: Model names that mean "the host's model".
_INHERIT = frozenset({"", "inherit"})
#: Claude Code fields kept in ``AgentFile.extra``, and whether noeta acts on
#: them (one it does not act on yet is logged when a file sets it).
_KEPT = {"isolation": True, "color": True, "background": False, "skills": False,
         "mcpServers": False, "hooks": False, "memory": False, "initialPrompt": False,
         "autoCompactWindow": False, "cacheTtl": False, "observer": False,
         "observerMessage": False, "observeSubagents": False}

PluginEntry = tuple[str, Mapping[str, Any]]
#: The host's setting of one plugin: its arguments, a ready instance, or on/off.
PluginSetting = Mapping[str, Any] | Extension | bool


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
    denied_tools: tuple[str, ...] = ()
    permission_mode: str | None = None
    max_turns: int | None = None
    omit_instructions: bool = False


def _names(value: Any, key: str, source: str) -> tuple[str, ...]:
    """Tool names; a Claude Code permission spec (``Bash(npm:*)``,
    ``Agent(Explore)``) counts as its tool's name."""
    if isinstance(value, str):
        items = [n.strip() for n in value.split(",") if n.strip()]
    elif isinstance(value, list) and all(isinstance(n, str) for n in value):
        items = [n.strip() for n in value]
    else:
        raise AgentFileError(f"{source}: '{key}' must be a list of names")
    return tuple(dict.fromkeys(n.split("(", 1)[0].strip() for n in items if n))


def parse_plugin_list(value: Any, source: str = "") -> tuple[PluginEntry, ...]:
    """A ``plugins`` list: each item a name or ``{name: {arguments}}``."""
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
    if ":" in name:
        raise AgentFileError(f"{source}: 'name' may not contain ':'")
    model = data.pop("model", None)
    if model is not None and not isinstance(model, str):
        raise AgentFileError(f"{source}: 'model' must be a string")
    plugins = data.pop("plugins", None)
    tools = data.pop("tools", None)
    denied = data.pop("disallowedTools", None)
    effort = data.pop("effort", None)
    one_shot = data.pop("one_shot", False)
    description = data.pop("description", "")
    mode = data.pop("permissionMode", None)
    max_turns = data.pop("maxTurns", None)
    omit = data.pop("omitClaudeMd", False)
    if not isinstance(description, str):
        raise AgentFileError(f"{source}: 'description' must be a string")
    if not isinstance(one_shot, bool):
        raise AgentFileError(f"{source}: 'one_shot' must be true or false")
    if not isinstance(omit, bool):
        raise AgentFileError(f"{source}: 'omitClaudeMd' must be true or false")
    if mode is not None and not isinstance(mode, str):
        raise AgentFileError(f"{source}: 'permissionMode' must be a string")
    if max_turns is not None and (isinstance(max_turns, bool) or not isinstance(max_turns, int)
                                  or max_turns < 1):
        raise AgentFileError(f"{source}: 'maxTurns' must be a positive whole number")
    names = None if tools is None else _names(tools, "tools", source)
    for key in data:
        if key not in _KEPT:
            log.warning("%s: unknown agent file field %r is ignored", source, key)
        elif not _KEPT[key]:
            log.warning("%s: agent file field %r is not applied by noeta yet", source, key)
    return AgentFile(
        name=name.strip(),
        prompt=body,
        description=description.strip(),
        model=None if model is None or model.strip() in _INHERIT else model.strip(),
        plugins=None if plugins is None else parse_plugin_list(plugins, source),
        tools=None if names is None or "*" in names else names,
        one_shot=one_shot,
        effort=str(effort) if effort is not None else None,
        source=source,
        extra=data,
        denied_tools=() if denied is None else _names(denied, "disallowedTools", source),
        permission_mode=mode,
        max_turns=max_turns,
        omit_instructions=omit,
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


def _setting(name: str, value: PluginSetting) -> tuple[bool, Mapping[str, Any], Extension | None]:
    """``value`` as (enabled, host arguments, ready instance)."""
    if isinstance(value, bool):
        return value, {}, None
    if isinstance(value, Extension):
        return True, {}, value
    if isinstance(value, Mapping):
        args = dict(value)
        enabled = args.pop("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"plugin {name!r}: 'enabled' must be true or false")
        return enabled, args, None
    raise TypeError(f"plugin {name!r}: a setting is arguments, an Extension, or true/false")


def build_agents(
    files: Iterable[AgentFile],
    *,
    model: str,
    plugins: Mapping[str, PluginSetting] | None = None,
    default_plugins: Sequence[PluginEntry] = (),
    add: Mapping[str, Sequence[PluginEntry]] | None = None,
    extensions: Sequence[Extension] = (),
) -> list[Agent]:
    """The agents of ``files`` (a later file replaces an earlier one of the
    same name).

    ``plugins`` is the host's setting per plugin name: its arguments (laid
    under the file's; ``enabled: false`` among them turns it off), a ready
    ``Extension`` (used where a file gives no arguments), or ``False`` to
    leave it out of every agent. ``add`` names more plugins per agent
    (``"*"``: every agent), after the file's own; a plugin an agent already
    has is not added twice. A plugin is made once per set of arguments and
    shared by every agent that names it with those arguments. ``extensions``
    are added after every agent's own."""
    by_name: dict[str, AgentFile] = {}
    for f in files:
        by_name[f.name] = f
    settings = {name: _setting(name, v) for name, v in (plugins or {}).items()}
    add = add or {}
    made: dict[tuple[str, Any], Extension] = {}

    def plugin(name: str, args: Mapping[str, Any]) -> Extension:
        _on, host_args, instance = settings.get(name, (True, {}, None))
        if not args and instance is not None:
            return instance
        key = (name, _freeze(args))
        if key not in made:
            factory = registry.load(registry.PLUGINS, name)
            made[key] = factory(**{**host_args, **args})
        return made[key]

    agents: list[Agent] = []
    for f in by_name.values():
        entries = list(f.plugins if f.plugins is not None else default_plugins)
        names = {name for name, _ in entries}
        for name, args in (*add.get(f.name, ()), *add.get("*", ())):
            if name not in names:
                names.add(name)
                entries.append((name, args))
        exts = [plugin(name, args) for name, args in entries
                if settings.get(name, (True,))[0]
                and not (f.omit_instructions and name == "instructions")]
        extra: dict[str, Any] = dict(f.extra)
        if f.model:
            extra["model"] = f.model
        if f.permission_mode:
            extra["permission_mode"] = f.permission_mode
        agents.append(Agent(
            name=f.name,
            model=models.resolve_alias(f.model) if f.model else model,
            system_prompt=f.prompt,
            description=f.description,
            extensions=(*exts, *extensions),
            effort=f.effort,  # type: ignore[arg-type]
            one_shot=f.one_shot,
            allowed_tools=f.tools,
            denied_tools=f.denied_tools,
            max_steps_per_turn=f.max_turns or Agent.max_steps_per_turn,
            extra=extra,
        ))
    return agents
