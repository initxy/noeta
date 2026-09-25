"""The child-observer recovery pass reads whole streams only for candidates.

``ChildLifecycleObserver`` runs a recovery pass at construction (every
``Client`` builds one): any foreground child that reached terminal while no
observer was alive gets its missing ``SubtaskCompleted`` + wake. The pass used
to read every task stream in full, so a store with hundreds of finished tasks
made every construction pay for the whole log. It now reads a short tail per
stream to find parents still waiting on a sub-agent, and reads whole streams
only for those parents' finished children.

The store below holds 300 finished root tasks (one-shot and parked
conversations, each longer than the tail window) plus a handful of pending
handoffs; a counting wrapper pins the reads.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from noeta.core import observers as observers_mod
from noeta.core.observers import ChildLifecycleObserver
from noeta.protocols.events import (
    AgentBoundPayload,
    TaskCancelledPayload,
    TaskCompletedPayload,
    SubtaskCompletedPayload,
    TaskCreatedPayload,
    TaskFailedPayload,
    TaskStartedPayload,
    TaskSuspendedPayload,
    TaskWokenPayload,
)
from noeta.protocols.wake import (
    NEXT_GOAL_WAKE_HANDLE,
    HumanResponseReceived,
    SubtaskCompleted,
    SubtaskGroupCompleted,
    SubtaskResult,
    derive_group_id,
)
from noeta.sdk.storage import SqliteEventLog
from noeta.storage.memory import InMemoryEventLog

_ROOTS = 300
_FILLER_PER_ROOT = 3 * observers_mod._RECOVERY_TAIL


class _CountingLog:
    """Delegates to a real log, recording every ``read`` call."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.reads: list[tuple[str, Optional[int], int]] = []

    def read(self, task_id: str, *, after_seq: Optional[int] = None) -> list[Any]:
        out = self._inner.read(task_id, after_seq=after_seq)
        self.reads.append((task_id, after_seq, len(out)))
        return list(out)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _FakeDispatcher:
    def __init__(self) -> None:
        self.woken: list[tuple[str, Any]] = []

    def enqueue(self, task_id: str, *, parent_task_id: Any = None) -> None:
        return None

    def wake(self, task_id: str, wake_event: Any) -> bool:
        self.woken.append((task_id, wake_event))
        return True


def _emit(log: Any, task_id: str, type_: str, payload: Any) -> None:
    log.system_emit(
        task_id=task_id, type=type_, payload=payload, actor="test", origin="system"
    )


def _created(log: Any, task_id: str, parent: Optional[str] = None, **kw: Any) -> None:
    _emit(
        log,
        task_id,
        "TaskCreated",
        TaskCreatedPayload(goal="g", policy_name="react", parent_task_id=parent, **kw),
    )
    _emit(log, task_id, "TaskStarted", TaskStartedPayload(lease_id="l"))


def _filler(log: Any, task_id: str, n: int) -> None:
    for i in range(n):
        _emit(log, task_id, "AgentBound", AgentBoundPayload(agent_name=f"a{i}"))


def _suspend(log: Any, task_id: str, wake_on: Any) -> None:
    _emit(
        log,
        task_id,
        "TaskSuspended",
        TaskSuspendedPayload(reason="waiting_subtask", wake_on=wake_on),
    )


def _completed(log: Any, task_id: str, answer: str = "done") -> None:
    _emit(log, task_id, "TaskCompleted", TaskCompletedPayload(answer=answer))


def _record(log: Any, parent: str, child: str) -> None:
    _emit(
        log,
        parent,
        "SubtaskCompleted",
        SubtaskCompletedPayload(
            subtask_id=child, result=SubtaskResult(status="completed", output="x")
        ),
    )


def _build_store(log: Any) -> set[tuple[str, str]]:
    """Populate ``log``; return the ``(parent, child)`` handoffs recovery owes."""
    parked = HumanResponseReceived(handle=NEXT_GOAL_WAKE_HANDLE)
    for i in range(_ROOTS):
        rid = f"root-{i:03d}"
        _created(log, rid)
        _filler(log, rid, _FILLER_PER_ROOT)
        if i % 2:
            _completed(log, rid)
        else:
            _suspend(log, rid, parked)
            # Post-turn noise after the park stays inside the tail window.
            _filler(log, rid, 3)

    owed: set[tuple[str, str]] = set()

    # Single children that finished with no observer alive: completed,
    # failed, cancelled.
    for n, finish in enumerate(("completed", "failed", "cancelled")):
        parent, child = f"p-single-{n}", f"c-single-{n}"
        _created(log, parent)
        _filler(log, parent, 5)
        _created(log, child, parent)
        _suspend(log, parent, SubtaskCompleted(subtask_id=child))
        _filler(log, child, 4)
        if finish == "completed":
            _completed(log, child)
        elif finish == "failed":
            _emit(log, child, "TaskFailed", TaskFailedPayload(reason="boom"))
        else:
            _emit(log, child, "TaskCancelled", TaskCancelledPayload(reason="stop"))
        owed.add((parent, child))

    # A group: one member already handed off, two pending, one still running.
    members = ("c-grp-a", "c-grp-b", "c-grp-c", "c-grp-d")
    _created(log, "p-group")
    for m in members:
        _created(log, m, "p-group")
    _suspend(
        log,
        "p-group",
        SubtaskGroupCompleted(group_id=derive_group_id(members), subtask_ids=members),
    )
    for m in members[:3]:
        _completed(log, m)
    _record(log, "p-group", members[0])
    owed.update({("p-group", members[1]), ("p-group", members[2])})

    # A waiting parent whose tail after the suspend outgrows the window: the
    # scan falls back to the whole stream and still finds the wait.
    _created(log, "p-long")
    _created(log, "c-long", "p-long")
    _suspend(log, "p-long", SubtaskCompleted(subtask_id="c-long"))
    _filler(log, "p-long", 2 * observers_mod._RECOVERY_TAIL)
    _completed(log, "c-long")
    owed.add(("p-long", "c-long"))

    # Nothing owed: an already-recorded handoff, a woken parent, a
    # background child.
    _created(log, "p-done")
    _created(log, "c-done", "p-done")
    _suspend(log, "p-done", SubtaskCompleted(subtask_id="c-done"))
    _completed(log, "c-done")
    _record(log, "p-done", "c-done")

    _created(log, "p-woken")
    _created(log, "c-woken", "p-woken")
    _suspend(log, "p-woken", SubtaskCompleted(subtask_id="c-woken"))
    _completed(log, "c-woken")
    _record(log, "p-woken", "c-woken")
    _emit(
        log,
        "p-woken",
        "TaskWoken",
        TaskWokenPayload(wake_event=SubtaskCompleted(subtask_id="c-woken")),
    )

    _created(log, "p-bg")
    _created(log, "c-bg", "p-bg", background=True)
    _completed(log, "c-bg")
    return owed


@pytest.fixture(params=["memory", "sqlite"])
def inner_log(request: Any, tmp_path: Any) -> Any:
    if request.param == "sqlite":
        return SqliteEventLog(str(tmp_path / "recovery_cost.db"))
    return InMemoryEventLog()


def _handoffs(log: Any, parent: str) -> list[str]:
    return [
        e.payload.subtask_id for e in log.read(parent) if e.type == "SubtaskCompleted"
    ]


def test_recovery_reads_whole_streams_only_for_candidates(inner_log: Any) -> None:
    owed = _build_store(inner_log)
    log = _CountingLog(inner_log)
    dispatcher = _FakeDispatcher()

    observer = ChildLifecycleObserver(event_log=log, dispatcher=dispatcher)
    observer.stop()

    streams = len(inner_log.list_task_streams())
    candidates = len(owed)
    # One tail read per stream, one fallback for the long parent, and a
    # bounded handful per owed handoff (the child's tail + what the handoff
    # itself reads).
    assert len(log.reads) <= streams + 1 + 6 * candidates
    # No finished root is ever read in full, and each costs at most one
    # tail's worth of envelopes.
    for task_id, after_seq, size in log.reads:
        if task_id.startswith("root-"):
            assert after_seq is not None
            assert size <= observers_mod._RECOVERY_TAIL
    root_events = sum(s for t, _, s in log.reads if t.startswith("root-"))
    assert root_events <= _ROOTS * observers_mod._RECOVERY_TAIL

    # Every owed handoff is emitted exactly once; nothing else is.
    for parent in {p for p, _ in owed} | {"p-done", "p-woken", "p-bg"}:
        recorded = _handoffs(inner_log, parent)
        assert len(recorded) == len(set(recorded)), parent
    emitted = {
        (p, c)
        for p in {p for p, _ in owed}
        for c in _handoffs(inner_log, p)
    } - {("p-group", "c-grp-a")}
    assert emitted == owed
    assert _handoffs(inner_log, "p-done") == ["c-done"]
    assert _handoffs(inner_log, "p-woken") == ["c-woken"]
    assert _handoffs(inner_log, "p-bg") == []

    # Wakes: each single parent once; the group stays short of its barrier
    # (member d is still running), so no group wake.
    woken = sorted(t for t, _ in dispatcher.woken)
    assert woken == ["p-long", "p-single-0", "p-single-1", "p-single-2"]

    # A second construction converges: nothing new is written.
    before = {p: _handoffs(inner_log, p) for p, _ in owed}
    ChildLifecycleObserver(event_log=inner_log, dispatcher=_FakeDispatcher()).stop()
    assert {p: _handoffs(inner_log, p) for p, _ in owed} == before
