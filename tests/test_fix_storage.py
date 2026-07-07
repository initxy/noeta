"""Regression tests for storage fixes (#35 / #37 / #38).

#35/#38: migration 5 widens the partial ``ix_events_snapshot`` index from
``type = 'TaskSnapshot'`` to ``type IN ('TaskSnapshot', 'TaskRewound')`` —
exactly the fold-baseline predicate ``find_latest_snapshot``
queries. Before, the narrower partial index could never be used by the
IN-list query, which fell back to a reverse PRIMARY KEY walk whose cost
grew with the tail since the last baseline; after, the lookup is an
indexed single-row hit. These tests pin both that the lookup still
returns the correct latest baseline AND that the SQLite plan now
consults the index (so it is no longer pure write-amplification).

#37: ``restore_task(status='suspended')`` deletes every buffered wake
before it could ever be drained, so a previously-buffered wake never
re-readies the task through restore_task. The drain dead code was
removed from both adapters; here we pin the observable behaviour.
"""

from __future__ import annotations

import pytest

from noeta.protocols.events import (
    TaskCreatedPayload,
    TaskRewoundPayload,
    TaskSnapshotPayload,
    TaskStartedPayload,
)
from noeta.protocols.event_log import SNAPSHOT_BASELINE_EVENT_TYPES
from noeta.protocols.values import ContentRef
from noeta.protocols.wake import HumanResponseReceived
from noeta.storage.memory import InMemoryDispatcher, InMemoryEventLog
from noeta.storage.sqlite.dispatcher import SqliteDispatcher
from noeta.storage.sqlite.eventlog import SqliteEventLog


def _ref(seed: str) -> ContentRef:
    return ContentRef(hash=seed * 64, size=10, media_type="application/json")


@pytest.fixture(params=["memory", "sqlite"])
def log(request):
    if request.param == "memory":
        instance = InMemoryEventLog()
    else:
        instance = SqliteEventLog(":memory:")
    yield instance
    close = getattr(instance, "close", None)
    if callable(close):
        close()


# ---------------------------------------------------------------------------
# #35 / #38 — find_latest_snapshot still returns the correct latest baseline
# ---------------------------------------------------------------------------


def test_latest_snapshot_wins(log) -> None:
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    log.emit(
        task_id="t1",
        type="TaskSnapshot",
        payload=TaskSnapshotPayload(state_ref=_ref("a")),
    )
    # A long tail of non-snapshot events after the snapshot: the rewritten
    # query must still resolve back to the snapshot (and the indexed lookup
    # no longer pays for the tail length, #35).
    for _ in range(50):
        log.emit(
            task_id="t1",
            type="TaskStarted",
            payload=TaskStartedPayload(lease_id="L"),
        )
    log.emit(
        task_id="t1",
        type="TaskSnapshot",
        payload=TaskSnapshotPayload(state_ref=_ref("b")),
    )

    snap = log.find_latest_snapshot("t1")
    assert snap is not None
    assert snap.type == "TaskSnapshot"
    assert snap.payload.state_ref == _ref("b")


def test_rewound_with_higher_seq_beats_earlier_snapshot(log) -> None:
    # The two UNION arms (TaskSnapshot via index, TaskRewound via PK) must
    # be reconciled by the outer ORDER BY: a later TaskRewound wins.
    log.emit(
        task_id="t1",
        type="TaskSnapshot",
        payload=TaskSnapshotPayload(state_ref=_ref("a")),
    )
    log.emit(
        task_id="t1",
        type="TaskRewound",
        payload=TaskRewoundPayload(target_seq=0, state_ref=_ref("c")),
    )

    snap = log.find_latest_snapshot("t1")
    assert snap is not None
    assert snap.type == "TaskRewound"
    assert snap.payload.state_ref == _ref("c")


def test_snapshot_after_rewound_wins(log) -> None:
    # Symmetric: a TaskSnapshot appended after a TaskRewound wins.
    log.emit(
        task_id="t1",
        type="TaskRewound",
        payload=TaskRewoundPayload(target_seq=0, state_ref=_ref("c")),
    )
    log.emit(
        task_id="t1",
        type="TaskSnapshot",
        payload=TaskSnapshotPayload(state_ref=_ref("b")),
    )

    snap = log.find_latest_snapshot("t1")
    assert snap is not None
    assert snap.type == "TaskSnapshot"
    assert snap.payload.state_ref == _ref("b")


def test_no_snapshot_returns_none(log) -> None:
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    assert log.find_latest_snapshot("t1") is None


def test_sqlite_snapshot_lookup_uses_index() -> None:
    # #38: the old partial index (WHERE type = 'TaskSnapshot') was dead —
    # the live IN-list query could never use it and fell back to the PK.
    # A partial index is only chosen when its WHERE matches the live query
    # predicate exactly (migration 5 learned this; migration 8 re-widened
    # both to include StepAttemptAbandoned), so the planner must choose it
    # for the live lookup. The probe query renders its IN-list from
    # SNAPSHOT_BASELINE_EVENT_TYPES — exactly like the adapters — so
    # growing that constant WITHOUT a new index-widening migration fails
    # HERE (the frozen index predicate stops matching and the plan falls
    # back to the PK). Seed rows + ANALYZE so the optimizer has stats; on
    # an empty table SQLite trivially prefers the clustered PK.
    elog = SqliteEventLog(":memory:")
    try:
        elog.emit(
            task_id="t1",
            type="TaskSnapshot",
            payload=TaskSnapshotPayload(state_ref=_ref("a")),
        )
        for _ in range(200):
            elog.emit(
                task_id="t1",
                type="TaskStarted",
                payload=TaskStartedPayload(lease_id="L"),
            )
        elog._conn.execute("ANALYZE")
        in_list = ", ".join(
            f"'{t}'" for t in SNAPSHOT_BASELINE_EVENT_TYPES
        )
        plan = elog._conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT * FROM events "
            f"WHERE task_id = ? AND type IN ({in_list}) "
            "ORDER BY seq DESC LIMIT 1",
            ("t1",),
        ).fetchall()
        detail = " ".join(str(r["detail"]) for r in plan)
        assert "ix_events_snapshot" in detail
    finally:
        elog.close()


# ---------------------------------------------------------------------------
# #37 — restore_task(suspended) never redelivers a buffered wake as ready
# ---------------------------------------------------------------------------


@pytest.fixture(params=["memory", "sqlite"])
def dispatcher(request):
    if request.param == "memory":
        instance = InMemoryDispatcher()
    else:
        instance = SqliteDispatcher(":memory:")
    yield instance
    close = getattr(instance, "close", None)
    if callable(close):
        close()


def test_restore_suspended_stays_suspended(dispatcher) -> None:
    wake = HumanResponseReceived(handle="r1")
    dispatcher.restore_task("t1", status="suspended", wake_on=wake)
    # No buffered wake was ever drained into ready: the task must not be
    # leasable, it is genuinely suspended.
    assert dispatcher.lease(worker_id="w1", lease_seconds=10.0) is None
