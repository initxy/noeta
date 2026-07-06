"""Schema migrations shared across every sqlite backend adapter.

Issues 15 / 16 / 17 all land in the **same** sqlite file. The migration
sequence here is the single source of truth for the file's schema —
``PRAGMA user_version`` records how far the file has been advanced;
each :class:`Migration` is applied in one ``BEGIN IMMEDIATE`` transaction
so a partial failure rolls back atomically and the next init retries
cleanly.

Forward-only. Downgrades are out of scope; a backwards-incompatible
change requires a new file and an explicit migration tool. Each new
adapter appends Migration entries; ``SCHEMA_VERSION`` bumps with them.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from noeta.protocols.canonical import from_canonical_bytes
from noeta.protocols.wake import TimerFired
from noeta.storage.sqlite._transaction import _begin_immediate_with_retry


__all__ = [
    "MIGRATIONS",
    "Migration",
    "SCHEMA_VERSION",
    "apply_migrations",
]


def _timer_fire_at(blob: object) -> Optional[float]:
    """Decode a ``wake_on_canonical`` blob and return the ``TimerFired``
    deadline, or ``None`` for a NULL / non-timer / undecodable blob.

    Registered on each connection as the SQL function
    ``_noeta_timer_fire_at`` so migration 7's backfill can seed the new
    ``fire_at`` column out of the opaque canonical blob (plain SQL cannot
    decode it). A poison row yields ``None`` (left un-swept, exactly as the
    live sweep's per-row guard treats it) rather than aborting the whole
    migration.
    """
    if blob is None:
        return None
    try:
        wake = from_canonical_bytes(bytes(blob))  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 — a poison row must not abort the migration
        return None
    return float(wake.fire_at) if isinstance(wake, TimerFired) else None


@dataclass(frozen=True, slots=True)
class Migration:
    """One forward-only schema step.

    ``statements`` is the ordered list of single SQL statements to
    execute. We intentionally do not use ``executescript`` because it
    issues an implicit ``COMMIT`` before running, which would defeat
    the ``BEGIN IMMEDIATE`` boundary that keeps each migration atomic
    with its ``PRAGMA user_version`` bump.
    """

    version: int
    description: str
    statements: tuple[str, ...]


# Migration 1 (issue 15): events + idempotency tables.
#
# The ``events`` table stores envelope metadata column-by-column so
# inspect / index queries (``(task_id, seq)``, ``(task_id,
# type)``) stay relational. ``payload_canonical`` is the canonical
# bytes produced by :func:`noeta.protocols.canonical.to_canonical_bytes`
# — the same single-source-of-truth path
# for Snapshot bodies and ContentStore hashes.
#
# ``WITHOUT ROWID`` turns ``(task_id, seq)`` into the clustered key so
# append-order writes are physically sequential on disk. The partial
# index on ``type='TaskSnapshot'`` was meant to accelerate the
# ``find_latest_snapshot`` lookup mandated as
# fold-loop fast — but that lookup was later broadened to
# ``type IN ('TaskSnapshot', 'TaskRewound')``, which this narrower
# partial index can no longer serve; migration 5 widens it.
#
# ``idempotency`` lives in its own table because ``lease_id`` /
# ``idempotency_key`` are write-time concurrency metadata, not envelope
# content. The InMemory adapter never stores them on the envelope
# either; keeping the events row column set equal to the
# :class:`noeta.protocols.events.EventEnvelope` field set keeps the
# adapters semantically equivalent under the contract suite.
_MIGRATION_1_EVENTS = """
CREATE TABLE events (
    task_id           TEXT    NOT NULL,
    seq               INTEGER NOT NULL,
    id                TEXT    NOT NULL,
    type              TEXT    NOT NULL,
    schema_version    INTEGER NOT NULL,
    occurred_at       REAL    NOT NULL,
    actor             TEXT    NOT NULL,
    trace_id          TEXT    NOT NULL,
    correlation_id    TEXT    NOT NULL,
    causation_id      TEXT    NULL,
    origin            TEXT    NOT NULL,
    payload_canonical BLOB    NOT NULL,
    PRIMARY KEY (task_id, seq)
) WITHOUT ROWID
""".strip()

_MIGRATION_1_SNAPSHOT_INDEX = (
    "CREATE INDEX ix_events_snapshot "
    "ON events (task_id, seq DESC) "
    "WHERE type = 'TaskSnapshot'"
)

_MIGRATION_1_IDEMPOTENCY = """
CREATE TABLE idempotency (
    task_id         TEXT    NOT NULL,
    lease_id        TEXT    NOT NULL,
    idempotency_key TEXT    NOT NULL,
    seq             INTEGER NOT NULL,
    PRIMARY KEY (task_id, lease_id, idempotency_key)
) WITHOUT ROWID
""".strip()


# Migration 2 (issue 16): content blobs.
#
# Content is keyed solely by ``hash`` — the "dedup-by-hash"
# clause and the hash-only ``ContentStore.get`` lookup pin
# this. ``media_type`` is recorded for the first put but does not
# participate in dedup; see ``noeta.protocols.content_store`` and
# ``noeta.protocols.values.ContentRef`` for the contract.
#
# CHECK constraints enforce the three storage invariants any caller
# bypassing the adapter would otherwise be able to violate: 64-char
# hex hash, non-negative size, and ``size == length(body)``.
_MIGRATION_2_CONTENT = """
CREATE TABLE content (
    hash       TEXT    NOT NULL,
    size       INTEGER NOT NULL,
    media_type TEXT    NOT NULL,
    body       BLOB    NOT NULL,
    PRIMARY KEY (hash),
    CHECK (length(hash) = 64),
    CHECK (size >= 0),
    CHECK (size = length(body))
) WITHOUT ROWID
""".strip()


# Migration 3 (issue 17): SqliteDispatcher tables.
#
# Single row per task in ``dispatcher_tasks`` carrying status + lease
# + suspend metadata; CHECK constraints physicalise three state-machine
# invariants (status enum, ready⇔ready_order, leased⇔lease_id +
# lease_expires_at) so any direct INSERT/UPDATE bypassing the adapter
# is rejected. ``dispatcher_pending_wakes`` keeps a per-task FIFO of
# wake events; it has **no FK** because ``wake(unknown, ...)`` may
# legitimately arrive before any ``enqueue`` creates the task row
# (issue 17 design B1 / G1).
_MIGRATION_3_DISPATCHER_TASKS = """
CREATE TABLE dispatcher_tasks (
    task_id            TEXT    PRIMARY KEY,
    status             TEXT    NOT NULL,
    lease_id           TEXT    NULL,
    lease_expires_at   REAL    NULL,
    heartbeat_count    INTEGER NOT NULL DEFAULT 0,
    fail_attempts      INTEGER NOT NULL DEFAULT 0,
    wake_on_canonical  BLOB    NULL,
    suspend_reason     TEXT    NULL,
    ready_order        INTEGER NULL,
    CHECK (status IN ('ready', 'leased', 'suspended', 'terminal')),
    CHECK ((status = 'ready') = (ready_order IS NOT NULL)),
    CHECK ((status = 'leased') = (lease_id IS NOT NULL AND lease_expires_at IS NOT NULL))
) WITHOUT ROWID
""".strip()

_MIGRATION_3_READY_INDEX = (
    "CREATE INDEX ix_dispatcher_ready "
    "ON dispatcher_tasks (ready_order) WHERE status = 'ready'"
)

_MIGRATION_3_LEASED_INDEX = (
    "CREATE INDEX ix_dispatcher_leased "
    "ON dispatcher_tasks (lease_expires_at) WHERE status = 'leased'"
)

_MIGRATION_3_LEASE_ID_INDEX = (
    "CREATE UNIQUE INDEX ix_dispatcher_lease_id "
    "ON dispatcher_tasks (lease_id) WHERE lease_id IS NOT NULL"
)

_MIGRATION_3_PENDING_WAKES = """
CREATE TABLE dispatcher_pending_wakes (
    task_id              TEXT    NOT NULL,
    arrival_seq          INTEGER NOT NULL,
    wake_event_canonical BLOB    NOT NULL,
    PRIMARY KEY (task_id, arrival_seq)
) WITHOUT ROWID
""".strip()


# Migration 4 (wake-resume): persist the matched wake_event between
# ``wake()`` / ``release(suspended)``-drain and the next ``lease()``.
#
# Wake-resume design rev3: when a wake matches a task's stored
# ``wake_on``, the originating event must survive long enough for the
# CLI to write the durable ``TaskWoken(wake_event=...)`` envelope on
# resume. The InMemory adapter holds it in ``_DispatcherTask.matched_wake_event``;
# this column is the sqlite equivalent.
#
# Atomic consume happens at ``lease()`` time: the column is read and
# cleared in the same UPDATE that flips the task to ``leased``. The
# CHECK that ``status = 'leased'`` implies ``lease_id NOT NULL`` /
# ``lease_expires_at NOT NULL`` already covers structural invariants;
# ``matched_wake_event_canonical`` is permitted to be NULL in any state.
# NULL-backfill of pre-migration rows is implicit in ``ALTER TABLE ...
# ADD COLUMN`` (sqlite default fill).
_MIGRATION_4_MATCHED_WAKE = (
    "ALTER TABLE dispatcher_tasks "
    "ADD COLUMN matched_wake_event_canonical BLOB NULL"
)


# Migration 5: widen the snapshot index to cover
# the actual fold baseline predicate.
#
# The migration-1 index was partial on ``type = 'TaskSnapshot'`` only,
# but ``find_latest_snapshot`` was made to look up
# ``type IN ('TaskSnapshot', 'TaskRewound')`` (TaskRewound is a
# snapshot-shaped baseline too). SQLite can't use a partial index whose
# WHERE is narrower than the query predicate, so the live IN-list query
# fell back to a reverse PRIMARY KEY walk — O(tail-since-last-baseline)
# on the hot fold/resume path — while every TaskSnapshot insert still
# paid for the now-unread index. Re-create the index with the same
# ``IN`` predicate as the query so it is actually chosen (an indexed
# single-row hit) and the write cost buys a read benefit.
_MIGRATION_5_DROP_SNAPSHOT_INDEX = "DROP INDEX IF EXISTS ix_events_snapshot"

_MIGRATION_5_BASELINE_INDEX = (
    "CREATE INDEX ix_events_snapshot "
    "ON events (task_id, seq DESC) "
    "WHERE type IN ('TaskSnapshot', 'TaskRewound')"
)


# Migration 6: stale-reclaim attempt counter (kernel #3).
#
# ``requeue_stale`` used to move an expired lease back to ready
# unconditionally, so a poison task that silently kills its worker
# loops lease → expire → reclaim forever. The counter tracks
# CONSECUTIVE no-progress reclaims; it is reset by any progress signal
# (successful heartbeat / clean release / controlled fail-requeue /
# force-enqueue) and at ``reclaim_max`` the task drops to ``terminal``
# with ``suspend_reason = 'stale_reclaim_exceeded'`` — the reclaim-path
# analogue of ``max_fail_attempts``. ``NOT NULL DEFAULT 0`` backfills
# pre-migration rows to the correct "no reclaims observed" state
# (sqlite requires a DEFAULT for a NOT NULL ADD COLUMN).
_MIGRATION_6_RECLAIM_COUNT = (
    "ALTER TABLE dispatcher_tasks "
    "ADD COLUMN reclaim_count INTEGER NOT NULL DEFAULT 0"
)


# Migration 7: indexed timer deadline (``fire_at``) for O(due) timer sweeps.
#
# ``fire_due_timers`` used to full-scan every suspended row and canonical-
# decode its ``wake_on`` blob on each ~1s poll to find due ``TimerFired``
# waits — O(all suspends) work plus a ``BEGIN IMMEDIATE`` write transaction
# even when nothing was due. This adds a nullable ``fire_at`` column that
# mirrors the deadline of a suspended timer wait (NULL for every non-timer
# suspend and every non-suspended state), a partial index over it, and a
# one-time backfill seeding the deadline out of existing suspended timer
# rows. The sweep then selects the due set straight off the index
# (``fire_at <= now``) and, when the set is empty, skips the write
# transaction entirely.
#
# The adapter maintains one invariant: ``fire_at`` is written in lockstep
# with ``wake_on_canonical`` — set to the ``TimerFired`` deadline whenever a
# suspend installs a timer wait, cleared to NULL on every other write of
# ``wake_on_canonical`` (leave-suspended, non-timer suspend, terminal,
# ready). The backfill uses the registered ``_noeta_timer_fire_at`` SQL
# function (plain SQL cannot decode the canonical blob) so an in-place
# upgrade never strands an in-flight ``wait_timer`` suspend at NULL.
_MIGRATION_7_FIRE_AT_COLUMN = (
    "ALTER TABLE dispatcher_tasks ADD COLUMN fire_at REAL NULL"
)

_MIGRATION_7_FIRE_AT_BACKFILL = (
    "UPDATE dispatcher_tasks "
    "SET fire_at = _noeta_timer_fire_at(wake_on_canonical) "
    "WHERE status = 'suspended' AND wake_on_canonical IS NOT NULL"
)

_MIGRATION_7_FIRE_AT_INDEX = (
    "CREATE INDEX ix_dispatcher_fire_at "
    "ON dispatcher_tasks (fire_at) WHERE fire_at IS NOT NULL"
)


# Migration 8: widen the fold-baseline index to include the
# crash-recovery seal.
#
# ``StepAttemptAbandoned`` is a third snapshot-shaped fold baseline
# (``state_ref``, like TaskRewound). ``find_latest_snapshot`` now looks up
# ``type IN ('TaskSnapshot', 'TaskRewound', 'StepAttemptAbandoned')``, and
# a partial index is only chosen when its WHERE matches the query
# predicate exactly (the migration-5 lesson), so the index is re-created
# with the widened IN-list.
_MIGRATION_8_DROP_SNAPSHOT_INDEX = "DROP INDEX IF EXISTS ix_events_snapshot"

_MIGRATION_8_BASELINE_INDEX = (
    "CREATE INDEX ix_events_snapshot "
    "ON events (task_id, seq DESC) "
    "WHERE type IN ('TaskSnapshot', 'TaskRewound', 'StepAttemptAbandoned')"
)


MIGRATIONS: list[Migration] = [
    Migration(
        version=1,
        description="events + idempotency (issue 15: SqliteEventLog)",
        statements=(
            _MIGRATION_1_EVENTS,
            _MIGRATION_1_SNAPSHOT_INDEX,
            _MIGRATION_1_IDEMPOTENCY,
        ),
    ),
    Migration(
        version=2,
        description="content blobs (issue 16: SqliteContentStore)",
        statements=(_MIGRATION_2_CONTENT,),
    ),
    Migration(
        version=3,
        description="dispatcher state (issue 17: SqliteDispatcher)",
        statements=(
            _MIGRATION_3_DISPATCHER_TASKS,
            _MIGRATION_3_READY_INDEX,
            _MIGRATION_3_LEASED_INDEX,
            _MIGRATION_3_LEASE_ID_INDEX,
            _MIGRATION_3_PENDING_WAKES,
        ),
    ),
    Migration(
        version=4,
        description="matched wake_event handoff (wake-resume)",
        statements=(_MIGRATION_4_MATCHED_WAKE,),
    ),
    Migration(
        version=5,
        description="widen snapshot index to fold-baseline predicate",
        statements=(
            _MIGRATION_5_DROP_SNAPSHOT_INDEX,
            _MIGRATION_5_BASELINE_INDEX,
        ),
    ),
    Migration(
        version=6,
        description="stale-reclaim attempt counter (kernel #3)",
        statements=(_MIGRATION_6_RECLAIM_COUNT,),
    ),
    Migration(
        version=7,
        description="indexed timer deadline fire_at (O(due) timer sweep)",
        statements=(
            _MIGRATION_7_FIRE_AT_COLUMN,
            _MIGRATION_7_FIRE_AT_BACKFILL,
            _MIGRATION_7_FIRE_AT_INDEX,
        ),
    ),
    Migration(
        version=8,
        description="widen snapshot index to include StepAttemptAbandoned",
        statements=(
            _MIGRATION_8_DROP_SNAPSHOT_INDEX,
            _MIGRATION_8_BASELINE_INDEX,
        ),
    ),
]


#: Highest ``user_version`` reachable by :func:`apply_migrations`.
SCHEMA_VERSION: int = MIGRATIONS[-1].version


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Advance ``conn``'s schema to :data:`SCHEMA_VERSION`.

    Loop-per-step structure (issue 16 B1): each iteration acquires
    ``BEGIN IMMEDIATE``, re-reads ``PRAGMA user_version`` **inside**
    the write lock, picks the next pending :class:`Migration`, and
    either commits its DDL + the version bump or returns when there's
    nothing left to do.

    This shape is the fix for the race that the issue-15 "read
    user_version once, then iterate" sequence had: two connections
    initialising the same empty file would both see ``current=0``,
    serialise on the write lock, and the second one would still
    re-run version-1 DDL with stale state and fail with
    ``table events already exists``. Re-reading inside the lock means
    the loser of the race sees the winner's bumped ``user_version``
    and exits cleanly.

    Atomicity: each migration's DDL statements and the matching
    ``PRAGMA user_version`` bump live in one transaction. A failure
    rolls back together, so ``user_version`` never advances past a
    half-applied schema. Idempotent: re-running after success is a
    no-op.
    """
    # Migration 7's backfill decodes each suspended timer's canonical
    # ``wake_on`` blob to seed ``fire_at``; plain SQL cannot, so expose the
    # decode as a per-connection SQL function. Registering it on every call
    # is cheap and harmless — unused once the file is already at head.
    conn.create_function("_noeta_timer_fire_at", 1, _timer_fire_at)
    while True:
        _begin_immediate_with_retry(conn)
        try:
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            pending = next(
                (m for m in MIGRATIONS if m.version > int(current)), None
            )
            if pending is None:
                conn.execute("COMMIT")
                return
            for statement in pending.statements:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {int(pending.version)}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
