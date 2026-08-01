"""Schema migrations for the Postgres backend, shared by all three adapters.

The sequence here is the single source of truth for the database's schema. A
one-row ``noeta_schema_version`` table records how far the database has been
advanced, and each :class:`Migration` runs in one transaction under the
migrations advisory lock, so a partial failure rolls back atomically and
concurrent initialisers serialise instead of racing DDL. Objects are created
unqualified and therefore land in the first schema of the connection's
``search_path``, which is how the contract suite isolates one schema per test.
Forward-only: a backwards-incompatible change requires a new database and an
explicit migration tool, not a downgrade.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from noeta.builtins.storage.impl.postgres._connection import _ADVISORY_CLASS_MIGRATIONS


__all__ = [
    "MIGRATIONS",
    "Migration",
    "SCHEMA_VERSION",
    "apply_migrations",
]


@dataclass(frozen=True, slots=True)
class Migration:
    """One forward-only schema step (ordered single SQL statements)."""

    version: int
    description: str
    statements: tuple[str, ...]


# ``events`` stores envelope metadata column-by-column so inspect / index
# queries stay relational; ``payload_canonical`` is the canonical bytes
# produced by :func:`noeta.protocols.canonical.to_canonical_bytes`.
# ``idempotency`` lives in its own table because ``lease_id`` /
# ``idempotency_key`` are write-time concurrency metadata, not envelope
# content — keeping the events row column set equal to the
# :class:`noeta.protocols.events.EventEnvelope` field set is what keeps the
# adapters semantically equivalent under the contract suite.
_MIGRATION_1_EVENTS = """
CREATE TABLE events (
    task_id           TEXT             NOT NULL,
    seq               BIGINT           NOT NULL,
    id                TEXT             NOT NULL,
    type              TEXT             NOT NULL,
    schema_version    BIGINT           NOT NULL,
    occurred_at       DOUBLE PRECISION NOT NULL,
    actor             TEXT             NOT NULL,
    trace_id          TEXT             NOT NULL,
    correlation_id    TEXT             NOT NULL,
    causation_id      TEXT             NULL,
    origin            TEXT             NOT NULL,
    payload_canonical BYTEA            NOT NULL,
    PRIMARY KEY (task_id, seq)
)
""".strip()

# Partial index matching the exact ``find_latest_snapshot`` predicate, so
# that lookup is an indexed single-row hit.
_MIGRATION_1_SNAPSHOT_INDEX = (
    "CREATE INDEX ix_events_snapshot "
    "ON events (task_id, seq DESC) "
    "WHERE type IN ('TaskSnapshot', 'TaskRewound')"
)

_MIGRATION_1_IDEMPOTENCY = """
CREATE TABLE idempotency (
    task_id         TEXT   NOT NULL,
    lease_id        TEXT   NOT NULL,
    idempotency_key TEXT   NOT NULL,
    seq             BIGINT NOT NULL,
    PRIMARY KEY (task_id, lease_id, idempotency_key)
)
""".strip()

# Content is keyed solely by ``hash`` (dedup-by-hash; ``media_type`` is
# recorded for the first put but does not participate in dedup). CHECK
# constraints enforce the storage invariants any caller bypassing the
# adapter could otherwise violate.
_MIGRATION_1_CONTENT = """
CREATE TABLE content (
    hash       TEXT   NOT NULL,
    size       BIGINT NOT NULL,
    media_type TEXT   NOT NULL,
    body       BYTEA  NOT NULL,
    PRIMARY KEY (hash),
    CHECK (length(hash) = 64),
    CHECK (size >= 0),
    CHECK (size = octet_length(body))
)
""".strip()

# Single row per task carrying status + lease + suspend metadata; CHECK
# constraints physicalise the state-machine invariants (status enum,
# ready⇔ready_order, leased⇔lease_id + lease_expires_at) so any direct
# INSERT/UPDATE bypassing the adapter is rejected.
_MIGRATION_1_DISPATCHER_TASKS = """
CREATE TABLE dispatcher_tasks (
    task_id                      TEXT             PRIMARY KEY,
    status                       TEXT             NOT NULL,
    lease_id                     TEXT             NULL,
    lease_expires_at             DOUBLE PRECISION NULL,
    heartbeat_count              BIGINT           NOT NULL DEFAULT 0,
    fail_attempts                BIGINT           NOT NULL DEFAULT 0,
    wake_on_canonical            BYTEA            NULL,
    suspend_reason               TEXT             NULL,
    ready_order                  BIGINT           NULL,
    matched_wake_event_canonical BYTEA            NULL,
    reclaim_count                BIGINT           NOT NULL DEFAULT 0,
    fire_at                      DOUBLE PRECISION NULL,
    CHECK (status IN ('ready', 'leased', 'suspended', 'terminal')),
    CHECK ((status = 'ready') = (ready_order IS NOT NULL)),
    CHECK ((status = 'leased') = (lease_id IS NOT NULL AND lease_expires_at IS NOT NULL))
)
""".strip()

_MIGRATION_1_READY_INDEX = (
    "CREATE INDEX ix_dispatcher_ready "
    "ON dispatcher_tasks (ready_order) WHERE status = 'ready'"
)

_MIGRATION_1_LEASED_INDEX = (
    "CREATE INDEX ix_dispatcher_leased "
    "ON dispatcher_tasks (lease_expires_at) WHERE status = 'leased'"
)

_MIGRATION_1_LEASE_ID_INDEX = (
    "CREATE UNIQUE INDEX ix_dispatcher_lease_id "
    "ON dispatcher_tasks (lease_id) WHERE lease_id IS NOT NULL"
)

_MIGRATION_1_FIRE_AT_INDEX = (
    "CREATE INDEX ix_dispatcher_fire_at "
    "ON dispatcher_tasks (fire_at) WHERE fire_at IS NOT NULL"
)

# Per-task FIFO of buffered wake events; **no FK** because
# ``wake(unknown, ...)`` may legitimately arrive before any ``enqueue``
# creates the task row.
_MIGRATION_1_PENDING_WAKES = """
CREATE TABLE dispatcher_pending_wakes (
    task_id              TEXT   NOT NULL,
    arrival_seq          BIGINT NOT NULL,
    wake_event_canonical BYTEA  NOT NULL,
    PRIMARY KEY (task_id, arrival_seq)
)
""".strip()


# Migration 2: widen the fold-baseline index to include the crash-recovery
# seal ``StepAttemptAbandoned``. A partial index is only chosen when its
# WHERE matches the query predicate exactly, so the index has to be dropped
# and re-created with the widened IN-list. The list is a frozen literal
# (an applied migration is immutable) while the live queries render theirs
# from ``noeta.protocols.event_log.SNAPSHOT_BASELINE_EVENT_TYPES``, so
# growing that constant requires a NEW migration re-widening this index.
_MIGRATION_2_DROP_SNAPSHOT_INDEX = "DROP INDEX IF EXISTS ix_events_snapshot"

_MIGRATION_2_BASELINE_INDEX = (
    "CREATE INDEX ix_events_snapshot "
    "ON events (task_id, seq DESC) "
    "WHERE type IN ('TaskSnapshot', 'TaskRewound', 'StepAttemptAbandoned')"
)


# Migration 3: nullable audit column recording which worker holds the lease
# (see ADR multi-host-lease-fencing.md). Populated by ``lease()``, cleared by
# every transition that clears ``lease_id``. Observability only — NOT a
# fencing token; no index, no CHECK.
_MIGRATION_3_WORKER_ID = (
    "ALTER TABLE dispatcher_tasks ADD COLUMN worker_id TEXT NULL"
)


# Migration 4: targeted-lease-only guard (``reserved``) for fresh subtask
# children. They are enqueued so their delegation drain / background executor
# can targeted-lease them, but a resident-worker pool's untargeted FIFO poll
# must NOT steal them first: only ``subtask_drain._descend_to_child`` seeds a
# child's goal, so an untargeted worker would drive it with an empty message
# history and the provider would reject the request. The untargeted
# ``lease(task_id=None)`` selection filters ``reserved = false`` and the FIRST
# successful lease clears the flag — a one-shot claim.
_MIGRATION_4_RESERVED = (
    "ALTER TABLE dispatcher_tasks ADD COLUMN reserved BOOLEAN NOT NULL DEFAULT false"
)


# Migration 5: widen the fold-baseline index to include the conversation
# branch marker ``TaskForked``, which names the history a forked task
# inherits from the conversation it branched off — not derivable from the
# fork's own genesis, so the marker has to be a real baseline. Dropped and
# re-created with the widened IN-list for the same reason as migration 2.
_MIGRATION_5_DROP_SNAPSHOT_INDEX = "DROP INDEX IF EXISTS ix_events_snapshot"

_MIGRATION_5_BASELINE_INDEX = (
    "CREATE INDEX ix_events_snapshot "
    "ON events (task_id, seq DESC) "
    "WHERE type IN ("
    "'TaskSnapshot', 'TaskRewound', 'StepAttemptAbandoned', 'TaskForked'"
    ")"
)


# Migration 6: worker queue routing (ADR ``worker-queue-routing``). Every row
# carries the worker-pool name whose untargeted ``lease(task_id=None)`` poll
# may claim it — assigned once at row birth (explicit / inherited from the
# parent row / ``DEFAULT_QUEUE``), immutable afterwards; targeted leases and
# the maintenance sweeps ignore it. The ready index is re-created as
# ``(queue, ready_order)`` so the per-queue FIFO selection stays an index
# walk. The literal default is frozen here; live code renders
# ``noeta.protocols.dispatcher.DEFAULT_QUEUE``.
_MIGRATION_6_QUEUE_COLUMN = (
    "ALTER TABLE dispatcher_tasks "
    "ADD COLUMN queue TEXT NOT NULL DEFAULT 'default'"
)

_MIGRATION_6_DROP_READY_INDEX = "DROP INDEX IF EXISTS ix_dispatcher_ready"

_MIGRATION_6_READY_INDEX = (
    "CREATE INDEX ix_dispatcher_ready "
    "ON dispatcher_tasks (queue, ready_order) WHERE status = 'ready'"
)


MIGRATIONS: list[Migration] = [
    Migration(
        version=1,
        description="consolidated head schema (= sqlite schema version 7)",
        statements=(
            _MIGRATION_1_EVENTS,
            _MIGRATION_1_SNAPSHOT_INDEX,
            _MIGRATION_1_IDEMPOTENCY,
            _MIGRATION_1_CONTENT,
            _MIGRATION_1_DISPATCHER_TASKS,
            _MIGRATION_1_READY_INDEX,
            _MIGRATION_1_LEASED_INDEX,
            _MIGRATION_1_LEASE_ID_INDEX,
            _MIGRATION_1_FIRE_AT_INDEX,
            _MIGRATION_1_PENDING_WAKES,
        ),
    ),
    Migration(
        version=2,
        description="widen snapshot index to include StepAttemptAbandoned",
        statements=(
            _MIGRATION_2_DROP_SNAPSHOT_INDEX,
            _MIGRATION_2_BASELINE_INDEX,
        ),
    ),
    Migration(
        version=3,
        description="worker_id audit column on dispatcher_tasks",
        statements=(_MIGRATION_3_WORKER_ID,),
    ),
    Migration(
        version=4,
        description="targeted-lease-only guard (reserved) for subtask children",
        statements=(_MIGRATION_4_RESERVED,),
    ),
    Migration(
        version=5,
        description="widen snapshot index to include TaskForked",
        statements=(
            _MIGRATION_5_DROP_SNAPSHOT_INDEX,
            _MIGRATION_5_BASELINE_INDEX,
        ),
    ),
    Migration(
        version=6,
        description="worker queue routing (queue column + per-queue ready index)",
        statements=(
            _MIGRATION_6_QUEUE_COLUMN,
            _MIGRATION_6_DROP_READY_INDEX,
            _MIGRATION_6_READY_INDEX,
        ),
    ),
]


#: Highest version reachable by :func:`apply_migrations`.
SCHEMA_VERSION: int = MIGRATIONS[-1].version


def apply_migrations(conn: psycopg.Connection) -> None:
    """Advance ``conn``'s database to :data:`SCHEMA_VERSION`.

    One transaction per step: each iteration takes the migrations advisory
    lock and re-reads the recorded version **inside** the lock, so two
    connections initialising the same database serialise and the loser sees
    the winner's bump instead of re-running DDL. Each migration commits
    together with its version bump, which makes re-running after success a
    no-op. The version ledger itself is created idempotently outside the
    numbered sequence, since the sequence has nowhere to record itself.
    """
    while True:
        conn.execute("BEGIN")
        try:
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s, 0)",
                (_ADVISORY_CLASS_MIGRATIONS,),
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS noeta_schema_version ("
                " version BIGINT NOT NULL"
                ")"
            )
            row = conn.execute(
                "SELECT version FROM noeta_schema_version"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO noeta_schema_version (version) VALUES (0)"
                )
                current = 0
            else:
                current = int(row["version"])
            pending = next(
                (m for m in MIGRATIONS if m.version > current), None
            )
            if pending is None:
                conn.execute("COMMIT")
                return
            for statement in pending.statements:
                conn.execute(statement)
            conn.execute(
                "UPDATE noeta_schema_version SET version = %s",
                (pending.version,),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
