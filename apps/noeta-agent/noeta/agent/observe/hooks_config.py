"""Parse + validate a `--hooks-file` (Phase 4.5 F3).

Lives in L3 (`noeta.agent`): reads a hooks JSON config and produces **two
independent plain-data rule lists** — PreToolUse rules (for the
`noeta.guards.HookGuard`) and PostToolUse / Notification rules (for the
`noeta.observers.HookObserver`). This is the only place that imports both
`noeta.guards.hook` and `noeta.observers.hook`; the guard and observer
packages never import each other.

Validation is **strict / fail-closed**: unknown top-level keys, unknown
keys inside any rule / `match_arg` / `notify`, unknown `action` /
notification `on`, a bad `regex`, or a malformed `command` all raise
:class:`HooksConfigError` — never silently ignored (F3 watchpoint #2).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from noeta.guards.hook import MatchArg, PreToolUseRule
from noeta.observers.hook import NotificationRule, PostToolUseRule


__all__ = ["HooksConfig", "HooksConfigError", "parse_hooks_file", "parse_hooks_obj"]


#: Caps for a notify command (fail-fast above these).
_MAX_ARGV = 32
_MAX_ARG_LEN = 4096
_VALID_ACTIONS = frozenset({"allow", "deny", "require_approval"})
_VALID_NOTIFY_ON = frozenset({"approval"})
_VALID_TOP_KEYS = frozenset({"pre_tool_use", "post_tool_use", "notification"})
_VALID_PRE_KEYS = frozenset({"match_tool", "match_arg", "action", "reason"})
_VALID_MATCH_ARG_KEYS = frozenset({"path", "equals", "contains", "regex"})
_VALID_POST_KEYS = frozenset({"match_tool", "notify"})
_VALID_NOTIF_KEYS = frozenset({"on", "notify"})
_VALID_NOTIFY_KEYS = frozenset({"command", "log"})


class HooksConfigError(ValueError):
    """A malformed `--hooks-file` (fail-fast at parse time)."""


@dataclass(frozen=True, slots=True)
class HooksConfig:
    pre_tool_use: tuple[PreToolUseRule, ...]
    post_tool_use: tuple[PostToolUseRule, ...]
    notification: tuple[NotificationRule, ...]


def parse_hooks_file(path: Path) -> HooksConfig:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HooksConfigError(f"cannot read hooks file {path}: {exc}") from exc
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HooksConfigError(f"hooks file {path} is not valid JSON: {exc}") from exc
    return parse_hooks_obj(obj)


def parse_hooks_obj(obj: Any) -> HooksConfig:
    if not isinstance(obj, dict):
        raise HooksConfigError("hooks config must be a JSON object")
    _reject_unknown(obj.keys(), _VALID_TOP_KEYS, "top-level")
    return HooksConfig(
        pre_tool_use=tuple(
            _parse_pre_rule(r) for r in _as_list(obj.get("pre_tool_use"), "pre_tool_use")
        ),
        post_tool_use=tuple(
            _parse_post_rule(r)
            for r in _as_list(obj.get("post_tool_use"), "post_tool_use")
        ),
        notification=tuple(
            _parse_notif_rule(r)
            for r in _as_list(obj.get("notification"), "notification")
        ),
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _as_list(value: Any, where: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise HooksConfigError(f"{where} must be a list")
    return value


def _reject_unknown(keys: Any, allowed: frozenset[str], where: str) -> None:
    extra = set(keys) - allowed
    if extra:
        raise HooksConfigError(
            f"unknown key(s) {sorted(extra)} in {where} (allowed: {sorted(allowed)})"
        )


def _req_str(d: dict[str, Any], key: str, where: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or v == "":
        raise HooksConfigError(f"{where}: {key!r} must be a non-empty string")
    return v


def _parse_pre_rule(r: Any) -> PreToolUseRule:
    if not isinstance(r, dict):
        raise HooksConfigError("pre_tool_use rule must be an object")
    _reject_unknown(r.keys(), _VALID_PRE_KEYS, "pre_tool_use rule")
    match_tool = _req_str(r, "match_tool", "pre_tool_use rule")
    action = r.get("action")
    if action not in _VALID_ACTIONS:
        raise HooksConfigError(
            f"pre_tool_use action {action!r} invalid (allowed: {sorted(_VALID_ACTIONS)})"
        )
    reason = r.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise HooksConfigError("pre_tool_use reason must be a string")
    match_arg = _parse_match_arg(r.get("match_arg")) if "match_arg" in r else None
    return PreToolUseRule(
        match_tool=match_tool, action=action, match_arg=match_arg, reason=reason
    )


def _parse_match_arg(m: Any) -> MatchArg:
    if not isinstance(m, dict):
        raise HooksConfigError("match_arg must be an object")
    _reject_unknown(m.keys(), _VALID_MATCH_ARG_KEYS, "match_arg")
    path_raw = m.get("path")
    if not isinstance(path_raw, str) or path_raw == "":
        raise HooksConfigError("match_arg.path must be a non-empty dotted string")
    path = tuple(path_raw.split("."))
    if any(seg == "" for seg in path):
        raise HooksConfigError(f"match_arg.path {path_raw!r} has an empty segment")
    ops = [op for op in ("equals", "contains", "regex") if op in m]
    if len(ops) != 1:
        raise HooksConfigError(
            "match_arg must have exactly one of equals/contains/regex"
        )
    op = ops[0]
    if op == "equals":
        return MatchArg(path=path, op="equals", value=m["equals"])
    val = m[op]
    if not isinstance(val, str):
        raise HooksConfigError(f"match_arg.{op} must be a string")
    if op == "contains":
        return MatchArg(path=path, op="contains", value=val)
    try:
        pattern = re.compile(val)
    except re.error as exc:
        raise HooksConfigError(f"match_arg.regex {val!r} failed to compile: {exc}") from exc
    return MatchArg(path=path, op="regex", pattern=pattern)


def _parse_post_rule(r: Any) -> PostToolUseRule:
    if not isinstance(r, dict):
        raise HooksConfigError("post_tool_use rule must be an object")
    _reject_unknown(r.keys(), _VALID_POST_KEYS, "post_tool_use rule")
    match_tool = _req_str(r, "match_tool", "post_tool_use rule")
    command, log = _parse_notify(r.get("notify"), "post_tool_use")
    return PostToolUseRule(match_tool=match_tool, command=command, log=log)


def _parse_notif_rule(r: Any) -> NotificationRule:
    if not isinstance(r, dict):
        raise HooksConfigError("notification rule must be an object")
    _reject_unknown(r.keys(), _VALID_NOTIF_KEYS, "notification rule")
    on = r.get("on")
    if on not in _VALID_NOTIFY_ON:
        raise HooksConfigError(
            f"notification on {on!r} invalid (allowed: {sorted(_VALID_NOTIFY_ON)})"
        )
    command, log = _parse_notify(r.get("notify"), "notification")
    return NotificationRule(on=on, command=command, log=log)


def _parse_notify(
    notify: Any, where: str
) -> tuple[tuple[str, ...] | None, bool]:
    if notify is None:
        raise HooksConfigError(f"{where} rule must have a 'notify'")
    if not isinstance(notify, dict):
        raise HooksConfigError(f"{where} notify must be an object")
    _reject_unknown(notify.keys(), _VALID_NOTIFY_KEYS, f"{where} notify")
    log = notify.get("log", False)
    if not isinstance(log, bool):
        raise HooksConfigError(f"{where} notify.log must be a boolean")
    command: tuple[str, ...] | None = None
    if "command" in notify:
        cmd = notify["command"]
        if not isinstance(cmd, list) or not cmd:
            raise HooksConfigError(f"{where} notify.command must be a non-empty list")
        if len(cmd) > _MAX_ARGV:
            raise HooksConfigError(f"{where} notify.command exceeds {_MAX_ARGV} args")
        for part in cmd:
            if not isinstance(part, str) or part == "":
                raise HooksConfigError(
                    f"{where} notify.command items must be non-empty strings"
                )
            if len(part) > _MAX_ARG_LEN:
                raise HooksConfigError(
                    f"{where} notify.command arg exceeds {_MAX_ARG_LEN} chars"
                )
        command = tuple(cmd)
    if command is None and not log:
        raise HooksConfigError(f"{where} notify must set 'command' and/or 'log'")
    return command, log
