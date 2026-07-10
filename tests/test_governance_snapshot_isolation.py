"""Verify that ``Engine._guard`` passes a deepcopy of the folded
``GovernanceState`` so a buggy Guard cannot mutate the live ``Task``
or the EventLog through ``ctx.governance``.
"""

from __future__ import annotations


from noeta.core.engine import Engine
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import FinishDecision
from noeta.protocols.hooks import (
    GuardContext,
    ProposedAction,
    VerdictResult,
)
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.composer import trivial_three_segment


class _MutatingGuard:
    name = "mutating"
    priority = 5

    def __init__(self) -> None:
        self.last_seen_iterations = None

    def check(self, action: ProposedAction, ctx: GuardContext) -> VerdictResult:
        self.last_seen_iterations = ctx.governance.iterations
        # Try to corrupt the snapshot in every way a buggy Guard might.
        ctx.governance.iterations = -999
        ctx.governance.tool_calls = -999
        ctx.governance.cost_usd = -1.0
        ctx.governance.denied.append({"type": "BogusDenied"})
        return VerdictResult.allow()


def test_guard_governance_snapshot_isolated_from_live_task() -> None:
    """A Guard that aggressively mutates ``ctx.governance`` must leave
    the live ``task.governance`` untouched."""
    from noeta.core.hooks import HookManager

    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    disp = InMemoryDispatcher()
    log.bind_lease_registry(disp)

    hooks = HookManager()
    bad_guard = _MutatingGuard()
    hooks.register(bad_guard)

    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=trivial_three_segment(cs),
        policy=StubScriptedPolicy([FinishDecision(answer="done")]),
        hooks=hooks,
    )

    task = engine.create_task(goal="g", policy_name="scripted")
    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="w")
    assert lease is not None
    finished = engine.run_one_step(task, lease_id=lease.lease_id)

    # The mutating guard ran; the live governance must NOT carry its
    # garbage values.
    assert finished.governance.iterations >= 0
    assert finished.governance.tool_calls >= 0
    assert finished.governance.cost_usd >= 0.0
    assert all(
        d.get("type") != "BogusDenied"
        for d in finished.governance.denied
    )
