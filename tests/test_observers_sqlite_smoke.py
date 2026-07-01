"""Sqlite EventLog wiring smoke for AuditObserver + MetricsObserver."""

from __future__ import annotations

from noeta.observers.audit import AuditObserver, AuditRecord
from noeta.observers.metrics import MetricsObserver
from noeta.protocols.events import TaskCreatedPayload
from noeta.storage.sqlite.eventlog import SqliteEventLog


def test_audit_and_metrics_observers_subscribe_to_sqlite_eventlog(tmp_path) -> None:
    db = tmp_path / "noeta.db"
    log = SqliteEventLog(db)
    audit_records: list[AuditRecord] = []
    audit = AuditObserver(event_log=log, sink=audit_records.append)
    metrics = MetricsObserver(event_log=log)

    try:
        for i in range(5):
            log.emit(
                task_id=f"t-{i}",
                type="TaskCreated",
                payload=TaskCreatedPayload(goal=f"g{i}", policy_name="p"),
            )
        snap = metrics.snapshot()
    finally:
        audit.stop()
        metrics.stop()
        log.close()

    assert len(audit_records) == 5
    assert {rec.task_id for rec in audit_records} == {f"t-{i}" for i in range(5)}
    for rec in audit_records:
        assert rec.type == "TaskCreated"
        assert rec.origin == "engine"
    assert snap.by_type == {"TaskCreated": 5}
    assert snap.total_events == 5


def test_reopened_sqlite_does_not_replay_historical_events_to_observers(
    tmp_path,
) -> None:
    """Subscribers are process-local: a fresh observer wired against a
    reopened SqliteEventLog sees only events emitted **after** wiring,
    not the historical prefix. This pins the sync-subscriber semantics
    the Phase 1 design relies on."""
    db = tmp_path / "noeta.db"
    log = SqliteEventLog(db)
    try:
        log.emit(
            task_id="historical",
            type="TaskCreated",
            payload=TaskCreatedPayload(goal="g", policy_name="p"),
        )
    finally:
        log.close()

    log2 = SqliteEventLog(db)
    audit_records: list[AuditRecord] = []
    audit = AuditObserver(event_log=log2, sink=audit_records.append)
    metrics = MetricsObserver(event_log=log2)
    try:
        log2.emit(
            task_id="new",
            type="TaskCreated",
            payload=TaskCreatedPayload(goal="g", policy_name="p"),
        )
        snap = metrics.snapshot()
    finally:
        audit.stop()
        metrics.stop()
        log2.close()

    # Only the post-wire emit reached the observers.
    assert [rec.task_id for rec in audit_records] == ["new"]
    assert snap.by_type == {"TaskCreated": 1}
    assert snap.by_task_type == {("new", "TaskCreated"): 1}
