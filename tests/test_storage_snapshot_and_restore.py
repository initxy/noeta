"""Snapshot-baseline lookup and suspended-restore across both EventLog backends.

``find_latest_snapshot`` resolves the fold baseline via the partial
``ix_events_snapshot`` index, whose ``WHERE type IN (...)`` predicate must match
the live query's IN-list exactly — a partial index SQLite can only choose when
the predicates align, otherwise the lookup silently falls back to a reverse
PRIMARY KEY walk whose cost grows with the tail since the last baseline. The
IN-list is rendered from ``SNAPSHOT_BASELINE_EVENT_TYPES``, so widening that
constant without widening the index makes the plan fall back — which the plan
probe here catches. The lookup must also return the correct latest baseline
across TaskSnapshot / TaskRewound / TaskForked, reconciled by the outer ORDER BY.

``restore_task(status='suspended')`` must never redeliver a buffered wake as
ready: a genuinely suspended task stays unleasable.
"""

from __future__ import annotations

import pytest

from noeta.protocols.events import (
    TaskCreatedPayload,
    TaskForkedPayload,
    TaskRewoundPayload,
    TaskSnapshotPayload,
    TaskStartedPayload,
)
from noeta.protocols.event_log import SNAPSHOT_BASELINE_EVENT_TYPES
from noeta.protocols.values import ContentRef
from noeta.protocols.wake import HumanResponseReceived
from noeta.storage.memory import InMemoryDispatcher, InMemoryEventLog
from noeta.sdk.storage import SqliteDispatcher, SqliteEventLog


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
# find_latest_snapshot returns the correct latest baseline
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
    # A long tail of non-snapshot events after the snapshot: the query must
    # still resolve back to the snapshot, and the indexed lookup does not pay
    # for the tail length.
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


def test_forked_baseline_round_trips_and_wins(log) -> None:
    """A conversation branch's inherited baseline is a fold baseline like any
    other: it must survive the payload round-trip through a durable backend
    and be what ``find_latest_snapshot`` returns."""
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    log.emit(
        task_id="t1",
        type="TaskForked",
        payload=TaskForkedPayload(
            source_task_id="t0", source_seq=7, state_ref=_ref("d")
        ),
    )

    snap = log.find_latest_snapshot("t1")
    assert snap is not None
    assert snap.type == "TaskForked"
    assert snap.payload.state_ref == _ref("d")
    assert snap.payload.source_task_id == "t0"
    assert snap.payload.source_seq == 7


def test_no_snapshot_returns_none(log) -> None:
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    assert log.find_latest_snapshot("t1") is None


def test_sqlite_snapshot_lookup_uses_index() -> None:
    # A partial index is only chosen when its WHERE matches the live query
    # predicate exactly, so the planner must pick ``ix_events_snapshot`` for
    # the live lookup. The probe query renders its IN-list from
    # SNAPSHOT_BASELINE_EVENT_TYPES — exactly like the adapters — so growing
    # that constant WITHOUT a new index-widening migration fails HERE (the
    # index predicate stops matching and the plan falls back to the PK). Seed
    # rows + ANALYZE so the optimizer has stats; on an empty table SQLite
    # trivially prefers the clustered PK.
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
# restore_task(suspended) never redelivers a buffered wake as ready
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
