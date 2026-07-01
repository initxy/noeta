"""HookManager unit tests for Phase 0 issue 05.

These tests exercise the manager directly (no Engine) so the
priority-ordering, short-circuiting on first non-allow, and the
"guard exception = deny" defensive behaviour are pinned down by
narrow tests rather than only by Engine-level integration tests.
"""

from __future__ import annotations

from typing import Any

from noeta.core.hooks import HookManager
from noeta.protocols.decisions import ToolCall
from noeta.protocols.hooks import (
    GuardContext,
    ProposedAction,
    ProposedToolCall,
    Verdict,
    VerdictResult,
)


class _RecordingGuard:
    """A Guard that records each call into a shared trace list."""

    def __init__(
        self,
        name: str,
        priority: int,
        verdict: VerdictResult,
        trace: list[str],
    ) -> None:
        self.name = name
        self.priority = priority
        self._verdict = verdict
        self._trace = trace

    def check(
        self, action: ProposedAction, ctx: GuardContext
    ) -> VerdictResult:  # noqa: ARG002
        self._trace.append(self.name)
        return self._verdict


def _proposed() -> ProposedAction:
    return ProposedToolCall(
        call=ToolCall(tool_name="t", arguments={}, call_id="c1")
    )


def _ctx() -> GuardContext:
    return GuardContext(task_id="task-x")


def test_no_guards_returns_allow() -> None:
    mgr = HookManager()
    result = mgr.check(_proposed(), _ctx())
    assert result.verdict is Verdict.ALLOW


def test_guards_run_in_priority_ascending_order() -> None:
    trace: list[str] = []
    mgr = HookManager()
    mgr.register(_RecordingGuard("low", 10, VerdictResult.allow(), trace))
    mgr.register(_RecordingGuard("mid", 50, VerdictResult.allow(), trace))
    mgr.register(_RecordingGuard("high", 100, VerdictResult.allow(), trace))

    result = mgr.check(_proposed(), _ctx())

    assert result.verdict is Verdict.ALLOW
    assert trace == ["low", "mid", "high"]


def test_first_deny_short_circuits_remaining_guards() -> None:
    trace: list[str] = []
    mgr = HookManager()
    mgr.register(_RecordingGuard("low", 10, VerdictResult.allow(), trace))
    mgr.register(
        _RecordingGuard("mid", 50, VerdictResult.deny("nope"), trace)
    )
    mgr.register(_RecordingGuard("high", 100, VerdictResult.allow(), trace))

    result = mgr.check(_proposed(), _ctx())

    assert result.verdict is Verdict.DENY
    assert result.reason == "nope"
    # 'high' must NOT be invoked because 'mid' already produced a non-allow.
    assert trace == ["low", "mid"]


def test_first_require_approval_short_circuits_remaining_guards() -> None:
    trace: list[str] = []
    mgr = HookManager()
    mgr.register(
        _RecordingGuard(
            "early", 1, VerdictResult.require_approval("ask first"), trace
        )
    )
    mgr.register(_RecordingGuard("later", 5, VerdictResult.allow(), trace))

    result = mgr.check(_proposed(), _ctx())

    assert result.verdict is Verdict.REQUIRE_APPROVAL
    assert result.reason == "ask first"
    assert trace == ["early"]


def test_register_assigns_priority_when_caller_omits_attr() -> None:
    """Callers may pass priority via register() instead of a class attr.

    This keeps tests and small inline guards from needing a class shell.
    """
    trace: list[str] = []
    mgr = HookManager()
    a = _RecordingGuard("a", 5, VerdictResult.allow(), trace)
    b = _RecordingGuard("b", 1, VerdictResult.allow(), trace)
    # Register in a deliberately wrong order to prove priority wins.
    mgr.register(a)
    mgr.register(b)

    mgr.check(_proposed(), _ctx())

    assert trace == ["b", "a"]


class _ExplodingGuard:
    name = "explody"
    priority = 5

    def check(
        self, action: ProposedAction, ctx: GuardContext
    ) -> VerdictResult:  # noqa: ARG002
        raise RuntimeError("guard crashed")


def test_guard_exception_is_treated_as_deny_with_reason() -> None:
    """Defensive: a buggy Guard must not crash the Engine; it falls
    through as ``deny`` (with a synthetic reason naming the guard).
    """
    mgr = HookManager()
    mgr.register(_ExplodingGuard())

    result = mgr.check(_proposed(), _ctx())

    assert result.verdict is Verdict.DENY
    assert result.reason is not None
    assert "explody" in result.reason


def test_guard_exception_short_circuits_lower_priority_guards() -> None:
    """An exploding Guard counts as the deciding (non-allow) verdict, so
    higher-priority guards below it in the queue still get to run first,
    but lower-priority guards after it must not be consulted."""
    trace: list[str] = []
    mgr = HookManager()
    mgr.register(_RecordingGuard("first", 1, VerdictResult.allow(), trace))
    mgr.register(_ExplodingGuard())
    mgr.register(_RecordingGuard("last", 99, VerdictResult.allow(), trace))

    result = mgr.check(_proposed(), _ctx())

    assert result.verdict is Verdict.DENY
    assert trace == ["first"]


def test_legacy_verdict_alias_is_still_importable() -> None:
    """Issue 01 exposed ``Verdict`` from ``noeta.core.hooks``.

    Callers (and the SDD module table) should not have to retarget the
    import path. This test fails if the alias is removed.
    """
    from noeta.core.hooks import Verdict as CoreVerdict
    from noeta.protocols.hooks import Verdict as ProtoVerdict

    assert CoreVerdict is ProtoVerdict


def test_register_returns_none_and_does_not_invoke_guard() -> None:
    """A defensive guarantee: registration itself MUST not call check().

    Some guards do non-trivial setup in __init__; registration is purely
    a list mutation.
    """

    class _NeverCallMe:
        name = "x"
        priority = 1

        def check(
            self, action: Any, ctx: Any  # noqa: ANN401, ARG002
        ) -> VerdictResult:
            raise AssertionError("check() must not run at register time")

    mgr = HookManager()
    rv = mgr.register(_NeverCallMe())
    assert rv is None
