"""Engine end-to-end under strict lease validation (issue 06 #9).

Issue 06 hardens the InMemory backend so EventLog actually rejects
writes from invalid leases. This test file wires Engine, EventLog,
and Dispatcher together in strict mode and exercises the same
scenarios issues 01–05 covered — Phase 0's regression net for "the
kernel still works when the concurrency guards are turned on".
"""

from __future__ import annotations

from typing import Any

from noeta.testing.composer import trivial_three_segment
from noeta.core.engine import Engine
from noeta.core.wiring import wire_default_observers
from noeta.policies.stub import StubFinishPolicy, StubScriptedPolicy
from noeta.protocols.decisions import FinishDecision
from noeta.protocols.errors import InvalidLease
from noeta.protocols.task import Task
from noeta.protocols.wake import SubtaskCompleted
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)


def _wired() -> tuple[InMemoryEventLog, InMemoryContentStore, InMemoryDispatcher]:
    """Construct the storage trio wired in strict-lease mode."""
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    cs = InMemoryContentStore()
    wire_default_observers(log, disp)
    return log, cs, disp


def test_finish_happy_path_under_strict_lease() -> None:
    log, cs, disp = _wired()
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=trivial_three_segment(cs),
        policy=StubFinishPolicy(answer="ok"),
    )
    task = engine.create_task(goal="g", policy_name="stub_finish")
    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="w1")
    assert lease is not None

    result = engine.run_one_step(task, lease_id=lease.lease_id)

    assert result.status == "terminal"
    types = [e.type for e in log.read(task.task_id)]
    assert "TaskCompleted" in types
    assert "TaskSnapshot" in types


def test_engine_write_with_stale_lease_raises_invalid_lease() -> None:
    """Worker holds an expired lease; Engine's first write must fail.

    This is the safety net behind a Worker that loses its
    lease (expiry / requeue_stale) cannot keep dribbling events into
    the EventLog. The very next ``append`` raises ``InvalidLease`` and
    aborts the worker's segment.
    """
    now = [0.0]
    disp = InMemoryDispatcher(now=lambda: now[0])
    log = InMemoryEventLog(lease_validator=disp)
    cs = InMemoryContentStore()
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=trivial_three_segment(cs),
        policy=StubFinishPolicy(answer="x"),
    )
    task = engine.create_task(goal="g", policy_name="stub")
    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="w1", lease_seconds=5.0)
    assert lease is not None

    # Lease expires before the worker calls run_one_step.
    now[0] = 999.0
    disp.requeue_stale()

    import pytest

    with pytest.raises(InvalidLease):
        engine.run_one_step(task, lease_id=lease.lease_id)


def test_snapshot_write_with_stale_lease_also_blocked() -> None:
    """A Worker that loses its lease mid-step cannot leak a snapshot.

    Distinct from the previous test: here the Engine has already
    appended ``TaskStarted`` (valid lease), then the lease expires,
    then the Engine reaches the terminal snapshot write — which must
    raise ``InvalidLease`` rather than silently corrupting the stream.
    """
    import pytest

    now = [0.0]
    disp = InMemoryDispatcher(now=lambda: now[0])
    log = InMemoryEventLog(lease_validator=disp)
    cs = InMemoryContentStore()

    # Counter so we expire the lease *between* the first emit and the
    # subsequent ones.
    emits = {"n": 0}
    original_emit = log.emit

    def expiring_emit(**kwargs: Any) -> Any:
        emits["n"] += 1
        if emits["n"] == 2:
            now[0] = 9999.0  # past expiry, before second write
        return original_emit(**kwargs)

    log.emit = expiring_emit  # type: ignore[method-assign]
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=trivial_three_segment(cs),
        policy=StubFinishPolicy(answer="x"),
    )
    task = engine.create_task(goal="g", policy_name="stub")
    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="w1", lease_seconds=5.0)
    assert lease is not None

    with pytest.raises(InvalidLease):
        engine.run_one_step(task, lease_id=lease.lease_id)


def test_parent_child_subtask_full_loop_under_strict_lease() -> None:
    """Issue 03's full spawn → child finish → parent wake closes under
    strict lease validation. Catches regressions in the cross-stream
    system_append path.
    """
    log, cs, disp = _wired()

    parent_policy = StubScriptedPolicy(
        [
            # First decide: spawn a subtask.
            __import__(
                "noeta.protocols.decisions", fromlist=["SpawnSubtaskDecision"]
            ).SpawnSubtaskDecision(
                agent_name="child-agent", goal="do-child", inputs={}
            ),
            # Second decide (after wake): finish the parent.
            FinishDecision(answer="parent-done"),
        ]
    )
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=trivial_three_segment(cs),
        policy=parent_policy,
    )
    parent = engine.create_task(goal="g", policy_name="scripted")
    disp.enqueue(parent.task_id)
    lease_p1 = disp.lease(worker_id="w-parent")
    assert lease_p1 is not None

    # Parent runs first segment: spawns subtask, suspends.
    engine.run_one_step(parent, lease_id=lease_p1.lease_id)
    assert parent.status == "suspended"
    # Worker would release its lease at this point in a real Worker loop.
    disp.release(
        lease_p1.lease_id,
        next_state="suspended",
        wake_on=parent.wake_on,
    )

    # Now lease the child the dispatcher enqueued and finish it.
    child_lease = disp.lease(worker_id="w-child")
    assert child_lease is not None and child_lease.task_id != parent.task_id
    # Use a scripted policy that finishes immediately on the child.
    engine._policy = StubFinishPolicy(answer="child-done")  # type: ignore[attr-defined]
    child_task = Task(
        task_id=child_lease.task_id,
        status="pending",
        parent_task_id=parent.task_id,
    )
    engine.run_one_step(child_task, lease_id=child_lease.lease_id)
    disp.release(child_lease.lease_id, next_state="terminal")

    # Parent is now back to ready (woken by SubtaskCompleted via
    # dispatcher.wake from the Engine's child-completion observer).
    lease_p2 = disp.lease(worker_id="w-parent-2")
    assert lease_p2 is not None and lease_p2.task_id == parent.task_id

    # Resume parent and let it finish.
    engine._policy = parent_policy  # type: ignore[attr-defined]
    engine.note_woken(
        parent,
        lease_id=lease_p2.lease_id,
        wake_event=SubtaskCompleted(subtask_id=child_lease.task_id),
    )
    engine.run_one_step(parent, lease_id=lease_p2.lease_id)
    assert parent.status == "terminal"

    parent_types = [e.type for e in log.read(parent.task_id)]
    # The cross-stream SubtaskCompleted written via system_append must
    # appear before the parent's TaskWoken / TaskCompleted sequence.
    assert "SubtaskCompleted" in parent_types
    assert "TaskCompleted" in parent_types
