"""Focused tests for ``StopHandle`` + ``subscribe_with_stop`` (issue E / C4).

Pins idempotency of the helper itself plus the four observer classes
that now delegate ``stop()`` to a :class:`StopHandle`. The exactly-
once invariant lives in the helper; multiple ``stop()`` calls must
not invoke the underlying :data:`Unsubscribe` more than once.
"""

from __future__ import annotations

import threading

from noeta.core.observers import ChildLifecycleObserver
from noeta.observers.audit import AuditObserver
from noeta.observers.metrics import MetricsObserver
from noeta.observers.fanout import EnvelopeBroadcaster, EventFanout
from noeta.protocols.event_log import (
    StopHandle,
    Subscriber,
    Unsubscribe,
    subscribe_with_stop,
)
from noeta.protocols.events import EventEnvelope
from noeta.storage.memory import InMemoryDispatcher, InMemoryEventLog


# ---------------------------------------------------------------------------
# StopHandle directly — exactly-once semantics
# ---------------------------------------------------------------------------


def test_stop_handle_calls_unsubscribe_exactly_once() -> None:
    calls = 0

    def fake_unsubscribe() -> None:
        nonlocal calls
        calls += 1

    handle = StopHandle(fake_unsubscribe)
    assert handle.stopped is False
    handle.stop()
    assert handle.stopped is True
    assert calls == 1
    # Repeat — still exactly one
    handle.stop()
    handle.stop()
    handle.stop()
    assert calls == 1
    assert handle.stopped is True


class _FakeSubscriber:
    """Structurally satisfies :class:`EventLogSubscriber` for tests
    without dragging the InMemory adapter into the type seam."""

    def __init__(self) -> None:
        self.subscribed: list[Subscriber] = []
        self.unsubscribed = 0

    def subscribe(self, callback: Subscriber) -> Unsubscribe:
        self.subscribed.append(callback)

        def _unsub() -> None:
            self.unsubscribed += 1

        return _unsub


def test_subscribe_with_stop_threads_callback_through_subscriber() -> None:
    """Adapter helper must call ``subscriber.subscribe(callback)`` and
    wrap the returned ``Unsubscribe`` in a ``StopHandle``."""
    fake = _FakeSubscriber()

    def my_callback(env: EventEnvelope) -> None:  # noqa: ARG001
        pass

    handle = subscribe_with_stop(fake, my_callback)
    assert fake.subscribed == [my_callback]
    assert fake.unsubscribed == 0
    handle.stop()
    assert fake.unsubscribed == 1
    handle.stop()  # idempotent
    assert fake.unsubscribed == 1


# ---------------------------------------------------------------------------
# Observer-level idempotency — each Observer.stop() exactly-once
# ---------------------------------------------------------------------------


def _make_event() -> EventEnvelope:
    return EventEnvelope(
        id="e1",
        task_id="t1",
        seq=0,
        type="TaskCreated",
        schema_version=1,
        occurred_at=0.0,
        actor="t",
        trace_id="tr",
        correlation_id="c",
        causation_id=None,
        payload={},
        origin="engine",
    )


def test_audit_observer_stop_is_idempotent() -> None:
    """rev2 B2: after stop, AuditObserver's sink must not receive
    further records (idempotent + actually un-subscribed)."""
    from noeta.observers.audit import AuditRecord

    log = InMemoryEventLog()
    captured: list[AuditRecord] = []

    def sink(record: AuditRecord) -> None:
        captured.append(record)

    obs = AuditObserver(event_log=log, sink=sink)
    log.system_emit(
        task_id="t1", type="TaskCreated", payload={},
        actor="engine", origin="engine",
    )
    assert len(captured) == 1  # baseline

    obs.stop()
    obs.stop()  # must not raise; idempotent
    log.system_emit(
        task_id="t1", type="TaskStarted", payload={},
        actor="engine", origin="engine",
    )
    log.system_emit(
        task_id="t1", type="TaskCompleted", payload={"answer": "x"},
        actor="engine", origin="engine",
    )
    # No new records — observer is genuinely unsubscribed.
    assert len(captured) == 1


def test_metrics_observer_stop_is_idempotent() -> None:
    log = InMemoryEventLog()
    obs = MetricsObserver(event_log=log)
    obs.stop()
    obs.stop()  # must not raise
    # After stop, snapshot stays empty even when events emit
    log.system_emit(
        task_id="t1", type="TaskCreated", payload={},
        actor="engine", origin="engine",
    )
    snap = obs.snapshot()
    assert snap.total_events == 0


def test_event_fanout_stop_is_idempotent() -> None:
    log = InMemoryEventLog()
    bc = EnvelopeBroadcaster()
    obs = EventFanout(event_log=log, broadcaster=bc)
    obs.stop()
    obs.stop()  # must not raise
    # Confirm no further events reach the broadcaster
    sub = bc.subscribe()
    log.system_emit(
        task_id="t1", type="TaskCreated", payload={},
        actor="engine", origin="engine",
    )
    assert sub.get(timeout=0.05) is None
    sub.close()
    bc.close()


def test_child_lifecycle_observer_stop_is_idempotent() -> None:
    """rev2 B2: after stop, child TaskCreated events must NOT cause
    dispatcher.enqueue — the observer is truly unsubscribed."""
    from noeta.protocols.events import TaskCreatedPayload

    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    obs = ChildLifecycleObserver(event_log=log, dispatcher=disp)

    # Baseline: emit a child TaskCreated while observer is live; the
    # observer should have enqueued the child so a subsequent lease
    # picks it up.
    log.system_emit(
        task_id="child-1",
        type="TaskCreated",
        payload=TaskCreatedPayload(
            goal="g",
            policy_name="scripted",
            agent_name="a",
            parent_task_id="parent-1",
            inputs={},
        ),
        actor="engine",
        origin="engine",
    )
    lease = disp.lease(worker_id="w", lease_seconds=10.0)
    assert lease is not None and lease.task_id == "child-1"
    disp.release(lease.lease_id, next_state="terminal")

    obs.stop()
    obs.stop()  # must not raise; idempotent

    # After stop, a second child TaskCreated must NOT be enqueued —
    # observer is genuinely unsubscribed.
    log.system_emit(
        task_id="child-2",
        type="TaskCreated",
        payload=TaskCreatedPayload(
            goal="g2",
            policy_name="scripted",
            agent_name="a",
            parent_task_id="parent-1",
            inputs={},
        ),
        actor="engine",
        origin="engine",
    )
    assert disp.lease(worker_id="w", lease_seconds=10.0) is None


# ---------------------------------------------------------------------------
# Thread safety of repeated stop()
# ---------------------------------------------------------------------------


def test_stop_handle_concurrent_stops_truly_exactly_once_under_slow_unsubscribe() -> None:
    """rev2 B1: even when the underlying ``Unsubscribe`` is slow
    enough to widen the race window, concurrent ``stop()`` calls
    must produce exactly one unsubscribe.

    Without the lock (pre-rev2) this test failed with multiple
    unsubscribes — architect repro'd 20× with `time.sleep(0.005)`.
    """
    import time

    calls = 0

    def slow_unsubscribe() -> None:
        nonlocal calls
        # The sleep widens the gap between "first thread enters the
        # critical section" and "first thread returns from
        # unsubscribe()". With the lock in place the other threads
        # see ``_stopped=True`` and short-circuit; without it they
        # would re-enter and bump ``calls``.
        time.sleep(0.005)
        calls += 1

    handle = StopHandle(slow_unsubscribe)

    def stopper() -> None:
        handle.stop()

    threads = [threading.Thread(target=stopper) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert calls == 1, (
        f"StopHandle.stop() must call unsubscribe exactly once even "
        f"under contention; got {calls} calls"
    )
