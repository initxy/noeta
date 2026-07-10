"""Contract tests for ``BudgetGuard`` (issue 18).

Verifies the action-specific cap matrix, the strict ``>`` comparison
for ``max_iterations`` (B5), and the consumption-style ``>=`` for
``tool_calls`` / ``spawned_subtasks`` / ``cost_usd``.
"""

from __future__ import annotations


from noeta.guards.budget import Budget, BudgetGuard
from noeta.protocols.decisions import (
    SpawnSubtaskDecision,
    ToolCall,
)
from noeta.protocols.hooks import (
    GuardContext,
    ProposedFinish,
    ProposedSpawnSubtask,
    ProposedToolCall,
    Verdict,
)
from noeta.protocols.task import GovernanceState


def _ctx(**fields) -> GuardContext:
    return GuardContext(task_id="t1", governance=GovernanceState(**fields))


def _tool_action() -> ProposedToolCall:
    return ProposedToolCall(
        call=ToolCall(tool_name="echo", arguments={}, call_id="c1")
    )


def _spawn_action() -> ProposedSpawnSubtask:
    return ProposedSpawnSubtask(
        decision=SpawnSubtaskDecision(
            agent_name="child", goal="g", inputs={}
        )
    )


def _finish_action() -> ProposedFinish:
    return ProposedFinish(answer="done")


# ---------------------------------------------------------------------------
# All-None Budget allows everything
# ---------------------------------------------------------------------------


def test_empty_budget_allows_all_actions() -> None:
    guard = BudgetGuard(Budget())
    for action in (_tool_action(), _spawn_action(), _finish_action()):
        assert guard.check(action, _ctx()).verdict is Verdict.ALLOW


# ---------------------------------------------------------------------------
# max_iterations: strict ``>`` (B5)
# ---------------------------------------------------------------------------


def test_max_iterations_one_allows_first_step_denies_second() -> None:
    """``ContextPlanComposed`` for the current step is emitted before
    the guard fires, so ``g.iterations`` already includes the in-flight
    step. ``max_iterations=1`` should still allow exactly one full step;
    only the second step deny."""
    guard = BudgetGuard(Budget(max_iterations=1))
    # iterations=1 = first step, in-flight → still allowed
    for action in (_tool_action(), _spawn_action(), _finish_action()):
        assert guard.check(action, _ctx(iterations=1)).verdict is Verdict.ALLOW
    # iterations=2 = second step → deny
    for action in (_tool_action(), _spawn_action(), _finish_action()):
        result = guard.check(action, _ctx(iterations=2))
        assert result.verdict is Verdict.DENY
        assert "max_iterations" in (result.reason or "")


def test_max_iterations_zero_denies_first_step() -> None:
    """``max_iterations=0`` is a degenerate config — deny everything
    once ``g.iterations`` has reached even 1."""
    guard = BudgetGuard(Budget(max_iterations=0))
    assert guard.check(_tool_action(), _ctx(iterations=1)).verdict is Verdict.DENY


# ---------------------------------------------------------------------------
# max_tool_calls: ``>=`` consumption cap; only tool action checks it
# ---------------------------------------------------------------------------


def test_max_tool_calls_geq_strict_on_tool_action() -> None:
    guard = BudgetGuard(Budget(max_tool_calls=2))
    # tool_calls=1 → still allowed (next push to 2 is fine)
    assert guard.check(_tool_action(), _ctx(tool_calls=1)).verdict is Verdict.ALLOW
    # tool_calls=2 → next tool call would push to 3, deny
    result = guard.check(_tool_action(), _ctx(tool_calls=2))
    assert result.verdict is Verdict.DENY
    assert "max_tool_calls" in (result.reason or "")


def test_max_tool_calls_not_checked_on_spawn_or_finish() -> None:
    """Spawn / finish do not consume tool slots — they should be
    admissible even when ``tool_calls`` is at the cap."""
    guard = BudgetGuard(Budget(max_tool_calls=2))
    assert guard.check(_spawn_action(), _ctx(tool_calls=99)).verdict is Verdict.ALLOW
    assert guard.check(_finish_action(), _ctx(tool_calls=99)).verdict is Verdict.ALLOW


# ---------------------------------------------------------------------------
# max_spawned_subtasks: tool + spawn check it; finish does not
# ---------------------------------------------------------------------------


def test_max_spawned_subtasks_checked_on_tool_and_spawn_not_finish() -> None:
    guard = BudgetGuard(Budget(max_spawned_subtasks=1))
    assert guard.check(_tool_action(), _ctx(spawned_subtasks=1)).verdict is Verdict.DENY
    assert guard.check(_spawn_action(), _ctx(spawned_subtasks=1)).verdict is Verdict.DENY
    # finish is unaffected even at the cap
    assert guard.check(_finish_action(), _ctx(spawned_subtasks=1)).verdict is Verdict.ALLOW


# ---------------------------------------------------------------------------
# max_cost_usd: ``>=`` historical accumulator; checked on all actions
# ---------------------------------------------------------------------------


def test_max_cost_usd_checked_on_all_actions() -> None:
    guard = BudgetGuard(Budget(max_cost_usd=1.0))
    assert guard.check(_tool_action(), _ctx(cost_usd=0.99)).verdict is Verdict.ALLOW
    for action in (_tool_action(), _spawn_action(), _finish_action()):
        result = guard.check(action, _ctx(cost_usd=1.0))
        assert result.verdict is Verdict.DENY
        assert "max_cost_usd" in (result.reason or "")


# ---------------------------------------------------------------------------
# Multiple caps interact predictably
# ---------------------------------------------------------------------------


def test_first_cap_to_trip_wins() -> None:
    guard = BudgetGuard(
        Budget(max_iterations=10, max_tool_calls=2, max_cost_usd=1.0)
    )
    # iterations check runs first → iterations cap wins when both at cap
    result = guard.check(
        _tool_action(),
        _ctx(iterations=11, tool_calls=2, cost_usd=1.0),
    )
    assert "max_iterations" in (result.reason or "")
