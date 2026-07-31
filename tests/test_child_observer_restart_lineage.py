"""ChildLifecycleObserver must survive a process restart.

The observer builds its ``_lineage`` (``child_id -> parent_id``) from live
``TaskCreated`` events, so a child created before a restart that reaches
terminal after it would have no lineage entry: its ``TaskCompleted`` /
``TaskFailed`` / ``TaskCancelled`` would be a no-op in ``_on_terminal``, the
parent stream would never get ``SubtaskCompleted``, and a parent suspended on
``SubtaskCompleted`` / ``SubtaskGroupCompleted`` would wait forever. At
construction the observer therefore replays the persisted event log (via
``list_task_streams`` + ``read``) to seed ``_lineage`` for every
not-yet-terminal, non-background child.

A restart is simulated by dropping the first observer and constructing a fresh
one on the *same* log. Parametrized over InMemory and SQLite so the replay
contract is pinned on a real storage backend, not only the in-memory reference
adapter.
"""

from __future__ import annotations

from typing import Any

import pytest

from noeta.core.observers import ChildLifecycleObserver
from noeta.protocols.events import (
    TaskCompletedPayload,
    TaskCreatedPayload,
    TaskSuspendedPayload,
)
from noeta.protocols.wake import (
    SubtaskCompleted,
    SubtaskGroupCompleted,
    SubtaskResult,
    derive_group_id,
)
from noeta.storage.memory import InMemoryEventLog
from noeta.sdk.storage import SqliteEventLog


class _FakeDispatcher:
    def __init__(self) -> None:
        self.enqueued: list[str] = []
        self.woken: list[tuple[str, Any]] = []

    def enqueue(self, task_id: str) -> None:
        self.enqueued.append(task_id)

    def wake(self, task_id: str, wake_event: Any) -> bool:
        self.woken.append((task_id, wake_event))
        return True


@pytest.fixture(params=["memory", "sqlite"])
def log(request: Any, tmp_path: Any) -> Any:
    if request.param == "sqlite":
        db = str(tmp_path / "restart_lineage.db")
        return SqliteEventLog(db)
    return InMemoryEventLog()


def _emit_created(log: Any, child_id: str, parent_id: str, *, background: bool = False) -> None:
    log.system_emit(
        task_id=child_id,
        type="TaskCreated",
        payload=TaskCreatedPayload(
            goal="do",
            policy_name="react",
            parent_task_id=parent_id,
            background=background,
        ),
        actor="test",
        origin="system",
    )


def _emit_completed(log: Any, child_id: str, *, answer: str = "done") -> None:
    log.system_emit(
        task_id=child_id,
        type="TaskCompleted",
        payload=TaskCompletedPayload(answer=answer),
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


def _parent_subtask_completed(log: Any, parent_id: str) -> list[Any]:
    return [e for e in log.read(parent_id) if e.type == "SubtaskCompleted"]


# ---------------------------------------------------------------------------
# Single child (SubtaskCompleted) completes after restart
# ---------------------------------------------------------------------------


def test_single_child_completes_after_restart_wakes_parent(log: Any) -> None:
    dispatcher = _FakeDispatcher()
    parent_id = "parent-1"
    child_id = "child-1"

    # Pre-restart observer: child created + parent suspended, but child does
    # NOT reach terminal before the restart.
    pre = ChildLifecycleObserver(event_log=log, dispatcher=dispatcher)
    _emit_created(log, child_id, parent_id)
    _emit_suspended(
        log, parent_id, SubtaskCompleted(subtask_id=child_id)
    )
    assert dispatcher.enqueued == [child_id]
    pre.stop()  # simulate restart

    # Post-restart observer on the SAME log (empty lineage at construction).
    post = ChildLifecycleObserver(event_log=log, dispatcher=dispatcher)
    try:
        _emit_completed(log, child_id)
    finally:
        post.stop()

    # Parent stream got exactly one SubtaskCompleted for the child ...
    completed = _parent_subtask_completed(log, parent_id)
    assert len(completed) == 1
    assert completed[0].payload.subtask_id == child_id
    assert completed[0].payload.result == SubtaskResult(status="completed", output="done")
    # ... and the parent was woken (so it can never hang on the child).
    assert len(dispatcher.woken) == 1
    woken_parent, wake_event = dispatcher.woken[0]
    assert woken_parent == parent_id
    assert isinstance(wake_event, SubtaskCompleted)
    assert wake_event.subtask_id == child_id


# ---------------------------------------------------------------------------
# Already-terminal child: no duplicate notification after restart
# ---------------------------------------------------------------------------


def test_already_terminal_child_is_not_double_notified_after_restart(log: Any) -> None:
    dispatcher = _FakeDispatcher()
    parent_id = "parent-2"
    child_id = "child-2"

    # Pre-restart: child created, parent suspended, AND child completed (the
    # pre-restart observer recorded SubtaskCompleted + woke the parent).
    pre = ChildLifecycleObserver(event_log=log, dispatcher=dispatcher)
    _emit_created(log, child_id, parent_id)
    _emit_suspended(log, parent_id, SubtaskCompleted(subtask_id=child_id))
    _emit_completed(log, child_id)
    pre.stop()

    assert len(_parent_subtask_completed(log, parent_id)) == 1
    assert len(dispatcher.woken) == 1

    # Restart: fresh observer on the same log. The already-terminal child must
    # NOT be re-seeded — otherwise a (impossible) second terminal would
    # double-notify, and the lineage entry would leak.
    post = ChildLifecycleObserver(event_log=log, dispatcher=dispatcher)
    post.stop()

    # No second SubtaskCompleted, no second wake.
    assert len(_parent_subtask_completed(log, parent_id)) == 1
    assert len(dispatcher.woken) == 1


# ---------------------------------------------------------------------------
# Group (SubtaskGroupCompleted): two members before restart, third after
# ---------------------------------------------------------------------------


def test_group_last_member_completes_after_restart_fires_group_wake(log: Any) -> None:
    dispatcher = _FakeDispatcher()
    parent_id = "parent-3"
    children = ("child-3a", "child-3b", "child-3c")
    group_id = derive_group_id(children)

    pre = ChildLifecycleObserver(event_log=log, dispatcher=dispatcher)
    for child_id in children:
        _emit_created(log, child_id, parent_id)
    _emit_suspended(
        log,
        parent_id,
        SubtaskGroupCompleted(group_id=group_id, subtask_ids=children),
    )
    # Two members complete before the restart → their SubtaskCompleted is
    # recorded, but the group barrier is not yet satisfied → no group wake.
    _emit_completed(log, children[0])
    _emit_completed(log, children[1])
    pre.stop()

    assert len(_parent_subtask_completed(log, parent_id)) == 2
    assert dispatcher.woken == []  # group not yet full

    # Restart: fresh observer; the third (not-yet-terminal) member is seeded
    # into _lineage from the persisted log.
    post = ChildLifecycleObserver(event_log=log, dispatcher=dispatcher)
    try:
        _emit_completed(log, children[2])
    finally:
        post.stop()

    # All three members' SubtaskCompleted are on the parent stream ...
    completed_ids = {
        e.payload.subtask_id for e in _parent_subtask_completed(log, parent_id)
    }
    assert completed_ids == set(children)
    # ... and the group wake fired exactly once (all-of barrier satisfied).
    group_wakes = [
        (tid, ev)
        for tid, ev in dispatcher.woken
        if isinstance(ev, SubtaskGroupCompleted)
    ]
    assert len(group_wakes) == 1
    woken_parent, wake_event = group_wakes[0]
    assert woken_parent == parent_id
    assert wake_event.group_id == group_id
    assert set(wake_event.subtask_ids) == set(children)


# ---------------------------------------------------------------------------
# Background child: invisible to the observer even across a restart
# ---------------------------------------------------------------------------


def test_background_child_is_not_notified_after_restart(log: Any) -> None:
    dispatcher = _FakeDispatcher()
    parent_id = "parent-4"
    child_id = "child-4"

    # Background child (spawn_subagent(background=True)): the parent never
    # suspended on it, so it must never be seeded / notified.
    pre = ChildLifecycleObserver(event_log=log, dispatcher=dispatcher)
    _emit_created(log, child_id, parent_id, background=True)
    _emit_completed(log, child_id)
    pre.stop()

    assert _parent_subtask_completed(log, parent_id) == []
    assert dispatcher.woken == []

    # Restart: the background child stays invisible.
    post = ChildLifecycleObserver(event_log=log, dispatcher=dispatcher)
    post.stop()
    assert _parent_subtask_completed(log, parent_id) == []
    assert dispatcher.woken == []
