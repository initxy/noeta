"""Claude Code's ``hooks`` settings object, parsed: events, matcher groups
and the hooks in them.

A matcher is matched against the event's subject (``SUBJECT``): a plain
list of names (``Edit|Write``, ``Edit, Write``) is compared name by name,
anything else is a regular expression searched in the subject; empty or
``*`` matches everything, and so does any matcher on an event without a
subject. A hook's ``if`` is a permission rule (``Bash(git *)``) the tool
call must match; on an event without a tool call such a hook never runs.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from noeta.toolkit.permission_rules import Rule, parse_rule

log = logging.getLogger("noeta.hooks")

#: The events noeta fires, with Claude Code's names.
EVENTS = (
    "PreToolUse", "PostToolUse", "PostToolUseFailure", "PostToolBatch", "UserPromptSubmit",
    "Stop", "StopFailure", "SubagentStart", "SubagentStop", "SessionStart", "SessionEnd",
    "PreCompact", "PostCompact", "PostModelSwitch", "PermissionRequest", "PermissionDenied",
    "Notification", "CwdChanged", "InstructionsLoaded",
)
#: Claude Code's other events: configured ones are skipped with a warning.
UNSUPPORTED_EVENTS = (
    "UserPromptExpansion", "PreModelSwitch", "Setup", "TeammateIdle", "TaskCreated",
    "TaskCompleted", "Elicitation", "ElicitationResult", "ConfigChange", "WorktreeCreate",
    "WorktreeRemove", "FileChanged", "DirectoryAdded", "MessageDisplay",
)
#: Events about one tool call (where ``if`` applies).
TOOL_EVENTS = frozenset({"PreToolUse", "PostToolUse", "PostToolUseFailure",
                         "PermissionRequest", "PermissionDenied"})
#: The input field a matcher is tested against, per event (none: every
#: matcher matches).
SUBJECT = {
    **{e: "tool_name" for e in TOOL_EVENTS},
    "SessionStart": "source", "SessionEnd": "reason", "PreCompact": "trigger",
    "PostCompact": "trigger", "Notification": "notification_type",
    "SubagentStart": "agent_type", "SubagentStop": "agent_type", "StopFailure": "error",
    "PostModelSwitch": "to_model", "InstructionsLoaded": "load_reason",
}
#: Old tool names a matcher may use, both ways.
_ALIASES = {"Task": "Agent", "Agent": "Task"}

#: Default timeouts in seconds: command, HTTP and MCP tool hooks 10 minutes,
#: prompt hooks 30 seconds, agent hooks a minute (Claude Code's).
COMMAND_TIMEOUT_S = 600.0
PROMPT_TIMEOUT_S = 30.0
AGENT_TIMEOUT_S = 60.0

HookType = Literal["command", "http", "prompt", "agent", "mcp_tool"]
HOOK_TYPES = ("command", "http", "prompt", "agent", "mcp_tool")


@dataclass(frozen=True)
class HookCommand:
    """One hook. ``type`` says which fields apply: ``command`` (with
    ``args``: run directly, no shell), ``url`` / ``headers`` /
    ``allowed_env_vars`` (``http``), ``prompt`` / ``model`` /
    ``continue_on_block`` (``prompt``; ``agent`` without the last),
    ``server`` / ``tool`` / ``arguments`` (``mcp_tool``)."""

    command: str = ""
    timeout: float = COMMAND_TIMEOUT_S
    type: HookType = "command"
    args: tuple[str, ...] | None = None
    url: str = ""
    headers: tuple[tuple[str, str], ...] = ()
    allowed_env_vars: tuple[str, ...] = ()
    prompt: str = ""
    model: str = ""
    continue_on_block: bool = False
    #: The permission rule a tool call must match (``if``).
    condition: Rule | None = None
    once: bool = False
    background: bool = False
    #: A failure (could not start, timed out, exit code other than 0 / 2)
    #: blocks like exit 2 (``onFailure: block``).
    block_on_failure: bool = False
    server: str = ""
    tool: str = ""
    #: An ``mcp_tool`` hook's arguments (``${path}`` filled from the input).
    arguments: Mapping[str, Any] = field(default_factory=dict)
    #: More environment for a ``command`` hook: a Claude Code plugin's
    #: option values (``CLAUDE_PLUGIN_OPTION_<KEY>``), set by noeta.
    env: tuple[tuple[str, str], ...] = ()

    @property
    def key(self) -> str:
        """Identical hooks (same key) run once per event."""
        args = "\x00".join(self.args or ())
        if self.type == "mcp_tool":
            args = json.dumps(dict(self.arguments), sort_keys=True)
        cond = self.condition.text if self.condition else ""
        return f"{self.type}\x00{self.label}\x00{args}\x00{cond}"

    @property
    def label(self) -> str:
        if self.type == "mcp_tool":
            return f"{self.server}/{self.tool}"
        return {"command": self.command, "http": self.url, "prompt": self.prompt,
                "agent": self.prompt}[self.type]


@dataclass(frozen=True)
class Matcher:
    pattern: str
    hooks: tuple[HookCommand, ...]
    #: Tool events accept ``,`` and spaces in a plain list too.
    lenient: bool = field(default=True, compare=False)

    def matches(self, subject: str | None) -> bool:
        if subject is None or self.pattern in ("", "*"):
            return True
        plain = r"[A-Za-z0-9_|, -]+" if self.lenient else r"[A-Za-z0-9_|]+"
        if re.fullmatch(plain, self.pattern):
            names = {n.strip() for n in re.split(r"[|,]" if self.lenient else r"\|",
                                                 self.pattern) if n.strip()}
            return subject in names or _ALIASES.get(subject) in names
        try:
            regex = re.compile(self.pattern)
        except re.error:
            log.warning("hooks: invalid matcher %r never matches", self.pattern)
            return False
        alias = _ALIASES.get(subject)
        return regex.search(subject) is not None or (
            alias is not None and regex.search(alias) is not None)


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{where}: 'timeout' must be a positive number of seconds")
    return float(value)


def _strings(value: Any, where: str, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{where}: '{name}' must be a list of strings")
    return tuple(value)


def _hook(event: str, h: Mapping[str, Any]) -> HookCommand | None:
    where = f"hooks.{event}"
    kind = h.get("type", "command")
    if kind not in HOOK_TYPES:
        log.warning("%s: %r hooks do not run in noeta; skipped", where, kind)
        return None
    if kind == "agent" and event == "PermissionRequest":
        log.warning("%s: an agent hook answers ok or not, never the allow / deny a "
                    "permission request needs; skipped", where)
        return None
    condition = None
    if h.get("if") is not None:
        if not isinstance(h["if"], str):
            raise ValueError(f"{where}: 'if' must be a permission rule string")
        condition = parse_rule(h["if"])
    common: dict[str, Any] = {
        "type": kind, "condition": condition, "once": bool(h.get("once")),
        "timeout": _number(h["timeout"], where) if h.get("timeout") is not None
        else {"prompt": PROMPT_TIMEOUT_S, "agent": AGENT_TIMEOUT_S}.get(kind, COMMAND_TIMEOUT_S),
        "block_on_failure": h.get("onFailure") == "block",
    }
    if kind == "command":
        command = h.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError(f"{where}: a command hook needs 'command'")
        args = _strings(h["args"], where, "args") if h.get("args") is not None else None
        env = h.get("env") or {}
        if not isinstance(env, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise ValueError(f"{where}: 'env' must map names to strings")
        return HookCommand(command=command, args=args, env=tuple(env.items()),
                           background=bool(h.get("async") or h.get("asyncRewake")), **common)
    if kind == "http":
        url = h.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError(f"{where}: an http hook needs an http(s) 'url'")
        headers = h.get("headers") or {}
        if not isinstance(headers, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
            raise ValueError(f"{where}: 'headers' must map names to strings")
        allowed = _strings(h["allowedEnvVars"], where, "allowedEnvVars") \
            if h.get("allowedEnvVars") is not None else ()
        return HookCommand(url=url, headers=tuple(headers.items()), allowed_env_vars=allowed,
                           **common)
    if kind == "mcp_tool":
        server, tool = h.get("server"), h.get("tool")
        if not isinstance(server, str) or not server or not isinstance(tool, str) or not tool:
            raise ValueError(f"{where}: an mcp_tool hook needs 'server' and 'tool'")
        arguments = h.get("input") or {}
        if not isinstance(arguments, dict):
            raise ValueError(f"{where}: an mcp_tool hook's 'input' must be an object")
        return HookCommand(server=server, tool=tool, arguments=dict(arguments), **common)
    prompt = h.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError(f"{where}: a {kind} hook needs 'prompt'")
    model = h.get("model")
    return HookCommand(prompt=prompt, model=model if isinstance(model, str) else "",
                       continue_on_block=kind == "prompt" and bool(h.get("continueOnBlock")),
                       **common)


def parse_hooks(config: Mapping[str, Any]) -> dict[str, list[Matcher]]:
    """Claude Code's ``hooks`` object -> matchers per event. Events noeta
    does not fire and hook types it does not run are skipped with a
    warning; a malformed entry raises ``ValueError``."""
    out: dict[str, list[Matcher]] = {}
    for event, groups in config.items():
        if event not in EVENTS:
            if event in UNSUPPORTED_EVENTS:
                log.warning("hooks: noeta does not fire %s; its hooks are skipped", event)
            else:
                log.warning("hooks: unknown event %r skipped", event)
            continue
        if not isinstance(groups, list):
            raise ValueError(f"hooks.{event} must be a list")
        for group in groups:
            if not isinstance(group, dict):
                raise ValueError(f"hooks.{event}: each entry is an object with 'hooks'")
            hooks: list[HookCommand] = []
            for h in group.get("hooks") or []:
                if not isinstance(h, dict):
                    raise ValueError(f"hooks.{event}: each hook is an object")
                hook = _hook(event, h)
                if hook is not None:
                    hooks.append(hook)
            if hooks:
                pattern = group.get("matcher") or ""
                out.setdefault(event, []).append(Matcher(
                    str(pattern), tuple(hooks), lenient=event in SUBJECT))
    return out


def read_settings(path: str | Path) -> Mapping[str, Any]:
    """The ``hooks`` object of a settings file (empty when the file is
    missing or unreadable)."""
    p = Path(path).expanduser()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("hooks: cannot read %s: %s", p, exc)
        return {}
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return {}
    # ``env`` is noeta's, for a Claude Code plugin's options; not Claude
    # Code's, so a settings file does not set it.
    return {event: [{**g, "hooks": [{k: v for k, v in h.items() if k != "env"}
                                    if isinstance(h, dict) else h for h in g["hooks"]]}
                    if isinstance(g, dict) and isinstance(g.get("hooks"), list) else g
                    for g in groups] if isinstance(groups, list) else groups
            for event, groups in hooks.items()}
