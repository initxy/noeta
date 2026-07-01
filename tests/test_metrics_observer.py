"""MetricsObserver: per-type / per-task counters + snapshot isolation."""

from __future__ import annotations

import threading

from noeta.observers.metrics import MetricsObserver, MetricsSnapshot
from noeta.protocols.events import TaskCreatedPayload
from noeta.storage.memory import InMemoryEventLog


def _emit_task_created(log: InMemoryEventLog, task_id: str) -> None:
    log.emit(
        task_id=task_id,
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )


def test_initial_snapshot_is_zero() -> None:
    log = InMemoryEventLog()
    obs = MetricsObserver(event_log=log)
    try:
        snap = obs.snapshot()
    finally:
        obs.stop()
    assert snap == MetricsSnapshot(
        by_type={}, by_task_type={}, total_events=0
    )


def test_single_emit_increments_all_three_views() -> None:
    log = InMemoryEventLog()
    obs = MetricsObserver(event_log=log)
    try:
        _emit_task_created(log, "t1")
        snap = obs.snapshot()
    finally:
        obs.stop()
    assert snap.by_type == {"TaskCreated": 1}
    assert snap.by_task_type == {("t1", "TaskCreated"): 1}
    assert snap.total_events == 1


def test_multi_task_multi_type_accumulation() -> None:
    log = InMemoryEventLog()
    obs = MetricsObserver(event_log=log)
    try:
        _emit_task_created(log, "t1")
        _emit_task_created(log, "t1")
        log.emit(task_id="t1", type="TaskStarted", payload={})
        _emit_task_created(log, "t2")
        snap = obs.snapshot()
    finally:
        obs.stop()

    assert snap.by_type == {"TaskCreated": 3, "TaskStarted": 1}
    assert snap.by_task_type == {
        ("t1", "TaskCreated"): 2,
        ("t1", "TaskStarted"): 1,
        ("t2", "TaskCreated"): 1,
    }
    assert snap.total_events == 4


def test_snapshot_is_defensive_copy() -> None:
    log = InMemoryEventLog()
    obs = MetricsObserver(event_log=log)
    try:
        _emit_task_created(log, "t1")
        snap_a = obs.snapshot()
        # Mutating the snapshot dict must not affect the observer.
        snap_a.by_type.clear()
        snap_a.by_task_type.clear()
        _emit_task_created(log, "t1")
        snap_b = obs.snapshot()
    finally:
        obs.stop()
    # Observer's internal state survived snap_a mutation.
    assert snap_b.by_type == {"TaskCreated": 2}
    assert snap_b.total_events == 2


def test_stop_is_idempotent_and_stops_counting() -> None:
    log = InMemoryEventLog()
    obs = MetricsObserver(event_log=log)
    _emit_task_created(log, "t1")
    obs.stop()
    obs.stop()  # idempotent
    _emit_task_created(log, "t1")
    snap = obs.snapshot()
    assert snap.total_events == 1
    assert snap.by_type == {"TaskCreated": 1}


def test_thread_safe_under_concurrent_writes() -> None:
    """Issue 19 B1 stress: multiple writer threads emitting
    concurrently must produce the right total count and per-task
    counts. The EventLog ``_notify`` fires post-COMMIT outside the
    writer lock, so the observer's internal lock is the real
    serialiser."""
    log = InMemoryEventLog()
    obs = MetricsObserver(event_log=log)

    def worker(task_id: str, n: int) -> None:
        for _ in range(n):
            _emit_task_created(log, task_id)

    threads = [
        threading.Thread(target=worker, args=(f"t-{i}", 100)) for i in range(8)
    ]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        snap = obs.snapshot()
    finally:
        obs.stop()

    assert all(not t.is_alive() for t in threads)
    assert snap.total_events == 800
    assert snap.by_type == {"TaskCreated": 800}
    assert snap.by_task_type == {
        (f"t-{i}", "TaskCreated"): 100 for i in range(8)
    }


def test_multiple_observers_independent_state() -> None:
    log = InMemoryEventLog()
    obs1 = MetricsObserver(event_log=log)
    obs2 = MetricsObserver(event_log=log)
    try:
        _emit_task_created(log, "t1")
        obs1.stop()                       # obs1 stops here
        _emit_task_created(log, "t1")
    finally:
        obs2.stop()
    assert obs1.snapshot().total_events == 1
    assert obs2.snapshot().total_events == 2
