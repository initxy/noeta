"""``HookGuard`` — user PreToolUse rules as a deterministic Guard.

A verdict is a pure function of ``(rules, ProposedToolCall, GuardContext)``
with no I/O, clock, or shell, so a guard rebuilt from the same rules
re-derives the same guard-origin events across hosts. Priority 100 sits behind
``BudgetGuard`` (10) and ``PermissionGuard`` (20) and ``HookManager.check``
stops at the first non-allow, so a user rule can only *tighten* a call the
built-ins already allowed — it can neither loosen a built-in denial nor
rewrite a built-in approval.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Any

from noeta.protocols.hooks import (
    GuardContext,
    ProposedAction,
    ProposedToolCall,
    VerdictResult,
)
from noeta.runtime.governance import MATCH_STRING_CAP, MatchArg, PreToolUseRule


__all__ = ["HookGuard"]


def _resolve_path(args: dict[str, Any], path: tuple[str, ...]) -> tuple[bool, Any]:
    """Walk dotted argument keys; a missing key or non-dict intermediate is no match."""
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
            if rule.action == "allow":
                return VerdictResult.allow()
            reason = rule.reason or f"hook rule on {rule.match_tool!r}"
            if rule.action == "deny":
                return VerdictResult.deny(reason)
            return VerdictResult.require_approval(reason)
        return VerdictResult.allow()
