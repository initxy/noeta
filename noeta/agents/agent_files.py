"""Agents as files: markdown whose YAML frontmatter says what the agent is
and whose body is its system prompt — Claude Code's agent file format, plus
``plugins`` and ``one_shot``::

    ---
    name: Explore
    description: Read-only search agent for wide searches.
    model: inherit                # inherit, an alias (sonnet, opus, haiku, fable) or a model id
    plugins:                      # the extensions, in order: a name, or name: {arguments}
      - compaction
      - fs: {tools: [Read]}
      - instructions
    tools: Read, Bash             # optional: offer only these tools ("*": all)
    disallowedTools: Edit, Write  # optional: never offer these
    permissionMode: plan          # optional: the permission mode it runs in
    maxTurns: 50                  # optional: model calls per turn
    omitClaudeMd: true            # optional: only the managed instructions
    one_shot: true                # optional: end with the first turn's answer
    effort: high                  # optional
    identity: You are ...         # optional: the one-line identity block
    ---
    You are a search agent...

Claude Code's other fields (``skills``, ``mcpServers``, ``hooks``, ``memory``,
``isolation``, ``background``, ``initialPrompt``, ``color``, ...) are kept in
``extra``; ``skills``, ``mcpServers``, ``hooks``, ``memory`` and
``autoCompactWindow`` become arguments of the plugins that do them
(:func:`plugin_overlays`). A field noeta does not act on is logged once per
file.

A plugin name is an entry point of ``noeta.plugins`` (``noeta.core.registry``),
so a third-party plugin is used exactly like a built-in one. A file without
``plugins`` (a plain Claude Code agent file) gets the host's default set.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from noeta.core import models, registry
from noeta.core.agent import Agent
from noeta.core.extension import Extension
from noeta.toolkit import frontmatter
from noeta.toolkit.hook_env import without_env

__all__ = [
    "DEFAULT_IDENTITY",
    "AgentFile",
    "AgentFileError",
    "PluginEntry",
    "PluginSetting",
    "build_agents",
    "parse_agent_file",
    "parse_plugin_list",
    "plugin_overlays",
    "read_agent_dir",
    "read_agent_file",
    "read_file_text",
    "resolve_secrets",
]

log = logging.getLogger("noeta.agents")

#: The identity line of an agent whose file sets none: the product, in one
#: line, as the first (cached) system block.
DEFAULT_IDENTITY = "You are noeta, an open-source coding agent."

#: Model names that mean "the host's model".
_INHERIT = frozenset({"", "inherit"})
#: The built-in agents whose inherited model is capped at a family: Claude
#: Code's Explore runs on Opus under a Fable caller (``inheritCap``), by
#: source, so an Explore file of the user's takes the caller's model as is.
BUILTIN_INHERIT_CAP = {"noeta.agents:explore.md": "opus"}
#: Claude Code fields kept in ``AgentFile.extra``, and whether noeta acts on
#: them (one it does not act on is logged once per file). ``skills``,
#: ``mcpServers``, ``hooks``, ``memory`` and ``autoCompactWindow`` become
#: arguments of the plugin that does it (:func:`plugin_overlays`);
#: ``initialPrompt`` is read by the host that creates a top-level task;
#: ``background`` is how every noeta sub-agent runs.
_KEPT = {"isolation": True, "color": True, "background": True, "skills": True,
         "mcpServers": True, "hooks": True, "memory": True, "initialPrompt": True,
         "autoCompactWindow": True, "criticalSystemReminder_EXPERIMENTAL": True,
         "cacheTtl": False, "observer": False,
         "observerMessage": False, "observeSubagents": False}
#: Claude Code's agent memory scopes.
MEMORY_SCOPES = ("user", "project", "local")
#: (file, field) pairs already warned about: a reload does not repeat them.
_WARNED: set[tuple[str, str]] = set()

#: Plugins every agent runs with, whether its file names them or not (a host
#: setting can still turn one off): any plugin's tool may be deferred, and
#: only ``tool_search`` loads deferred tools; any task may be given an answer
#: schema (``output_schema``), and only ``structured_output`` takes the answer.
ALWAYS_PLUGINS: tuple[str, ...] = ("tool_search", "structured_output")

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
    #: The identity line; ``None`` for :data:`DEFAULT_IDENTITY`.
    identity: str | None = None
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


def _check_claude_code_fields(data: Mapping[str, Any], source: str) -> None:
    """Claude Code's fields noeta applies, checked for their shapes."""
    if "skills" in data:
        _names(data["skills"], "skills", source)
    servers = data.get("mcpServers")
    if servers is not None and not (isinstance(servers, list) and all(
            isinstance(s, str) or (isinstance(s, dict) and s
                                   and all(isinstance(v, dict) for v in s.values()))
            for s in servers)):
        raise AgentFileError(f"{source}: 'mcpServers' must list server names or "
                             "'name: {config}' entries")
    if data.get("hooks") is not None and not isinstance(data["hooks"], dict):
        raise AgentFileError(f"{source}: 'hooks' must be a hooks object")
    if data.get("memory") is not None and data["memory"] not in MEMORY_SCOPES:
        raise AgentFileError(f"{source}: 'memory' must be one of {', '.join(MEMORY_SCOPES)}")
    if data.get("initialPrompt") is not None and not isinstance(data["initialPrompt"], str):
        raise AgentFileError(f"{source}: 'initialPrompt' must be a string")
    if data.get("autoCompactWindow") is not None:
        _tokens(data["autoCompactWindow"], source)


def _tokens(value: Any, source: str) -> int:
    """A token count: a whole number, or a string such as ``"200k"`` / ``"1m"``."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if isinstance(value, str):
        m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kKmM]?)\s*", value)
        if m:
            n = float(m[1]) * {"": 1, "k": 1_000, "m": 1_000_000}[m[2].lower()]
            if n >= 1:
                return int(n)
    raise AgentFileError(f"{source}: 'autoCompactWindow' must be a positive token count")


def plugin_overlays(f: AgentFile) -> list[PluginEntry]:
    """What the file's Claude Code fields ask of plugins, as arguments laid
    over that plugin's (the plugin is added when the agent lacks it):
    ``skills`` preloaded by the skills plugin, ``mcpServers`` added to the
    MCP plugin's servers (a bare name refers to a server configured
    elsewhere: it only needs the plugin), ``hooks`` added to the hooks
    plugin's while this agent runs, ``memory`` an agent memory directory in
    that scope, ``autoCompactWindow`` the compaction window,
    ``criticalSystemReminder_EXPERIMENTAL`` a text the environment plugin
    sends with every model call."""
    x = f.extra
    out: list[PluginEntry] = []
    if x.get("skills"):
        out.append(("skills", {"preload": list(_names(x["skills"], "skills", f.source))}))
    if x.get("mcpServers"):
        inline: dict[str, Any] = {}
        for item in x["mcpServers"]:
            if isinstance(item, dict):
                inline.update(item)
        out.append(("mcp", {"agent_servers": inline} if inline else {}))
    if x.get("hooks"):
        hooks = dict(x["hooks"])
        if isinstance(hooks.get("Stop"), list):
            # The agent's own Stop hooks are its SubagentStop when it runs
            # as a sub-agent (Claude Code converts them); a main agent
            # never fires SubagentStop, a sub-agent never Stop.
            hooks["SubagentStop"] = [*(hooks.get("SubagentStop") or []), *hooks["Stop"]]
        out.append(("hooks", {"hooks": without_env(hooks)}))
    if x.get("memory"):
        out.append(("memory", {"agent_scope": x["memory"], "agent_name": f.name}))
    critical = x.get("criticalSystemReminder_EXPERIMENTAL")
    if isinstance(critical, str) and critical.strip():
        out.append(("environment", {"critical_reminder": critical}))
    if x.get("autoCompactWindow") is not None:
        out.append(("compaction", {"auto_compact_window": _tokens(x["autoCompactWindow"],
                                                                 f.source)}))
    return out


def _lay_over(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """``overlay`` laid over ``base``, adding rather than replacing: lists
    join, mappings join key by key (lists under one key join too), anything
    else is replaced."""
    out = dict(base)
    for key, value in overlay.items():
        old = out.get(key)
        if isinstance(old, Mapping) and isinstance(value, Mapping):
            merged = dict(old)
            for k, v in value.items():
                prev = merged.get(k)
                merged[k] = [*prev, *v] if isinstance(prev, list) and isinstance(v, list) \
                    else v
            out[key] = merged
        elif isinstance(old, (list, tuple)) and isinstance(value, (list, tuple)):
            out[key] = list(dict.fromkeys([*old, *value]))
        else:
            out[key] = value
    return out


def parse_agent_file(text: str, *, source: str = "", default_name: str = "") -> AgentFile:
    """Read one agent file. The body after the frontmatter is the system
    prompt, unchanged."""
    if not frontmatter.opens(text):
        raise AgentFileError(f"{source}: an agent file starts with '---' frontmatter")
    front, body = frontmatter.split(text)
    if front is None:
        raise AgentFileError(f"{source}: the frontmatter has no closing '---'")
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
    identity = data.pop("identity", None)
    if identity is not None and not isinstance(identity, str):
        raise AgentFileError(f"{source}: 'identity' must be a string")
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
    _check_claude_code_fields(data, source)
    for key in data:
        if (source, key) in _WARNED or _KEPT.get(key, False):
            continue
        _WARNED.add((source, key))
        if key not in _KEPT:
            log.warning("%s: unknown agent file field %r is ignored", source, key)
        else:
            log.warning("%s: agent file field %r is not applied by noeta", source, key)
    return AgentFile(
        name=name.strip(),
        prompt=body,
        description=description.strip(),
        model=None if model is None or model.strip() in _INHERIT else model.strip(),
        plugins=None if plugins is None else parse_plugin_list(plugins, source),
        tools=None if names is None or "*" in names else names,
        one_shot=one_shot,
        effort=str(effort) if effort is not None else None,
        identity=identity.strip() if identity is not None else None,
        source=source,
        extra=data,
        denied_tools=() if denied is None else _names(denied, "disallowedTools", source),
        permission_mode=mode,
        max_turns=max_turns,
        omit_instructions=omit,
    )


def read_file_text(path: Path) -> str | None:
    """``path``'s UTF-8 text, or ``None`` (logged) when it cannot be read (a
    dangling link, not UTF-8, gone): a loader skips that one file and goes
    on, as Claude Code does, so one bad file in a plugin stops nothing."""
    try:
        return path.read_text("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("skipping %s: it cannot be read (%s)", path, exc)
        return None


def read_agent_file(file: Path) -> AgentFile | None:
    """The agent file at ``file`` (named by its stem unless it names
    itself), or ``None`` (logged) when it cannot be read or parsed."""
    text = read_file_text(file)
    if text is None:
        return None
    try:
        return parse_agent_file(text, source=str(file), default_name=file.stem)
    except AgentFileError as exc:
        log.warning("skipping agent file %s: %s", file, exc)
        return None


def read_agent_dir(path: str | Path) -> list[AgentFile]:
    """Every ``*.md`` agent file in ``path`` (not recursive), by file name.
    A file that cannot be read is logged and skipped."""
    root = Path(path).expanduser()
    if not root.is_dir():
        return []
    return [f for file in sorted(root.glob("*.md")) if (f := read_agent_file(file)) is not None]


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    try:
        hash(value)
    except TypeError:
        return ("object", id(value))
    return value


_SECRET = re.compile(r"\{(env|keyring):([^{}]+)\}")


def resolve_secrets(value: Any, where: str = "") -> Any:
    """``value`` (a plugin's arguments) with each ``{env:NAME}`` replaced
    by that environment variable and each ``{keyring:service/user}`` by the
    OS keyring's secret (the optional ``keyring`` package), inside strings
    of any nesting. The references, not the secrets, are what config files
    and records hold."""
    if isinstance(value, str):
        return _SECRET.sub(lambda m: _secret(m[1], m[2], where), value)
    if isinstance(value, Mapping):
        return {k: resolve_secrets(v, where) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(resolve_secrets(v, where) for v in value)
    return value


def _secret(kind: str, ref: str, where: str) -> str:
    if kind == "env":
        found = os.environ.get(ref)
        if found is None:
            raise ValueError(f"{where}: {{env:{ref}}} names an environment variable that is "
                             "not set")
        return found
    service, _, account = ref.partition("/")
    try:
        import keyring  # type: ignore[import-not-found, unused-ignore]
    except ImportError:
        raise ValueError(f"{where}: {{keyring:{ref}}} needs the keyring package") from None
    try:
        secret = keyring.get_password(service, account)
    except Exception as exc:  # noqa: BLE001 — NoKeyringError, a locked or broken backend
        raise ValueError(f"{where}: this machine's keyring cannot be read for {ref!r} "
                         f"({type(exc).__name__}: {exc})") from None
    if secret is None:
        raise ValueError(f"{where}: the keyring has no secret for {ref!r}")
    return str(secret)


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
    made: dict[Any, Extension] | None = None,
) -> list[Agent]:
    """The agents of ``files`` (a later file replaces an earlier one of the
    same name).

    ``plugins`` is the host's setting per plugin name: its arguments (laid
    under the file's; ``enabled: false`` among them turns it off), a ready
    ``Extension`` (used where a file gives no arguments), or ``False`` to
    leave it out of every agent. ``add`` names more plugins per agent
    (``"*"``: every agent), after the file's own; a plugin an agent already
    has is not added twice. A plugin is made once per set of arguments and
    shared by every agent that names it with those arguments. Every agent also
    gets the :data:`ALWAYS_PLUGINS` it does not name. ``extensions``
    are added after every agent's own. ``made`` keeps the plugins made, so a
    later build with it reuses each one whose code and arguments are the same
    (a reload keeps what the unchanged plugins hold in memory)."""
    by_name: dict[str, AgentFile] = {}
    for f in files:
        by_name[f.name] = f
    settings = {name: _setting(name, v) for name, v in (plugins or {}).items()}
    add = add or {}
    made = {} if made is None else made

    def plugin(name: str, args: Mapping[str, Any],
               overlay: Mapping[str, Any] | None = None) -> Extension:
        _on, host_args, instance = settings.get(name, (True, {}, None))
        if not args and instance is not None:
            if overlay:
                log.warning("plugin %r is given as a ready instance; an agent file's fields "
                            "for it are not applied", name)
            return instance
        factory = registry.load(registry.PLUGINS, name)
        arguments = _lay_over({**host_args, **args}, overlay or {})
        key = (name, factory, _freeze(arguments))
        if key not in made:
            made[key] = factory(**resolve_secrets(arguments, f"plugin {name!r}"))
            registry.note_arguments(made[key], arguments)
        return made[key]

    agents: list[Agent] = []
    for f in by_name.values():
        entries = list(f.plugins if f.plugins is not None else default_plugins)
        names = {name for name, _ in entries}
        for name in ALWAYS_PLUGINS:
            if name not in names:
                names.add(name)
                # Before compaction, which stays last (it reuses the request).
                at = len(entries) - 1 if entries and entries[-1][0] == "compaction" \
                    else len(entries)
                entries.insert(at, (name, {}))
        for name, args in (*add.get(f.name, ()), *add.get("*", ())):
            if name not in names:
                names.add(name)
                entries.append((name, args))
        overlays: dict[str, dict[str, Any]] = {}
        for name, args in plugin_overlays(f):
            overlays[name] = _lay_over(overlays.get(name, {}), args)
            if not settings.get(name, (True,))[0]:
                if (f.source, f"plugin:{name}") not in _WARNED:
                    _WARNED.add((f.source, f"plugin:{name}"))
                    log.warning("%s: the %s plugin is off here; the agent file's fields for "
                                "it are not applied", f.source or f.name, name)
            elif name not in names:
                names.add(name)
                # Before compaction, which stays last (it reuses the request).
                at = len(entries) - 1 if entries and entries[-1][0] == "compaction" \
                    else len(entries)
                entries.insert(at, (name, {}))
        # ``omitClaudeMd`` keeps only the machine's managed files (Claude Code).
        exts = [plugin(name, {**args, "managed_only": True}
                       if f.omit_instructions and name == "instructions" else args,
                       overlays.get(name)) for name, args in entries
                if settings.get(name, (True,))[0]]
        agents.append(Agent(
            name=f.name,
            model=models.resolve_alias(f.model) if f.model else model,
            system_prompt=f.prompt,
            identity=f.identity if f.identity is not None else DEFAULT_IDENTITY,
            description=f.description,
            extensions=(*exts, *extensions),
            effort=f.effort,  # type: ignore[arg-type]
            one_shot=f.one_shot,
            allowed_tools=f.tools,
            denied_tools=f.denied_tools,
            step_budget=f.max_turns or Agent.step_budget,
            # Claude Code's spelling: no model is "inherit" (the caller's).
            extra={**f.extra, "model": f.model or "inherit",
                   **({"permissionMode": f.permission_mode} if f.permission_mode else {}),
                   **({"inheritCap": BUILTIN_INHERIT_CAP[f.source]}
                      if not f.model and f.source in BUILTIN_INHERIT_CAP else {})},
        ))
    return agents
