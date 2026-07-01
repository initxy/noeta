"""CompactionWorker end-to-end on SqliteEventLog + SqliteContentStore.

Asserts the Sqlite-backed adapter pair behaves the same as the
InMemory pair under issue 20 semantics: accelerated fold starts from
the Worker snapshot, governance counters survive a close/reopen, and
the snapshot body restores post-issue-18 governance fields.
"""

from __future__ import annotations

from noeta.core.fold import fold
from noeta.protocols.events import (
    ContextPlanComposedPayload,
    SubtaskSpawnedPayload,
    TaskCreatedPayload,
    ToolCallStartedPayload,
)
from noeta.protocols.values import ContentRef
from noeta.runtime.compaction import CompactionWorker
from noeta.storage.sqlite.contentstore import SqliteContentStore
from noeta.storage.sqlite.eventlog import SqliteEventLog


def _ref(tag: str, size: int = 10) -> ContentRef:
    return ContentRef(hash=tag * 64, size=size, media_type="application/json")


def _seed(log: SqliteEventLog, task_id: str) -> None:
    log.emit(
        task_id=task_id,
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    for i in range(6):
        log.emit(
            task_id=task_id,
            type="ContextPlanComposed",
            payload=ContextPlanComposedPayload(plan_ref=_ref(f"p{i}", size=i + 1)),
        )
    for i in range(3):
        log.emit(
            task_id=task_id,
            type="ToolCallStarted",
            payload=ToolCallStartedPayload(
                call_id=f"c{i}", tool_name="t", arguments={}
            ),
        )
    log.emit(
        task_id=task_id,
        type="SubtaskSpawned",
        payload=SubtaskSpawnedPayload(
            subtask_id="sub-0", agent_name="a", goal="g"
        ),
    )


def test_compaction_then_reopen_restores_governance(tmp_path) -> None:
    db = tmp_path / "noeta.db"
    log = SqliteEventLog(db)
    cs = SqliteContentStore(db)
    try:
        _seed(log, "t1")
        worker = CompactionWorker(
            event_log=log, content_store=cs, max_uncompacted_events=5
        )
        res = worker.compact_if_needed("t1")
        assert res.compacted
    finally:
        cs.close()
        log.close()

    log2 = SqliteEventLog(db)
    cs2 = SqliteContentStore(db)
    try:
        accelerated = fold(log2, cs2, "t1", ignore_snapshots=False)
        from_scratch = fold(log2, cs2, "t1", ignore_snapshots=True)
        assert accelerated == from_scratch
        assert accelerated.governance.iterations == 6
        assert accelerated.governance.tool_calls == 3
        assert accelerated.governance.spawned_subtasks == 1
    finally:
        cs2.close()
        log2.close()


def test_compaction_snapshot_envelope_persists_origin_and_actor(tmp_path) -> None:
    db = tmp_path / "noeta.db"
    log = SqliteEventLog(db)
    cs = SqliteContentStore(db)
    try:
        _seed(log, "t1")
        worker = CompactionWorker(
            event_log=log, content_store=cs, max_uncompacted_events=5
        )
        worker.compact_if_needed("t1")
    finally:
        cs.close()
        log.close()

    log2 = SqliteEventLog(db)
    try:
        snapshots = [e for e in log2.read("t1") if e.type == "TaskSnapshot"]
        assert len(snapshots) == 1
        assert snapshots[0].origin == "system"
        assert snapshots[0].actor == "compaction"
    finally:
        log2.close()
