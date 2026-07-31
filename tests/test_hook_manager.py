"""HookManager arbitration, exercised without an Engine.

Two properties decide whether governance can be trusted: guards run in
ascending priority and the first non-allow short-circuits the rest, and a guard
whose ``check`` raises counts as that deciding deny — a buggy guard must never
be able to quietly grant what it was registered to block.
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
    """Registration order carries no meaning: ``priority`` alone orders the
    queue, so a host can register guards in any order it finds readable."""
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
    """A buggy Guard must not crash the Engine; it decides ``deny``, and the
    reason names the guard so the failure is traceable from the EventLog."""
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
    """``noeta.core.hooks`` re-exports ``Verdict`` so a guard author imports
    the manager and its verdict vocabulary from one module."""
    from noeta.core.hooks import Verdict as CoreVerdict
    from noeta.protocols.hooks import Verdict as ProtoVerdict

    assert CoreVerdict is ProtoVerdict


def test_register_returns_none_and_does_not_invoke_guard() -> None:
    """Registration is a list mutation and nothing more — a guard that does
    real work in ``check`` must not have it triggered at wiring time."""

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
