"""Engine ``spawn_subtask`` branch: parent emits the 7-step sequence and
the child receives an independent ``TaskCreated`` on its own stream.

Issue 03 acceptance (parent half):

* ``SubtaskSpawned`` is appended to the parent stream.
* A ``TaskCreated`` event is appended to the *child* stream (independent),
  carrying ``parent_task_id`` = the parent's task_id.
* Dispatcher is told to ``enqueue`` the child task_id.
* ``TaskSnapshot`` is appended to the parent stream before the suspend.
* ``TaskSuspended`` is appended to the parent stream with
  ``wake_on = SubtaskCompleted(subtask_id=<child>)``.
* The Engine releases its lease (returns parent with ``status='suspended'``)
  and the dispatcher reflects that.

The child's own run loop is exercised by the full-loop integration test
in ``test_subtask_full_loop.py``.
"""

from __future__ import annotations

from typing import Any

from noeta.testing.composer import trivial_three_segment
from noeta.core.engine import Engine
from noeta.core.wiring import wire_default_observers
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import FinishDecision, SpawnSubtaskDecision
from noeta.protocols.wake import SubtaskCompleted
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)


def _build_parent_engine() -> tuple[
    Engine,
    InMemoryEventLog,
    InMemoryContentStore,
    InMemoryDispatcher,
    str,
    Any,
]:
    content_store = InMemoryContentStore()
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    wire_default_observers(event_log, dispatcher)
    composer = trivial_three_segment(content_store)

    policy = StubScriptedPolicy(
        [
            SpawnSubtaskDecision(
                agent_name="child_agent",
                goal="do the small thing",
                inputs={"k": "v"},
            ),
            FinishDecision(answer="parent done"),
        ]
    )

    engine = Engine(
        event_log=event_log,
        content_store=content_store,
        composer=composer,
        policy=policy,
    )

    parent = engine.create_task(goal="parent", policy_name="scripted")
    dispatcher.enqueue(parent.task_id)
    lease = dispatcher.lease(worker_id="w-parent")
    assert lease is not None
    return engine, event_log, content_store, dispatcher, lease.lease_id, parent


def test_spawn_subtask_returns_parent_suspended() -> None:
    engine, _log, _cs, _disp, lease_id, parent = _build_parent_engine()

    result = engine.run_one_step(parent, lease_id=lease_id)

    assert result.status == "suspended"


def test_parent_stream_contains_subtask_spawned_snapshot_suspended_in_order() -> None:
    engine, log, _cs, _disp, lease_id, parent = _build_parent_engine()

    engine.run_one_step(parent, lease_id=lease_id)

    types = [e.type for e in log.read(parent.task_id)]
    # The full suspend sequence on the parent stream.
    required = [
        "TaskCreated",
        "TaskStarted",
        "SubtaskSpawned",
        "TaskSnapshot",
        "TaskSuspended",
    ]
    positions = [types.index(t) for t in required]
    assert positions == sorted(positions), f"out of order: {types}"


def test_subtask_spawned_payload_carries_child_id_and_spec() -> None:
    engine, log, _cs, _disp, lease_id, parent = _build_parent_engine()
    engine.run_one_step(parent, lease_id=lease_id)

    spawned = [
        e for e in log.read(parent.task_id) if e.type == "SubtaskSpawned"
    ]
    assert len(spawned) == 1
    payload = spawned[0].payload
    assert payload.subtask_id.startswith("task-")
    assert payload.agent_name == "child_agent"
    assert payload.goal == "do the small thing"
    assert payload.inputs == {"k": "v"}


def test_parent_suspend_wakes_on_subtask_completed_with_child_id() -> None:
    engine, log, _cs, _disp, lease_id, parent = _build_parent_engine()
    engine.run_one_step(parent, lease_id=lease_id)

    spawned = [
        e for e in log.read(parent.task_id) if e.type == "SubtaskSpawned"
    ][0]
    suspended = [
        e for e in log.read(parent.task_id) if e.type == "TaskSuspended"
    ][0]
    child_id = spawned.payload.subtask_id
    assert suspended.payload.wake_on == SubtaskCompleted(subtask_id=child_id)
    assert suspended.payload.reason == "waiting_subtask"


def test_child_stream_has_independent_task_created_with_parent_id() -> None:
    engine, log, _cs, _disp, lease_id, parent = _build_parent_engine()
    engine.run_one_step(parent, lease_id=lease_id)

    spawned = [
        e for e in log.read(parent.task_id) if e.type == "SubtaskSpawned"
    ][0]
    child_id = spawned.payload.subtask_id

    # Child stream must exist and start with TaskCreated whose parent_task_id
    # points to the parent. The parent stream must NOT carry the child's
    # TaskCreated event.
    child_events = log.read(child_id)
    assert child_events, "child stream is empty"
    assert child_events[0].type == "TaskCreated"
    assert child_events[0].payload.parent_task_id == parent.task_id
    assert child_events[0].payload.agent_name == "child_agent"
    assert child_events[0].payload.goal == "do the small thing"
    assert child_events[0].payload.inputs == {"k": "v"}

    parent_types = [e.type for e in log.read(parent.task_id)]
    # The TaskCreated on the parent stream is exactly one (the parent's own).
    assert parent_types.count("TaskCreated") == 1


def test_child_is_enqueued_in_dispatcher_ready() -> None:
    engine, log, _cs, disp, lease_id, parent = _build_parent_engine()
    engine.run_one_step(parent, lease_id=lease_id)

    spawned = [
        e for e in log.read(parent.task_id) if e.type == "SubtaskSpawned"
    ][0]
    child_id = spawned.payload.subtask_id

    # The next thing a worker leases must be the child.
    child_lease = disp.lease(worker_id="w-child")
    assert child_lease is not None
    assert child_lease.task_id == child_id
