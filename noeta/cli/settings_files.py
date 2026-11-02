"""noeta's settings files, in Claude Code's format and precedence. Claude
Code's own (``~/.claude``, ``.claude/``) are never read.

The files, lowest precedence first:

1. ``~/.noeta/settings.json`` (the user's);
2. ``<project>/.noeta/settings.json`` and
3. ``<project>/.noeta/settings.local.json`` — only once the project is
   trusted (``noeta trust``), as everything that changes what an agent may do;
4. the machine's managed ``managed-settings.json`` (:func:`managed_dir`).

A headless run's ``--settings`` adds a layer just below the managed one, and
``--setting-sources`` picks which of 1 to 3 are read (:func:`settings_layers`).

A key a later file sets wins (``model``, ``agent``, ``outputStyle``, ``language``,
each ``attribution`` line, each ``env`` variable, ``permissions.defaultMode``);
permission rule lists and ``additionalDirectories`` add up across files, without repeats; the
``sandbox`` object merges as :func:`merge_sandbox` says. Hooks and
MCP consent (``enabledMcpjsonServers``) are read from the same files by the
hooks and MCP plugins, which take the file list.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("noeta.cli")

#: Where a machine's managed settings (``managed-settings.json``) and
#: instructions (``NOETA.md``, ``.noeta/rules``) live: Claude Code's places
#: under noeta's name.
MANAGED_DIRS = {"darwin": "/Library/Application Support/Noeta",
                "win32": r"C:\Program Files\Noeta"}
DEFAULT_MANAGED_DIR = "/etc/noeta"
#: Permission rule lists and their keys in the permissions plugin's
#: arguments.
RULE_LISTS = ("allow", "deny", "ask")


def managed_dir() -> Path:
    return Path(MANAGED_DIRS.get(sys.platform, DEFAULT_MANAGED_DIR))


@dataclass
class FileSettings:
    """What the settings files say, merged."""

    #: The files read, lowest precedence first (missing ones included).
    files: list[Path] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    model: str | None = None
    #: The agent front ends start with (Claude Code's ``agent``).
    agent: str | None = None
    output_style: str | None = None
    #: ``language``: the language the model answers in.
    language: str | None = None
    #: ``attribution.commit`` / ``attribution.pr`` (an empty string: no
    #: line); ``includeCoAuthoredBy: false`` without them sets both empty.
    attribution: dict[str, str] = field(default_factory=dict)
    #: ``allow`` / ``deny`` / ``ask`` rule lists (Claude Code's
    #: ``Tool(spec)`` strings, unparsed), ``defaultMode``,
    #: ``additionalDirectories`` and the top-level ``useAutoModeDuringPlan``
    #: (kept here only when a file turns it off).
    permissions: dict[str, Any] = field(default_factory=dict)
    #: The ``sandbox`` object, merged (:func:`merge_sandbox`).
    sandbox: dict[str, Any] = field(default_factory=dict)
    #: The permissions that feed the sandbox: ``deny`` rules from every
    #: file, but ``allow`` rules and ``additionalDirectories`` (which widen
    #: it) not from a project's files.
    sandbox_permissions: dict[str, Any] = field(default_factory=dict)
    #: Claude Code's skill listing settings (``skillListingBudgetFraction``,
    #: ``skillListingMaxDescChars``), the last file's of each.
    skill_listing: dict[str, Any] = field(default_factory=dict)


#: Claude Code's skill listing settings, the range each takes and whether
#: it is a whole number.
SKILL_LISTING_KEYS = {"skillListingBudgetFraction": (0.0, 1.0, False),
                      "skillListingMaxDescChars": (0.0, float("inf"), True)}

#: Claude Code's ``--setting-sources`` names.
SETTING_SOURCES = ("user", "project", "local")


def settings_layers(cwd: str | None, *, noeta_home: Path, trusted: bool,
                    managed: Path | None = None, sources: Collection[str] | None = None,
                    flag: Path | None = None) -> list[tuple[Path, str]]:
    """The settings files for a front end started in ``cwd``, lowest
    precedence first, each with whose it is (``user`` / ``project`` /
    ``managed``); the project's only when ``trusted``. ``sources``
    (``--setting-sources``) keeps only those of the user / project / local
    files (``None``: all); ``flag`` (``--settings``, given by the person
    running noeta, so a ``user`` layer) goes after them, below the managed
    file, which is always read."""
    layers: list[tuple[Path, str]] = []
    if sources is None or "user" in sources:
        layers.append((noeta_home / "settings.json", "user"))
    if cwd and trusted:
        project = Path(cwd).expanduser() / ".noeta"
        if sources is None or "project" in sources:
            layers.append((project / "settings.json", "project"))
        if sources is None or "local" in sources:
            layers.append((project / "settings.local.json", "project"))
    if flag is not None:
        layers.append((flag, "user"))
    layers.append(((managed or managed_dir()) / "managed-settings.json", "managed"))
    return layers


def settings_files(cwd: str | None, *, noeta_home: Path, trusted: bool,
                   managed: Path | None = None) -> list[Path]:
    """The paths of :func:`settings_layers` (every source, no flag layer)."""
    return [path for path, _ in settings_layers(cwd, noeta_home=noeta_home, trusted=trusted,
                                                managed=managed)]


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


#: ``sandbox`` lists that add up across files, by their path in the object.
SANDBOX_LISTS = (("excludedCommands",), ("filesystem", "allowWrite"),
                 ("filesystem", "denyWrite"), ("filesystem", "denyRead"),
                 ("filesystem", "allowRead"), ("network", "allowedDomains"),
                 ("network", "deniedDomains"), ("network", "allowUnixSockets"),
                 ("credentials", "files"), ("credentials", "envVars"))
#: ``sandbox`` switches a project's files may turn only one way, as in Claude
#: Code: a cloned repository can tighten its sandbox, never loosen it.
SANDBOX_PROJECT_ONLY = {("enabled",): True, ("allowUnsandboxedCommands",): False,
                        ("autoAllowBashIfSandboxed",): False,
                        ("failIfUnavailable",): True}
#: ``sandbox`` keys a project's files never set: the switches and the lists
#: that would open the sandbox up (more commands outside it, more paths to
#: write or read, more hosts or sockets to reach).
SANDBOX_NOT_FROM_PROJECT = (("allowNestedUserNamespaces",), ("enableWeakerNestedSandbox",),
                            ("filesystem", "disabled"), ("network", "allowAllUnixSockets"),
                            ("network", "allowLocalBinding"), ("network", "httpProxyPort"),
                            ("network", "socksProxyPort"), ("bwrapPath",),
                            ("excludedCommands",), ("filesystem", "allowWrite"),
                            ("filesystem", "allowRead"), ("network", "allowedDomains"),
                            ("network", "allowUnixSockets"))


def _get(data: dict[str, Any], key: tuple[str, ...]) -> Any:
    for part in key[:-1]:
        inner = data.get(part)
        data = inner if isinstance(inner, dict) else {}
    return data.get(key[-1])


def _put(data: dict[str, Any], key: tuple[str, ...], value: Any) -> None:
    for part in key[:-1]:
        data = data.setdefault(part, {})
    data[key[-1]] = value


def _leaves(data: dict[str, Any], prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    out: list[tuple[str, ...]] = []
    for key, value in data.items():
        if isinstance(value, dict):
            out += _leaves(value, (*prefix, key))
        else:
            out.append((*prefix, key))
    return out


def merge_sandbox(layers: list[tuple[dict[str, Any], str]]) -> dict[str, Any]:
    """Claude Code's ``sandbox`` objects merged, lowest precedence first;
    each comes with where it is from (``user``, ``project``, ``managed``). Lists add up; a later
    switch wins, except that a project's file may only tighten
    (:data:`SANDBOX_PROJECT_ONLY`) and never sets the keys or lists that
    loosen it (:data:`SANDBOX_NOT_FROM_PROJECT`); ``bwrapPath`` comes from
    managed settings only."""
    out: dict[str, Any] = {}
    for data, kind in layers:
        if not isinstance(data, dict):
            continue
        project, managed = kind == "project", kind == "managed"
        for key in _leaves(data):
            value = _get(data, key)
            if project and key in SANDBOX_NOT_FROM_PROJECT:
                log.warning("settings: a project's sandbox.%s would loosen the sandbox; "
                            "ignored (set it in ~/.noeta/settings.json)", ".".join(key))
                continue
            if key in SANDBOX_LISTS:
                if isinstance(value, list):
                    have = _get(out, key) or []
                    _put(out, key, have + [v for v in value if v not in have])
                continue
            if key == ("bwrapPath",) and not managed:
                continue
            if project and key in SANDBOX_PROJECT_ONLY and value != SANDBOX_PROJECT_ONLY[key] \
                    and _get(out, key) is not None:
                continue
            _put(out, key, value)
    return out


def load(files: list[Path], kinds: list[str] | None = None) -> FileSettings:
    """Read and merge ``files`` (lowest precedence first, as
    :func:`settings_files` lists them: the user's first, managed last, a
    project's in between); ``kinds`` says whose each is when the list is
    not that one (:func:`settings_layers`)."""
    out = FileSettings(files=list(files))
    if kinds is None:
        kinds = ["user" if i == 0 else "managed" if i == len(files) - 1 else "project"
                 for i in range(len(files))]
    lists: dict[str, list[str]] = {k: [] for k in (*RULE_LISTS, "additionalDirectories")}
    widening: dict[str, list[str]] = {"allow": [], "additionalDirectories": []}
    sandboxes: list[tuple[dict[str, Any], str]] = []
    for path, kind in zip(files, kinds, strict=True):
        data = _read(path)
        if isinstance(data.get("sandbox"), dict):
            sandboxes.append((data["sandbox"], kind))
        env = data.get("env")
        if isinstance(env, dict):
            out.env.update({str(k): str(v) for k, v in env.items()
                            if isinstance(v, (str, int, float, bool))})
        if isinstance(data.get("model"), str) and data["model"]:
            out.model = data["model"]
        if isinstance(data.get("agent"), str) and data["agent"]:
            out.agent = data["agent"]
        if isinstance(data.get("outputStyle"), str) and data["outputStyle"]:
            out.output_style = data["outputStyle"]
        if isinstance(data.get("language"), str):
            out.language = data["language"].strip() or None
        attribution = data.get("attribution")
        if isinstance(attribution, dict):
            out.attribution.update({k: v for k, v in attribution.items()
                                    if k in ("commit", "pr") and isinstance(v, str)})
        elif data.get("includeCoAuthoredBy") is False:
            out.attribution.update({"commit": "", "pr": ""})
        for key, (low, high, whole) in SKILL_LISTING_KEYS.items():
            value = data.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) \
                    and low < value <= high and (not whole or float(value).is_integer()):
                out.skill_listing[key] = value
        if data.get("useAutoModeDuringPlan") is False:
            # As Claude Code: any file that turns it off turns it off.
            out.permissions["useAutoModeDuringPlan"] = False
        perms = data.get("permissions")
        if not isinstance(perms, dict):
            continue
        for key, values in lists.items():
            for value in perms.get(key) or []:
                if isinstance(value, str) and value not in values:
                    values.append(value)
                if key in widening and kind != "project" and isinstance(value, str):
                    widening[key].append(value)
        if isinstance(perms.get("defaultMode"), str):
            out.permissions["defaultMode"] = perms["defaultMode"]
        if perms.get("disableBypassPermissionsMode") == "disable":
            # Any file may switch the mode off; none switches it back on.
            out.permissions["disableBypassPermissionsMode"] = "disable"
    out.permissions.update({k: v for k, v in lists.items() if v})
    out.sandbox = merge_sandbox(sandboxes)
    out.sandbox_permissions = {**({"deny": lists["deny"]} if lists["deny"] else {}),
                               **{k: v for k, v in widening.items() if v}}
    return out


def apply_env(settings: FileSettings) -> None:
    """Set the settings' ``env`` in this process, as Claude Code does: a
    provider, a tool and a hook started from here all see it."""
    os.environ.update(settings.env)


def permission_arguments(settings: FileSettings, accepted: set[str]) -> dict[str, Any]:
    """The permissions plugin's arguments the settings give, as plain data:
    ``mode`` (``defaultMode``), ``allow`` / ``deny`` / ``ask`` (rule strings),
    ``additional_directories``, ``disable_bypass_mode``
    (``disableBypassPermissionsMode: "disable"``) and ``auto_during_plan``
    (the top-level ``useAutoModeDuringPlan``); only those the plugin takes
    (``accepted``, its argument names)."""
    perms = settings.permissions
    args: dict[str, Any] = {k: list(perms[k]) for k in RULE_LISTS if k in perms}
    if "additionalDirectories" in perms:
        args["additional_directories"] = [str(Path(d).expanduser())
                                          for d in perms["additionalDirectories"]]
    if perms.get("disableBypassPermissionsMode") == "disable":
        args["disable_bypass_mode"] = True
    if perms.get("useAutoModeDuringPlan") is False:
        args["auto_during_plan"] = False
    if "defaultMode" in perms:
        if perms["defaultMode"] == "bypassPermissions" and args.get("disable_bypass_mode"):
            log.warning("settings: defaultMode bypassPermissions is disabled "
                        "(disableBypassPermissionsMode); ignored")
        else:
            args["mode"] = perms["defaultMode"]
    dropped = sorted(k for k in args if k not in accepted)
    if dropped:
        log.warning("settings: the permissions plugin takes no %s yet; ignored",
                    ", ".join(dropped))
    return {k: v for k, v in args.items() if k in accepted}
