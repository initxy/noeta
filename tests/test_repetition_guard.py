"""Contract tests for ``RepetitionGuard`` (work item ④, D-4 / D-4b).

The guard reads the last few ``(tool_name, canonical input bytes)`` pairs
folded by the Engine into ``GuardContext.recent_tool_calls`` and, once the
*same* identity key has repeated ``threshold`` times **consecutively**, returns
the configured action (``require_approval`` by default, ``deny`` when
configured). Identity is provider-neutral: ``tool_name`` plus
``to_canonical_bytes(arguments)`` (key-order independent, stable for replay).

Determinism is load-bearing: the guard is a pure function of its policy and the
recorded history, so a recording replays to the identical verdict (the final
case nails this down).
"""

from __future__ import annotations

import pytest

from noeta.guards.repetition import RepetitionGuard, RepetitionPolicy
from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.decisions import SpawnSubtaskDecision, ToolCall
from noeta.protocols.hooks import (
    GuardContext,
    ProposedFinish,
    ProposedSpawnSubtask,
    ProposedToolCall,
    Verdict,
)


def _key(tool_name: str, arguments: dict) -> tuple[str, bytes]:
    return (tool_name, to_canonical_bytes(arguments))


def _ctx(recent: tuple[tuple[str, bytes], ...] = ()) -> GuardContext:
    return GuardContext(task_id="t1", recent_tool_calls=recent)


def _tool_action(
    tool_name: str = "echo", arguments: dict | None = None
) -> ProposedToolCall:
    return ProposedToolCall(
        call=ToolCall(
            tool_name=tool_name, arguments=arguments or {}, call_id="c1"
        )
    )


def _spawn_action() -> ProposedSpawnSubtask:
    return ProposedSpawnSubtask(
        decision=SpawnSubtaskDecision(agent_name="child", goal="g", inputs={})
    )


def _finish_action() -> ProposedFinish:
    return ProposedFinish(answer="done")


# ---------------------------------------------------------------------------
# Empty history / nothing repeated → always allow
# ---------------------------------------------------------------------------


def test_empty_history_allows() -> None:
    guard = RepetitionGuard(RepetitionPolicy(threshold=3))
    assert guard.check(_tool_action(), _ctx()).verdict is Verdict.ALLOW


def test_below_threshold_allows() -> None:
    """threshold=3: only two prior identical calls in history → the proposed
    call would make three, but with the consecutive-count-includes-proposed
    semantics fewer than threshold consecutive matches must still allow."""
    args = {"k": "a"}
    guard = RepetitionGuard(RepetitionPolicy(threshold=3))
    history = (_key("echo", args),)  # one prior identical call
    # proposed call = 2nd identical in a row → still below threshold=3
    assert guard.check(_tool_action("echo", args), _ctx(history)).verdict is (
        Verdict.ALLOW
    )


# ---------------------------------------------------------------------------
# Consecutive identical calls reaching the threshold → configured action
# ---------------------------------------------------------------------------


def test_threshold_reached_requires_approval_by_default() -> None:
    args = {"k": "a"}
    guard = RepetitionGuard(RepetitionPolicy(threshold=3))
    # two prior identical calls + the proposed one = 3 consecutive → trip
    history = (_key("echo", args), _key("echo", args))
    result = guard.check(_tool_action("echo", args), _ctx(history))
    assert result.verdict is Verdict.REQUIRE_APPROVAL
    assert "echo" in (result.reason or "")


def test_threshold_reached_can_deny() -> None:
    args = {"k": "a"}
    guard = RepetitionGuard(
        RepetitionPolicy(threshold=3, action="deny")
    )
    history = (_key("echo", args), _key("echo", args))
    result = guard.check(_tool_action("echo", args), _ctx(history))
    assert result.verdict is Verdict.DENY
    assert "echo" in (result.reason or "")


def test_threshold_two_trips_on_second_identical() -> None:
    args = {"k": "a"}
    guard = RepetitionGuard(RepetitionPolicy(threshold=2))
    # one prior + proposed = 2 consecutive → trip at threshold=2
    history = (_key("echo", args),)
    assert guard.check(_tool_action("echo", args), _ctx(history)).verdict is (
        Verdict.REQUIRE_APPROVAL
    )


# ---------------------------------------------------------------------------
# Different arguments → not a repeat
# ---------------------------------------------------------------------------


def test_different_arguments_allows() -> None:
    guard = RepetitionGuard(RepetitionPolicy(threshold=2))
    history = (_key("echo", {"k": "a"}), _key("echo", {"k": "a"}))
    # proposed has different args → the consecutive run resets to 1
    assert guard.check(
        _tool_action("echo", {"k": "b"}), _ctx(history)
    ).verdict is Verdict.ALLOW


def test_key_order_independent_match() -> None:
    """Canonical bytes are key-order independent, so {a,b} == {b,a}."""
    guard = RepetitionGuard(RepetitionPolicy(threshold=2))
    history = (_key("echo", {"a": 1, "b": 2}),)
    result = guard.check(
        _tool_action("echo", {"b": 2, "a": 1}), _ctx(history)
    )
    assert result.verdict is Verdict.REQUIRE_APPROVAL


# ---------------------------------------------------------------------------
# Different tool name with same arguments → not a repeat (identity = both)
# ---------------------------------------------------------------------------


def test_different_tool_same_args_allows() -> None:
    args = {"k": "a"}
    guard = RepetitionGuard(RepetitionPolicy(threshold=2))
    history = (_key("read", args),)
    assert guard.check(
        _tool_action("write", args), _ctx(history)
    ).verdict is Verdict.ALLOW


# ---------------------------------------------------------------------------
# Non-consecutive repeats don't trip (the run must be unbroken at the tail)
# ---------------------------------------------------------------------------


def test_non_consecutive_repeat_does_not_trip() -> None:
    args = {"k": "a"}
    guard = RepetitionGuard(RepetitionPolicy(threshold=3))
    # a, a, b — the run of `a` was broken by `b`; proposed `a` is only the
    # first in a fresh run → allow.
    history = (
        _key("echo", args),
        _key("echo", args),
        _key("echo", {"k": "b"}),
    )
    assert guard.check(_tool_action("echo", args), _ctx(history)).verdict is (
        Verdict.ALLOW
    )


# ---------------------------------------------------------------------------
# Spawn / finish are out of scope: the guard only watches tool calls
# ---------------------------------------------------------------------------


def test_spawn_and_finish_always_allow() -> None:
    guard = RepetitionGuard(RepetitionPolicy(threshold=1))
    assert guard.check(_spawn_action(), _ctx()).verdict is Verdict.ALLOW
    assert guard.check(_finish_action(), _ctx()).verdict is Verdict.ALLOW


# ---------------------------------------------------------------------------
# Disabled policy (threshold <= 0) is a no-op
# ---------------------------------------------------------------------------


def test_disabled_threshold_allows_everything() -> None:
    args = {"k": "a"}
    guard = RepetitionGuard(RepetitionPolicy(threshold=0))
    history = (_key("echo", args), _key("echo", args), _key("echo", args))
    assert guard.check(_tool_action("echo", args), _ctx(history)).verdict is (
        Verdict.ALLOW
    )


# ---------------------------------------------------------------------------
# Identity / ordering metadata
# ---------------------------------------------------------------------------


def test_guard_name_and_priority() -> None:
    guard = RepetitionGuard(RepetitionPolicy())
    assert guard.name == "repetition"
    # Budget (10) < Permission (20) < Repetition (30) < Hook (100): hard
    # allow/deny precedes the "suspected loop" heuristic.
    assert guard.priority == 30


def test_policy_is_frozen() -> None:
    policy = RepetitionPolicy(threshold=3)
    with pytest.raises(Exception):
        policy.threshold = 5  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Determinism: identical (policy, history, action) → identical verdict
# ---------------------------------------------------------------------------


def test_deterministic_same_inputs_same_verdict() -> None:
    """Replay safety: the verdict is a pure function of policy + recorded
    history, so two evaluations with the same inputs agree byte-for-byte on
    verdict and reason."""
    args = {"x": 1, "y": [2, 3]}
    guard = RepetitionGuard(RepetitionPolicy(threshold=3, action="deny"))
    history = (_key("tool", args), _key("tool", args))
    first = guard.check(_tool_action("tool", args), _ctx(history))
    second = guard.check(_tool_action("tool", args), _ctx(history))
    assert first.verdict is second.verdict is Verdict.DENY
    assert first.reason == second.reason


def test_non_json_native_arguments_are_stable() -> None:
    """``to_canonical_bytes`` uses ``default=str``; a tuple value still yields
    a stable, comparable key so identity holds across calls."""
    args = {"coords": (1, 2)}
    guard = RepetitionGuard(RepetitionPolicy(threshold=2))
    history = (_key("plot", args),)
    result = guard.check(_tool_action("plot", args), _ctx(history))
    assert result.verdict is Verdict.REQUIRE_APPROVAL
