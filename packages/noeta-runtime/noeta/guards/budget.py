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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from noeta.protocols.hooks import (
    GuardContext,
    ProposedAction,
    ProposedFinish,
    ProposedSpawnSubtask,
    ProposedToolCall,
    VerdictResult,
)


__all__ = ["Budget", "BudgetGuard"]


@dataclass(frozen=True, slots=True)
class Budget:
    """Hard caps on a Task's resource consumption.

    All fields optional; ``None`` means "no cap on this dimension".
    Per-instance (the same Budget covers every Task the BudgetGuard
    sees in Phase 1); Phase 2 introduces per-task budgets through
    the Principal / Contract record.
    """

    max_iterations: Optional[int] = None
    max_tool_calls: Optional[int] = None
    max_cost_usd: Optional[float] = None
    max_spawned_subtasks: Optional[int] = None
    #: SR1 — maximum **child depth** allowed to be created. Root depth is 0;
    #: ``max_subtask_depth=1`` lets a root spawn a child (depth 1) but denies
    #: that child spawning a grandchild (depth 2). ``None`` = no depth cap
    #: (existing behaviour). Checked only for ``ProposedSpawnSubtask``.
    max_subtask_depth: Optional[int] = None


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
