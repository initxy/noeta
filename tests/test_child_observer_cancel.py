"""ChildLifecycleObserver must notify the parent when a child is CANCELLED.

Regression: the observer handled ``TaskCompleted`` / ``TaskFailed`` but not
``TaskCancelled``. A child that reached terminal via cancellation (outside a
full-tree cascade that also cancels the parent) emitted no ``SubtaskCompleted``,
so a parent suspended on ``SubtaskCompleted`` waited forever on a wake that
never fired. The observer now surfaces a cancelled child as a ``failed``
``SubtaskResult`` carrying the cancel reason.
"""

from __future__ import annotations

from typing import Any

from noeta.core.observers import ChildLifecycleObserver
from noeta.protocols.events import TaskCancelledPayload, TaskCreatedPayload
from noeta.storage.memory import InMemoryEventLog


class _FakeDispatcher:
    def __init__(self) -> None:
        self.enqueued: list[str] = []
        self.woken: list[tuple[str, Any]] = []

    def enqueue(self, task_id: str) -> None:
        self.enqueued.append(task_id)

    def wake(self, task_id: str, wake_event: Any) -> bool:
        self.woken.append((task_id, wake_event))
        return True


def test_cancelled_child_wakes_parent_and_records_subtask_completed() -> None:
    log = InMemoryEventLog()
    dispatcher = _FakeDispatcher()
    observer = ChildLifecycleObserver(event_log=log, dispatcher=dispatcher)
    try:
        # Child genesis names the parent → observer records lineage + enqueues.
        log.system_emit(
            task_id="child-1",
            type="TaskCreated",
            payload=TaskCreatedPayload(
                goal="do", policy_name="react", parent_task_id="parent-1"
            ),
            actor="test",
            origin="system",
        )
        assert dispatcher.enqueued == ["child-1"]

        # Child is cancelled (not a full-tree cascade).
        log.system_emit(
            task_id="child-1",
            type="TaskCancelled",
            payload=TaskCancelledPayload(reason="user stop"),
            actor="test",
            origin="system",
        )
    finally:
        observer.stop()

    # The parent stream got a SubtaskCompleted for the child ...
    parent_events = [e for e in log.read("parent-1") if e.type == "SubtaskCompleted"]
    assert len(parent_events) == 1
    result = parent_events[0].payload.result
    assert result.status == "failed"
    assert result.error == "cancelled: user stop"

    # ... and the parent was woken (so it can never hang on the child).
    assert len(dispatcher.woken) == 1
    woken_parent, wake_event = dispatcher.woken[0]
    assert woken_parent == "parent-1"
    assert wake_event.subtask_id == "child-1"
