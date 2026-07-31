"""``BudgetGuard`` — per-instance resource caps on a Task's consumption.

The comparison asymmetry is deliberate: ``iterations`` uses strict ``>``
because ``ContextPlanComposed`` for the current step is emitted before the
guard fires, so the in-flight iteration is already counted and
``max_iterations=1`` must still allow exactly one full step; ``cost_usd`` /
``tool_calls`` / ``spawned_subtasks`` use ``>=`` because they count what has
already been consumed. ``ProposedFinish`` is checked against the historical
accumulators only — finish consumes neither a tool slot nor a subtask, so a
task sitting at those caps may still terminate.
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
    """Synchronous resource-cap Guard over the Engine-folded governance state."""

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
            # ``ctx.subtask_depth`` is THIS task's depth and a spawn creates a
            # child at depth+1, so the cap trips once the current depth has
            # reached it (root=0; max=1 allows root→child, denies
            # child→grandchild). Denying here precedes any subtask_id,
            # SubtaskSpawned or child TaskCreated.
            if (
                b.max_subtask_depth is not None
                and ctx.subtask_depth >= b.max_subtask_depth
            ):
                return VerdictResult.deny(
                    f"max_subtask_depth={b.max_subtask_depth} reached"
                )
        # ProposedFinish is bound by the iterations / cost caps above only.
        _ = isinstance(action, ProposedFinish)  # a no-op that keeps the branch visible

        return VerdictResult.allow()
