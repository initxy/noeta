"""Kernel #3 (stale-reclaim attempt cap) + kernel #8 (buffered-wake GC
on terminal) — deferred structural round.

#3: ``requeue_stale`` used to move an expired lease back to ready
unconditionally — a poison task that silently kills its worker loops
lease → expire → reclaim forever. Now each reclaim increments a
``reclaim_count`` (reset on any progress signal: successful heartbeat /
clean release / controlled fail-requeue / force-enqueue) and at
``reclaim_max`` the task drops to terminal
(``stale_reclaim_exceeded``) — the reclaim-path analogue of
``max_fail_attempts``.

#8: buffered wake events that never match a ``wake_on`` were only ever
drained by a matching suspend-release; once a task went terminal they
leaked forever. Terminal transitions (release-terminal, fail-terminal,
reclaim-cap-terminal) now GC them. The matched wake (H2 exactly-once
handoff) is deliberately untouched.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from noeta.protocols.wake import HumanResponseReceived, SubtaskCompleted
from noeta.storage.memory import InMemoryDispatcher
from noeta.storage.sqlite.dispatcher import SqliteDispatcher
from noeta.storage.sqlite.migrations import SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Adapter parametrisation with an injected clock + tunable caps
# ---------------------------------------------------------------------------


@pytest.fixture(params=["memory", "sqlite"])
def make_dispatcher(request):
    kind = request.param

    def _factory(**kwargs: Any) -> Any:
        if kind == "memory":
            return InMemoryDispatcher(**kwargs)
        return SqliteDispatcher(":memory:", **kwargs)

    factory = _factory
    factory.kind = kind  # type: ignore[attr-defined]
    return factory


def _pending_count(disp: Any, task_id: str) -> int:
    if isinstance(disp, InMemoryDispatcher):
        task = disp._tasks.get(task_id)
        return 0 if task is None else len(task.pending_wake_events)
    row = disp._conn.execute(
        "SELECT COUNT(*) FROM dispatcher_pending_wakes WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    return int(row[0])


def _lease_then_expire(disp: Any, clock: dict[str, float], task_id: str) -> None:
    lease = disp.lease(worker_id="w", task_id=task_id, lease_seconds=10.0)
    assert lease is not None
    clock["t"] += 11.0  # past the lease deadline, no heartbeat = no progress


# ---------------------------------------------------------------------------
# Kernel #3 — reclaim cap
# ---------------------------------------------------------------------------


def test_poison_task_terminal_after_reclaim_max(make_dispatcher) -> None:
    """lease → expire → reclaim, repeated with zero progress, lands
    terminal at ``reclaim_max`` instead of looping forever."""
    clock = {"t": 1_000.0}
    disp = make_dispatcher(now=lambda: clock["t"], reclaim_max=3)
    disp.enqueue("t1")

    _lease_then_expire(disp, clock, "t1")
    assert disp.requeue_stale() == ["t1"]  # reclaim 1
    _lease_then_expire(disp, clock, "t1")
    assert disp.requeue_stale() == ["t1"]  # reclaim 2
    _lease_then_expire(disp, clock, "t1")
    # Reclaim 3 hits the cap: terminal, NOT in the returned list.
    assert disp.requeue_stale() == []
    assert disp.task_status("t1") == "terminal"
    assert disp.lease(worker_id="w", task_id="t1") is None
    if isinstance(disp, InMemoryDispatcher):
        assert disp.suspend_reason("t1") == "stale_reclaim_exceeded"
    else:
        row = disp._conn.execute(
            "SELECT suspend_reason FROM dispatcher_tasks WHERE task_id = ?",
            ("t1",),
        ).fetchone()
        assert row["suspend_reason"] == "stale_reclaim_exceeded"


def test_single_reclaim_recovery_still_works(make_dispatcher) -> None:
    """The ordinary crash-recovery path — one reclaim, then the task
    completes — is untouched by the cap."""
    clock = {"t": 1_000.0}
    disp = make_dispatcher(now=lambda: clock["t"])
    disp.enqueue("t1")
    _lease_then_expire(disp, clock, "t1")
    assert disp.requeue_stale() == ["t1"]
    lease = disp.lease(worker_id="w", task_id="t1")
    assert lease is not None
    disp.release(lease.lease_id, next_state="terminal")
    assert disp.task_status("t1") == "terminal"


def test_successful_heartbeat_resets_reclaim_counter(make_dispatcher) -> None:
    """Two reclaims, then a lease that heartbeats (progress): the
    counter starts over, so two MORE reclaims still requeue."""
    clock = {"t": 1_000.0}
    disp = make_dispatcher(now=lambda: clock["t"], reclaim_max=3)
    disp.enqueue("t1")
    for _ in range(2):
        _lease_then_expire(disp, clock, "t1")
        assert disp.requeue_stale() == ["t1"]
    # Progress: a live worker heartbeats, then its lease still expires.
    lease = disp.lease(worker_id="w", task_id="t1", lease_seconds=10.0)
    disp.heartbeat(lease.lease_id, lease_seconds=10.0)  # reset-on-progress
    clock["t"] += 11.0
    assert disp.requeue_stale() == ["t1"]  # count restarted at 1
    _lease_then_expire(disp, clock, "t1")
    assert disp.requeue_stale() == ["t1"]  # 2 — still under the cap
    assert disp.task_status("t1") == "ready"


def test_clean_release_resets_reclaim_counter(make_dispatcher) -> None:
    """A clean suspend/wake cycle between reclaims is progress."""
    clock = {"t": 1_000.0}
    disp = make_dispatcher(now=lambda: clock["t"], reclaim_max=3)
    disp.enqueue("t1")
    for _ in range(2):
        _lease_then_expire(disp, clock, "t1")
        assert disp.requeue_stale() == ["t1"]
    # Progress: the worker completes a segment (suspend + wake).
    lease = disp.lease(worker_id="w", task_id="t1")
    disp.release(
        lease.lease_id,
        next_state="suspended",
        wake_on=HumanResponseReceived(handle="h1"),
    )
    assert disp.wake("t1", HumanResponseReceived(handle="h1")) is True
    for _ in range(2):
        _lease_then_expire(disp, clock, "t1")
        assert disp.requeue_stale() == ["t1"]  # counter restarted
    assert disp.task_status("t1") == "ready"


def test_reclaim_cap_preserves_matched_wake_discipline(make_dispatcher) -> None:
    """A reclaim between wake-match and consume keeps re-delivering the
    matched wake (H2) — the counter must not interfere below the cap."""
    clock = {"t": 1_000.0}
    disp = make_dispatcher(now=lambda: clock["t"], reclaim_max=3)
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w", task_id="t1")
    disp.release(
        lease.lease_id,
        next_state="suspended",
        wake_on=HumanResponseReceived(handle="h1"),
    )
    assert disp.wake("t1", HumanResponseReceived(handle="h1")) is True
    _lease_then_expire(disp, clock, "t1")
    assert disp.requeue_stale() == ["t1"]
    lease = disp.lease(worker_id="w", task_id="t1")
    assert lease.wake_event == HumanResponseReceived(handle="h1")


# ---------------------------------------------------------------------------
# Kernel #8 — buffered-wake GC on terminal
# ---------------------------------------------------------------------------


def _buffer_never_matching_wake(disp: Any, task_id: str) -> None:
    disp.enqueue(task_id)
    lease = disp.lease(worker_id="w", task_id=task_id)
    disp.release(
        lease.lease_id,
        next_state="suspended",
        wake_on=HumanResponseReceived(handle="wanted"),
    )
    # Never matches the stored wake_on → buffered.
    assert disp.wake(task_id, SubtaskCompleted(subtask_id="stray")) is False
    assert _pending_count(disp, task_id) == 1
    # Re-ready it so the caller can drive it terminal.
    disp.enqueue(task_id)


def test_release_terminal_gcs_buffered_wakes(make_dispatcher) -> None:
    disp = make_dispatcher()
    _buffer_never_matching_wake(disp, "t1")
    lease = disp.lease(worker_id="w", task_id="t1")
    disp.release(lease.lease_id, next_state="terminal")
    assert _pending_count(disp, "t1") == 0


def test_fail_nonretryable_terminal_gcs_buffered_wakes(make_dispatcher) -> None:
    disp = make_dispatcher()
    _buffer_never_matching_wake(disp, "t1")
    lease = disp.lease(worker_id="w", task_id="t1")
    disp.fail(lease.lease_id, retryable=False, reason="boom")
    assert disp.task_status("t1") == "terminal"
    assert _pending_count(disp, "t1") == 0


def test_fail_retryable_cap_terminal_gcs_buffered_wakes(make_dispatcher) -> None:
    disp = make_dispatcher(max_fail_attempts=1)
    _buffer_never_matching_wake(disp, "t1")
    lease = disp.lease(worker_id="w", task_id="t1")
    disp.fail(lease.lease_id, retryable=True, reason="boom")
    assert disp.task_status("t1") == "terminal"
    assert _pending_count(disp, "t1") == 0


def test_fail_requeue_keeps_buffered_wakes(make_dispatcher) -> None:
    """A retryable fail below the cap is NOT terminal — the buffer must
    survive (it may still drain on a later suspend)."""
    disp = make_dispatcher(max_fail_attempts=3)
    _buffer_never_matching_wake(disp, "t1")
    lease = disp.lease(worker_id="w", task_id="t1")
    disp.fail(lease.lease_id, retryable=True, reason="boom")
    assert disp.task_status("t1") == "ready"
    assert _pending_count(disp, "t1") == 1


def test_reclaim_cap_terminal_gcs_buffered_wakes(make_dispatcher) -> None:
    clock = {"t": 1_000.0}
    disp = make_dispatcher(now=lambda: clock["t"], reclaim_max=1)
    _buffer_never_matching_wake(disp, "t1")
    _lease_then_expire(disp, clock, "t1")
    assert disp.requeue_stale() == []
    assert disp.task_status("t1") == "terminal"
    assert _pending_count(disp, "t1") == 0


def test_buffered_wake_still_drains_on_matching_suspend(make_dispatcher) -> None:
    """The legitimate buffer path is untouched: an early wake buffered
    against a running task drains on the matching suspend-release."""
    disp = make_dispatcher()
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w", task_id="t1")
    assert disp.wake("t1", HumanResponseReceived(handle="early")) is False
    assert _pending_count(disp, "t1") == 1
    disp.release(
        lease.lease_id,
        next_state="suspended",
        wake_on=HumanResponseReceived(handle="early"),
    )
    assert _pending_count(disp, "t1") == 0  # drained into matched
    lease = disp.lease(worker_id="w", task_id="t1")
    assert lease.wake_event == HumanResponseReceived(handle="early")


# ---------------------------------------------------------------------------
# Migration 6 — additive column + backfill
# ---------------------------------------------------------------------------


def test_migration_6_reclaim_count_backfills_zero(tmp_path) -> None:
    """A pre-migration-6 DB upgrades in place; legacy rows read
    ``reclaim_count = 0`` (sqlite ADD COLUMN DEFAULT fill)."""
    db_path = tmp_path / "reclaim_backfill.sqlite"

    # Hand-build a version-3 DB (the same faithful fixture
    # test_wake_resume.py uses) so migrations 4 → 5 → 6 all run.
    bootstrap = sqlite3.connect(str(db_path))
    bootstrap.row_factory = sqlite3.Row
    bootstrap.execute("PRAGMA journal_mode = WAL")
    bootstrap.execute(
        "CREATE TABLE dispatcher_tasks ("
        " task_id TEXT PRIMARY KEY,"
        " status TEXT NOT NULL,"
        " lease_id TEXT NULL,"
        " lease_expires_at REAL NULL,"
        " heartbeat_count INTEGER NOT NULL DEFAULT 0,"
        " fail_attempts INTEGER NOT NULL DEFAULT 0,"
        " wake_on_canonical BLOB NULL,"
        " suspend_reason TEXT NULL,"
        " ready_order INTEGER NULL,"
        " CHECK (status IN ('ready', 'leased', 'suspended', 'terminal')),"
        " CHECK ((status = 'ready') = (ready_order IS NOT NULL)),"
        " CHECK ((status = 'leased') = (lease_id IS NOT NULL AND "
        "lease_expires_at IS NOT NULL))"
        ") WITHOUT ROWID"
    )
    bootstrap.execute(
        "INSERT INTO dispatcher_tasks "
        "(task_id, status, ready_order) VALUES (?, 'ready', 1)",
        ("legacy-task",),
    )
    bootstrap.execute(
        "CREATE TABLE events ("
        " task_id TEXT NOT NULL, seq INTEGER NOT NULL, id TEXT NOT NULL,"
        " type TEXT NOT NULL, schema_version INTEGER NOT NULL,"
        " occurred_at REAL NOT NULL, actor TEXT NOT NULL, trace_id TEXT NOT NULL,"
        " correlation_id TEXT NOT NULL, causation_id TEXT NULL,"
        " origin TEXT NOT NULL, payload_canonical BLOB NOT NULL,"
        " PRIMARY KEY (task_id, seq)) WITHOUT ROWID"
    )
    bootstrap.execute(
        "CREATE INDEX ix_events_snapshot ON events (task_id, seq DESC) "
        "WHERE type = 'TaskSnapshot'"
    )
    bootstrap.execute("PRAGMA user_version = 3")
    bootstrap.commit()
    bootstrap.close()

    disp = SqliteDispatcher(str(db_path))
    try:
        version = disp._conn.execute("PRAGMA user_version").fetchone()[0]
        assert int(version) == SCHEMA_VERSION >= 6
        row = disp._conn.execute(
            "SELECT reclaim_count FROM dispatcher_tasks WHERE task_id = ?",
            ("legacy-task",),
        ).fetchone()
        assert row is not None
        assert int(row["reclaim_count"]) == 0
        # The legacy row is still leasable after the upgrade.
        assert disp.lease(worker_id="w", task_id="legacy-task") is not None
    finally:
        disp.close()
