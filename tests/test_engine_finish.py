"""End-to-end: a Task whose Policy returns finish on its first decide.

Verifies Acceptance criteria from issue 01:
* run_one_step drives the task to terminal
* core EventLog sequence is present (TaskCreated, TaskStarted, TaskSnapshot,
  TaskCompleted)
* TaskSnapshot.state_ref body deserializes equal to runtime task state
* fold(event_log, content_store, task_id) returns a Task byte-equal to the
  runtime task
"""

from __future__ import annotations

from noeta.testing.composer import trivial_three_segment
from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.policies.stub import StubFinishPolicy
from noeta.protocols.task import Task
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)


def _start_task(*, goal: str = "say hello", answer: str = "hello") -> tuple[
    Task, Engine, InMemoryEventLog, InMemoryContentStore, str
]:
    content_store = InMemoryContentStore()
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)

    policy = StubFinishPolicy(answer=answer)
    composer = trivial_three_segment(content_store)
    engine = Engine(
        event_log=event_log,
        content_store=content_store,
        composer=composer,
        policy=policy,
    )

    task = engine.create_task(goal=goal, policy_name="stub_finish")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w-test")
    assert lease is not None

    return task, engine, event_log, content_store, lease.lease_id


def test_run_one_step_drives_finish_policy_to_terminal() -> None:
    task, engine, _log, _cs, lease_id = _start_task(answer="hello")

    result = engine.run_one_step(task, lease_id=lease_id)

    assert result.status == "terminal"


def test_event_sequence_contains_core_four_events_in_order() -> None:
    task, engine, event_log, _cs, lease_id = _start_task()

    engine.run_one_step(task, lease_id=lease_id)

    types = [e.type for e in event_log.read(task.task_id)]
    # Core required events must appear in this relative order.
    required = ["TaskCreated", "TaskStarted", "TaskSnapshot", "TaskCompleted"]
    positions = [types.index(t) for t in required]
    assert positions == sorted(positions), (
        f"required events out of order: {types}"
    )
    # And there's exactly one of each.
    for t in required:
        assert types.count(t) == 1, f"expected exactly one {t}, got {types}"


def test_task_completed_carries_finish_answer() -> None:
    task, engine, event_log, _cs, lease_id = _start_task(answer="bye")

    engine.run_one_step(task, lease_id=lease_id)

    completed = [
        e for e in event_log.read(task.task_id) if e.type == "TaskCompleted"
    ]
    assert len(completed) == 1
    assert completed[0].payload.answer == "bye"


def test_task_snapshot_body_deserializes_equal_to_runtime_state() -> None:
    task, engine, event_log, content_store, lease_id = _start_task()

    finished_task = engine.run_one_step(task, lease_id=lease_id)

    snap = event_log.find_latest_snapshot(task.task_id)
    assert snap is not None
    from noeta.core.snapshot import deserialize_task_state

    body = content_store.get(snap.payload.state_ref)
    state = deserialize_task_state(body)

    assert state == finished_task.state_dict()


def test_fold_rebuilds_task_byte_equal_to_runtime() -> None:
    task, engine, event_log, content_store, lease_id = _start_task(answer="hi")
    finished = engine.run_one_step(task, lease_id=lease_id)

    rebuilt = fold(event_log, content_store, task.task_id)

    assert rebuilt == finished


def test_fold_without_snapshot_acceleration_also_byte_equal() -> None:
    """Even when we ignore the snapshot and scan the full event tail,
    fold must produce identical state. This guards the rule that snapshots
    are an optimisation, not a source of truth."""
    task, engine, event_log, content_store, lease_id = _start_task(answer="hi")
    finished = engine.run_one_step(task, lease_id=lease_id)

    rebuilt = fold(
        event_log, content_store, task.task_id, ignore_snapshots=True
    )

    assert rebuilt == finished
