"""Claude Code plugin directories in noeta's own plugin places.

A directory under ``~/.noeta/plugins`` (or a trusted project's
``.noeta/plugins``) without an ``__init__.py`` that has
``.claude-plugin/plugin.json`` or Claude Code's standard layout is a Claude
Code plugin. Its parts load through the plugins that already read those
formats: ``skills/`` (the skills plugin), ``agents/**/*.md`` (agent files),
``commands/**/*.md`` (the ``markdown_commands`` plugin); its skills, agents
and commands are named ``<plugin>:<name>`` (a subdirectory adds
``<directory>:`` before the name) as Claude Code names them; ``hooks/hooks.json`` (the hooks
plugin) and ``.mcp.json`` (the MCP plugin; never from a project's plugin,
whose servers the person is told to copy into ``.noeta/mcp.json``, so a
cloned repository never starts a server by itself).
``${CLAUDE_PLUGIN_ROOT}`` and ``${CLAUDE_PLUGIN_DATA}`` (``~/.noeta/plugin-data/<name>``)
are substituted in hooks, MCP servers, skills, commands and agent prompts, and the
manifest's ``userConfig`` values as Claude Code does (:mod:`noeta.cli.plugin_options`).
The plugin's ``settings.json`` (else the manifest's ``settings``) may name
the ``agent`` front ends start with, below the person's own settings.
Monitors, ``bin/``, LSP servers, themes and output styles are not loaded.
A Python plugin directory may ship ``skills/``, ``commands/`` and ``agents/``
beside its code; those load the same way.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from noeta.agents.agent_files import (
    AgentFile,
    AgentFileError,
    parse_agent_file,
    resolve_secrets,
)
from noeta.cli import plugin_options
from noeta.toolkit import frontmatter

__all__ = ["ClaudePlugin", "find_claude_plugins", "is_claude_plugin", "plugin_settings",
           "read_claude_plugin", "read_plugin_dir", "ships_parts"]

log = logging.getLogger("noeta.cli")

_PARTS = ("skills", "commands", "agents", "hooks/hooks.json", ".mcp.json")
#: What a Python plugin directory may ship besides its code.
_PYTHON_PARTS = ("skills", "commands", "agents")


def is_claude_plugin(directory: Path) -> bool:
    """A directory laid out as a Claude Code plugin (and not a Python one)."""
    if not directory.is_dir() or (directory / "__init__.py").exists():
        return False
    return (directory / ".claude-plugin" / "plugin.json").is_file() or any(
        (directory / part).exists() for part in _PARTS)


def ships_parts(directory: Path) -> bool:
    """A Python plugin directory that also ships Claude Code's ``skills/``,
    ``commands/`` or ``agents/``."""
    return (directory / "__init__.py").is_file() and any(
        (directory / part).is_dir() for part in _PYTHON_PARTS)


def read_plugin_dir(directory: Path, data_home: Path, *,
                    project: bool = False) -> ClaudePlugin | None:
    """What a plugin directory brings in Claude Code's formats: all of a
    Claude Code plugin, only ``skills/``, ``commands/`` and ``agents/`` of a
    Python one (its code is loaded as a plugin file); ``None`` for neither."""
    if is_claude_plugin(directory):
        return read_claude_plugin(directory, data_home, project=project)
    if not ships_parts(directory):
        return None
    plugin = read_claude_plugin(directory, data_home, project=project)
    return dataclasses.replace(plugin, hooks={}, mcp_servers={}, user_config={},
                               settings={})


@dataclass
class ClaudePlugin:
    name: str
    root: Path
    version: str = ""
    #: From a project's ``.noeta/plugins`` (its MCP servers never start).
    project: bool = False
    #: Its skill directories (their skills are named ``<plugin>:<directory>``).
    skill_dirs: list[str] = field(default_factory=list)
    #: What its skills' bodies fill in: ``${CLAUDE_PLUGIN_ROOT}``,
    #: ``${CLAUDE_PLUGIN_DATA}`` and each ``${user_config.KEY}`` (a sensitive
    #: one gets Claude Code's placeholder, an unset one stays as written).
    skill_vars: dict[str, str] = field(default_factory=dict)
    agents: list[AgentFile] = field(default_factory=list)
    #: ``markdown_commands`` entries.
    commands: list[dict[str, Any]] = field(default_factory=list)
    #: The same commands as skills the model can call (Claude Code's): the
    #: skills plugin's ``plugin_commands`` entries (``name``, ``path``, the
    #: file's ``text`` with the plugin's variables filled in).
    skill_commands: list[dict[str, str]] = field(default_factory=list)
    #: Claude Code's hooks event map.
    hooks: dict[str, list[Any]] = field(default_factory=dict)
    #: Claude Code's ``mcpServers`` map.
    mcp_servers: dict[str, Any] = field(default_factory=dict)
    #: The manifest's ``userConfig`` fields.
    user_config: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: The settings it brings (of Claude Code's plugin keys, ``agent``).
    settings: dict[str, Any] = field(default_factory=dict)
    #: What the person should know: parts left out here, and why.
    notices: list[str] = field(default_factory=list)


def _paths(root: Path, value: Any, key: str) -> list[Path]:
    """A manifest's path or list of paths, each inside ``root``."""
    items = value if isinstance(value, list) else [value]
    out = []
    for item in items:
        if not isinstance(item, str):
            continue
        path = (root / item).resolve()
        if path != root and root not in path.parents:
            log.warning("plugin %s: %s path %s escapes the plugin directory", root, key, item)
            continue
        out.append(path)
    return out


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("plugin file %s could not be read: %s", path, exc)
        return {}


def _substitute(value: Any, names: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return re.sub(r"\$\{(CLAUDE_PLUGIN_ROOT|CLAUDE_PLUGIN_DATA)\}",
                      lambda m: names[m[1]], value)
    if isinstance(value, Mapping):
        return {k: _substitute(v, names) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, names) for v in value]
    return value


def _frontmatter(text: str) -> tuple[dict[str, Any], str]:
    head, body = frontmatter.split(text)
    if head is None:
        return {}, body
    try:
        data = yaml.safe_load(head) or {}
    except yaml.YAMLError:
        data = {}
    return (data if isinstance(data, dict) else {}), body.lstrip("\n")


def _allowed(value: Any, names: Mapping[str, str]) -> str:
    """A command's ``allowed-tools`` (a list or one string) as one string."""
    text = ", ".join(map(str, value)) if isinstance(value, list) else str(value or "")
    return str(_substitute(text, names))


def _md_files(paths: Iterable[Path]) -> list[tuple[Path, tuple[str, ...]]]:
    """The ``.md`` files of ``paths`` (a directory's, at any depth), each with
    the subdirectories it lies in: Claude Code puts them in the name."""
    out: list[tuple[Path, tuple[str, ...]]] = []
    for path in paths:
        if path.is_dir():
            out += [(f, f.relative_to(path).parent.parts) for f in sorted(path.rglob("*.md"))]
        elif path.suffix == ".md" and path.is_file():
            out.append((path, ()))
    return out


def _qualified(plugin: str, within: tuple[str, ...], name: str) -> str:
    """Claude Code's name for a plugin's agent or command."""
    return ":".join((plugin, *within, name))


def _manifest(root: Path) -> dict[str, Any]:
    path = root / ".claude-plugin" / "plugin.json"
    manifest = _json(path) if path.is_file() else {}
    return manifest if isinstance(manifest, dict) else {}


def _settings(root: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """The settings a plugin brings: its ``settings.json``, else the
    manifest's ``settings``; only the key Claude Code takes from a plugin
    that noeta has (``agent``)."""
    path = root / "settings.json"
    for data in (_json(path) if path.is_file() else None, manifest.get("settings")):
        if isinstance(data, dict) and isinstance(data.get("agent"), str) and data["agent"]:
            return {"agent": data["agent"]}
    return {}


#: Events whose hooks decide whether something may happen: one that cannot
#: run as written here refuses instead of being left out (fail closed).
GUARD_EVENTS = frozenset({"PreToolUse", "PermissionRequest", "UserPromptSubmit"})
_KEYRING_REF = re.compile(r"\{keyring:([^{}]+)\}")


def _foreign_secret(name: str, value: Any) -> str | None:
    """A ``{keyring:…}`` reference in what the plugin itself wrote that is
    not one of its own option values' (``noeta-plugin/<name>/…``): a plugin
    reads only its own secrets."""
    own = f"{plugin_options.KEYRING_SERVICE}/{name}/"
    for ref in _KEYRING_REF.findall(json.dumps(value)):
        if not ref.startswith(own):
            return f"it names the keyring secret {ref!r}, which is not one of its own options"
    return None


def _unreadable(name: str, value: Any) -> str | None:
    """Why the secret references in ``value`` cannot be read on this
    machine (a variable not set, no keyring backend, no such secret), or
    ``None``: each plugin's are checked on their own, so one never stops
    another, nor noeta."""
    try:
        resolve_secrets(value, f"plugin {name}")
    except ValueError as exc:
        return str(exc).removeprefix(f"plugin {name}: ")
    return None


def _refusing_hook(text: str) -> dict[str, Any]:
    """A command hook that blocks with ``text`` (exit 2), standing in for a
    guard hook that cannot run as written. A secret reference in ``text``
    loses its braces: the hook's arguments are resolved later."""
    text = re.sub(r"\{(env|keyring):([^{}]+)\}", r"\1:\2", text)
    return {"type": "command", "command": sys.executable, "onFailure": "block",
            "args": ["-c", "import sys; sys.stderr.write(sys.argv[1]); sys.exit(2)", text]}


def _hook_with_options(name: str, event: str, hook: dict[str, Any],
                       values: Mapping[str, Any], unreadable: Mapping[str, str],
                       notices: list[str]) -> dict[str, Any] | None:
    """A plugin's hook with its option values filled into a command's
    ``args`` form (a shell-form command naming one cannot be: the shell
    would read the value again), and every option this machine can read as
    ``CLAUDE_PLUGIN_OPTION_<KEY>``, as Claude Code gives them. One that
    cannot run as written here (it names an option that is unset or
    ``unreadable`` here) is left out with a notice, or for a
    :data:`GUARD_EVENTS` event replaced by one that refuses."""
    problem = _foreign_secret(name, hook)
    for key, why in unreadable.items():
        if problem is None and plugin_options.names(hook, key):
            problem = f"it uses option {key!r}, which cannot be read here: {why}"
    if problem is None and hook.get("args") is None and plugin_options.uses(hook.get("command")):
        problem = "it names ${user_config.*} in a shell-form command (use args, or read " \
                  "$CLAUDE_PLUGIN_OPTION_<KEY>)"
    if problem is None:
        try:
            hook = plugin_options.substitute(hook, values)
        except plugin_options.OptionError as exc:
            problem = str(exc)
    if problem is None and hook.get("type", "command") == "command":
        env = plugin_options.hook_env({k: v for k, v in values.items() if k not in unreadable})
        if env:
            hook = {**hook, "env": env}
    if problem is None:
        problem = _unreadable(name, hook)
    if problem is None:
        return hook
    if event in GUARD_EVENTS:
        text = (f"plugin {name}'s {event} hook cannot run on this machine ({problem}), so "
                f"it refuses; run noeta plugin configure {name}")
        notices.append(f"{text}.")
        return _refusing_hook(text)
    notices.append(f"plugin {name}: a {event} hook is left out: {problem}; run noeta "
                   f"plugin configure {name}")
    return None


def _hooks_with_options(name: str, events: Mapping[str, Any], values: Mapping[str, Any],
                        unreadable: Mapping[str, str],
                        notices: list[str]) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for event, groups in events.items():
        kept = []
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, dict):
                continue
            hooks = [h for h in (_hook_with_options(name, event, hook, values, unreadable,
                                                    notices)
                                 for hook in group.get("hooks") or [] if isinstance(hook, dict))
                     if h is not None]
            if hooks:
                kept.append({**group, "hooks": hooks})
        out[event] = kept
    return out


def read_claude_plugin(root: Path, data_home: Path, *, project: bool = False,
                       options: Mapping[str, Any] | None = None) -> ClaudePlugin:
    """``options``: the person's ``userConfig`` values (references
    unresolved); left out, those kept for it (:func:`plugin_options.load`)."""
    root = root.resolve()
    manifest = _manifest(root)
    name = str(manifest.get("name") or root.name)
    names = {"CLAUDE_PLUGIN_ROOT": str(root),
             "CLAUDE_PLUGIN_DATA": str(data_home / re.sub(r"[^A-Za-z0-9_-]", "-", name))}
    fields = plugin_options.schema(manifest)
    values = dict(options) if options is not None else plugin_options.load(name, fields)
    #: Options this machine cannot read (a secret its keyring lacks, a
    #: variable not set): hooks get no variable for them, and what uses one
    #: does not run.
    unreadable = {k: why for k, v in values.items()
                  if (why := _unreadable(name, v)) is not None}
    notice_list = [f"plugin {name}: option {k!r} cannot be read here ({why}); its hooks get "
                   f"no {plugin_options.env_name(k)}; run noeta plugin configure {name}"
                   for k, why in unreadable.items()]
    plugin = ClaudePlugin(name=name, root=root, version=str(manifest.get("version") or ""),
                          project=project, user_config=fields,
                          settings=_settings(root, manifest), notices=notice_list)

    def text(body: str) -> str:
        return plugin_options.content(_substitute(body, names), values, fields)
    skills = [root / "skills", *_paths(root, manifest.get("skills", []), "skills")]
    plugin.skill_dirs = [str(p) for p in skills if p.is_dir()]
    plugin.skill_vars = {"${" + k + "}": v for k, v in names.items()}
    plugin.skill_vars.update({ref: text(ref) for ref in
                              ("${user_config." + key + "}" for key in fields)})
    agents = _md_files(_paths(root, manifest["agents"], "agents") if "agents" in manifest
                       else [root / "agents"])
    for file, within in agents:
        try:
            agent = parse_agent_file(file.read_text("utf-8"), source=str(file),
                                     default_name=file.stem)
        except (OSError, AgentFileError) as exc:
            log.warning("skipping agent file %s: %s", file, exc)
            continue
        plugin.agents.append(dataclasses.replace(
            agent, name=_qualified(name, within, agent.name), prompt=text(agent.prompt)))
    commands = manifest.get("commands")
    sources = [] if isinstance(commands, dict) else \
        _paths(root, commands, "commands") if commands is not None else [root / "commands"]
    for file, within in _md_files(sources):
        raw = file.read_text("utf-8")
        front, body = _frontmatter(raw)
        # A ``SKILL.md`` among the commands is named by its directory.
        own = _qualified(name, within[:-1], within[-1]) \
            if file.name.lower() == "skill.md" and within else _qualified(name, within, file.stem)
        plugin.commands.append({"name": own,
                                "body": text(body),
                                "description": str(front.get("description") or ""),
                                "argument_hint": str(front.get("argument-hint") or ""),
                                "allowed_tools": _allowed(front.get("allowed-tools"), names)})
        plugin.skill_commands.append({"name": own, "path": str(file), "text": text(raw)})
    for key, entry in (commands or {}).items() if isinstance(commands, dict) else ():
        if not isinstance(entry, dict):
            continue
        content: Any = entry.get("content")
        files = _paths(root, entry.get("source"), "commands") if content is None else []
        front = {}
        for path in files:
            if path.is_file():
                front, content = _frontmatter(path.read_text("utf-8"))
        if isinstance(content, str):
            allowed = entry.get("allowedTools", front.get("allowed-tools"))
            plugin.commands.append({"name": f"{name}:{key}", "body": text(content),
                                    "description": str(entry.get("description") or ""),
                                    "argument_hint": str(entry.get("argumentHint") or ""),
                                    "allowed_tools": _allowed(allowed, names)})
            head = {"description": entry.get("description") or front.get("description"),
                    "allowed-tools": allowed}
            fm = "".join(f"{k}: {json.dumps(', '.join(v) if isinstance(v, list) else str(v))}\n"
                         for k, v in head.items() if v)
            plugin.skill_commands.append({
                "name": f"{name}:{key}", "path": str(files[0]) if files else "",
                "text": (f"---\n{fm}---\n" if fm else "") + text(content)})
    hook_sources: list[Any] = [root / "hooks" / "hooks.json"]
    declared = manifest.get("hooks")
    for item in declared if isinstance(declared, list) else [declared] if declared else []:
        hook_sources += [item] if isinstance(item, dict) else _paths(root, item, "hooks")
    for source in hook_sources:
        events = source if isinstance(source, dict) else \
            (_json(source).get("hooks") or {}) if source.is_file() else {}
        for event, groups in _hooks_with_options(name, _substitute(events, names),
                                                 values, unreadable, plugin.notices).items():
            plugin.hooks.setdefault(event, []).extend(groups)
    mcp_sources: list[Any] = [root / ".mcp.json"]
    declared = manifest.get("mcpServers")
    for item in declared if isinstance(declared, list) else [declared] if declared else []:
        mcp_sources += [{"mcpServers": item}] if isinstance(item, dict) \
            else [p for p in _paths(root, item, "mcpServers") if p.suffix == ".json"]
    for source in mcp_sources:
        data = source if isinstance(source, dict) else _json(source) if source.is_file() else {}
        servers = data.get("mcpServers", data) if isinstance(data, dict) else {}
        for server, spec in _substitute(servers, names).items():
            problem = _foreign_secret(name, spec)
            if problem is None:
                try:
                    spec = plugin_options.substitute(spec, values)
                except plugin_options.OptionError as exc:
                    problem = str(exc)
            problem = problem or _unreadable(name, spec)
            if problem is None:
                plugin.mcp_servers[server] = spec
            else:
                plugin.notices.append(f"plugin {name}: MCP server {server} does not start: "
                                      f"{problem}; run noeta plugin configure {name}")
    return plugin


def find_claude_plugins(directories: Iterable[tuple[Path, bool]],
                        data_home: Path) -> list[ClaudePlugin]:
    """The Claude Code plugins in ``directories`` (each with whether it is a
    project's), by directory name, and what Python plugin directories there
    ship in Claude Code's formats (:func:`read_plugin_dir`)."""
    out = []
    for directory, project in directories:
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if not entry.name.startswith((".", "_")) and \
                    (plugin := read_plugin_dir(entry, data_home, project=project)) is not None:
                out.append(plugin)
    return out


def plugin_settings(plugins: Iterable[ClaudePlugin]) -> tuple[dict[str, Any], list[str]]:
    """What ``plugins`` add to the plugin-setting table (skills, hooks, MCP
    servers, ``markdown_commands``), and notices for the person."""
    skills: dict[str, str] = {}
    skill_vars: dict[str, dict[str, str]] = {}
    hooks: dict[str, list[Any]] = {}
    servers: dict[str, Any] = {}
    commands: list[dict[str, Any]] = []
    skill_commands: list[dict[str, str]] = []
    notices: list[str] = []
    for p in plugins:
        skills.update(dict.fromkeys(p.skill_dirs, p.name))
        skill_vars.update(dict.fromkeys(p.skill_dirs, p.skill_vars))
        for event, groups in p.hooks.items():
            hooks.setdefault(event, []).extend(groups)
        if p.mcp_servers and p.project:
            notices.append(f"plugin {p.name} ({p.root}): its MCP servers do not start from a "
                           "project; add them to .noeta/mcp.json to consent")
        elif p.mcp_servers:
            servers.update(p.mcp_servers)
        commands += p.commands
        skill_commands += p.skill_commands
    table: dict[str, Any] = {}
    if skills or skill_commands:
        table["skills"] = {"plugin_dirs": skills, "plugin_vars": skill_vars,
                           "plugin_commands": skill_commands}
    if hooks:
        table["hooks"] = {"hooks": hooks}
    if servers:
        table["mcp"] = {"servers": servers}
    if commands:
        table["markdown_commands"] = {"commands": commands}
    return table, notices
