"""Phase 4.5 F3 — `HookGuard` (deterministic PreToolUse) + precedence.

Unit coverage of the declarative verdict (first-match deny/approval/
allow), the `match_arg` predicates (equals/contains/regex + missing-path
no-match + string-only), and the architect-pinned precedence: a built-in
non-allow (deny OR require_approval) always wins over a user hook.
"""

from __future__ import annotations

import re
from typing import Any

from noeta.core.hooks import HookManager
from noeta.guards.hook import HookGuard, MatchArg, PreToolUseRule
from noeta.protocols.decisions import ToolCall
from noeta.protocols.hooks import (
    GuardContext,
    ProposedFinish,
    ProposedToolCall,
    Verdict,
    VerdictResult,
)


def _verdict(rules: tuple[PreToolUseRule, ...], tool: str, args: dict[str, Any]) -> Verdict:
    g = HookGuard(rules)
    action = ProposedToolCall(call=ToolCall(tool_name=tool, arguments=args, call_id="c"))
    return g.check(action, GuardContext(task_id="t")).verdict


def test_no_rule_allows() -> None:
    assert _verdict((), "write", {}) is Verdict.ALLOW


def test_deny_and_require_approval_first_match() -> None:
    rules = (
        PreToolUseRule(match_tool="write", action="require_approval"),
        PreToolUseRule(match_tool="*", action="deny"),
    )
    assert _verdict(rules, "write", {}) is Verdict.REQUIRE_APPROVAL  # first match
    assert _verdict(rules, "shell_run", {}) is Verdict.DENY


def test_glob_match() -> None:
    rules = (PreToolUseRule(match_tool="mcp__*", action="require_approval"),)
    assert _verdict(rules, "mcp__git__commit", {}) is Verdict.REQUIRE_APPROVAL
    assert _verdict(rules, "read_file", {}) is Verdict.ALLOW


def test_user_allow_short_circuits_later_user_rules() -> None:
    rules = (
        PreToolUseRule(match_tool="mcp__git__status", action="allow"),
        PreToolUseRule(match_tool="mcp__*", action="deny"),
    )
    assert _verdict(rules, "mcp__git__status", {}) is Verdict.ALLOW  # exception
    assert _verdict(rules, "mcp__git__commit", {}) is Verdict.DENY


def test_match_arg_contains() -> None:
    rules = (
        PreToolUseRule(
            match_tool="shell_run",
            action="deny",
            match_arg=MatchArg(path=("command",), op="contains", value="rm -rf"),
        ),
    )
    assert _verdict(rules, "shell_run", {"command": "rm -rf /"}) is Verdict.DENY
    assert _verdict(rules, "shell_run", {"command": "ls"}) is Verdict.ALLOW


def test_match_arg_equals_structural() -> None:
    rules = (
        PreToolUseRule(
            match_tool="t",
            action="deny",
            match_arg=MatchArg(path=("opts", "force"), op="equals", value=True),
        ),
    )
    assert _verdict(rules, "t", {"opts": {"force": True}}) is Verdict.DENY
    assert _verdict(rules, "t", {"opts": {"force": False}}) is Verdict.ALLOW


def test_match_arg_regex_string_only() -> None:
    rules = (
        PreToolUseRule(
            match_tool="t",
            action="deny",
            match_arg=MatchArg(path=("x",), op="regex", pattern=re.compile(r"^secret")),
        ),
    )
    assert _verdict(rules, "t", {"x": "secret-token"}) is Verdict.DENY
    assert _verdict(rules, "t", {"x": "public"}) is Verdict.ALLOW
    assert _verdict(rules, "t", {"x": 123}) is Verdict.ALLOW  # non-str → no match


def test_match_arg_missing_path_no_match() -> None:
    rules = (
        PreToolUseRule(
            match_tool="t",
            action="deny",
            match_arg=MatchArg(path=("a", "b"), op="contains", value="z"),
        ),
    )
    assert _verdict(rules, "t", {"a": {}}) is Verdict.ALLOW
    assert _verdict(rules, "t", {}) is Verdict.ALLOW


def test_non_tool_action_allows() -> None:
    g = HookGuard((PreToolUseRule(match_tool="*", action="deny"),))
    assert g.check(ProposedFinish(answer="x"), GuardContext(task_id="t")).verdict is Verdict.ALLOW


# -- precedence: built-in non-allow always wins (architect watchpoint #3) ----


class _StubGuard:
    """A built-in-like guard returning a fixed verdict at priority 20."""

    name = "stub-builtin"
    priority = 20

    def __init__(self, result: VerdictResult) -> None:
        self._result = result

    def check(self, action: object, ctx: object) -> VerdictResult:
        return self._result


def _manager_verdict(builtin: VerdictResult, hook_rules: tuple[PreToolUseRule, ...]) -> Verdict:
    mgr = HookManager()
    mgr.register(_StubGuard(builtin))  # priority 20
    mgr.register(HookGuard(hook_rules))  # priority 100 (after)
    action = ProposedToolCall(call=ToolCall(tool_name="write", arguments={}, call_id="c"))
    return mgr.check(action, GuardContext(task_id="t")).verdict


def test_builtin_deny_beats_user_allow() -> None:
    # user 'allow' cannot loosen a built-in deny (built-in runs first).
    v = _manager_verdict(
        VerdictResult.deny("built-in denied"),
        (PreToolUseRule(match_tool="write", action="allow"),),
    )
    assert v is Verdict.DENY


def test_builtin_require_approval_beats_user_allow_and_deny() -> None:
    # user 'allow' cannot turn a built-in require_approval into allow...
    v_allow = _manager_verdict(
        VerdictResult.require_approval("built-in approval"),
        (PreToolUseRule(match_tool="write", action="allow"),),
    )
    assert v_allow is Verdict.REQUIRE_APPROVAL
    # ...nor can a user 'deny' rewrite it to deny (built-in runs first).
    v_deny = _manager_verdict(
        VerdictResult.require_approval("built-in approval"),
        (PreToolUseRule(match_tool="write", action="deny"),),
    )
    assert v_deny is Verdict.REQUIRE_APPROVAL


def test_hook_tightens_when_builtin_allows() -> None:
    # built-in allows → the user HookGuard is consulted and can tighten.
    v = _manager_verdict(
        VerdictResult.allow(),
        (PreToolUseRule(match_tool="write", action="deny"),),
    )
    assert v is Verdict.DENY
