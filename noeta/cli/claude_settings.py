"""Claude Code's settings files, read with Claude Code's meaning next to
noeta's own ``~/.noeta/settings.json``.

The files, lowest precedence first:

1. ``~/.claude/settings.json`` (the user's Claude Code settings);
2. ``~/.noeta/settings.json`` (noeta's, at the same level, over it);
3. ``<project>/.claude/settings.json`` and
4. ``<project>/.claude/settings.local.json`` — only once the project is
   trusted (``noeta trust``), as everything that changes what an agent may do;
5. the machine's managed ``managed-settings.json``.

A key a later file sets wins (``model``, ``outputStyle``, each ``env``
variable, ``permissions.defaultMode``); permission rule lists and
``additionalDirectories`` add up across files, without repeats. Hooks and
MCP consent (``enabledMcpjsonServers``) are read from the same files by the
hooks and MCP plugins, which take the file list.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("noeta.cli")

#: Where Claude Code keeps a machine's managed settings and instructions.
MANAGED_DIRS = {"darwin": "/Library/Application Support/ClaudeCode",
                "win32": r"C:\Program Files\ClaudeCode"}
DEFAULT_MANAGED_DIR = "/etc/claude-code"
#: Permission rule lists and their keys in the permissions plugin's
#: arguments.
RULE_LISTS = ("allow", "deny", "ask")


def managed_dir() -> Path:
    return Path(MANAGED_DIRS.get(sys.platform, DEFAULT_MANAGED_DIR))


@dataclass
class ClaudeSettings:
    """What the settings files say, merged."""

    #: The files read, lowest precedence first (missing ones included).
    files: list[Path] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    model: str | None = None
    output_style: str | None = None
    #: ``allow`` / ``deny`` / ``ask`` rule lists (Claude Code's
    #: ``Tool(spec)`` strings, unparsed), ``defaultMode`` and
    #: ``additionalDirectories``.
    permissions: dict[str, Any] = field(default_factory=dict)


def settings_files(cwd: str | None, *, claude_home: Path, noeta_home: Path,
                   trusted: bool, managed: Path | None = None) -> list[Path]:
    """The settings files for a front end started in ``cwd``, lowest
    precedence first; the project's only when ``trusted``."""
    files = [claude_home / "settings.json", noeta_home / "settings.json"]
    if cwd and trusted:
        project = Path(cwd).expanduser() / ".claude"
        files += [project / "settings.json", project / "settings.local.json"]
    files.append((managed or managed_dir()) / "managed-settings.json")
    return files


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        log.warning("settings: %s could not be read (%s); ignored", path, exc)
        return {}
    if not isinstance(data, dict):
        log.warning("settings: %s is not a JSON object; ignored", path)
        return {}
    return data


def load(files: list[Path]) -> ClaudeSettings:
    out = ClaudeSettings(files=list(files))
    lists: dict[str, list[str]] = {k: [] for k in (*RULE_LISTS, "additionalDirectories")}
    for path in files:
        data = _read(path)
        env = data.get("env")
        if isinstance(env, dict):
            out.env.update({str(k): str(v) for k, v in env.items()
                            if isinstance(v, (str, int, float, bool))})
        if isinstance(data.get("model"), str) and data["model"]:
            out.model = data["model"]
        if isinstance(data.get("outputStyle"), str) and data["outputStyle"]:
            out.output_style = data["outputStyle"]
        perms = data.get("permissions")
        if not isinstance(perms, dict):
            continue
        for key, values in lists.items():
            for value in perms.get(key) or []:
                if isinstance(value, str) and value not in values:
                    values.append(value)
        if isinstance(perms.get("defaultMode"), str):
            out.permissions["defaultMode"] = perms["defaultMode"]
    out.permissions.update({k: v for k, v in lists.items() if v})
    return out


def apply_env(settings: ClaudeSettings) -> None:
    """Set the settings' ``env`` in this process, as Claude Code does: a
    provider, a tool and a hook started from here all see it."""
    os.environ.update(settings.env)


def permission_arguments(settings: ClaudeSettings, accepted: set[str]) -> dict[str, Any]:
    """The permissions plugin's arguments the settings give, as plain data:
    ``mode`` (``defaultMode``), ``allow`` / ``deny`` / ``ask`` (rule strings)
    and ``additional_directories``; only those the plugin takes
    (``accepted``, its argument names)."""
    perms = settings.permissions
    args: dict[str, Any] = {k: list(perms[k]) for k in RULE_LISTS if k in perms}
    if "additionalDirectories" in perms:
        args["additional_directories"] = [str(Path(d).expanduser())
                                          for d in perms["additionalDirectories"]]
    if "defaultMode" in perms:
        args["mode"] = perms["defaultMode"]
    dropped = sorted(k for k in args if k not in accepted)
    if dropped:
        log.warning("settings: the permissions plugin takes no %s yet; ignored",
                    ", ".join(dropped))
    return {k: v for k, v in args.items() if k in accepted}
