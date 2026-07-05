"""Full-stack smoke for the Postgres storage adapters.

Mirrors ``tests/test_sqlite_full_stack_smoke.py`` over a live Postgres
server: the three adapters share one database, cooperate with the
Engine / fold / ChildLifecycleObserver wiring, and the storage-URL
dispatch in ``noeta.storage.stacks`` picks them for a ``postgresql://``
DSN. Every Postgres-backed test is gated on ``NOETA_TEST_POSTGRES_DSN``
(see ``tests/_pg.py``); the config-parsing tests run everywhere.
"""

from __future__ import annotations

import os

import pytest

from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.wiring import wire_default_observers
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import FinishDecision, SpawnSubtaskDecision
from noeta.protocols.events import MessagesAppendedPayload, TaskCreatedPayload
from noeta.protocols.wake import SubtaskCompleted
from noeta.testing.composer import trivial_three_segment
from tests._pg import POSTGRES_DSN_ENV, isolated_schema_dsn


requires_postgres = pytest.mark.skipif(
    not os.environ.get(POSTGRES_DSN_ENV),
    reason=f"{POSTGRES_DSN_ENV} not set",
)


# ---------------------------------------------------------------------------
# Storage-URL dispatch (no server needed beyond the gated cases)
# ---------------------------------------------------------------------------


def test_open_storage_stack_recognizes_postgres_urls() -> None:
    from noeta.storage.stacks import is_memory_path, is_postgres_url

    assert is_postgres_url("postgresql://u:p@h:5432/db")
    assert is_postgres_url("postgres://u:p@h/db")
    assert not is_postgres_url("/tmp/noeta.db")
    assert not is_postgres_url(":memory:")
    assert is_memory_path(None)
    assert is_memory_path(":memory:")


def test_backend_config_storage_url_aliases(tmp_path) -> None:
    """``NOETA_AGENT_STORAGE`` / ``storage_url`` is the general spelling;
    the legacy ``NOETA_AGENT_SQLITE`` env var keeps working and loses to
    the new one. A postgres DSN passes through un-expanded; a file path
    keeps the historical ``~`` expansion."""
    from noeta.agent.backend.lifecycle import BackendConfig

    dsn = "postgresql://u:p@h:5432/db"
    cfg = BackendConfig.from_env({"NOETA_AGENT_STORAGE": dsn})
    assert cfg.storage_url == dsn

    cfg = BackendConfig.from_env({"NOETA_AGENT_SQLITE": ":memory:"})
    assert cfg.storage_url == ":memory:"

    cfg = BackendConfig.from_env(
        {"NOETA_AGENT_STORAGE": dsn, "NOETA_AGENT_SQLITE": ":memory:"}
    )
    assert cfg.storage_url == dsn

    cfg = BackendConfig.from_env({"NOETA_AGENT_SQLITE": "~/noeta.db"})
    assert cfg.storage_url is not None
    assert "~" not in cfg.storage_url

    cfg = BackendConfig.from_env({})
    assert cfg.storage_url is None


@requires_postgres
def test_open_storage_stack_postgres_dsn_returns_postgres_adapters() -> None:
    from noeta.storage.postgres import (
        PostgresContentStore,
        PostgresDispatcher,
        PostgresEventLog,
    )
    from noeta.storage.stacks import open_storage_stack

    with isolated_schema_dsn() as dsn:
        event_log, content_store, dispatcher = open_storage_stack(dsn)
        try:
            assert isinstance(event_log, PostgresEventLog)
            assert isinstance(content_store, PostgresContentStore)
            assert isinstance(dispatcher, PostgresDispatcher)
            # The lease_validator wiring invariant holds.
            assert event_log._lease_validator is dispatcher
        finally:
            for adapter in (content_store, event_log, dispatcher):
                adapter.close()


# ---------------------------------------------------------------------------
# Adapter-pair + Engine round trips (mirroring the sqlite smoke)
# ---------------------------------------------------------------------------


@requires_postgres
def test_eventlog_and_contentstore_share_one_database() -> None:
    from noeta.storage.postgres import PostgresContentStore, PostgresEventLog

    with isolated_schema_dsn() as dsn:
        log = PostgresEventLog(dsn)
        cs = PostgresContentStore(dsn)
        try:
            log.emit(
                task_id="t1",
                type="TaskCreated",
                payload=TaskCreatedPayload(goal="g", policy_name="p"),
            )

            # Real payload too large for the EventLog inline cap goes
            # into ContentStore first.
            body = b"large body chunk " * 1024  # ~17 KB
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


@requires_postgres
def test_spawn_subtask_end_to_end_over_postgres() -> None:
    """Full Postgres stack: EL + CS + Dispatcher running a real
    parent → child → wake parent → finish loop, no InMemory backend in
    the runtime stack (the sqlite smoke's Q7 scenario on the second
    persistent backend)."""
    from noeta.storage.postgres import (
        PostgresContentStore,
        PostgresDispatcher,
        PostgresEventLog,
    )

    with isolated_schema_dsn() as dsn:
        log = PostgresEventLog(dsn, lease_validator=None)
        cs = PostgresContentStore(dsn)
        disp = PostgresDispatcher(dsn)
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
            parent = parent_engine.run_one_step(parent, lease_id=p_lease_2.lease_id)
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
            # the Postgres EventLog the same way it does with InMemory.
            st_comp = next(
                e for e in log.read(parent.task_id) if e.type == "SubtaskCompleted"
            )
            assert st_comp.origin == "observer"
            assert st_comp.payload.subtask_id == child_id

            # is_lease_valid is the hot wire between EventLog and the
            # PostgresDispatcher LeaseRegistry. By this point all leases
            # have been released, so the validator must return False for
            # the no-longer-held lease ids.
            assert disp.is_lease_valid(parent.task_id, p_lease_2.lease_id) is False

            # Fold-side: parent's GovernanceState should carry the
            # child's completed SubtaskResult.
            final_parent = fold(log, cs, parent.task_id)
            results = final_parent.governance.subtask_results
            assert len(results) == 1
            assert results[0].status == "completed"
            assert results[0].output == "child done"
        finally:
            cs.close()
            log.close()
            disp.close()


@requires_postgres
def test_migrations_are_idempotent_and_concurrent_init_safe() -> None:
    """Re-running migrations is a no-op; two adapters initialising the
    same fresh database serialise on the migrations advisory lock
    instead of racing DDL."""
    from noeta.storage.postgres.migrations import SCHEMA_VERSION, apply_migrations
    from noeta.storage.postgres._connection import _open_connection

    with isolated_schema_dsn() as dsn:
        c1 = _open_connection(dsn)
        c2 = _open_connection(dsn)
        try:
            apply_migrations(c1)
            apply_migrations(c2)  # concurrent-second-initialiser path
            apply_migrations(c1)  # idempotent re-run
            row = c1.execute("SELECT version FROM noeta_schema_version").fetchone()
            assert row is not None and int(row["version"]) == SCHEMA_VERSION
        finally:
            c1.close()
            c2.close()


@requires_postgres
def test_readonly_store_reads_without_writing() -> None:
    """The inspect mirror of ``SqliteReadOnlyStore``: reads a store the
    live adapters wrote, refuses ``put``, and the session-level
    read-only mode rejects any write server-side."""
    import psycopg

    from noeta.storage.postgres import (
        PostgresContentStore,
        PostgresEventLog,
        PostgresReadOnlyError,
        PostgresReadOnlyStore,
    )

    with isolated_schema_dsn() as dsn:
        log = PostgresEventLog(dsn)
        cs = PostgresContentStore(dsn)
        try:
            log.emit(
                task_id="t1",
                type="TaskCreated",
                payload=TaskCreatedPayload(goal="g", policy_name="p"),
            )
            ref = cs.put(b"body-bytes", media_type="text/plain")
        finally:
            cs.close()
            log.close()

        ro = PostgresReadOnlyStore(dsn)
        try:
            events = ro.read("t1")
            assert [e.type for e in events] == ["TaskCreated"]
            assert ro.find_latest_snapshot("t1") is None
            streams = ro.list_task_streams()
            assert [s.task_id for s in streams] == ["t1"]
            assert ro.get(ref) == b"body-bytes"
            with pytest.raises(PostgresReadOnlyError):
                ro.put(b"x", media_type="text/plain")
            # The read-only session characteristic blocks a raw write too.
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                ro._conn.execute("DELETE FROM events")
        finally:
            ro.close()


@requires_postgres
def test_readonly_store_rejects_uninitialised_database() -> None:
    """A schema the live adapters never migrated reads as version 0 and
    is refused up front (never silently read, never migrated)."""
    from noeta.storage.postgres import PostgresSchemaVersionError
    from noeta.storage.postgres.readonly import PostgresReadOnlyStore

    with isolated_schema_dsn() as dsn:
        with pytest.raises(PostgresSchemaVersionError) as exc:
            PostgresReadOnlyStore(dsn)
        assert exc.value.found == 0
