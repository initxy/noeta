"""Engine end-to-end smoke against ``SqliteEventLog`` (issue 15).

The contract suite proves the adapter satisfies each L0 Protocol
behaviour in isolation. This module wires the real Engine + Dispatcher
+ ChildLifecycleObserver stack on top of SqliteEventLog and runs a
representative end-to-end loop so we catch any integration-level
mismatch between adapters (idempotency keys flowing through the engine,
observer-driven cross-stream writes triggered by the dispatcher, etc.).

Two scenarios:

* ``test_minimal_loop_*`` runs a single-step ``TaskCreated → finish``
  flow and asserts Sqlite produces the same event-type / payload /
  origin sequence as InMemory under identical inputs.
* ``test_parent_child_subtask_loop_*`` runs the full spawn → child
  → wake parent → finish loop. This is the canonical exercise for
  ``ChildLifecycleObserver`` writing ``SubtaskCompleted`` to the
  parent stream from inside a subscriber callback — the path that
  ``test_subscriber_can_re_emit_inline_in_callback`` covers at the
  adapter level, now verified end-to-end with the real observer.
"""

from __future__ import annotations

from typing import Any

import pytest

from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.wiring import wire_default_observers
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import FinishDecision, SpawnSubtaskDecision
from noeta.protocols.events import EventEnvelope
from noeta.protocols.wake import SubtaskCompleted
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.storage.sqlite.eventlog import SqliteEventLog
from noeta.testing.composer import trivial_three_segment


_BACKENDS = ("memory", "sqlite")


def _make_runtime(backend: str):
    disp = InMemoryDispatcher()
    if backend == "memory":
        log: Any = InMemoryEventLog(lease_validator=disp)
    else:
        log = SqliteEventLog(":memory:", lease_validator=disp)
    cs = InMemoryContentStore()
    wire_default_observers(log, disp)
    return log, cs, disp


def _engine(log, cs, policy) -> Engine:
    return Engine(
        event_log=log,
        content_store=cs,
        composer=trivial_three_segment(cs),
        policy=policy,
    )


def _shape(envelope: EventEnvelope) -> tuple[str, str, str]:
    """Compare across backends by stable fields only (mask id /
    occurred_at / trace_id which are factory-dependent)."""
    return (envelope.type, envelope.origin, envelope.actor)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_minimal_loop_event_sequence(backend) -> None:
    log, cs, disp = _make_runtime(backend)
    try:
        eng = _engine(log, cs, StubScriptedPolicy([FinishDecision(answer="ok")]))
        task = eng.create_task(goal="g", policy_name="scripted")
        disp.enqueue(task.task_id)
        lease = disp.lease(worker_id="w1")
        assert lease is not None
        result = eng.run_one_step(task, lease_id=lease.lease_id)
        assert result.status == "terminal"
        disp.release(lease.lease_id, next_state="terminal")

        types = [e.type for e in log.read(task.task_id)]
        assert types[0] == "TaskCreated"
        assert types[-1] == "TaskCompleted"
    finally:
        close = getattr(log, "close", None)
        if callable(close):
            close()


def test_minimal_loop_matches_in_memory_shape() -> None:
    """Same inputs, same event-type / origin / actor sequence across
    the two adapters."""

    def run(backend: str) -> list[tuple[str, str, str]]:
        log, cs, disp = _make_runtime(backend)
        try:
            eng = _engine(
                log, cs, StubScriptedPolicy([FinishDecision(answer="ok")])
            )
            task = eng.create_task(goal="g", policy_name="scripted")
            disp.enqueue(task.task_id)
            lease = disp.lease(worker_id="w1")
            assert lease is not None
            eng.run_one_step(task, lease_id=lease.lease_id)
            disp.release(lease.lease_id, next_state="terminal")
            return [_shape(e) for e in log.read(task.task_id)]
        finally:
            close = getattr(log, "close", None)
            if callable(close):
                close()

    assert run("memory") == run("sqlite")


@pytest.mark.parametrize("backend", _BACKENDS)
def test_parent_child_subtask_loop(backend) -> None:
    """Full spawn → child → wake parent → finish loop on SqliteEventLog.

    Verifies the observer-driven ``SubtaskCompleted`` cross-stream
    write fires correctly through ``log.subscribe`` → callback →
    ``log.system_emit`` even when the storage layer is Sqlite.
    """
    log, cs, disp = _make_runtime(backend)
    try:
        parent_engine = _engine(
            log,
            cs,
            StubScriptedPolicy(
                [
                    SpawnSubtaskDecision(
                        agent_name="child_agent", goal="do thing", inputs={}
                    ),
                    FinishDecision(answer="parent done"),
                ]
            ),
        )
        child_engine = _engine(
            log, cs, StubScriptedPolicy([FinishDecision(answer="child done")])
        )

        parent = parent_engine.create_task(goal="parent", policy_name="scripted")
        disp.enqueue(parent.task_id)

        p_lease_1 = disp.lease(worker_id="w1")
        assert p_lease_1 is not None and p_lease_1.task_id == parent.task_id
        parent = parent_engine.run_one_step(parent, lease_id=p_lease_1.lease_id)
        assert parent.status == "suspended"
        disp.release(
            p_lease_1.lease_id, next_state="suspended", wake_on=parent.wake_on
        )

        c_lease = disp.lease(worker_id="w1")
        assert c_lease is not None
        child_id = c_lease.task_id

        child = fold(log, cs, child_id)
        child = child_engine.run_one_step(child, lease_id=c_lease.lease_id)
        assert child.status == "terminal"
        disp.release(c_lease.lease_id, next_state="terminal")

        # The observer should have system_emit'd SubtaskCompleted to
        # the parent stream and woken the dispatcher.
        p_lease_2 = disp.lease(worker_id="w1")
        assert p_lease_2 is not None and p_lease_2.task_id == parent.task_id

        parent = fold(log, cs, parent.task_id)
        parent_engine.note_woken(
            parent,
            lease_id=p_lease_2.lease_id,
            wake_event=SubtaskCompleted(subtask_id=child_id),
        )
        parent = parent_engine.run_one_step(
            parent, lease_id=p_lease_2.lease_id
        )
        assert parent.status == "terminal"
        disp.release(p_lease_2.lease_id, next_state="terminal")

        parent_types = [e.type for e in log.read(parent.task_id)]
        for required in (
            "TaskCreated",
            "SubtaskSpawned",
            "SubtaskCompleted",
            "TaskWoken",
            "TaskCompleted",
        ):
            assert required in parent_types, parent_types

        # The cross-stream ``SubtaskCompleted`` event was system-emitted
        # from the ChildLifecycleObserver callback, so its origin is
        # the observer.
        st_comp = next(
            e for e in log.read(parent.task_id) if e.type == "SubtaskCompleted"
        )
        assert st_comp.origin == "observer"
        assert st_comp.payload.subtask_id == child_id
    finally:
        close = getattr(log, "close", None)
        if callable(close):
            close()
