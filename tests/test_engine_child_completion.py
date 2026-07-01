"""Engine child-completion observer (Phase 0 inline).

Issue 03: when the Engine writes a ``TaskCompleted`` / ``TaskFailed``
to a child stream (a task whose ``parent_task_id`` is non-None), it
must also:

1. Append ``SubtaskCompleted(subtask_id, result)`` to the *parent*
   stream.
2. Call ``dispatcher.wake(parent_task_id, SubtaskCompleted(...))`` so
   the parent (currently suspended) re-queues.

The observer is built into the Engine for Phase 0 — there is no
general-purpose Observer framework yet (that's Phase 1). Failures of
this wake are not silently swallowed: a missing Dispatcher when a
child Task is terminating is a programmer error.
"""

from __future__ import annotations

from typing import Any

from noeta.testing.composer import trivial_three_segment
from noeta.core.engine import Engine
from noeta.core.wiring import wire_default_observers
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import FailDecision, FinishDecision
from noeta.protocols.wake import SubtaskCompleted, SubtaskResult
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)


def _build_engine_with_child(
    *,
    child_decision: Any,
    parent_id: str = "task-parent",
) -> tuple[Engine, InMemoryEventLog, InMemoryDispatcher, str]:
    content_store = InMemoryContentStore()
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    wire_default_observers(event_log, dispatcher)
    composer = trivial_three_segment(content_store)

    policy = StubScriptedPolicy([child_decision])
    engine = Engine(
        event_log=event_log,
        content_store=content_store,
        composer=composer,
        policy=policy,
    )
    # Pretend the parent is suspended on the child we are about to run.
    # We do not actually run the parent — we only need the dispatcher
    # bookkeeping to reflect suspension so the wake re-queues it.
    dispatcher.enqueue(parent_id)
    parent_lease = dispatcher.lease(worker_id="w-parent")
    assert parent_lease is not None

    # We need a real child task to drive: bootstrap its TaskCreated and
    # lease it.
    child = engine.create_task(
        goal="child", policy_name="scripted", parent_task_id=parent_id
    )
    dispatcher.release(
        parent_lease.lease_id,
        next_state="suspended",
        wake_on=SubtaskCompleted(subtask_id=child.task_id),
    )
    dispatcher.enqueue(child.task_id)
    child_lease = dispatcher.lease(worker_id="w-child")
    assert child_lease is not None
    return engine, event_log, dispatcher, child_lease.lease_id


def test_child_finish_appends_subtask_completed_to_parent_stream() -> None:
    engine, log, _disp, lease_id = _build_engine_with_child(
        child_decision=FinishDecision(answer="child done")
    )
    child_id = _find_child_id(log, parent_id="task-parent")
    from noeta.protocols.task import Task

    child_task = Task(
        task_id=child_id, status="pending", parent_task_id="task-parent"
    )
    engine.run_one_step(child_task, lease_id=lease_id)

    parent_events = log.read("task-parent")
    completed = [
        e for e in parent_events if e.type == "SubtaskCompleted"
    ]
    assert len(completed) == 1
    payload = completed[0].payload
    assert payload.subtask_id == child_id
    assert payload.result == SubtaskResult(status="completed", output="child done")


def test_child_finish_wakes_parent_in_dispatcher() -> None:
    engine, log, disp, lease_id = _build_engine_with_child(
        child_decision=FinishDecision(answer="child done")
    )
    child_id = _find_child_id(log, parent_id="task-parent")
    from noeta.protocols.task import Task

    child_task = Task(
        task_id=child_id, status="pending", parent_task_id="task-parent"
    )
    engine.run_one_step(child_task, lease_id=lease_id)

    # Parent re-queued: a new lease for it should be available.
    next_lease = disp.lease(worker_id="w-parent-2")
    assert next_lease is not None
    assert next_lease.task_id == "task-parent"


def test_child_finish_lease_after_wake_carries_subtask_result_round_trip() -> None:
    """End-to-end wake-resume chain (issue 26): observer fires
    ``dispatcher.wake(parent, SubtaskCompleted(subtask_id=X, result=R))``
    after child terminates → the next ``dispatcher.lease(task_id=parent)``
    delivers ``Lease.wake_event=SubtaskCompleted(subtask_id=X, result=R)``.
    Projection matching preserves ``result`` through the chain.
    """
    engine, log, disp, lease_id = _build_engine_with_child(
        child_decision=FinishDecision(answer="payload-7")
    )
    child_id = _find_child_id(log, parent_id="task-parent")
    from noeta.protocols.task import Task

    child_task = Task(
        task_id=child_id, status="pending", parent_task_id="task-parent"
    )
    engine.run_one_step(child_task, lease_id=lease_id)

    parent_lease = disp.lease(worker_id="w-parent-2", task_id="task-parent")
    assert parent_lease is not None
    assert isinstance(parent_lease.wake_event, SubtaskCompleted)
    assert parent_lease.wake_event.subtask_id == child_id
    assert parent_lease.wake_event.result == SubtaskResult(
        status="completed", output="payload-7"
    )


def test_child_fail_appends_failed_subtask_result_to_parent_stream() -> None:
    engine, log, disp, lease_id = _build_engine_with_child(
        child_decision=FailDecision(reason="boom", retryable=False)
    )
    child_id = _find_child_id(log, parent_id="task-parent")
    from noeta.protocols.task import Task

    child_task = Task(
        task_id=child_id, status="pending", parent_task_id="task-parent"
    )
    engine.run_one_step(child_task, lease_id=lease_id)

    parent_events = log.read("task-parent")
    completed = [
        e for e in parent_events if e.type == "SubtaskCompleted"
    ]
    assert len(completed) == 1
    assert completed[0].payload.result == SubtaskResult(
        status="failed", error="boom"
    )
    # Parent still re-queued (failed child also wakes parent).
    next_lease = disp.lease(worker_id="w-parent-2")
    assert next_lease is not None and next_lease.task_id == "task-parent"


# -- helpers ---------------------------------------------------------------


def _all_streams(log: InMemoryEventLog) -> list[str]:
    # Pull from the private stream dict for test introspection — fine for
    # Phase 0 since the InMemory backend is the test fixture.
    return list(log._streams.keys())  # type: ignore[attr-defined]


def _find_child_id(log: InMemoryEventLog, *, parent_id: str) -> str:
    for tid in _all_streams(log):
        events = log.read(tid)
        if not events or events[0].type != "TaskCreated":
            continue
        if getattr(events[0].payload, "parent_task_id", None) == parent_id:
            return tid
    raise AssertionError("no child stream found")
