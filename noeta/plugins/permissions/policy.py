"""Pure permission policy: modes, the per-tool risk table, the shell and
WebFetch gates, and the project's remembered shell rules file.

Nothing here touches a task. :class:`~noeta.plugins.permissions.Permissions`
combines these into one gate; hosts and tests can call them directly.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from noeta.toolkit.shell import (
    DEFAULT_SHELL_RULES,
    AllowRule,
    build_allowlist,
    command_in_allowlist,
    rule_from_spec,
    rule_spec_from_command,
    rule_spec_text,
)

PermissionMode = Literal["default", "acceptEdits", "plan", "auto", "dontAsk",
                         "bypassPermissions"]

#: Claude Code's permission modes: ``default`` asks before anything that
#: needs permission, ``acceptEdits`` also lets file edits in the working
#: directories run, ``plan`` explores without changing anything, ``auto``
#: lets a classifier model approve or block what would ask, ``dontAsk``
#: refuses what would ask, ``bypassPermissions`` asks for nothing but deny
#: and ask rules.
PERMISSION_MODES: tuple[str, ...] = ("default", "acceptEdits", "plan", "auto", "dontAsk",
                                     "bypassPermissions")

RiskLevel = Literal["low", "medium", "high"]

#: The ordered risk scale; the ``max_risk`` ceiling indexes into it.
KNOWN_RISK_LEVELS: tuple[str, ...] = ("low", "medium", "high")

#: 0.6's declared ``risk_level`` of every tool, by name. 1.0 tools carry no
#: risk field, so the table is the source; a host extends or overrides it
#: with ``Permissions(risk=...)``. Control tools (Agent,
#: AskUserQuestion, skill, ...) were never gated in 0.6 and are ``low``.
DEFAULT_RISK: Mapping[str, str] = {
    "Read": "low",
    "Edit": "high",
    "Write": "high",
    "NotebookEdit": "high",
    "Bash": "high",
    "Monitor": "high",
    "TaskStop": "high",
    "WebFetch": "low",
    "WebSearch": "low",
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
    "Skill": "low",
    "RecallHistory": "low",
    "structured_output": "low",
}

#: The edit tools ``acceptEdits`` exempts from the risk gate.
EDIT_TOOL_NAMES: frozenset[str] = frozenset({"Edit", "Write", "NotebookEdit"})

#: Tools whose ``command`` the shell allowlist gates (Monitor runs its script
#: through the same gate).
SHELL_GATED_TOOLS: frozenset[str] = frozenset({"Bash", "Monitor"})

#: Tools that run or manage shell commands alongside ``Bash``; disallowing
#: ``Bash`` removes them too.
SHELL_COMPANION_TOOLS: frozenset[str] = frozenset({"Monitor", "TaskStop"})

WEBFETCH_TOOL = "WebFetch"

# ---------------------------------------------------------------- texts (0.6)

DENIED_BY_POLICY = "tool {name!r} denied by policy"
NOT_IN_ALLOWLIST = "tool {name!r} not in allowlist"
NO_METADATA = (
    "tool {name!r} has no metadata registered with PermissionGuard; fail-closed"
)
UNKNOWN_RISK = (
    "tool {name!r} has unknown risk_level {risk!r}; fail-closed (known levels: {known})"
)
RISK_EXCEEDS = "tool {name!r} risk_level {risk!r} exceeds max {ceiling!r}"
REQUIRES_APPROVAL = "tool {name!r} requires human approval"
DENIED_BY_RULE = "Permission to use {name} has been denied by the rule {rule}."
ASK_RULE = "The rule {rule} asks before {name} runs."
CALL_REQUIRES_APPROVAL = "tool {name!r} call requires human approval"


def check_mode(mode: str) -> PermissionMode:
    if mode not in PERMISSION_MODES:
        raise ValueError(
            f"Invalid permission_mode {mode!r}. Must be one of {PERMISSION_MODES}."
        )
    return mode  # type: ignore[return-value]


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


def mode_requires_approval(mode: str, name: str, risk: str) -> bool:
    """The static gate: ``default`` asks for every tool whose risk is not
    ``low``; ``acceptEdits`` the same minus Edit/Write; bypass never."""
    if mode == "bypassPermissions":
        return False
    if risk == "low":
        return False
    return not (mode == "acceptEdits" and name in EDIT_TOOL_NAMES)


def expand_disallowed(names: Iterable[str]) -> frozenset[str]:
    """``names`` plus, when ``Bash`` is among them, its companions."""
    out = set(names)
    if "Bash" in out:
        out |= SHELL_COMPANION_TOOLS
    return frozenset(out)


# ---------------------------------------------------------------- shell gate


def effective_shell_rules(
    host_specs: Sequence[Mapping[str, Any]] = (),
    project_specs: Sequence[Mapping[str, Any]] = (),
    remembered: Sequence[Mapping[str, Any]] = (),
    *,
    trusted: bool,
    base_rules: Sequence[AllowRule] = DEFAULT_SHELL_RULES,
) -> tuple[AllowRule, ...]:
    """The curated rules (those that run workspace code only when the
    workspace is trusted), the host's specs, the project file's specs (only
    when trusted) and the rules this task remembered from "always" answers."""
    base = tuple(r for r in base_rules if trusted or not r.runs_workspace_code)
    specs = [*host_specs, *(project_specs if trusted else ()), *remembered]
    return build_allowlist(specs, base_rules=base)


def shell_call_needs_approval(input: Mapping[str, Any], rules: Sequence[AllowRule]) -> bool:
    """A command in the allowlist runs silently; anything else, including an
    empty or malformed command, asks."""
    command = input.get("command")
    if not isinstance(command, str) or not command.strip():
        return True
    return not command_in_allowlist(command, rules)


PROJECT_RULES_FILE = ".noeta/shell-allowlist.json"


def parse_project_rules(raw: bytes | str | None) -> tuple[dict[str, Any], ...]:
    """The project file's specs; empty when absent or malformed."""
    if raw is None:
        return ()
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return ()
    if not isinstance(data, list):
        return ()
    return tuple(dict(item) for item in data if isinstance(item, Mapping) and _valid_spec(item))


def _valid_spec(spec: Mapping[str, Any]) -> bool:
    try:
        rule_from_spec(spec)
    except ValueError:
        return False
    return True


def append_project_rule(existing: Sequence[Mapping[str, Any]],
                        spec: Mapping[str, Any]) -> str | None:
    """The file's new text with ``spec`` appended, or ``None`` when it is
    already there."""
    if any(dict(e) == dict(spec) for e in existing):
        return None
    return json.dumps([*map(dict, existing), dict(spec)], ensure_ascii=False, indent=2) + "\n"


# ---------------------------------------------------------------- web gate

_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$")


def _idna(host: str) -> str:
    try:
        return host.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return host.lower()


def url_host(url: str) -> str | None:
    """The URL's real host, normalised (userinfo dropped, escapes decoded,
    trailing dot dropped, IDNA); ``None`` when it names none."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = unquote(host).strip().rstrip(".").lower()
    return _idna(host) if host else None


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
        name = _idna(entry[2:] if wildcard else entry)
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


# ---------------------------------------------------------------- "always"


def always_rule(name: str, input: Mapping[str, Any]) -> dict[str, Any] | None:
    """What an "always" answer to this call remembers: a command rule
    (:func:`~noeta.toolkit.shell.rule_spec_from_command`) for a shell
    command, the host for a fetch, the tool otherwise; ``None`` when nothing
    safe can be remembered (a command with shell metacharacters, a URL
    without a host)."""
    if name in SHELL_GATED_TOOLS:
        command = input.get("command")
        spec = rule_spec_from_command(command) if isinstance(command, str) else None
        return {"kind": "shell", "spec": spec} if spec else None
    if name == WEBFETCH_TOOL:
        url = input.get("url")
        host = url_host(url) if isinstance(url, str) else None
        return {"kind": "host", "host": host} if host else None
    return {"kind": "tool", "tool": name}


def always_label(name: str, rule: Mapping[str, Any] | None) -> str | None:
    """The rule an "always" answer remembers, as the person approving reads
    it (Claude Code's rule form): ``Bash(git push *)``,
    ``WebFetch(domain:example.com)``, ``Write``."""
    if rule is None:
        return None
    kind = rule.get("kind")
    if kind == "shell" and isinstance(rule.get("spec"), Mapping):
        return f"{name}({rule_spec_text(rule['spec'])})"
    if kind == "host":
        return f"{name}(domain:{rule.get('host')})"
    return name
