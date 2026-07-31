"""``noeta.read_models.tasks`` — the one task-list projection hosts read.

Enumeration goes through the ``EventLogTaskIndex`` capability and every field
is folded from the task's own stream, so a row can never disagree with the
stream it summarises and no adapter private leaks into the projection. Run
against both real adapters, because the row shape is a host-facing contract
and must not vary with the backend behind it.
"""

from __future__ import annotations

from typing import Any, Callable, Iterator

import pytest

from noeta.protocols.events import (
    BackgroundShellExitedPayload,
    BackgroundShellKilledPayload,
    BackgroundShellPolledPayload,
    BackgroundShellStartedPayload,
    ConversationClosedPayload,
    TaskCreatedPayload,
    TaskHostBoundPayload,
    TaskStartedPayload,
)
from noeta.protocols.values import ContentRef
from noeta.read_models.tasks import list_task_summaries
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog
from noeta.sdk.storage import SqliteContentStore, SqliteEventLog


def _ref(tag: str) -> ContentRef:
    return ContentRef(hash=(tag * 64)[:64], size=4, media_type="text/plain")


@pytest.fixture(params=["memory", "sqlite"])
def stack(
    request: Any,
) -> Iterator[Callable[..., tuple[Any, Any]]]:
    """Build ``(event_log, content_store)`` per adapter, with an injectable
    clock so recency ordering is deterministic rather than wall-clock racy."""
    closers: list[Any] = []

    def _make(clock: Callable[[], float] | None = None) -> tuple[Any, Any]:
        if request.param == "memory":
            log: Any = InMemoryEventLog(clock=clock)
            cs: Any = InMemoryContentStore()
        else:
            log = SqliteEventLog(":memory:", clock=clock)
            cs = SqliteContentStore(":memory:")
        closers.extend([log, cs])
        return log, cs

    yield _make

    for obj in closers:
        close = getattr(obj, "close", None)
        if callable(close):
            close()


def test_summary_shape_status_and_closed(
    stack: Callable[..., tuple[Any, Any]],
) -> None:
    log, cs = stack()
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    # closed is ORTHOGONAL to status: a ConversationClosed leaves status alone.
    log.emit(
        task_id="t1",
        type="ConversationClosed",
        payload=ConversationClosedPayload(closed_by="leo", reason=None),
    )

    rows = list_task_summaries(log, log, cs)
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == {
        "task_id",
        "status",
        "closed",
        "last_seq",
        "last_event_time",
        "created_event_time",
        "parent_task_id",
        "agent_name",
        "workspace_dir",
        "background_jobs",
    }
    assert row["task_id"] == "t1"
    assert row["closed"] is True
    assert isinstance(row["created_event_time"], float)
    assert row["created_event_time"] <= row["last_event_time"]
    # A root conversation has no spawning parent.
    assert row["parent_task_id"] is None
    assert row["agent_name"] == "unnamed"  # genesis TaskCreated default
    # No TaskHostBound means no welded workspace, which a host renders as
    # ungrouped rather than guessing a directory.
    assert row["workspace_dir"] is None
    assert row["background_jobs"] == []
    # No terminal was synthesized; folding a bare TaskCreated stream is "pending".
    assert row["status"] in {"pending", "running", "suspended"}


def test_row_carries_welded_workspace_dir(
    stack: Callable[..., tuple[Any, Any]],
) -> None:
    # The path is surfaced ABSOLUTE so a host can group sessions by workspace
    # without resolving anything itself.
    log, cs = stack()
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    log.emit(
        task_id="t1",
        type="TaskHostBound",
        payload=TaskHostBoundPayload(
            host_id="h",
            workspace_dir="/abs/projects/noeta",
        ),
    )
    rows = list_task_summaries(log, log, cs)
    assert rows[0]["workspace_dir"] == "/abs/projects/noeta"


def test_subtask_row_carries_parent_task_id(
    stack: Callable[..., tuple[Any, Any]],
) -> None:
    # The row carries the parent so a host can tell conversations apart from
    # the subtasks they spawned and list only the former.
    log, cs = stack()
    log.emit(
        task_id="root",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    log.emit(
        task_id="child",
        type="TaskCreated",
        payload=TaskCreatedPayload(
            goal="sub", policy_name="p", parent_task_id="root", subtask_depth=1
        ),
    )
    rows = {r["task_id"]: r for r in list_task_summaries(log, log, cs)}
    assert rows["root"]["parent_task_id"] is None
    assert rows["child"]["parent_task_id"] == "root"


def test_summary_carries_created_time_for_tree_order(
    stack: Callable[..., tuple[Any, Any]],
) -> None:
    times = iter([1.0, 2.0, 3.0])
    log, cs = stack(clock=lambda: next(times))
    log.emit(
        task_id="parent",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    log.emit(
        task_id="child",
        type="TaskCreated",
        payload=TaskCreatedPayload(
            goal="sub", policy_name="p", parent_task_id="parent"
        ),
    )
    # A later update on the parent must not overwrite its creation bookmark —
    # that timestamp is what keeps sibling ordering stable in a task tree.
    log.emit(
        task_id="parent",
        type="TaskStarted",
        payload=TaskStartedPayload(lease_id="lease-1"),
    )

    rows = {r["task_id"]: r for r in list_task_summaries(log, log, cs)}
    assert rows["parent"]["created_event_time"] == 1.0
    assert rows["parent"]["last_event_time"] == 3.0
    assert rows["child"]["created_event_time"] == 2.0


def test_order_recency_desc_then_task_id(
    stack: Callable[..., tuple[Any, Any]],
) -> None:
    times = iter([20.0, 20.0, 10.0])
    log, cs = stack(clock=lambda: next(times))
    for tid in ("tc", "tb", "ta"):
        log.emit(
            task_id=tid,
            type="TaskCreated",
            payload=TaskCreatedPayload(goal="g", policy_name="p"),
        )

    rows = list_task_summaries(log, log, cs)
    assert [r["task_id"] for r in rows] == ["tb", "tc", "ta"]


def test_empty_store_returns_empty_list(
    stack: Callable[..., tuple[Any, Any]],
) -> None:
    log, cs = stack()
    assert list_task_summaries(log, log, cs) == []


# ---------------------------------------------------------------------------
# Background-shell jobs surfaced per session
# ---------------------------------------------------------------------------


def test_running_background_job_listed_in_session_row(
    stack: Callable[..., tuple[Any, Any]],
) -> None:
    log, cs = stack()
    log.emit(
        task_id="root",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    log.emit(
        task_id="root",
        type="BackgroundShellStarted",
        payload=BackgroundShellStartedPayload(
            job_id="j1",
            command="npm run dev",
            spawned_by_task_id="root",
            pid=4242,
            ref=_ref("a"),
        ),
    )
    rows = {r["task_id"]: r for r in list_task_summaries(log, log, cs)}
    jobs = rows["root"]["background_jobs"]
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == "j1"
    assert jobs[0]["command"] == "npm run dev"
    assert jobs[0]["status"] == "running"
    assert jobs[0]["spawned_by_task_id"] == "root"
    assert jobs[0]["ref"] == _ref("a")


def test_exited_background_job_updates_status_and_exit_code(
    stack: Callable[..., tuple[Any, Any]],
) -> None:
    log, cs = stack()
    log.emit(
        task_id="root",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    log.emit(
        task_id="root",
        type="BackgroundShellStarted",
        payload=BackgroundShellStartedPayload(
            job_id="j1", command="sleep 1", spawned_by_task_id="root",
            pid=7, ref=_ref("a"),
        ),
    )
    log.emit(
        task_id="root",
        type="BackgroundShellExited",
        payload=BackgroundShellExitedPayload(
            job_id="j1", exit_code=0, final_ref=_ref("b"), summary="done",
        ),
    )
    rows = {r["task_id"]: r for r in list_task_summaries(log, log, cs)}
    jobs = rows["root"]["background_jobs"]
    # An exited job stays on the row, updated in place — the list is an audit
    # trail of what ran, not a live process table.
    assert len(jobs) == 1
    assert jobs[0]["status"] == "exited"
    assert jobs[0]["exit_code"] == 0
    assert jobs[0]["ref"] == _ref("b")


def test_killed_background_job_updates_status_and_signal(
    stack: Callable[..., tuple[Any, Any]],
) -> None:
    log, cs = stack()
    log.emit(
        task_id="root",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    log.emit(
        task_id="root",
        type="BackgroundShellStarted",
        payload=BackgroundShellStartedPayload(
            job_id="j1", command="tail -f log", spawned_by_task_id="root",
            pid=99, ref=_ref("a"),
        ),
    )
    log.emit(
        task_id="root",
        type="BackgroundShellKilled",
        payload=BackgroundShellKilledPayload(job_id="j1", signal=15),
    )
    rows = {r["task_id"]: r for r in list_task_summaries(log, log, cs)}
    jobs = rows["root"]["background_jobs"]
    assert len(jobs) == 1
    assert jobs[0]["status"] == "killed"
    assert jobs[0]["signal"] == 15


def test_poll_advances_background_job_ref(
    stack: Callable[..., tuple[Any, Any]],
) -> None:
    # The ref advances to the newest snapshot so a drill-in dereferences the
    # latest output rather than whatever the job printed at startup.
    log, cs = stack()
    log.emit(
        task_id="root",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    log.emit(
        task_id="root",
        type="BackgroundShellStarted",
        payload=BackgroundShellStartedPayload(
            job_id="j1", command="server", spawned_by_task_id="root",
            pid=1, ref=_ref("a"),
        ),
    )
    log.emit(
        task_id="root",
        type="BackgroundShellPolled",
        payload=BackgroundShellPolledPayload(job_id="j1", ref=_ref("c"), offset=42),
    )
    rows = {r["task_id"]: r for r in list_task_summaries(log, log, cs)}
    jobs = rows["root"]["background_jobs"]
    assert len(jobs) == 1
    assert jobs[0]["status"] == "running"
    assert jobs[0]["ref"] == _ref("c")


def test_subtask_spawned_job_shows_under_root_session(
    stack: Callable[..., tuple[Any, Any]],
) -> None:
    # Background-shell events are emitted on the session root stream whoever
    # spawns them, so folding the root is enough to see every job.
    log, cs = stack()
    log.emit(
        task_id="root",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    log.emit(
        task_id="child",
        type="TaskCreated",
        payload=TaskCreatedPayload(
            goal="sub", policy_name="p", parent_task_id="root", subtask_depth=1
        ),
    )
    # Emitted on the root stream, attributed to the subtask that spawned it.
    log.emit(
        task_id="root",
        type="BackgroundShellStarted",
        payload=BackgroundShellStartedPayload(
            job_id="jsub",
            command="build",
            spawned_by_task_id="child",
            pid=321,
            ref=_ref("a"),
        ),
    )
    rows = {r["task_id"]: r for r in list_task_summaries(log, log, cs)}
    root_jobs = rows["root"]["background_jobs"]
    assert [j["job_id"] for j in root_jobs] == ["jsub"]
    assert root_jobs[0]["spawned_by_task_id"] == "child"
    # The subtask's own row carries no jobs (lifetime is owned by the root).
    assert rows["child"]["background_jobs"] == []
