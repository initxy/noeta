"""CW5a Phase 1 — `noeta.read_models.sessions` list projection.

Pins the shared session/task read-model that the management CLI, code CLI, and
Web surfaces consume (extracted from the former `server._list_tasks`). It
enumerates via the `EventLogTaskIndex` capability and folds each task for
`status` / `closed` — no adapter privates. Parametrized over both real adapters.
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
from noeta.read_models.sessions import list_session_summaries
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog
from noeta.storage.sqlite import SqliteContentStore, SqliteEventLog


def _ref(tag: str) -> ContentRef:
    return ContentRef(hash=(tag * 64)[:64], size=4, media_type="text/plain")


@pytest.fixture(params=["memory", "sqlite"])
def stack(
    request: Any,
) -> Iterator[Callable[..., tuple[Any, Any]]]:
    """Yield a builder for ``(event_log, content_store)`` over each real adapter,
    with an injectable clock so ordering is deterministic."""
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

    rows = list_session_summaries(log, log, cs)
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
    # a session with no TaskHostBound (no welded workspace) groups
    # as ungrouped — its workspace_dir is None.
    assert row["workspace_dir"] is None
    # A session with no background-shell events lists no jobs.
    assert row["background_jobs"] == []
    # No terminal was synthesized; folding a bare TaskCreated stream is "pending".
    assert row["status"] in {"pending", "running", "suspended"}


def test_row_carries_welded_workspace_dir(
    stack: Callable[..., tuple[Any, Any]],
) -> None:
    # a session whose TaskHostBound welded a workspace_dir surfaces
    # that ABSOLUTE PATH on the row so the Web session list can group by it.
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
    rows = list_session_summaries(log, log, cs)
    assert rows[0]["workspace_dir"] == "/abs/projects/noeta"


def test_subtask_row_carries_parent_task_id(
    stack: Callable[..., tuple[Any, Any]],
) -> None:
    # A subtask's TaskCreated names its spawning parent; the row must surface it
    # so the Web session list can filter subtasks out (follow-on).
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
    rows = {r["task_id"]: r for r in list_session_summaries(log, log, cs)}
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
    # A later parent update must not overwrite the creation bookmark the Web
    # task tree uses for stable sibling ordering.
    log.emit(
        task_id="parent",
        type="TaskStarted",
        payload=TaskStartedPayload(lease_id="lease-1"),
    )

    rows = {r["task_id"]: r for r in list_session_summaries(log, log, cs)}
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

    rows = list_session_summaries(log, log, cs)
    assert [r["task_id"] for r in rows] == ["tb", "tc", "ta"]


def test_empty_store_returns_empty_list(
    stack: Callable[..., tuple[Any, Any]],
) -> None:
    log, cs = stack()
    assert list_session_summaries(log, log, cs) == []


# ---------------------------------------------------------------------------
# Background-shell jobs surfaced per session
# ---------------------------------------------------------------------------


def test_running_background_job_listed_in_session_row(
    stack: Callable[..., tuple[Any, Any]],
) -> None:
    # The read model lists running background jobs per session (job_id / command / status / spawned_by).
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
    rows = {r["task_id"]: r for r in list_session_summaries(log, log, cs)}
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
    # After the process exits, the displayed status updates + carries exit_code + ref points at the final snapshot.
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
    rows = {r["task_id"]: r for r in list_session_summaries(log, log, cs)}
    jobs = rows["root"]["background_jobs"]
    # Audit trail: still exactly one entry, not deleted.
    assert len(jobs) == 1
    assert jobs[0]["status"] == "exited"
    assert jobs[0]["exit_code"] == 0
    assert jobs[0]["ref"] == _ref("b")


def test_killed_background_job_updates_status_and_signal(
    stack: Callable[..., tuple[Any, Any]],
) -> None:
    # After being killed, status -> killed + carries signal.
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
    rows = {r["task_id"]: r for r in list_session_summaries(log, log, cs)}
    jobs = rows["root"]["background_jobs"]
    assert len(jobs) == 1
    assert jobs[0]["status"] == "killed"
    assert jobs[0]["signal"] == 15


def test_poll_advances_background_job_ref(
    stack: Callable[..., tuple[Any, Any]],
) -> None:
    # poll advances ref to the latest snapshot, so a drill-in derefs the latest output.
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
    rows = {r["task_id"]: r for r in list_session_summaries(log, log, cs)}
    jobs = rows["root"]["background_jobs"]
    assert len(jobs) == 1
    assert jobs[0]["status"] == "running"
    assert jobs[0]["ref"] == _ref("c")


def test_subtask_spawned_job_shows_under_root_session(
    stack: Callable[..., tuple[Any, Any]],
) -> None:
    # Events go on the session root stream: even a background job spawned by a
    # subtask (spawned_by=subtask) lands on the ROOT row when the root stream is folded.
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
    # Emitted on the SESSION ROOT stream even though a subtask spawned it.
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
    rows = {r["task_id"]: r for r in list_session_summaries(log, log, cs)}
    root_jobs = rows["root"]["background_jobs"]
    assert [j["job_id"] for j in root_jobs] == ["jsub"]
    assert root_jobs[0]["spawned_by_task_id"] == "child"
    # The subtask's own row carries no jobs (lifetime is owned by the root).
    assert rows["child"]["background_jobs"] == []
