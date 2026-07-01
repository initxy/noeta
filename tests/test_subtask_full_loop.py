"""Phase 0's most important demo: parent → child → parent 5-step loop.

This is the integration test for issue 03. It exercises the entire
spawn_subtask + wake machinery end-to-end:

1. Parent runs one step → ``spawn_subtask`` → suspends on
   ``SubtaskCompleted(child_id)``.
2. Worker leases the child.
3. Child runs one step → ``finish``.
4. Child's terminal triggers ``SubtaskCompleted`` on the parent stream
   plus ``dispatcher.wake(parent_id, ...)``.
5. Parent is re-leased, runs one step → ``finish``.

The test asserts:

* Both Tasks reach ``terminal``.
* Parent's EventLog contains the canonical sequence including
  ``TaskWoken`` between the suspend and the re-step.
* Child's stream is independent and carries only its own events.
* ``fold(parent)`` exposes the child's outcome via the folded
  ``governance.subtask_results`` slice (writer = Engine via fold,
  per the "Engine-folded GovernanceState" clause).
* Subtask-fail variant: parent's folded ``subtask_results`` carries a
  ``failed`` SubtaskResult; the parent Policy still finishes normally.
"""

from __future__ import annotations

from typing import Any

from noeta.testing.composer import trivial_three_segment
from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.wiring import wire_default_observers
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import (
    FailDecision,
    FinishDecision,
    SpawnSubtaskDecision,
)
from noeta.protocols.task import Task
from noeta.protocols.wake import SubtaskCompleted, SubtaskResult
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)


def _make_runtime() -> tuple[InMemoryEventLog, InMemoryContentStore, InMemoryDispatcher]:
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    wire_default_observers(log, disp)
    return (log, InMemoryContentStore(), disp)


def _engine_for(
    *,
    log: InMemoryEventLog,
    cs: InMemoryContentStore,
    disp: InMemoryDispatcher,  # noqa: ARG001
    policy: Any,
) -> Engine:
    return Engine(
        event_log=log,
        content_store=cs,
        composer=trivial_three_segment(cs),
        policy=policy,
    )


def _run_full_loop(
    *,
    child_decision: Any,
    parent_finish_answer: str = "parent done",
) -> tuple[Task, str, InMemoryEventLog, InMemoryContentStore, InMemoryDispatcher]:
    log, cs, disp = _make_runtime()

    parent_engine = _engine_for(
        log=log,
        cs=cs,
        disp=disp,
        policy=StubScriptedPolicy(
            [
                SpawnSubtaskDecision(
                    agent_name="child_agent",
                    goal="do thing",
                    inputs={},
                ),
                FinishDecision(answer=parent_finish_answer),
            ]
        ),
    )
    child_engine = _engine_for(
        log=log,
        cs=cs,
        disp=disp,
        policy=StubScriptedPolicy([child_decision]),
    )

    parent = parent_engine.create_task(goal="parent", policy_name="scripted")
    disp.enqueue(parent.task_id)

    # Step 1: lease + run parent → suspended on subtask.
    p_lease_1 = disp.lease(worker_id="w1")
    assert p_lease_1 is not None and p_lease_1.task_id == parent.task_id
    parent = parent_engine.run_one_step(parent, lease_id=p_lease_1.lease_id)
    assert parent.status == "suspended"
    disp.release(
        p_lease_1.lease_id,
        next_state="suspended",
        wake_on=parent.wake_on,
    )

    # Step 2: child is next ready.
    c_lease = disp.lease(worker_id="w1")
    assert c_lease is not None
    child_id = c_lease.task_id
    assert child_id != parent.task_id

    # Step 3: run child to terminal. The child's TaskCreated lives only on
    # its own stream; we fold to rehydrate the Task object before running.
    child = fold(log, cs, child_id)
    child = child_engine.run_one_step(child, lease_id=c_lease.lease_id)
    assert child.status == "terminal"
    disp.release(c_lease.lease_id, next_state="terminal")

    # Step 4: parent should now be re-queued by the child-completion
    # observer that fired inside the child engine's _finish/_fail.
    p_lease_2 = disp.lease(worker_id="w1")
    assert p_lease_2 is not None and p_lease_2.task_id == parent.task_id

    # Step 5: rehydrate parent (so subtask_results is folded in), append
    # TaskWoken, and run the next decision.
    parent = fold(log, cs, parent.task_id)
    parent_engine.note_woken(
        parent,
        lease_id=p_lease_2.lease_id,
        wake_event=SubtaskCompleted(subtask_id=child_id),
    )
    parent = parent_engine.run_one_step(parent, lease_id=p_lease_2.lease_id)
    assert parent.status == "terminal"
    disp.release(p_lease_2.lease_id, next_state="terminal")

    return parent, child_id, log, cs, disp


def test_full_loop_reaches_terminal_for_both_tasks() -> None:
    parent, child_id, log, _cs, _disp = _run_full_loop(
        child_decision=FinishDecision(answer="child done")
    )
    assert parent.status == "terminal"
    child_events = log.read(child_id)
    assert child_events[-1].type == "TaskCompleted"


def test_parent_eventlog_has_full_sequence_including_task_woken() -> None:
    parent, _child_id, log, _cs, _disp = _run_full_loop(
        child_decision=FinishDecision(answer="child done")
    )
    types = [e.type for e in log.read(parent.task_id)]
    required = [
        "TaskCreated",
        "TaskStarted",
        "SubtaskSpawned",
        "TaskSnapshot",
        "TaskSuspended",
        "SubtaskCompleted",
        "TaskWoken",
        "TaskCompleted",
    ]
    positions = [types.index(t) for t in required]
    assert positions == sorted(positions), f"out of order: {types}"


def test_child_stream_is_independent_of_parent_stream() -> None:
    parent, child_id, log, _cs, _disp = _run_full_loop(
        child_decision=FinishDecision(answer="child done")
    )
    parent_types = [e.type for e in log.read(parent.task_id)]
    child_types = [e.type for e in log.read(child_id)]
    # The child's TaskCreated never appears on the parent stream — only
    # the SubtaskSpawned / SubtaskCompleted bookkeeping does.
    assert "TaskCreated" in parent_types  # parent's own
    assert parent_types.count("TaskCreated") == 1
    assert child_types[0] == "TaskCreated"
    # Child stream has no SubtaskSpawned / TaskSuspended events of its own.
    assert "SubtaskSpawned" not in child_types
    assert "TaskSuspended" not in child_types


def test_fold_parent_subtask_results_carries_completed_child_outcome() -> None:
    parent, child_id, log, cs, _disp = _run_full_loop(
        child_decision=FinishDecision(answer="child done"),
        parent_finish_answer="parent done",
    )
    rebuilt = fold(log, cs, parent.task_id)
    assert rebuilt.governance.subtask_results == [
        SubtaskResult(status="completed", output="child done"),
    ]
    # Sanity: the child id is matchable via the SubtaskCompleted event too.
    completed = [
        e for e in log.read(parent.task_id) if e.type == "SubtaskCompleted"
    ]
    assert completed[0].payload.subtask_id == child_id


def test_wake_arriving_before_parent_release_requeues_parent_on_release() -> None:
    """End-to-end race test: SubtaskCompleted wake arrives at the dispatcher
    while the parent is still leased (between TaskSuspended-append and
    lease release). The dispatcher must remember it and re-queue the
    parent at release time so no deadlock occurs."""
    log, cs, disp = _make_runtime()

    parent_engine = _engine_for(
        log=log,
        cs=cs,
        disp=disp,
        policy=StubScriptedPolicy(
            [
                SpawnSubtaskDecision(
                    agent_name="child_agent", goal="thing", inputs={}
                ),
                FinishDecision(answer="done"),
            ]
        ),
    )

    parent = parent_engine.create_task(goal="parent", policy_name="scripted")
    disp.enqueue(parent.task_id)
    p_lease_1 = disp.lease(worker_id="w1")
    assert p_lease_1 is not None
    parent = parent_engine.run_one_step(parent, lease_id=p_lease_1.lease_id)
    assert parent.status == "suspended"

    # Simulate the race: the wake event arrives at the dispatcher BEFORE
    # the parent worker has called release(). The dispatcher must queue
    # it as pending so the subsequent release immediately re-queues.
    child_id = parent.wake_on.subtask_id
    accepted_now = disp.wake(
        parent.task_id, SubtaskCompleted(subtask_id=child_id)
    )
    assert accepted_now is False  # parent still "leased" from disp's POV

    disp.release(
        p_lease_1.lease_id,
        next_state="suspended",
        wake_on=parent.wake_on,
    )

    # Drain ready tasks: both the child (enqueued by spawn_subtask) and
    # the parent (re-queued via the pending wake event) must be leasable
    # without any second wake call. No deadlock.
    leased_ids = set()
    for _ in range(2):
        lease = disp.lease(worker_id="w1")
        assert lease is not None
        leased_ids.add(lease.task_id)
    assert parent.task_id in leased_ids
    assert child_id in leased_ids


def test_failed_child_yields_failed_subtask_result_and_parent_still_finishes() -> None:
    parent, _child_id, log, cs, _disp = _run_full_loop(
        child_decision=FailDecision(reason="kaboom"),
        parent_finish_answer="parent recovered",
    )
    assert parent.status == "terminal"
    rebuilt = fold(log, cs, parent.task_id)
    assert rebuilt.governance.subtask_results == [
        SubtaskResult(status="failed", error="kaboom"),
    ]
    # Parent still ends with TaskCompleted (its Policy chose to finish).
    parent_types = [e.type for e in log.read(parent.task_id)]
    assert "TaskCompleted" in parent_types
