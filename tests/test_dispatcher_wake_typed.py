"""Dispatcher wake semantics for typed WakeCondition (issue 03).

These tests pin down four invariants:

1. ``wake`` with a matching ``SubtaskCompleted`` flips a suspended task
   back to ready (the ``_matches`` rule must understand typed
   conditions, not just bare equality).
2. ``wake`` arriving while a task is still running (a "wake-before-
   suspend" race) is persisted to ``pending_wake_events``; the task is
   re-queued immediately on the corresponding ``release(suspended,…)``.
3. ``lease`` never returns a task whose status is ``suspended``.
4. A non-matching wake (different ``subtask_id``) does not re-queue;
   it is queued as a pending event.
"""

from __future__ import annotations

from noeta.protocols.wake import SubtaskCompleted
from noeta.storage.memory import InMemoryDispatcher


def _enqueue_and_lease(disp: InMemoryDispatcher, task_id: str) -> str:
    disp.enqueue(task_id)
    lease = disp.lease(worker_id="w-test")
    assert lease is not None
    return lease.lease_id


def test_wake_with_matching_subtask_completed_requeues_suspended_task() -> None:
    disp = InMemoryDispatcher()
    lease_id = _enqueue_and_lease(disp, "t-parent")
    wake_on = SubtaskCompleted(subtask_id="t-child-1")
    disp.release(lease_id, next_state="suspended", wake_on=wake_on)

    requeued = disp.wake("t-parent", SubtaskCompleted(subtask_id="t-child-1"))

    assert requeued is True
    lease2 = disp.lease(worker_id="w-test")
    assert lease2 is not None and lease2.task_id == "t-parent"


def test_wake_before_suspend_for_typed_condition_then_release_immediately_requeues() -> None:
    disp = InMemoryDispatcher()
    lease_id = _enqueue_and_lease(disp, "t-parent")

    # Wake arrives while parent is still leased.
    accepted = disp.wake(
        "t-parent", SubtaskCompleted(subtask_id="t-child-1")
    )
    assert accepted is False

    # Now parent suspends with matching wake_on; pending event drains.
    disp.release(
        lease_id,
        next_state="suspended",
        wake_on=SubtaskCompleted(subtask_id="t-child-1"),
    )

    lease2 = disp.lease(worker_id="w-test")
    assert lease2 is not None and lease2.task_id == "t-parent"


def test_suspended_task_is_not_returned_by_lease() -> None:
    disp = InMemoryDispatcher()
    lease_id = _enqueue_and_lease(disp, "t-parent")
    disp.release(
        lease_id,
        next_state="suspended",
        wake_on=SubtaskCompleted(subtask_id="t-child-1"),
    )

    assert disp.lease(worker_id="w-test") is None


def test_wake_with_unrelated_subtask_id_does_not_requeue() -> None:
    disp = InMemoryDispatcher()
    lease_id = _enqueue_and_lease(disp, "t-parent")
    disp.release(
        lease_id,
        next_state="suspended",
        wake_on=SubtaskCompleted(subtask_id="t-child-1"),
    )

    requeued = disp.wake(
        "t-parent", SubtaskCompleted(subtask_id="t-other")
    )

    assert requeued is False
    assert disp.lease(worker_id="w-test") is None
