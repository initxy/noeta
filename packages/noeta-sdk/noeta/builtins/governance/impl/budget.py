"""``BudgetGuard`` — resource caps on a Task's consumption.

Issue 18. Reads the ``GovernanceState`` snapshot folded by the Engine
(see :meth:`noeta.core.engine.Engine._guard`) and refuses further
actions once any configured cap has been reached. Caps are
**per-instance** in Phase 1 (no per-task ``Budget`` field on
``TaskCreated`` yet); Phase 2 will read budgets off a Task ``Principal``
or ``Contract``.

Action-specific caps matrix (issue 18 sign-off):

* ``ProposedToolCall``: check all four caps (iterations / cost_usd /
  tool_calls / spawned_subtasks).
* ``ProposedSpawnSubtask``: iterations / cost_usd / spawned_subtasks.
  ``tool_calls`` is not relevant — spawning does not consume a tool
  slot.
* ``ProposedFinish``: iterations / cost_usd only. ``tool_calls`` and
  ``spawned_subtasks`` are consumption caps; finish does not consume
  them, so it stays admissible even at those caps. ``iterations`` and
  ``cost_usd`` are historical accumulators — if the task has
  overspent there, even finish is blocked.

Comparison operator choice:

* ``iterations`` uses ``>`` (strict). ``ContextPlanComposed`` for the
  current step is emitted **before** the guard fires, so
  ``g.iterations`` already counts the in-flight iteration. We want
  ``max_iterations=1`` to allow exactly one full step.
* ``tool_calls`` / ``spawned_subtasks`` use ``>=`` because they
  represent counts already consumed **before** the proposed action;
  ``>=`` lets the next action push the count to ``cap+1`` only if
  it's still below.
* ``cost_usd`` uses ``>=`` — the cost has already been incurred; once
  at the cap the task should not continue.

Microkernel M2: the class moved here from ``noeta.guards.budget``; its
:class:`~noeta.runtime.governance.Budget` configuration type sank into the
kernel vocabulary module.
"""

from __future__ import annotations

from noeta.protocols.hooks import (
    GuardContext,
    ProposedAction,
    ProposedFinish,
    ProposedSpawnSubtask,
    ProposedToolCall,
    VerdictResult,
)
from noeta.runtime.governance import Budget


__all__ = ["BudgetGuard"]


class BudgetGuard:
    """Synchronous resource-cap Guard. Returns ``DENY`` once any
    configured cap is reached; otherwise ``ALLOW``."""

    name = "budget"
    priority = 10

    def __init__(self, budget: Budget) -> None:
        self._budget = budget

    def check(
        self, action: ProposedAction, ctx: GuardContext
    ) -> VerdictResult:
        g = ctx.governance
        b = self._budget

        if b.max_iterations is not None and g.iterations > b.max_iterations:
            return VerdictResult.deny(
                f"max_iterations={b.max_iterations} exceeded"
            )
        if b.max_cost_usd is not None and g.cost_usd >= b.max_cost_usd:
            return VerdictResult.deny(
                f"max_cost_usd={b.max_cost_usd} reached"
            )

        if isinstance(action, ProposedToolCall):
            if (
                b.max_tool_calls is not None
                and g.tool_calls >= b.max_tool_calls
            ):
                return VerdictResult.deny(
                    f"max_tool_calls={b.max_tool_calls} reached"
                )
            if (
                b.max_spawned_subtasks is not None
                and g.spawned_subtasks >= b.max_spawned_subtasks
            ):
                return VerdictResult.deny(
                    f"max_spawned_subtasks={b.max_spawned_subtasks} reached"
                )
        elif isinstance(action, ProposedSpawnSubtask):
            if (
                b.max_spawned_subtasks is not None
                and g.spawned_subtasks >= b.max_spawned_subtasks
            ):
                return VerdictResult.deny(
                    f"max_spawned_subtasks={b.max_spawned_subtasks} reached"
                )
            # SR1: depth cap. ``ctx.subtask_depth`` is THIS task's depth; a
            # spawn would create a child at depth+1, so deny once the
            # current depth has reached the cap (root=0; max=1 allows
            # root→child, denies child→grandchild). Deny here happens
            # before any subtask_id / SubtaskSpawned / child TaskCreated.
            if (
                b.max_subtask_depth is not None
                and ctx.subtask_depth >= b.max_subtask_depth
            ):
                return VerdictResult.deny(
                    f"max_subtask_depth={b.max_subtask_depth} reached"
                )
        # ProposedFinish: only the iterations / cost caps above apply.
        _ = isinstance(action, ProposedFinish)  # documents the branch

        return VerdictResult.allow()
