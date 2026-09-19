"""A Dispatcher cap must not leave a Task terminal in the queue and alive in
the log.

``fail``'s ``max_fail_attempts`` cap and ``requeue_stale``'s ``reclaim_max``
cap both drop the dispatcher row to ``terminal`` and write nothing to the
EventLog — the Dispatcher holds no log handle. The row is then unleasable and
unwakeable while ``fold`` still reads the task as live, so a parent suspended
on a subtask barrier waits on a child that already ended: the handoff
``ChildLifecycleObserver`` makes fires off a terminal EVENT, and there is none.

These tests drive the real dispatchers (in-memory **and** sqlite) with a real
observer wired, and assert the thing an operator actually cares about — the
parent's group barrier fires — for both caps, plus the recovery pass that heals
a row capped while no worker was watching.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from noeta.core.observers import ChildLifecycleObserver
from noeta.protocols.events import (
    TaskCompletedPayload,
    TaskCreatedPayload,
    TaskSuspendedPayload,
)
from noeta.protocols.wake import SubtaskGroupCompleted, derive_group_id
from noeta.runtime.worker import WorkerLoop, recover_cap_terminal
from noeta.sdk.storage import open_storage_stack


PARENT = "cap-parent"
CHILD_A = "cap-child-a"
CHILD_B = "cap-child-b"


class _BoomEngine:
    """Engine whose step always raises — the in-process crash class the
    worker's backstop turns into ``fail(retryable=True)``."""

    def append_user_message(self, task: Any, **kwargs: Any) -> Any:
        return task

    def run_one_step(self, task: Any, **kwargs: Any) -> Any:
        raise RuntimeError("poison step")


class _Sink:
    def __init__(self) -> None:
        self.kinds: list[str] = []

    def __call__(self, event: Any) -> None:
        self.kinds.append(event.kind)


@pytest.fixture(params=["memory", "sqlite"])
def world(request: Any, tmp_path: Any) -> Iterator[Any]:
    """A real storage stack with the child-lifecycle observer wired, holding a
    parent suspended on a two-member group barrier and two unfinished children.

    Hand-emitted rather than driven through a session: the point is the
    dispatcher/EventLog split, and a hand-built tree keeps the cap the only
    moving part.
    """
    path = ":memory:" if request.param == "memory" else str(tmp_path / "cap.sqlite")
    event_log, content_store, dispatcher = open_storage_stack(path)
    observer = ChildLifecycleObserver(event_log=event_log, dispatcher=dispatcher)
    event_log.emit(
        task_id=PARENT,
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="delegate", policy_name="react"),
    )
    # The observer enqueues each child off its own ``TaskCreated``, exactly as
    # a spawned fan-out reaches the queue.
    for child_id in (CHILD_A, CHILD_B):
        event_log.emit(
            task_id=child_id,
            type="TaskCreated",
            payload=TaskCreatedPayload(
                goal=f"goal for {child_id}",
                policy_name="react",
                parent_task_id=PARENT,
                subtask_depth=1,
            ),
        )
    barrier = SubtaskGroupCompleted(
        group_id=derive_group_id((CHILD_A, CHILD_B)),
        subtask_ids=(CHILD_A, CHILD_B),
    )
    # Park the parent on the group barrier on BOTH sides: the recorded suspend
    # the observer counts arrivals against, and the row whose wake_on its wake
    # has to match.
    event_log.emit(
        task_id=PARENT,
        type="TaskSuspended",
        payload=TaskSuspendedPayload(
            reason="waiting_subtask_group", wake_on=barrier
        ),
    )
    dispatcher.enqueue(PARENT)
    parent_lease = dispatcher.lease(
        worker_id="seed", lease_seconds=60.0, task_id=PARENT
    )
    assert parent_lease is not None
    dispatcher.release(
        parent_lease.lease_id,
        next_state="suspended",
        wake_on=barrier,
        suspend_reason="waiting_subtask_group",
    )
    # Member A finishes normally: one of the two arrivals the barrier needs.
    event_log.system_emit(
        task_id=CHILD_A,
        type="TaskCompleted",
        payload=TaskCompletedPayload(answer="A done"),
        actor="test",
        origin="system",
    )
    a_lease = dispatcher.lease(
        worker_id="seed", lease_seconds=60.0, task_id=CHILD_A
    )
    assert a_lease is not None
    dispatcher.release(a_lease.lease_id, next_state="terminal")
    assert dispatcher.task_status(PARENT) == "suspended"  # barrier not yet full
    rt = SimpleNamespace(
        engine=_BoomEngine(),
        event_log=event_log,
        content_store=content_store,
        dispatcher=dispatcher,
    )
    try:
        yield rt
    finally:
        observer.stop()
        for closeable in (event_log, dispatcher):
            close = getattr(closeable, "close", None)
            if callable(close):
                close()


def _types(rt: Any, task_id: str) -> list[str]:
    return [e.type for e in rt.event_log.read(task_id)]


def _assert_barrier_fired(rt: Any) -> None:
    """The parent left its suspend: both members are recorded on its stream and
    its row is back in the ready queue."""
    completed = [
        e.payload.subtask_id
        for e in rt.event_log.read(PARENT)
        if e.type == "SubtaskCompleted"
    ]
    assert sorted(completed) == sorted([CHILD_A, CHILD_B])
    assert rt.dispatcher.task_status(PARENT) == "ready"


# ---------------------------------------------------------------------------
# 1. the fail cap
# ---------------------------------------------------------------------------


def test_fail_cap_writes_the_terminal_event_and_fires_the_parent_barrier(
    world: Any,
) -> None:
    """A child whose step raises until ``max_fail_attempts`` is reached ends
    terminal in the dispatcher AND terminal on its own stream, so its parent's
    group barrier fills instead of waiting forever."""
    sink = _Sink()
    loop = WorkerLoop(
        world,
        heartbeat_interval=0,
        stale_sweep_interval=0,
        reliability_sink=sink,
    )
    attempts = 0
    while world.dispatcher.task_status(CHILD_B) != "terminal":
        assert loop.tick() is True, "the poison child stopped being leasable"
        attempts += 1
        assert attempts <= 10, "the fail cap never landed"
    assert attempts == 3  # max_fail_attempts
    assert "TaskFailed" in _types(world, CHILD_B)
    assert "cap_terminal_reconciled" in sink.kinds
    _assert_barrier_fired(world)


def test_fail_cap_reconciliation_does_not_fire_before_the_cap(world: Any) -> None:
    """A retryable failure that merely requeues writes no terminal event — the
    reconciliation reads the row, it does not assume the cap."""
    loop = WorkerLoop(world, heartbeat_interval=0, stale_sweep_interval=0)
    assert loop.tick() is True  # first failure → requeued, not capped
    assert world.dispatcher.task_status(CHILD_B) == "ready"
    assert "TaskFailed" not in _types(world, CHILD_B)
    assert world.dispatcher.task_status(PARENT) == "suspended"


# ---------------------------------------------------------------------------
# 2. the stale-reclaim cap
# ---------------------------------------------------------------------------


def test_reclaim_cap_writes_the_terminal_event_and_fires_the_parent_barrier(
    world: Any,
) -> None:
    """A child whose worker dies every time is dropped to terminal by
    ``reclaim_max`` consecutive no-progress reclaims. ``requeue_stale`` omits
    the capped id from its result, so the sweep reads the row of every id it
    stopped requeueing and writes the missing terminal event."""
    sink = _Sink()
    now = {"t": 100.0}
    loop = WorkerLoop(
        world,
        heartbeat_interval=0,
        stale_sweep_interval=10.0,
        clock=lambda: now["t"],
        reliability_sink=sink,
    )
    for _ in range(4):  # reclaim_max defaults to 3; the 3rd sweep caps
        lease = world.dispatcher.lease(
            worker_id="dying-worker", lease_seconds=0.001, task_id=CHILD_B
        )
        if lease is None:
            break  # the cap already landed
        time.sleep(0.03)  # let the tiny deadline pass, write nothing
        now["t"] += 11.0
        assert loop.maybe_sweep() is True
    assert world.dispatcher.task_status(CHILD_B) == "terminal"
    assert "TaskFailed" in _types(world, CHILD_B)
    assert "cap_terminal_reconciled" in sink.kinds
    _assert_barrier_fired(world)


# ---------------------------------------------------------------------------
# 3. the recovery pass
# ---------------------------------------------------------------------------


def _cap_out_of_band(rt: Any, task_id: str) -> None:
    """Hit the fail cap with no worker watching — what a process dying between
    the cap and the terminal write leaves behind."""
    for _ in range(3):  # max_fail_attempts defaults to 3
        lease = rt.dispatcher.lease(
            worker_id="ghost", lease_seconds=60.0, task_id=task_id
        )
        assert lease is not None
        rt.dispatcher.fail(lease.lease_id, retryable=True, reason="boom")
    assert rt.dispatcher.task_status(task_id) == "terminal"
    assert "TaskFailed" not in _types(rt, task_id)


def test_recovery_pass_heals_a_pre_existing_cap_terminal_row(world: Any) -> None:
    """A row capped while nobody watched converges the next time a worker
    starts: the terminal event lands and the stranded parent wakes."""
    _cap_out_of_band(world, CHILD_B)
    assert recover_cap_terminal(world) == [CHILD_B]
    assert "TaskFailed" in _types(world, CHILD_B)
    _assert_barrier_fired(world)


def test_recovery_pass_is_idempotent(world: Any) -> None:
    """Re-running it (a restart, a second worker in the pool) converges instead
    of stacking a second terminal event — and a healthy store is untouched."""
    _cap_out_of_band(world, CHILD_B)
    assert recover_cap_terminal(world) == [CHILD_B]
    before = _types(world, CHILD_B)
    assert recover_cap_terminal(world) == []
    assert _types(world, CHILD_B) == before
    assert before.count("TaskFailed") == 1
    # The live child A (terminal with its own event) and the parent are left
    # exactly as they were.
    assert _types(world, CHILD_A).count("TaskCompleted") == 1


def test_run_forever_runs_the_recovery_pass_once(world: Any) -> None:
    """The daemon heals what the previous process left behind before it starts
    handing out leases."""
    _cap_out_of_band(world, CHILD_B)
    sink = _Sink()
    loop = WorkerLoop(
        world,
        heartbeat_interval=0,
        stale_sweep_interval=0,
        timer_poll_interval=0,
        reliability_sink=sink,
    )
    loop.stop()  # the recovery pass runs before the first iteration
    loop.run_forever()
    assert "TaskFailed" in _types(world, CHILD_B)
    assert sink.kinds.count("cap_terminal_reconciled") == 1
    _assert_barrier_fired(world)
    # One-shot per loop: a later call is a no-op.
    assert loop.recover_cap_terminal() == []
