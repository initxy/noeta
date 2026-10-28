"""Pure permission policy: modes, which tools need permission, which allow
rules auto mode ignores, the WebFetch host list check and the project's
local settings file.

Nothing here touches a task. :class:`~noeta.plugins.permissions.Permissions`
combines these into one gate; hosts and tests can call them directly.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from noeta.core.pending import MODE_CYCLE as _CYCLE
from noeta.core.pending import PERMISSION_MODES as _MODES
from noeta.toolkit.permission_rules import Rule, idna, url_host
from noeta.toolkit.shell import WRAPPER_PROGRAMS

PermissionMode = Literal["default", "acceptEdits", "plan", "auto", "dontAsk",
                         "bypassPermissions"]

#: Claude Code's permission modes and the mode key's order (their home is
#: ``noeta.core.pending``, which front ends read): ``default`` asks before
#: anything that needs permission, ``acceptEdits`` also lets file edits in
#: the working directories run, ``plan`` explores without changing anything,
#: ``auto`` lets a classifier model approve or block what would ask,
#: ``dontAsk`` refuses what would ask, ``bypassPermissions`` asks only for
#: ask rules and protected paths.
PERMISSION_MODES = _MODES
MODE_CYCLE = _CYCLE

RiskLevel = Literal["low", "medium", "high"]

#: The ordered risk scale; the ``max_risk`` ceiling indexes into it.
KNOWN_RISK_LEVELS: tuple[str, ...] = ("low", "medium", "high")

#: Which tools need permission (``low``: none) where the file, shell and
#: web checks do not decide. Claude Code's table: reading, searching,
#: delegation and conversation tools need none; anything that changes
#: something or reaches outside does. A tool the table does not name (an
#: MCP tool) needs permission.
DEFAULT_RISK: Mapping[str, str] = {
    "Read": "low",
    "Edit": "high",
    "Write": "high",
    "NotebookEdit": "high",
    "Bash": "high",
    "Monitor": "high",
    "TaskStop": "low",
    "WebFetch": "low",
    "WebSearch": "high",
    "memory_read": "low",
    "memory_search": "low",
    "memory_write": "medium",
    "memory_archive": "medium",
    "open_app": "low",
    "run_skill_script": "high",
    "ToolSearch": "low",
    "Task": "low",
    "Agent": "low",
    "SendMessage": "low",
    "ListAgents": "low",
    "SubagentHandback": "low",
    "AskUserQuestion": "low",
    "ExitPlanMode": "low",
    "Skill": "low",
    "ListMcpResourcesTool": "low",
    "ReadMcpResourceTool": "low",
    "ScheduleWakeup": "low",
    "ReportFindings": "low",
    "Workflow": "low",
    "RecallHistory": "low",
    "TodoWrite": "low",
    "structured_output": "low",
}

#: Tools auto mode lets through without the classifier (they read, or only
#: talk to the person); every other call that needs no permission elsewhere
#: is still classified (an ``Agent`` spawn, a sub-agent's hand-back).
AUTO_SAFE_TOOLS: frozenset[str] = frozenset({
    "Read", "Glob", "Grep", "LS", "NotebookRead", "ToolSearch", "Skill", "AskUserQuestion",
    "ExitPlanMode", "ListMcpResourcesTool", "ReadMcpResourceTool", "ListAgents", "TodoWrite",
    "TaskStop", "memory_read", "memory_search", "RecallHistory", "structured_output",
    "ReportFindings", "ScheduleWakeup",
})

#: Tools that run or manage shell commands alongside ``Bash``; disallowing
#: ``Bash`` removes them too.
SHELL_COMPANION_TOOLS: frozenset[str] = frozenset({"Monitor", "TaskStop"})

WEBFETCH_TOOL = "WebFetch"
#: A shell command naming this directory asks (noeta's own config).
CONFIG_DIR = ".noeta"
#: Where "always" answers are kept in a trusted project (noeta's place for
#: Claude Code's local settings file, in its format).
LOCAL_SETTINGS = ".noeta/settings.local.json"

#: Programs whose prefix rule would let any code run (interpreters): auto
#: mode ignores such an allow rule, as Claude Code drops rules that would
#: bypass its classifier.
INTERPRETERS = frozenset({"python", "python3", "node", "perl", "ruby", "php", "deno", "bun",
                          "lua", "osascript", "eval", "exec", "source", "."})

# ---------------------------------------------------------------- texts (0.6)

DENIED_BY_POLICY = "tool {name!r} denied by policy"
NOT_IN_ALLOWLIST = "tool {name!r} not in allowlist"
RISK_EXCEEDS = "tool {name!r} risk_level {risk!r} exceeds max {ceiling!r}"


def check_mode(mode: str) -> PermissionMode:
    if mode not in PERMISSION_MODES:
        raise ValueError(
            f"Invalid permission_mode {mode!r}. Must be one of {PERMISSION_MODES}."
        )
    return mode  # type: ignore[return-value]


def next_mode(mode: str, *, bypass: bool = True) -> str:
    """The mode after ``mode`` in :data:`MODE_CYCLE`."""
    cycle = [m for m in MODE_CYCLE if bypass or m != "bypassPermissions"]
    if mode not in cycle:
        return cycle[0]
    return cycle[(cycle.index(mode) + 1) % len(cycle)]


#: The keys of Claude Code's ``autoMode`` settings object.
AUTO_MODE_KEYS: tuple[str, ...] = ("allow", "soft_deny", "hard_deny", "environment")


def check_auto_mode(value: Mapping[str, Sequence[str]] | None) -> dict[str, tuple[str, ...]]:
    """``autoMode`` as ``{key: lines}`` with every key present; an unknown
    key or a line that is not a string raises."""
    out: dict[str, tuple[str, ...]] = {k: () for k in AUTO_MODE_KEYS}
    for key, lines in (value or {}).items():
        if key not in AUTO_MODE_KEYS:
            raise ValueError(f"auto_mode: unknown key {key!r}; known: {AUTO_MODE_KEYS}")
        if isinstance(lines, str) or not all(isinstance(x, str) for x in lines):
            raise ValueError(f"auto_mode.{key} must be a list of strings")
        out[key] = tuple(x.strip() for x in lines if x.strip())
    return out


def expand_disallowed(names: Iterable[str]) -> frozenset[str]:
    """``names`` plus, when ``Bash`` is among them, its companions."""
    out = set(names)
    if "Bash" in out:
        out |= SHELL_COMPANION_TOOLS
    return frozenset(out)


def bypasses_classifier(rule: Rule) -> bool:
    """An allow rule auto mode ignores: one that lets any shell command, any
    interpreter or wrapper, or any sub-agent through without review."""
    if rule.tool in ("Bash", "Monitor"):
        if rule.content is None:
            return True
        words = rule.content.replace(":*", " *").split()
        first = words[0] if words else ""
        return first == "*" or first in INTERPRETERS or first in WRAPPER_PROGRAMS
    return rule.tool == "Agent" and rule.content is None


# ---------------------------------------------------------------- web hosts

_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$")


def normalize_allowed_hosts(entries: Iterable[object]) -> tuple[str, ...]:
    """Validate ``example.com`` / ``*.example.com`` entries; anything else
    raises (a security knob must not fail open on a typo)."""
    out: list[str] = []
    for raw in entries:
        if not isinstance(raw, str):
            raise ValueError(
                "webfetch_allowed_hosts entries must be strings like "
                f"'example.com' or '*.example.com'; got {raw!r}"
            )
        entry = raw.strip().lower().rstrip(".")
        if not entry:
            raise ValueError(
                "webfetch_allowed_hosts entry is empty; write a host like "
                "'example.com' or '*.example.com'"
            )
        for bad, why in (
            ("://", "a scheme"),
            ("/", "a path"),
            ("@", "userinfo"),
            ("?", "a query"),
            (" ", "whitespace"),
            (":", "a colon — the list names hostnames, never ports"),
        ):
            if bad in entry:
                raise ValueError(
                    f"webfetch_allowed_hosts entry {raw!r} carries {why}; "
                    "write the host alone, e.g. 'example.com' or '*.example.com'"
                )
        wildcard = entry.startswith("*.")
        name = idna(entry[2:] if wildcard else entry)
        if not name or "*" in name:
            raise ValueError(
                f"webfetch_allowed_hosts entry {raw!r} is not a host; the only "
                "wildcard form is a leading '*.' (e.g. '*.example.com')"
            )
        if any(not _LABEL_RE.match(label) for label in name.split(".")):
            raise ValueError(f"webfetch_allowed_hosts entry {raw!r} is not a valid hostname")
        out.append(f"*.{name}" if wildcard else name)
    return tuple(out)


def host_in_allowlist(host: str | None, allowed: Sequence[str]) -> bool:
    """Whole-host matching: ``*.a.com`` covers ``www.a.com`` but not
    ``a.com``; ``None`` matches nothing."""
    if not host:
        return False
    for entry in allowed:
        if entry.startswith("*."):
            suffix = entry[1:]
            if host.endswith(suffix) and len(host) > len(suffix):
                return True
        elif host == entry:
            return True
    return False


def webfetch_needs_approval(input: Mapping[str, Any], allowed: Sequence[str]) -> bool:
    url = input.get("url")
    if not isinstance(url, str) or not url.strip():
        return True
    return not host_in_allowlist(url_host(url), allowed)


# ---------------------------------------------------------------- local settings


def add_allow_rules(raw: bytes | str | None, rules: Sequence[str]) -> str | None:
    """The local settings file's new text with ``rules`` added to
    ``permissions.allow`` (other keys kept), or ``None`` when nothing
    changes or the file is not a JSON object (left alone)."""
    data: Any = {}
    if raw:
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return None
    if not isinstance(data, dict):
        return None
    permissions = data.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        return None
    allow = permissions.setdefault("allow", [])
    if not isinstance(allow, list):
        return None
    new = [x for x in rules if x not in allow]
    if not new:
        return None
    allow.extend(new)
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"
