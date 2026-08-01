"""Shared-store behaviour of the child-lifecycle handoff (ADR
``worker-queue-routing``).

Lineage is derived from the log, so the handoff must be correct in whichever
*instance* commits the child's terminal — a second event log over the same
sqlite file, a fresh process after a crash — and must never double-fire when
several clients wire observers over one triple. These tests pin exactly those
topologies; single-instance restart behaviour lives in
``test_child_observer_restart_lineage.py``.
"""

from __future__ import annotations

from typing import Any

from noeta.core.observers import ChildLifecycleObserver
from noeta.core.wiring import wire_default_observers
from noeta.protocols.events import (
    TaskCancelledPayload,
    TaskCompletedPayload,
    TaskCreatedPayload,
    TaskSuspendedPayload,
)
from noeta.protocols.wake import SubtaskCompleted
from noeta.sdk.storage import SqliteEventLog
from noeta.storage.memory import InMemoryDispatcher, InMemoryEventLog


class _FakeDispatcher:
    def __init__(self) -> None:
        self.enqueued: list[str] = []
        self.woken: list[tuple[str, Any]] = []

    def enqueue(
        self, task_id: str, *, parent_task_id: Any = None
    ) -> None:
        self.enqueued.append(task_id)

    def wake(self, task_id: str, wake_event: Any) -> bool:
        self.woken.append((task_id, wake_event))
        return True


def _emit_created(log: Any, child_id: str, parent_id: str) -> None:
    log.system_emit(
        task_id=child_id,
        type="TaskCreated",
        payload=TaskCreatedPayload(
            goal="do", policy_name="react", parent_task_id=parent_id
        ),
        actor="test",
        origin="system",
    )


def _emit_completed(log: Any, child_id: str) -> None:
    log.system_emit(
        task_id=child_id,
        type="TaskCompleted",
        payload=TaskCompletedPayload(answer="done"),
        actor="test",
        origin="system",
    )


def _emit_suspended(log: Any, parent_id: str, wake_on: Any) -> None:
    log.system_emit(
        task_id=parent_id,
        type="TaskSuspended",
        payload=TaskSuspendedPayload(reason="waiting_subtask", wake_on=wake_on),
        actor="test",
        origin="system",
    )


def _subtask_completed(log: Any, parent_id: str) -> list[Any]:
    return [e for e in log.read(parent_id) if e.type == "SubtaskCompleted"]


# ---------------------------------------------------------------------------
# The committing instance owns the handoff — whichever instance that is
# ---------------------------------------------------------------------------


def test_terminal_committed_by_second_instance_still_notifies(tmp_path: Any) -> None:
    """Instance A creates the child; instance B (its own event log + observer
    over the same file) drives it to terminal. B never saw ``TaskCreated``
    live — the parent must still get exactly one handoff, from the log."""
    db = str(tmp_path / "shared.db")
    log_a, log_b = SqliteEventLog(db), SqliteEventLog(db)
    disp_a, disp_b = _FakeDispatcher(), _FakeDispatcher()
    obs_a = ChildLifecycleObserver(event_log=log_a, dispatcher=disp_a)
    _emit_created(log_a, "child-1", "parent-1")
    _emit_suspended(log_a, "parent-1", SubtaskCompleted(subtask_id="child-1"))
    obs_b = ChildLifecycleObserver(event_log=log_b, dispatcher=disp_b)
    try:
        _emit_completed(log_b, "child-1")
    finally:
        obs_a.stop()
        obs_b.stop()

    assert len(_subtask_completed(log_a, "parent-1")) == 1
    # A's observer never saw the commit (per-instance notify); B's did the
    # whole handoff, including the wake.
    assert disp_a.woken == []
    assert [(t, w.subtask_id) for t, w in disp_b.woken] == [
        ("parent-1", "child-1")
    ]


# ---------------------------------------------------------------------------
# Crash window: terminal durable, handoff never emitted
# ---------------------------------------------------------------------------


def test_construction_recovers_unrecorded_handoff() -> None:
    """A crash between the child's terminal commit and the handoff emit used
    to strand the parent forever — construction now emits the missing
    ``SubtaskCompleted`` and fires the wake."""
    log = InMemoryEventLog()
    disp = _FakeDispatcher()
    # No observer alive: the terminal lands with no handoff.
    _emit_created(log, "child-1", "parent-1")
    _emit_suspended(log, "parent-1", SubtaskCompleted(subtask_id="child-1"))
    _emit_completed(log, "child-1")
    assert _subtask_completed(log, "parent-1") == []

    observer = ChildLifecycleObserver(event_log=log, dispatcher=disp)
    observer.stop()

    completed = _subtask_completed(log, "parent-1")
    assert [e.payload.subtask_id for e in completed] == ["child-1"]
    assert [(t, w.subtask_id) for t, w in disp.woken] == [
        ("parent-1", "child-1")
    ]


def test_recovery_skips_terminal_parent() -> None:
    """A cascade cancel leaves parent AND child terminal with no handoff —
    recovery must not append to a terminal stream (nothing could ever
    consume the wake)."""
    log = InMemoryEventLog()
    disp = _FakeDispatcher()
    _emit_created(log, "child-1", "parent-1")
    log.system_emit(
        task_id="parent-1",
        type="TaskCancelled",
        payload=TaskCancelledPayload(reason="user stop"),
        actor="test",
        origin="system",
    )
    log.system_emit(
        task_id="child-1",
        type="TaskCancelled",
        payload=TaskCancelledPayload(reason="user stop"),
        actor="test",
        origin="system",
    )

    observer = ChildLifecycleObserver(event_log=log, dispatcher=disp)
    observer.stop()

    assert _subtask_completed(log, "parent-1") == []
    assert disp.woken == []


# ---------------------------------------------------------------------------
# Idempotent wiring: one triple, one default observer
# ---------------------------------------------------------------------------


def test_wire_default_observers_is_idempotent_per_event_log() -> None:
    """N clients over one shared triple wire once: a repeat call is a no-op,
    and a child completion produces exactly one handoff."""
    dispatcher = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=dispatcher)
    stop_1 = wire_default_observers(log, dispatcher)
    stop_2 = wire_default_observers(log, dispatcher)
    assert stop_1 is stop_2

    _emit_created(log, "child-1", "parent-1")
    _emit_suspended(log, "parent-1", SubtaskCompleted(subtask_id="child-1"))
    _emit_completed(log, "child-1")

    assert len(_subtask_completed(log, "parent-1")) == 1
    stop_1()

    # ``stop`` clears the marker: a later wire call installs a fresh observer.
    stop_3 = wire_default_observers(log, dispatcher)
    assert stop_3 is not stop_1
    stop_3()


def test_duplicate_observers_still_single_handoff() -> None:
    """Even with two live observers on one log (a manually-built triple wired
    twice around the marker), the durable dedupe collapses the handoff to
    one ``SubtaskCompleted`` and one wake."""
    log = InMemoryEventLog()
    disp = _FakeDispatcher()
    obs_1 = ChildLifecycleObserver(event_log=log, dispatcher=disp)
    obs_2 = ChildLifecycleObserver(event_log=log, dispatcher=disp)
    try:
        _emit_created(log, "child-1", "parent-1")
        _emit_suspended(
            log, "parent-1", SubtaskCompleted(subtask_id="child-1")
        )
        _emit_completed(log, "child-1")
    finally:
        obs_1.stop()
        obs_2.stop()

    assert len(_subtask_completed(log, "parent-1")) == 1
    assert len(disp.woken) == 1
