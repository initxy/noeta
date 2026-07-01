"""File-on-disk durability smoke for ``SqliteEventLog`` (issue 15).

The contract suite (`test_event_log_contract.py`) runs the in-memory
sqlite engine; this module covers the disk-only behaviours: WAL mode
actually engaged, ``synchronous=FULL`` honoured, and round-tripping
typed payloads survives a close + reopen.
"""

from __future__ import annotations

from noeta.protocols.events import (
    ConversationClosedPayload,
    ConversationReopenedPayload,
    MessagesAppendedPayload,
    TaskCreatedPayload,
    TaskSuspendedPayload,
)
from noeta.protocols.values import ContentRef
from noeta.protocols.wake import HumanResponseReceived
from noeta.storage.sqlite.eventlog import SqliteEventLog


def test_typed_payloads_survive_close_reopen(tmp_path) -> None:
    db = tmp_path / "noeta.db"

    log = SqliteEventLog(db)
    try:
        log.emit(
            task_id="t1",
            type="TaskCreated",
            payload=TaskCreatedPayload(goal="g", policy_name="p"),
        )
        log.emit(
            task_id="t1",
            type="MessagesAppended",
            payload=MessagesAppendedPayload(
                messages_ref=ContentRef(
                    hash="m" * 64, size=10, media_type="application/json"
                ),
                count=3,
            ),
        )
        log.emit(
            task_id="t1",
            type="TaskSuspended",
            payload=TaskSuspendedPayload(
                reason="waiting_human",
                wake_on=HumanResponseReceived(handle="h1"),
            ),
        )
    finally:
        log.close()

    reopened = SqliteEventLog(db)
    try:
        events = reopened.read("t1")
        assert [e.type for e in events] == [
            "TaskCreated",
            "MessagesAppended",
            "TaskSuspended",
        ]
        # Typed payload restore survives the round-trip.
        assert isinstance(events[0].payload, TaskCreatedPayload)
        assert events[0].payload.goal == "g"
        assert isinstance(events[1].payload, MessagesAppendedPayload)
        assert events[1].payload.count == 3
        assert isinstance(events[2].payload, TaskSuspendedPayload)
        assert isinstance(events[2].payload.wake_on, HumanResponseReceived)
        assert events[2].payload.wake_on.handle == "h1"
    finally:
        reopened.close()


def test_conversation_lifecycle_payloads_survive_close_reopen(tmp_path) -> None:
    """Issue 08: the new ``ConversationClosed`` / ``ConversationReopened``
    L0 payloads round-trip through the sqlite restorer (registered in
    ``_PAYLOAD_RESTORERS``), including the optional ``reason`` carried as
    ``None``."""
    db = tmp_path / "noeta.db"
    log = SqliteEventLog(db)
    try:
        log.system_emit(
            task_id="t1",
            type="ConversationClosed",
            payload=ConversationClosedPayload(closed_by="leo", reason="eod"),
            actor="engine",
            origin="engine",
        )
        log.system_emit(
            task_id="t1",
            type="ConversationReopened",
            payload=ConversationReopenedPayload(reopened_by="leo", reason=None),
            actor="engine",
            origin="engine",
        )
    finally:
        log.close()

    reopened = SqliteEventLog(db)
    try:
        events = reopened.read("t1")
        assert [e.type for e in events] == [
            "ConversationClosed",
            "ConversationReopened",
        ]
        assert isinstance(events[0].payload, ConversationClosedPayload)
        assert events[0].payload.closed_by == "leo"
        assert events[0].payload.reason == "eod"
        assert events[0].origin == "engine"
        assert isinstance(events[1].payload, ConversationReopenedPayload)
        assert events[1].payload.reopened_by == "leo"
        assert events[1].payload.reason is None
    finally:
        reopened.close()


def test_journal_mode_is_wal_on_disk(tmp_path) -> None:
    db = tmp_path / "noeta.db"
    log = SqliteEventLog(db)
    try:
        mode = log._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
    finally:
        log.close()


def test_synchronous_is_full(tmp_path) -> None:
    db = tmp_path / "noeta.db"
    log = SqliteEventLog(db)
    try:
        # PRAGMA synchronous: 0=OFF, 1=NORMAL, 2=FULL, 3=EXTRA. Issue
        # 15 sign-off pinned the default at FULL.
        value = log._conn.execute("PRAGMA synchronous").fetchone()[0]
        assert int(value) == 2
    finally:
        log.close()


def test_wal_checkpoint_truncate_succeeds(tmp_path) -> None:
    db = tmp_path / "noeta.db"
    log = SqliteEventLog(db)
    try:
        for i in range(3):
            log.emit(
                task_id="t1",
                type="TaskCreated",
                payload=TaskCreatedPayload(goal=f"g{i}", policy_name="p"),
            )
        row = log._conn.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
        # PRAGMA wal_checkpoint(TRUNCATE) returns (busy, log, checkpointed).
        # busy=0 means the checkpoint succeeded.
        assert int(row[0]) == 0
    finally:
        log.close()


def test_close_is_idempotent(tmp_path) -> None:
    log = SqliteEventLog(tmp_path / "noeta.db")
    log.close()
    log.close()  # second call must not raise


def test_context_manager_closes_on_exit(tmp_path) -> None:
    db = tmp_path / "noeta.db"
    with SqliteEventLog(db) as log:
        log.emit(
            task_id="t1",
            type="TaskCreated",
            payload=TaskCreatedPayload(goal="g", policy_name="p"),
        )
    assert log._closed
