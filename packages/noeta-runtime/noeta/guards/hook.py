"""``HookGuard`` — Phase 4.5 F3 user PreToolUse hooks as a deterministic Guard.

A user-configured PreToolUse hook can only ``allow`` / ``deny`` /
``require_approval`` a tool call (no Mutator). It is a
**declarative, deterministic** Guard: a verdict is a pure function of
``(rules, ProposedToolCall, GuardContext)`` with no I/O, clock, or shell,
so a resume rebuilds the same guard from the same ``--hooks-file`` and
re-derives the same guard-origin events (``ToolCallDenied`` /
``TaskSuspended(approval-…)``) across hosts.

Registered AFTER ``BudgetGuard`` (priority 10) + ``PermissionGuard``
(priority 20): ``HookManager.check`` returns the first non-allow in
priority order, so any built-in non-allow (deny **or** require_approval)
wins first and this guard is never consulted for that call. A user rule
can therefore only **tighten** a call the built-ins already allowed — it
can neither loosen a built-in denial nor rewrite a built-in approval.

Rules arrive as plain data (parsed in L3), so this module imports only
``noeta.protocols`` (the ``guards-only-protocols`` contract holds).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, Literal, Optional

from noeta.protocols.hooks import (
    GuardContext,
    ProposedAction,
    ProposedToolCall,
    VerdictResult,
)


__all__ = ["HookGuard", "MatchArg", "PreToolUseRule", "MATCH_STRING_CAP"]


#: Cap on the length of a string before ``contains`` / ``regex`` is
#: evaluated — bounds pathological-regex / huge-argument cost.
MATCH_STRING_CAP = 64 * 1024

HookAction = Literal["allow", "deny", "require_approval"]


@dataclass(frozen=True, slots=True)
class MatchArg:
    """A deterministic predicate on one tool argument.

    ``path`` is a tuple of **object keys** (dotted in the config, e.g.
    ``("opts", "force")``) — no array indexing. Exactly one operator is
    set. ``equals`` is structural/scalar JSON equality; ``contains`` /
    ``regex`` apply only when the resolved value is a ``str`` (else: no
    match). ``pattern`` is the regex compiled at parse time (fail-fast)."""

    path: tuple[str, ...]
    op: Literal["equals", "contains", "regex"]
    value: Any = None
    pattern: Optional[re.Pattern[str]] = None


@dataclass(frozen=True, slots=True)
class PreToolUseRule:
    match_tool: str
    action: HookAction
    match_arg: Optional[MatchArg] = None
    reason: Optional[str] = None


def _resolve_path(args: dict[str, Any], path: tuple[str, ...]) -> tuple[bool, Any]:
    """Walk dotted object keys; return ``(found, value)``. A missing key
    or a non-dict intermediate ⇒ ``(False, None)`` (no match)."""
    cur: Any = args
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return False, None
        cur = cur[key]
    return True, cur


def _arg_matches(ma: MatchArg, args: dict[str, Any]) -> bool:
    found, value = _resolve_path(args, ma.path)
    if not found:
        return False
    if ma.op == "equals":
        return bool(value == ma.value)
    # contains / regex are string-only.
    if not isinstance(value, str):
        return False
    capped = value[:MATCH_STRING_CAP]
    if ma.op == "contains":
        return isinstance(ma.value, str) and ma.value in capped
    # regex
    return ma.pattern is not None and ma.pattern.search(capped) is not None


class HookGuard:
    """User PreToolUse rules as a deterministic Guard (first-match)."""

    name = "hook"
    priority = 100  # after BudgetGuard (10) + PermissionGuard (20)

    def __init__(self, rules: tuple[PreToolUseRule, ...]) -> None:
        self._rules = rules

    def check(self, action: ProposedAction, ctx: GuardContext) -> VerdictResult:
        # v1 scopes PreToolUse to tool calls; spawn/finish are allowed.
        if not isinstance(action, ProposedToolCall):
            return VerdictResult.allow()
        call = action.call
        for rule in self._rules:
            if not fnmatchcase(call.tool_name, rule.match_tool):
                continue
            if rule.match_arg is not None and not _arg_matches(
                rule.match_arg, dict(call.arguments)
            ):
                continue
            # first match wins
            if rule.action == "allow":
                return VerdictResult.allow()
            reason = rule.reason or f"hook rule on {rule.match_tool!r}"
            if rule.action == "deny":
                return VerdictResult.deny(reason)
            return VerdictResult.require_approval(reason)
        return VerdictResult.allow()
