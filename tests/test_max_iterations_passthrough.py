"""Core #2 — decouple ``max_iterations`` from ``plan_ref``.

``governance.iterations`` folds from ``ContextPlanComposed``. That event
used to be skipped when the composer produced no stored plan
(``view.plan_ref is None`` — the protocols-only ``PassthroughComposer``
fallback), so under Passthrough the counter never advanced and
``BudgetGuard.max_iterations`` was inert. The Engine now emits the event
unconditionally once per step, with ``plan_ref=None`` when there is no
stored plan. Byte-safety: the shipped ``ThreeSegmentComposer`` always
set ``plan_ref``, so historical recordings are unchanged.
"""

from __future__ import annotations

from noeta.core.composer import PassthroughComposer
from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.hooks import HookManager
from noeta.core.snapshot import serialize_task_state
from noeta.guards.budget import Budget, BudgetGuard
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import FinishDecision, WaitTimerDecision
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)


def _build(*, policy, hooks=None, clock=None):
    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    disp = InMemoryDispatcher()
    log.bind_lease_registry(disp)
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=PassthroughComposer(),
        policy=policy,
        hooks=hooks or HookManager(),
        clock=clock,
    )
    task = engine.create_task(goal="g", policy_name="scripted")
    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="w")
    assert lease is not None
    return engine, log, cs, lease.lease_id, task


def test_passthrough_step_emits_context_plan_with_null_ref() -> None:
    """The step-boundary event is recorded even without a stored plan,
    and the fold counts it."""
    policy = StubScriptedPolicy([FinishDecision(answer="done")])
    engine, log, cs, lease_id, task = _build(policy=policy)
    finished = engine.run_one_step(task, lease_id=lease_id)
    assert finished.status == "terminal"

    events = log.read(task.task_id)
    plan_events = [e for e in events if e.type == "ContextPlanComposed"]
    assert len(plan_events) == 1
    assert plan_events[0].payload.plan_ref is None

    folded = fold(log, cs, task.task_id)
    assert folded.governance.iterations == 1
    assert folded.context.plan_ref is None


def test_budget_guard_max_iterations_trips_under_passthrough() -> None:
    """The review finding itself: ``max_iterations`` must not be inert
    under a composer that stores no plan. ``max_iterations=0`` means the
    single-step finish sees ``iterations=1 > 0`` → deny → TaskFailed."""
    policy = StubScriptedPolicy([FinishDecision(answer="done")])
    hooks = HookManager()
    hooks.register(BudgetGuard(Budget(max_iterations=0)))
    engine, log, _cs, lease_id, task = _build(policy=policy, hooks=hooks)
    finished = engine.run_one_step(task, lease_id=lease_id)
    assert finished.status == "terminal"
    types = [e.type for e in log.read(task.task_id)]
    assert "TaskFailed" in types
    assert "TaskCompleted" not in types


def test_budget_guard_allows_within_iteration_cap_under_passthrough() -> None:
    policy = StubScriptedPolicy([FinishDecision(answer="done")])
    hooks = HookManager()
    hooks.register(BudgetGuard(Budget(max_iterations=1)))
    engine, log, _cs, lease_id, task = _build(policy=policy, hooks=hooks)
    finished = engine.run_one_step(task, lease_id=lease_id)
    assert finished.status == "terminal"
    types = [e.type for e in log.read(task.task_id)]
    assert "TaskCompleted" in types


def test_null_plan_ref_survives_snapshot_fold_byte_equality() -> None:
    """The two fold paths (snapshot-accelerated vs from-scratch) agree —
    as ``Task`` equality and as ``serialize_task_state`` bytes — on a
    recording that contains the new ``plan_ref=None`` event shape and a
    suspend snapshot."""
    policy = StubScriptedPolicy(
        [WaitTimerDecision(seconds=30), FinishDecision(answer="done")]
    )
    engine, log, cs, lease_id, task = _build(
        policy=policy, clock=lambda: 1_000.0
    )
    suspended = engine.run_one_step(task, lease_id=lease_id)
    assert suspended.status == "suspended"

    accelerated = fold(log, cs, task.task_id, ignore_snapshots=False)
    scratch = fold(log, cs, task.task_id, ignore_snapshots=True)
    assert accelerated == scratch
    assert serialize_task_state(accelerated) == serialize_task_state(scratch)
    assert accelerated.governance.iterations == 1


def test_null_plan_ref_roundtrips_through_sqlite_eventlog(tmp_path) -> None:
    """The persisted payload decoder restores ``plan_ref=None`` (the new
    Passthrough shape) as well as the historical non-null shape."""
    from noeta.storage.sqlite.dispatcher import SqliteDispatcher
    from noeta.storage.sqlite.eventlog import SqliteEventLog

    db = str(tmp_path / "null_plan.sqlite")
    disp = SqliteDispatcher(db)
    log = SqliteEventLog(db, lease_validator=disp)
    cs = InMemoryContentStore()
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=PassthroughComposer(),
        policy=StubScriptedPolicy([FinishDecision(answer="done")]),
    )
    task = engine.create_task(goal="g", policy_name="scripted")
    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="w")
    engine.run_one_step(task, lease_id=lease.lease_id)

    # Re-open the log so payloads decode from persisted bytes.
    log.close()
    reopened = SqliteEventLog(db)
    try:
        plan_events = [
            e
            for e in reopened.read(task.task_id)
            if e.type == "ContextPlanComposed"
        ]
        assert len(plan_events) == 1
        assert plan_events[0].payload.plan_ref is None
        folded = fold(reopened, cs, task.task_id)
        assert folded.governance.iterations == 1
    finally:
        reopened.close()
        disp.close()
