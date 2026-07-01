"""Contract tests for ``PermissionGuard`` (issue 18).

Verifies allowlist / denylist / risk_level cap / agent allowlist and
the fail-closed behaviour required by issue 18 B4.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from noeta.guards.permission import PermissionGuard, PermissionPolicy
from noeta.protocols.decisions import SpawnSubtaskDecision, ToolCall
from noeta.protocols.hooks import (
    GuardContext,
    ProposedFinish,
    ProposedSpawnSubtask,
    ProposedToolCall,
    Verdict,
)
from noeta.protocols.task import GovernanceState


@dataclass
class _FakeTool:
    name: str
    risk_level: str
    input_schema: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.input_schema is None:
            self.input_schema = {"type": "object"}

    def invoke(self, arguments, ctx):  # noqa: ANN001
        raise NotImplementedError


def _ctx() -> GuardContext:
    return GuardContext(task_id="t1", governance=GovernanceState())


def _tool(name: str) -> ProposedToolCall:
    return ProposedToolCall(call=ToolCall(tool_name=name, arguments={}, call_id="c"))


def _spawn(agent: str) -> ProposedSpawnSubtask:
    return ProposedSpawnSubtask(
        decision=SpawnSubtaskDecision(agent_name=agent, goal="g", inputs={})
    )


# ---------------------------------------------------------------------------
# allowlist / denylist
# ---------------------------------------------------------------------------


def test_no_policy_constraints_allows_all() -> None:
    guard = PermissionGuard(PermissionPolicy(), tools={})
    for action in (_tool("any"), _spawn("any"), ProposedFinish(answer="x")):
        assert guard.check(action, _ctx()).verdict is Verdict.ALLOW


def test_denied_tool_returns_deny() -> None:
    policy = PermissionPolicy(denied_tools=frozenset({"bad"}))
    guard = PermissionGuard(policy, tools={})
    result = guard.check(_tool("bad"), _ctx())
    assert result.verdict is Verdict.DENY
    assert "denied by policy" in (result.reason or "")


def test_tool_not_in_allowlist_returns_deny() -> None:
    policy = PermissionPolicy(allowed_tools=frozenset({"alpha"}))
    guard = PermissionGuard(policy, tools={})
    assert guard.check(_tool("beta"), _ctx()).verdict is Verdict.DENY


def test_tool_in_allowlist_passes() -> None:
    policy = PermissionPolicy(allowed_tools=frozenset({"alpha"}))
    guard = PermissionGuard(policy, tools={})
    assert guard.check(_tool("alpha"), _ctx()).verdict is Verdict.ALLOW


# ---------------------------------------------------------------------------
# risk_level cap (issue 18 B4 fail-closed)
# ---------------------------------------------------------------------------


def test_risk_level_within_max_is_allowed() -> None:
    policy = PermissionPolicy(max_risk_level="medium")
    tools = {"echo": _FakeTool(name="echo", risk_level="low")}
    guard = PermissionGuard(policy, tools=tools)
    assert guard.check(_tool("echo"), _ctx()).verdict is Verdict.ALLOW


def test_risk_level_exceeds_max_returns_deny() -> None:
    policy = PermissionPolicy(max_risk_level="low")
    tools = {"shell": _FakeTool(name="shell", risk_level="high")}
    guard = PermissionGuard(policy, tools=tools)
    result = guard.check(_tool("shell"), _ctx())
    assert result.verdict is Verdict.DENY
    assert "risk_level" in (result.reason or "")


def test_permission_guard_fails_closed_on_unknown_tool_metadata() -> None:
    """B4: max_risk_level set + tools dict missing the tool → DENY."""
    policy = PermissionPolicy(max_risk_level="low")
    guard = PermissionGuard(policy, tools={})  # no metadata for any tool
    result = guard.check(_tool("mystery"), _ctx())
    assert result.verdict is Verdict.DENY
    assert "no metadata" in (result.reason or "") or "fail-closed" in (
        result.reason or ""
    )


def test_permission_guard_fails_closed_on_unknown_risk_level_string() -> None:
    """B4: tool.risk_level is not in the known ordering → DENY."""
    policy = PermissionPolicy(max_risk_level="low")
    tools = {"weird": _FakeTool(name="weird", risk_level="exotic-tier")}
    guard = PermissionGuard(policy, tools=tools)
    result = guard.check(_tool("weird"), _ctx())
    assert result.verdict is Verdict.DENY
    assert "unknown risk_level" in (result.reason or "")


def test_permission_guard_skips_risk_check_when_max_unset() -> None:
    """No ``max_risk_level`` configured → don't query tools dict at all."""
    policy = PermissionPolicy()  # no max_risk_level
    guard = PermissionGuard(policy, tools={})  # empty intentionally
    assert guard.check(_tool("anything"), _ctx()).verdict is Verdict.ALLOW


# ---------------------------------------------------------------------------
# spawn agent allowlist
# ---------------------------------------------------------------------------


def test_agent_not_in_subtask_allowlist_returns_deny() -> None:
    policy = PermissionPolicy(allowed_subtask_agents=frozenset({"writer"}))
    guard = PermissionGuard(policy, tools={})
    assert guard.check(_spawn("hacker"), _ctx()).verdict is Verdict.DENY


def test_agent_in_subtask_allowlist_allows() -> None:
    policy = PermissionPolicy(allowed_subtask_agents=frozenset({"writer"}))
    guard = PermissionGuard(policy, tools={})
    assert guard.check(_spawn("writer"), _ctx()).verdict is Verdict.ALLOW


def test_no_subtask_allowlist_allows_any_agent() -> None:
    policy = PermissionPolicy()
    guard = PermissionGuard(policy, tools={})
    assert guard.check(_spawn("anything"), _ctx()).verdict is Verdict.ALLOW


# ---------------------------------------------------------------------------
# finish never denied
# ---------------------------------------------------------------------------


def test_finish_action_never_denied_by_permission_guard() -> None:
    # Even with strict policy, ProposedFinish passes.
    policy = PermissionPolicy(
        allowed_tools=frozenset(),
        denied_tools=frozenset({"any"}),
        max_risk_level="low",
        allowed_subtask_agents=frozenset(),
    )
    guard = PermissionGuard(policy, tools={})
    assert guard.check(ProposedFinish(answer="x"), _ctx()).verdict is Verdict.ALLOW


# ---------------------------------------------------------------------------
# conditional_approval — per-call, arguments-aware approval predicate
# ---------------------------------------------------------------------------


def _call(name: str, args: dict[str, Any]) -> ProposedToolCall:
    return ProposedToolCall(call=ToolCall(tool_name=name, arguments=args, call_id="c"))


def test_conditional_approval_gates_only_matching_calls() -> None:
    """The injected predicate gates per call/argument: ``shell_run`` whose
    command is unknown → require_approval; a known one → allow; other tools and
    a None predicate are unaffected (byte-identical to before)."""

    def needs_approval(tool_name: str, args: dict[str, Any]) -> bool:
        return tool_name == "shell_run" and args.get("command") != "git status"

    policy = PermissionPolicy(conditional_approval=needs_approval)
    guard = PermissionGuard(policy, tools={})

    # unknown shell command → approval
    v = guard.check(_call("shell_run", {"command": "rm -rf /"}), _ctx())
    assert v.verdict is Verdict.REQUIRE_APPROVAL
    # allowlisted shell command → allow
    assert (
        guard.check(_call("shell_run", {"command": "git status"}), _ctx()).verdict
        is Verdict.ALLOW
    )
    # non-shell tool → predicate says no → allow
    assert guard.check(_call("read_file", {"path": "x"}), _ctx()).verdict is Verdict.ALLOW
    # no predicate → allow (default path)
    bare = PermissionGuard(PermissionPolicy(), tools={})
    assert bare.check(_call("shell_run", {"command": "rm -rf /"}), _ctx()).verdict is Verdict.ALLOW


def test_static_require_approval_takes_precedence_over_predicate() -> None:
    """A tool in the static ``require_approval_tools`` set is gated regardless of
    the predicate (the predicate only adds gating, never removes it)."""

    policy = PermissionPolicy(
        require_approval_tools=frozenset({"shell_run"}),
        conditional_approval=lambda n, a: False,  # predicate would allow
    )
    guard = PermissionGuard(policy, tools={})
    v = guard.check(_call("shell_run", {"command": "git status"}), _ctx())
    assert v.verdict is Verdict.REQUIRE_APPROVAL
