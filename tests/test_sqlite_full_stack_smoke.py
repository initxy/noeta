"""Full-stack smoke: SqliteEventLog + SqliteContentStore wired together.

Issue 16 architect Q4: cheap end-to-end test that the two persistent
adapters cooperate in the canonical "large body lives in ContentStore,
EventLog references it" pattern that the event-sourcing / replay model codifies.

Walks the typical flow: put a payload body into ContentStore, emit a
``MessagesAppended`` envelope carrying the returned :class:`ContentRef`
into EventLog, read the envelope back via ``log.read``, dereference
the ref via ``cs.get``, assert byte-equal recovery. This proves the
two adapters share a single DB file. A future smoke can wire the real
``fold`` to cover the full Engine round-trip; this one focuses on the
adapter-pair contract.
"""

from __future__ import annotations

from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.wiring import wire_default_observers
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import FinishDecision, SpawnSubtaskDecision
from noeta.protocols.events import MessagesAppendedPayload, TaskCreatedPayload
from noeta.protocols.wake import SubtaskCompleted
from noeta.storage.sqlite.contentstore import SqliteContentStore
from noeta.storage.sqlite.dispatcher import SqliteDispatcher
from noeta.storage.sqlite.eventlog import SqliteEventLog
from noeta.testing.composer import trivial_three_segment


def test_eventlog_and_contentstore_share_one_sqlite_file(tmp_path) -> None:
    db = tmp_path / "noeta.db"

    log = SqliteEventLog(db)
    cs = SqliteContentStore(db)
    try:
        log.emit(
            task_id="t1",
            type="TaskCreated",
            payload=TaskCreatedPayload(goal="g", policy_name="p"),
        )

        # Real payload too large for the EventLog inline cap goes into
        # ContentStore first.
        body = (b"large body chunk " * 1024)  # ~17 KB
        ref = cs.put(body, media_type="application/octet-stream")
        log.emit(
            task_id="t1",
            type="MessagesAppended",
            payload=MessagesAppendedPayload(messages_ref=ref, count=1),
        )

        events = log.read("t1")
        assert [e.type for e in events] == ["TaskCreated", "MessagesAppended"]
        appended = events[1]
        assert isinstance(appended.payload, MessagesAppendedPayload)
        # The ref carried on the envelope must dereference to the
        # original body via the shared ContentStore.
        assert cs.get(appended.payload.messages_ref) == body
    finally:
        cs.close()
        log.close()


def test_eventlog_contentstore_dispatcher_spawn_subtask_end_to_end(tmp_path) -> None:
    """Full Phase 1 sqlite stack: EL + CS + Dispatcher running a real
    parent → child → wake parent → finish loop. Proves the three
    persistent adapters cooperate with the Engine, fold, and
    ChildLifecycleObserver wiring without any InMemory backend in the
    runtime stack (issue 17 architect Q7).
    """
    db = tmp_path / "noeta.db"

    log = SqliteEventLog(db, lease_validator=None)
    cs = SqliteContentStore(db)
    disp = SqliteDispatcher(db)
    log.bind_lease_registry(disp)
    wire_default_observers(log, disp)

    try:
        parent_engine = Engine(
            event_log=log,
            content_store=cs,
            composer=trivial_three_segment(cs),
            policy=StubScriptedPolicy(
                [
                    SpawnSubtaskDecision(
                        agent_name="child_agent",
                        goal="do thing",
                        inputs={},
                    ),
                    FinishDecision(answer="parent done"),
                ]
            ),
        )
        child_engine = Engine(
            event_log=log,
            content_store=cs,
            composer=trivial_three_segment(cs),
            policy=StubScriptedPolicy([FinishDecision(answer="child done")]),
        )

        parent = parent_engine.create_task(goal="parent", policy_name="scripted")
        disp.enqueue(parent.task_id)

        p_lease_1 = disp.lease(worker_id="w1")
        assert p_lease_1 is not None and p_lease_1.task_id == parent.task_id
        parent = parent_engine.run_one_step(parent, lease_id=p_lease_1.lease_id)
        assert parent.status == "suspended"
        disp.release(
            p_lease_1.lease_id,
            next_state="suspended",
            wake_on=parent.wake_on,
        )

        c_lease = disp.lease(worker_id="w1")
        assert c_lease is not None
        child_id = c_lease.task_id

        child = fold(log, cs, child_id)
        child = child_engine.run_one_step(child, lease_id=c_lease.lease_id)
        assert child.status == "terminal"
        disp.release(c_lease.lease_id, next_state="terminal")

        # The child-lifecycle observer should have system_emit'd
        # SubtaskCompleted to the parent stream AND woken the
        # dispatcher.
        p_lease_2 = disp.lease(worker_id="w1")
        assert p_lease_2 is not None and p_lease_2.task_id == parent.task_id

        parent = fold(log, cs, parent.task_id)
        parent_engine.note_woken(
            parent,
            lease_id=p_lease_2.lease_id,
            wake_event=SubtaskCompleted(subtask_id=child_id),
        )
        parent = parent_engine.run_one_step(
            parent, lease_id=p_lease_2.lease_id
        )
        assert parent.status == "terminal"
        disp.release(p_lease_2.lease_id, next_state="terminal")

        parent_types = [e.type for e in log.read(parent.task_id)]
        for required in (
            "TaskCreated",
            "SubtaskSpawned",
            "SubtaskCompleted",
            "TaskWoken",
            "TaskCompleted",
        ):
            assert required in parent_types

        # SubtaskCompleted is system-emitted by the observer; check
        # origin to prove the cross-stream system write went through
        # the sqlite EventLog the same way it does with InMemory.
        st_comp = next(
            e for e in log.read(parent.task_id) if e.type == "SubtaskCompleted"
        )
        assert st_comp.origin == "observer"
        assert st_comp.payload.subtask_id == child_id

        # is_lease_valid is the hot wire between EventLog and the
        # SqliteDispatcher LeaseRegistry. By this point all leases
        # have been released, so the validator must return False for
        # the no-longer-held lease ids.
        assert disp.is_lease_valid(parent.task_id, p_lease_2.lease_id) is False

        # Fold-side: parent's GovernanceState should carry the child's
        # completed SubtaskResult (issue 17 acceptance).
        final_parent = fold(log, cs, parent.task_id)
        results = final_parent.governance.subtask_results
        assert len(results) == 1
        assert results[0].status == "completed"
        assert results[0].output == "child done"
    finally:
        disp.close()
        cs.close()
        log.close()


def test_reopening_recovers_eventlog_and_contentstore_state(tmp_path) -> None:
    """Both adapters on the same file must see the same persisted
    schema on reopen, and the cross-adapter wiring still works after
    a restart cycle."""
    db = tmp_path / "noeta.db"

    log = SqliteEventLog(db)
    cs = SqliteContentStore(db)
    try:
        ref = cs.put(b"persistent body", media_type="text/plain")
        log.emit(
            task_id="t-persist",
            type="MessagesAppended",
            payload=MessagesAppendedPayload(messages_ref=ref, count=1),
        )
    finally:
        cs.close()
        log.close()

    log2 = SqliteEventLog(db)
    cs2 = SqliteContentStore(db)
    try:
        events = log2.read("t-persist")
        assert len(events) == 1
        assert events[0].payload.messages_ref == ref
        assert cs2.get(events[0].payload.messages_ref) == b"persistent body"
    finally:
        cs2.close()
        log2.close()
