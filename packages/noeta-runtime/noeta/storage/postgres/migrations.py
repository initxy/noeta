"""Schema migrations shared across every Postgres backend adapter.

All three adapters land in the **same** database; the migration
sequence here is the single source of truth for its schema. A one-row
``noeta_schema_version`` table records how far the database has been
advanced (the ``PRAGMA user_version`` analogue); each
:class:`Migration` is applied in one transaction under the migrations
advisory lock, so a partial failure rolls back atomically and
concurrent initialisers serialise instead of racing DDL.

The Postgres sequence is **independent** of the sqlite one: sqlite's
migrations 1–7 are that file format's upgrade history, while a fresh
Postgres database starts directly at the consolidated head schema
(version 1 below = sqlite schema version 7). Type mapping from the
sqlite DDL: TEXT→TEXT, INTEGER→BIGINT, REAL→DOUBLE PRECISION,
BLOB→BYTEA; ``WITHOUT ROWID`` clustering has no Postgres equivalent and
is simply dropped. Objects are created unqualified, so they land in the
first schema of the connection's ``search_path`` — the contract suite
uses that for per-test schema isolation.

Forward-only. Downgrades are out of scope; a backwards-incompatible
change requires a new database and an explicit migration tool.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from noeta.storage.postgres._connection import _ADVISORY_CLASS_MIGRATIONS


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


# Migration 1: the consolidated head schema (= sqlite migrations 1–7).
#
# ``events`` stores envelope metadata column-by-column so inspect /
# index queries stay relational; ``payload_canonical`` is the canonical
# bytes produced by :func:`noeta.protocols.canonical.to_canonical_bytes`.
# ``idempotency`` lives in its own table because ``lease_id`` /
# ``idempotency_key`` are write-time concurrency metadata, not envelope
# content — keeping the events row column set equal to the
# :class:`noeta.protocols.events.EventEnvelope` field set keeps the
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

# Partial index matching the exact ``find_latest_snapshot`` predicate
# (sqlite migration 5's widened form — TaskRewound is a snapshot-shaped
# fold baseline too), so the lookup is an indexed single-row hit.
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
# INSERT/UPDATE bypassing the adapter is rejected. Includes the columns
# sqlite added incrementally: ``matched_wake_event_canonical``
# (migration 4), ``reclaim_count`` (6), ``fire_at`` (7).
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


# Migration 2 (= sqlite migration 8): widen the fold-baseline index to
# include the crash-recovery seal ``StepAttemptAbandoned`` — a partial
# index is only chosen when its WHERE matches the query predicate
# exactly, so it is re-created with the widened IN-list. The list is a
# frozen literal (applied migrations are immutable); the live queries
# render theirs from
# ``noeta.protocols.event_log.SNAPSHOT_BASELINE_EVENT_TYPES``, so growing
# that constant requires a NEW migration re-widening this index.
_MIGRATION_2_DROP_SNAPSHOT_INDEX = "DROP INDEX IF EXISTS ix_events_snapshot"

_MIGRATION_2_BASELINE_INDEX = (
    "CREATE INDEX ix_events_snapshot "
    "ON events (task_id, seq DESC) "
    "WHERE type IN ('TaskSnapshot', 'TaskRewound', 'StepAttemptAbandoned')"
)


# Migration 3: nullable audit column recording which worker holds the
# lease (ADR multi-host-lease-fencing.md D3). Populated by ``lease()``,
# cleared by every transition that clears ``lease_id``. Observability
# only — NOT a fencing token; no index, no CHECK.
_MIGRATION_3_WORKER_ID = (
    "ALTER TABLE dispatcher_tasks ADD COLUMN worker_id TEXT NULL"
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
]


#: Highest version reachable by :func:`apply_migrations`.
SCHEMA_VERSION: int = MIGRATIONS[-1].version


def apply_migrations(conn: psycopg.Connection) -> None:
    """Advance ``conn``'s database to :data:`SCHEMA_VERSION`.

    Loop-per-step structure mirroring the sqlite runner: each iteration
    opens a transaction, takes the migrations advisory lock, re-reads
    the recorded version **inside** the lock, and either commits the
    next pending migration's DDL + version bump or returns when there is
    nothing left to do — so two connections initialising the same
    database serialise and the loser sees the winner's bump instead of
    re-running DDL. Each migration and its version bump are atomic;
    re-running after success is a no-op.

    The version ledger itself (``noeta_schema_version``) is created
    idempotently outside the numbered sequence: a one-row table seeded
    at 0, the ``PRAGMA user_version`` analogue.
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
