"""InMemoryEventLog: emit / read / find_latest_snapshot / subscribe.

Phase 0 only checks the basic shape; full concurrency enforcement is
issue 06.
"""

from __future__ import annotations

from noeta.protocols.events import (
    EventEnvelope,
    TaskCreatedPayload,
    TaskSnapshotPayload,
)
from noeta.protocols.values import ContentRef
from noeta.storage.memory import InMemoryEventLog


def test_emit_assigns_monotonic_seq_starting_from_zero() -> None:
    log = InMemoryEventLog()
    e1 = log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="stub"),
    )
    e2 = log.emit(task_id="t1", type="TaskStarted", payload={})

    assert e1.seq == 0
    assert e2.seq == 1


def test_read_returns_appended_events_in_order() -> None:
    log = InMemoryEventLog()
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    log.emit(task_id="t1", type="TaskStarted", payload={})

    events = log.read("t1")

    assert [e.type for e in events] == ["TaskCreated", "TaskStarted"]


def test_read_after_seq_returns_only_later_events() -> None:
    log = InMemoryEventLog()
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    log.emit(task_id="t1", type="TaskStarted", payload={})
    log.emit(task_id="t1", type="TaskCompleted", payload={"answer": "x"})

    tail = log.read("t1", after_seq=0)

    assert [e.type for e in tail] == ["TaskStarted", "TaskCompleted"]


def test_streams_are_isolated_by_task_id() -> None:
    log = InMemoryEventLog()
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="a", policy_name="p"),
    )
    log.emit(
        task_id="t2",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="b", policy_name="p"),
    )

    assert len(log.read("t1")) == 1
    assert len(log.read("t2")) == 1


def test_find_latest_snapshot_returns_most_recent_snapshot_envelope() -> None:
    log = InMemoryEventLog()
    ref = ContentRef(hash="a" * 64, size=10, media_type="application/json")
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    log.emit(task_id="t1", type="TaskSnapshot", payload=TaskSnapshotPayload(state_ref=ref))
    log.emit(task_id="t1", type="TaskStarted", payload={})
    ref2 = ContentRef(hash="b" * 64, size=12, media_type="application/json")
    log.emit(task_id="t1", type="TaskSnapshot", payload=TaskSnapshotPayload(state_ref=ref2))

    snap = log.find_latest_snapshot("t1")

    assert snap is not None
    assert snap.payload.state_ref == ref2


def test_find_latest_snapshot_returns_none_when_no_snapshot_present() -> None:
    log = InMemoryEventLog()
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )

    assert log.find_latest_snapshot("t1") is None


def test_subscribe_invokes_callback_for_each_appended_envelope() -> None:
    log = InMemoryEventLog()
    seen: list[str] = []
    log.subscribe(lambda ev: seen.append(ev.type))

    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    log.emit(task_id="t1", type="TaskStarted", payload={})

    assert seen == ["TaskCreated", "TaskStarted"]


def test_subscriber_exception_does_not_break_writer() -> None:
    log = InMemoryEventLog()

    def boom(_: EventEnvelope) -> None:
        raise RuntimeError("observer crashed")

    log.subscribe(boom)

    # Must not raise.
    ev = log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    assert ev.seq == 0
    assert len(log.read("t1")) == 1
