"""Claude Code plugin directories in noeta's own plugin places.

A directory under ``~/.noeta/plugins`` (or a trusted project's
``.noeta/plugins``) without an ``__init__.py`` that has
``.claude-plugin/plugin.json`` or Claude Code's standard layout is a Claude
Code plugin. Its parts load through the plugins that already read those
formats: ``skills/`` (the skills plugin), ``agents/*.md`` (agent files),
``commands/*.md`` (the ``markdown_commands`` plugin, named
``<plugin>:<command>`` as Claude Code does), ``hooks/hooks.json`` (the hooks
plugin) and ``.mcp.json`` (the MCP plugin; from a project only once its
settings consent, so a cloned repository never starts a server by itself).
``${CLAUDE_PLUGIN_ROOT}`` and ``${CLAUDE_PLUGIN_DATA}`` (``~/.noeta/plugin-data/<name>``)
are substituted in hooks, MCP servers, commands and agent prompts. Monitors,
``bin/``, LSP servers, themes and output styles are not loaded.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from noeta.agents.agent_files import AgentFile, AgentFileError, parse_agent_file

__all__ = ["ClaudePlugin", "find_claude_plugins", "is_claude_plugin", "plugin_settings"]

log = logging.getLogger("noeta.cli")

_PARTS = ("skills", "commands", "agents", "hooks/hooks.json", ".mcp.json")


def is_claude_plugin(directory: Path) -> bool:
    """A directory laid out as a Claude Code plugin (and not a Python one)."""
    if not directory.is_dir() or (directory / "__init__.py").exists():
        return False
    return (directory / ".claude-plugin" / "plugin.json").is_file() or any(
        (directory / part).exists() for part in _PARTS)


@dataclass
class ClaudePlugin:
    name: str
    root: Path
    version: str = ""
    #: From a project's ``.noeta/plugins`` (its MCP servers need consent).
    project: bool = False
    skill_dirs: list[str] = field(default_factory=list)
    agents: list[AgentFile] = field(default_factory=list)
    #: ``markdown_commands`` entries.
    commands: list[dict[str, Any]] = field(default_factory=list)
    #: Claude Code's hooks event map.
    hooks: dict[str, list[Any]] = field(default_factory=dict)
    #: Claude Code's ``mcpServers`` map.
    mcp_servers: dict[str, Any] = field(default_factory=dict)


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
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end > 0:
            try:
                data = yaml.safe_load(text[4:end]) or {}
            except yaml.YAMLError:
                data = {}
            return (data if isinstance(data, dict) else {}), text[end + 4:].lstrip("\n")
    return {}, text


def _command_files(paths: Iterable[Path]) -> list[Path]:
    out: list[Path] = []
    for path in paths:
        if path.is_dir():
            out += sorted(path.glob("*.md"))
        elif path.suffix == ".md" and path.is_file():
            out.append(path)
    return out


def read_claude_plugin(root: Path, data_home: Path, *, project: bool = False) -> ClaudePlugin:
    root = root.resolve()
    manifest = _json(root / ".claude-plugin" / "plugin.json") \
        if (root / ".claude-plugin" / "plugin.json").is_file() else {}
    manifest = manifest if isinstance(manifest, dict) else {}
    name = str(manifest.get("name") or root.name)
    names = {"CLAUDE_PLUGIN_ROOT": str(root),
             "CLAUDE_PLUGIN_DATA": str(data_home / re.sub(r"[^A-Za-z0-9_-]", "-", name))}
    plugin = ClaudePlugin(name=name, root=root, version=str(manifest.get("version") or ""),
                          project=project)
    skills = [root / "skills", *_paths(root, manifest.get("skills", []), "skills")]
    plugin.skill_dirs = [str(p) for p in skills if p.is_dir()]
    agents = _paths(root, manifest["agents"], "agents") if "agents" in manifest \
        else sorted((root / "agents").glob("*.md"))
    for file in agents:
        try:
            agent = parse_agent_file(file.read_text("utf-8"), source=str(file),
                                     default_name=file.stem)
        except (OSError, AgentFileError) as exc:
            log.warning("skipping agent file %s: %s", file, exc)
            continue
        plugin.agents.append(dataclasses.replace(agent, prompt=_substitute(agent.prompt, names)))
    commands = manifest.get("commands")
    sources = [] if isinstance(commands, dict) else \
        _paths(root, commands, "commands") if commands is not None else [root / "commands"]
    for file in _command_files(sources):
        front, body = _frontmatter(file.read_text("utf-8"))
        plugin.commands.append({"name": f"{name}:{file.stem}",
                                "body": _substitute(body, names),
                                "description": str(front.get("description") or ""),
                                "argument_hint": str(front.get("argument-hint") or "")})
    for key, entry in (commands or {}).items() if isinstance(commands, dict) else ():
        if not isinstance(entry, dict):
            continue
        content: Any = entry.get("content")
        files = _paths(root, entry.get("source"), "commands") if content is None else []
        for path in files:
            if path.is_file():
                content = _frontmatter(path.read_text("utf-8"))[1]
        if isinstance(content, str):
            plugin.commands.append({"name": f"{name}:{key}", "body": _substitute(content, names),
                                    "description": str(entry.get("description") or ""),
                                    "argument_hint": str(entry.get("argumentHint") or "")})
    hook_sources: list[Any] = [root / "hooks" / "hooks.json"]
    declared = manifest.get("hooks")
    for item in declared if isinstance(declared, list) else [declared] if declared else []:
        hook_sources += [item] if isinstance(item, dict) else _paths(root, item, "hooks")
    for source in hook_sources:
        events = source if isinstance(source, dict) else \
            (_json(source).get("hooks") or {}) if source.is_file() else {}
        for event, groups in _substitute(events, names).items():
            plugin.hooks.setdefault(event, []).extend(groups if isinstance(groups, list) else [])
    mcp_sources: list[Any] = [root / ".mcp.json"]
    declared = manifest.get("mcpServers")
    for item in declared if isinstance(declared, list) else [declared] if declared else []:
        mcp_sources += [{"mcpServers": item}] if isinstance(item, dict) \
            else [p for p in _paths(root, item, "mcpServers") if p.suffix == ".json"]
    for source in mcp_sources:
        data = source if isinstance(source, dict) else _json(source) if source.is_file() else {}
        servers = data.get("mcpServers", data) if isinstance(data, dict) else {}
        plugin.mcp_servers.update(_substitute(servers, names))
    return plugin


def find_claude_plugins(directories: Iterable[tuple[Path, bool]],
                        data_home: Path) -> list[ClaudePlugin]:
    """The Claude Code plugins in ``directories`` (each with whether it is a
    project's), by directory name."""
    out = []
    for directory, project in directories:
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if not entry.name.startswith((".", "_")) and is_claude_plugin(entry):
                out.append(read_claude_plugin(entry, data_home, project=project))
    return out


def plugin_settings(plugins: Iterable[ClaudePlugin]) -> tuple[dict[str, Any], list[str]]:
    """What ``plugins`` add to the plugin-setting table (skills, hooks, MCP
    servers, ``markdown_commands``), and notices for the person."""
    skills: list[str] = []
    hooks: dict[str, list[Any]] = {}
    servers: dict[str, Any] = {}
    commands: list[dict[str, Any]] = []
    notices: list[str] = []
    for p in plugins:
        skills += p.skill_dirs
        for event, groups in p.hooks.items():
            hooks.setdefault(event, []).extend(groups)
        if p.mcp_servers and p.project:
            notices.append(f"plugin {p.name} ({p.root}): its MCP servers do not start from a "
                           "project; add them to .noeta/mcp.json to consent")
        elif p.mcp_servers:
            servers.update(p.mcp_servers)
        commands += p.commands
    table: dict[str, Any] = {}
    if skills:
        table["skills"] = {"dirs": skills}
    if hooks:
        table["hooks"] = {"hooks": hooks}
    if servers:
        table["mcp"] = {"servers": servers}
    if commands:
        table["markdown_commands"] = {"commands": commands}
    return table, notices
