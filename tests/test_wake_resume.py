"""Acceptance tests for wake-resume + task-id resume (issue 26).

Covers the design doc's W1–W6 watchpoints, the projection-matching
invariant, the at-most-once-loss / crash-then-requeue path, and the
sqlite migration's NULL backfill.

Pure-L0 / storage-layer tests live here; CLI-level coverage is in
``test_cli_commands.py`` and ``test_cli_resume_targeted.py``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from noeta.protocols.dispatcher import Lease
from noeta.protocols.wake import (
    HumanResponseReceived,
    SubtaskCompleted,
    SubtaskResult,
    TimerFired,
    matches_wake,
)
from noeta.storage.memory import InMemoryDispatcher
from noeta.storage.sqlite.dispatcher import SqliteDispatcher


# ---------------------------------------------------------------------------
# Adapter parametrisation
# ---------------------------------------------------------------------------


@pytest.fixture(params=["memory", "sqlite"])
def make_dispatcher(request, tmp_path):
    """Yield a factory that builds a fresh dispatcher of each kind.

    Sqlite uses ``:memory:`` so the test file does not pollute the
    workspace; the test_sqlite_*.py durability suite covers on-disk
    behaviour separately.
    """
    kind = request.param

    def _factory() -> Any:
        if kind == "memory":
            return InMemoryDispatcher()
        return SqliteDispatcher(":memory:")

    factory = _factory
    factory.kind = kind  # type: ignore[attr-defined]
    return factory


# ---------------------------------------------------------------------------
# matches_wake — L0 truth table (projection invariant + temporal >=)
# ---------------------------------------------------------------------------


def test_matches_wake_subtask_projection_ignores_result() -> None:
    condition = SubtaskCompleted(subtask_id="t-child")
    event_r1 = SubtaskCompleted(
        subtask_id="t-child", result=SubtaskResult(status="completed", output=1)
    )
    event_r2 = SubtaskCompleted(
        subtask_id="t-child", result=SubtaskResult(status="failed", error="x")
    )
    assert matches_wake(condition, event_r1) is True
    assert matches_wake(condition, event_r2) is True


def test_matches_wake_subtask_different_id_returns_false() -> None:
    assert (
        matches_wake(
            SubtaskCompleted(subtask_id="X"),
            SubtaskCompleted(subtask_id="Y"),
        )
        is False
    )


def test_matches_wake_human_response_equality_on_handle() -> None:
    assert (
        matches_wake(
            HumanResponseReceived(handle="reply-1"),
            HumanResponseReceived(handle="reply-1"),
        )
        is True
    )
    assert (
        matches_wake(
            HumanResponseReceived(handle="reply-1"),
            HumanResponseReceived(handle="reply-2"),
        )
        is False
    )


def test_matches_wake_timer_fired_threshold_semantics() -> None:
    # Equality boundary — inclusive
    assert matches_wake(TimerFired(fire_at=100.0), TimerFired(fire_at=100.0)) is True
    # Event observed after condition's deadline — matches (timer elapsed)
    assert matches_wake(TimerFired(fire_at=100.0), TimerFired(fire_at=200.0)) is True
    # Event observed before deadline — does not match (timer not elapsed)
    assert matches_wake(TimerFired(fire_at=100.0), TimerFired(fire_at=50.0)) is False


def test_matches_wake_cross_variant_never_matches() -> None:
    assert (
        matches_wake(
            SubtaskCompleted(subtask_id="X"),
            HumanResponseReceived(handle="X"),
        )
        is False
    )
    assert (
        matches_wake(
            TimerFired(fire_at=0.0),
            SubtaskCompleted(subtask_id="anything"),
        )
        is False
    )
    assert (
        matches_wake(
            HumanResponseReceived(handle="h"),
            TimerFired(fire_at=0.0),
        )
        is False
    )


# ---------------------------------------------------------------------------
# Targeted lease — W3 watchpoint
# ---------------------------------------------------------------------------


def test_targeted_lease_ready_task_succeeds(make_dispatcher) -> None:
    disp = make_dispatcher()
    disp.enqueue("t1")

    lease = disp.lease(worker_id="w", task_id="t1")

    assert lease is not None
    assert lease.task_id == "t1"
    assert lease.wake_event is None


def test_targeted_lease_unknown_task_returns_none(make_dispatcher) -> None:
    disp = make_dispatcher()

    assert disp.lease(worker_id="w", task_id="nope") is None


def test_targeted_lease_other_ready_task_returns_none(make_dispatcher) -> None:
    """A targeted lease on a task that exists in ready does NOT
    accidentally pick up a different ready task (no fallback to FIFO).
    """
    disp = make_dispatcher()
    disp.enqueue("t1")
    disp.enqueue("t2")

    lease = disp.lease(worker_id="w", task_id="t-missing")

    assert lease is None
    # The two ready tasks are still leasable via untargeted lease.
    untargeted = disp.lease(worker_id="w")
    assert untargeted is not None
    assert untargeted.task_id in {"t1", "t2"}


def test_targeted_lease_leased_task_returns_none(make_dispatcher) -> None:
    disp = make_dispatcher()
    disp.enqueue("t1")
    first = disp.lease(worker_id="w1", task_id="t1")
    assert first is not None

    assert disp.lease(worker_id="w2", task_id="t1") is None


def test_targeted_lease_suspended_task_returns_none(make_dispatcher) -> None:
    disp = make_dispatcher()
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w", task_id="t1")
    assert lease is not None
    disp.release(
        lease.lease_id,
        next_state="suspended",
        wake_on=HumanResponseReceived(handle="x"),
    )

    assert disp.lease(worker_id="w", task_id="t1") is None


def test_targeted_lease_terminal_task_returns_none(make_dispatcher) -> None:
    disp = make_dispatcher()
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w", task_id="t1")
    assert lease is not None
    disp.release(lease.lease_id, next_state="terminal")

    assert disp.lease(worker_id="w", task_id="t1") is None


# ---------------------------------------------------------------------------
# Wake-event lease handoff (W1 / W5)
# ---------------------------------------------------------------------------


def test_lease_after_wake_carries_wake_event(make_dispatcher) -> None:
    disp = make_dispatcher()
    disp.enqueue("t1")
    lease1 = disp.lease(worker_id="w", task_id="t1")
    assert lease1 is not None
    disp.release(
        lease1.lease_id,
        next_state="suspended",
        wake_on=SubtaskCompleted(subtask_id="t-child"),
    )

    event = SubtaskCompleted(
        subtask_id="t-child",
        result=SubtaskResult(status="completed", output="payload"),
    )
    assert disp.wake("t1", event) is True

    lease2 = disp.lease(worker_id="w", task_id="t1")
    assert lease2 is not None
    assert isinstance(lease2.wake_event, SubtaskCompleted)
    assert lease2.wake_event.subtask_id == "t-child"
    assert lease2.wake_event.result == SubtaskResult(
        status="completed", output="payload"
    )


def test_lease_after_release_drain_carries_pending_wake_event(
    make_dispatcher,
) -> None:
    """release(suspended) drains a matching pending wake → matched_wake_event
    is set on the row → next lease delivers it on Lease.wake_event."""
    disp = make_dispatcher()
    disp.enqueue("t1")
    lease1 = disp.lease(worker_id="w", task_id="t1")
    assert lease1 is not None

    # Wake arrives before suspend → buffered as pending
    event = HumanResponseReceived(handle="reply-99")
    assert disp.wake("t1", event) is False

    # Task suspends with matching wake_on → pending drains, matched_wake_event set
    disp.release(
        lease1.lease_id,
        next_state="suspended",
        wake_on=HumanResponseReceived(handle="reply-99"),
    )

    lease2 = disp.lease(worker_id="w", task_id="t1")
    assert lease2 is not None
    assert lease2.wake_event == event


def test_lease_does_not_consume_requeue_redelivers_wake_event(
    make_dispatcher,
) -> None:
    """H2 — exactly-once via at-least-once re-delivery:
    ``lease()`` does NOT clear ``matched_wake_event``; if the worker crashes
    (simulated by not calling release) and the lease expires,
    ``requeue_stale`` brings the task back to ready WITH the matched wake
    preserved, so the next ``lease()`` **re-delivers** the same wake_event.
    (Pre-H2 this was an at-most-once loss; H2 inverts it.)"""
    disp = make_dispatcher()
    if isinstance(disp, SqliteDispatcher):
        # Force a deterministic clock so we can drive lease expiry below.
        clock_t = [1000.0]

        def now() -> float:
            return clock_t[0]

        disp.close()
        disp = SqliteDispatcher(":memory:", now=now)
    else:
        clock_t = [1000.0]
        disp = InMemoryDispatcher(now=lambda: clock_t[0])

    disp.enqueue("t1")
    first = disp.lease(worker_id="w", task_id="t1", lease_seconds=10.0)
    assert first is not None
    disp.release(
        first.lease_id,
        next_state="suspended",
        wake_on=SubtaskCompleted(subtask_id="t-child"),
    )

    event = SubtaskCompleted(
        subtask_id="t-child", result=SubtaskResult(status="completed")
    )
    assert disp.wake("t1", event) is True

    leased = disp.lease(worker_id="w", task_id="t1", lease_seconds=10.0)
    assert leased is not None
    assert leased.wake_event == event

    # Simulate worker crash — never release. Advance clock past expiry,
    # then requeue_stale brings the task back to ready.
    clock_t[0] += 100.0
    requeued = disp.requeue_stale()
    assert "t1" in requeued

    redelivered = disp.lease(worker_id="w", task_id="t1", lease_seconds=10.0)
    assert redelivered is not None
    assert redelivered.wake_event == event, (
        "H2: lease must NOT consume matched_wake_event; requeue_stale must "
        "preserve it so the next lease re-delivers it (at-least-once)"
    )
    # And a consuming release now clears it (D2): after re-delivery, release
    # with consumed_wake_event drops matched; a further requeue re-leases
    # with no wake.
    disp.release(
        redelivered.lease_id, next_state="terminal", consumed_wake_event=event
    )


# ---------------------------------------------------------------------------
# B1 regression — enqueue(non-ready) must clear matched_wake_event
# ---------------------------------------------------------------------------


def test_enqueue_clears_matched_wake_on_non_ready_row(make_dispatcher) -> None:
    """``enqueue(task_id)`` is a force-reset of the row's lifecycle
    fields when the row is currently non-ready. Any stale
    ``matched_wake_event`` (e.g. from a future code path or a partial
    failure leaving a suspended row with the field still set) must be
    cleared so the next ``lease()`` does not deliver a wake_event the
    caller never asked for. B1 invariant: matched wake_event is owned
    by the single wake → lease handoff that produced it."""
    disp = make_dispatcher()
    disp.enqueue("t1")
    lease1 = disp.lease(worker_id="w", task_id="t1")
    assert lease1 is not None
    disp.release(
        lease1.lease_id,
        next_state="suspended",
        wake_on=SubtaskCompleted(subtask_id="x"),
    )

    # Stamp matched_wake directly on the suspended row to simulate
    # the "stale matched state" the B1 fix defends against.
    event = SubtaskCompleted(
        subtask_id="x", result=SubtaskResult(status="completed", output="stale")
    )
    if isinstance(disp, InMemoryDispatcher):
        disp._tasks["t1"].matched_wake_event = event
    else:  # SqliteDispatcher
        from noeta.protocols.canonical import to_canonical_bytes

        disp._conn.execute(
            "UPDATE dispatcher_tasks SET matched_wake_event_canonical = ? "
            "WHERE task_id = ?",
            (to_canonical_bytes(event), "t1"),
        )
        disp._conn.commit()

    disp.enqueue("t1")  # non-ready → ready: must clear matched_wake

    lease2 = disp.lease(worker_id="w", task_id="t1")
    assert lease2 is not None
    assert lease2.wake_event is None, (
        "enqueue() must clear stale matched_wake_event on non-ready "
        "→ ready transition (B1)."
    )


# ---------------------------------------------------------------------------
# Sqlite migration — NULL backfill (Q8)
# ---------------------------------------------------------------------------


def test_sqlite_migration_adds_matched_wake_column(tmp_path: Path) -> None:
    """A fresh dispatcher creates the column at the current schema
    version; rows pre-existing the migration would see NULL backfill.
    """
    db_path = tmp_path / "wake_resume_migration.sqlite"
    disp = SqliteDispatcher(str(db_path))
    try:
        cols = {
            row[1]
            for row in disp._conn.execute(
                "PRAGMA table_info(dispatcher_tasks)"
            ).fetchall()
        }
        assert "matched_wake_event_canonical" in cols

        version = disp._conn.execute("PRAGMA user_version").fetchone()[0]
        assert int(version) >= 4
    finally:
        disp.close()


def test_sqlite_migration_null_backfill_on_pre_migration_rows(
    tmp_path: Path,
) -> None:
    """Construct a database at schema version 3 (pre-wake-resume),
    insert a row, then open it again so migration 4 runs against
    existing rows. The new column must be NULL on the pre-existing row
    (sqlite ALTER TABLE ADD COLUMN default-fill behaviour)."""
    db_path = tmp_path / "wake_resume_backfill.sqlite"

    # Manually create a version-3 DB to simulate an upgrade scenario.
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
    # A real version-3 DB also carries the events table + snapshot index from
    # migration 1 (every DB advances through 1→3 in order). Create them so the
    # synthetic fixture is faithful — migration 5 rebuilds ``ix_events_snapshot``
    # and would otherwise have no events table / index to operate on.
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

    # Re-open via SqliteDispatcher — migration 4 must run and add the
    # column with NULL for the pre-existing row.
    disp = SqliteDispatcher(str(db_path))
    try:
        row = disp._conn.execute(
            "SELECT matched_wake_event_canonical FROM dispatcher_tasks "
            "WHERE task_id = ?",
            ("legacy-task",),
        ).fetchone()
        assert row is not None
        assert row["matched_wake_event_canonical"] is None
        # The legacy task still exists at status='ready' and can be leased.
        lease = disp.lease(worker_id="w", task_id="legacy-task")
        assert lease is not None
        assert lease.wake_event is None
    finally:
        disp.close()


# ---------------------------------------------------------------------------
# Lease.wake_event default + dataclass shape (W2)
# ---------------------------------------------------------------------------


def test_lease_dataclass_wake_event_default_none() -> None:
    lease = Lease(lease_id="L", task_id="t", expires_at=0.0)
    assert lease.wake_event is None
