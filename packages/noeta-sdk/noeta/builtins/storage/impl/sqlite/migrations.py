"""Schema migrations shared across every sqlite backend adapter.

All three adapters land in the **same** sqlite file, so this sequence is the
single source of truth for that file's schema: ``PRAGMA user_version`` records
how far a file has been advanced, and each :class:`Migration` applies inside
one ``BEGIN IMMEDIATE`` transaction so a partial failure rolls back atomically
and the next init retries cleanly. Forward-only — downgrades are out of scope,
and a backwards-incompatible change requires a new file plus an explicit
migration tool. An applied migration is immutable: correcting one means
appending another and bumping ``SCHEMA_VERSION``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from noeta.protocols.canonical import from_canonical_bytes
from noeta.protocols.wake import TimerFired
from noeta.builtins.storage.impl.sqlite._transaction import _begin_immediate_with_retry


__all__ = [
    "MIGRATIONS",
    "Migration",
    "SCHEMA_VERSION",
    "apply_migrations",
]


def _timer_fire_at(blob: object) -> Optional[float]:
    """Decode a ``wake_on_canonical`` blob and return the ``TimerFired``
    deadline, or ``None`` for a NULL / non-timer / undecodable blob.

    Registered on each connection as the SQL function ``_noeta_timer_fire_at``
    so migration 7's backfill can seed ``fire_at`` out of the opaque canonical
    blob, which plain SQL cannot decode. A poison row yields ``None`` — left
    un-swept, exactly as the live sweep's per-row guard treats it — rather than
    aborting the whole migration.
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

    ``statements`` holds single SQL statements executed in order. Never
    ``executescript``: it issues an implicit ``COMMIT`` before running, which
    would break the ``BEGIN IMMEDIATE`` boundary keeping each migration atomic
    with its ``PRAGMA user_version`` bump.
    """

    version: int
    description: str
    statements: tuple[str, ...]


# Migration 1: events + idempotency tables.
#
# The ``events`` table stores envelope metadata column-by-column so inspect /
# index queries (``(task_id, seq)``, ``(task_id, type)``) stay relational.
# ``payload_canonical`` holds the bytes produced by
# :func:`noeta.protocols.canonical.to_canonical_bytes` — the same
# single-source-of-truth path used for Snapshot bodies and ContentStore hashes.
#
# ``WITHOUT ROWID`` makes ``(task_id, seq)`` the clustered key so append-order
# writes are physically sequential on disk. The partial index here is narrower
# than the live ``find_latest_snapshot`` predicate and therefore unusable by
# it; migration 5 (and later 8 / 10) re-creates it to match. It stays in this
# form because an applied migration is immutable.
#
# ``idempotency`` lives in its own table because ``lease_id`` /
# ``idempotency_key`` are write-time concurrency metadata, not envelope
# content. The InMemory adapter does not store them on the envelope either;
# keeping the events row column set equal to the
# :class:`noeta.protocols.events.EventEnvelope` field set is what makes the
# two adapters interchangeable under the contract suite.
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


# Migration 2: content blobs.
#
# Content is keyed solely by ``hash``, which is what the dedup-by-hash rule and
# the hash-only ``ContentStore.get`` lookup require. ``media_type`` is recorded
# for the first put but takes no part in dedup; see
# ``noeta.protocols.content_store`` and ``noeta.protocols.values.ContentRef``
# for the contract.
#
# The CHECK constraints enforce the three storage invariants a caller
# bypassing the adapter could otherwise violate: 64-char hex hash,
# non-negative size, and ``size == length(body)``.
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


# Migration 3: SqliteDispatcher tables.
#
# Single row per task in ``dispatcher_tasks`` carrying status + lease +
# suspend metadata; CHECK constraints physicalise three state-machine
# invariants (status enum, ready⇔ready_order, leased⇔lease_id +
# lease_expires_at) so any direct INSERT/UPDATE bypassing the adapter is
# rejected. ``dispatcher_pending_wakes`` keeps a per-task FIFO of wake events;
# it has **no FK** because ``wake(unknown, ...)`` may legitimately arrive
# before any ``enqueue`` creates the task row.
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


# Migration 4: persist the matched wake_event between ``wake()`` /
# ``release(suspended)``-drain and the next ``lease()``.
#
# When a wake matches a task's stored ``wake_on``, the originating event must
# survive long enough for the resume path to write the durable
# ``TaskWoken(wake_event=...)`` envelope. The InMemory adapter holds it in
# ``_DispatcherTask.matched_wake_event``; this column is the sqlite equivalent.
#
# ``matched_wake_event_canonical`` may be NULL in any state, so it needs no
# CHECK of its own — the ``status = 'leased'`` CHECK already covers the
# structural invariants. Existing rows backfill to NULL implicitly through
# sqlite's ``ALTER TABLE ... ADD COLUMN`` default fill.
_MIGRATION_4_MATCHED_WAKE = (
    "ALTER TABLE dispatcher_tasks "
    "ADD COLUMN matched_wake_event_canonical BLOB NULL"
)


# Migration 5: widen the snapshot index to the fold-baseline predicate.
#
# ``find_latest_snapshot`` looks up ``type IN ('TaskSnapshot', 'TaskRewound')``
# — TaskRewound is a snapshot-shaped baseline too. SQLite will not use a
# partial index whose WHERE is narrower than the query predicate, so the
# migration-1 index cannot serve that query: the lookup degrades to a reverse
# PRIMARY KEY walk, O(tail-since-last-baseline) on the hot fold/resume path,
# while every TaskSnapshot insert still pays to maintain an index nobody reads.
# Re-creating the index with the query's exact ``IN`` predicate is what makes
# it eligible, turning the lookup into an indexed single-row hit.
_MIGRATION_5_DROP_SNAPSHOT_INDEX = "DROP INDEX IF EXISTS ix_events_snapshot"

_MIGRATION_5_BASELINE_INDEX = (
    "CREATE INDEX ix_events_snapshot "
    "ON events (task_id, seq DESC) "
    "WHERE type IN ('TaskSnapshot', 'TaskRewound')"
)


# Migration 6: stale-reclaim attempt counter.
#
# Without it ``requeue_stale`` returns an expired lease to ready
# unconditionally, so a poison task that silently kills its worker loops
# lease → expire → reclaim forever. The counter tracks CONSECUTIVE no-progress
# reclaims: any progress signal (successful heartbeat / clean release /
# controlled fail-requeue / force-enqueue) resets it, and at ``reclaim_max``
# the task drops to ``terminal`` with
# ``suspend_reason = 'stale_reclaim_exceeded'`` — the reclaim-path analogue of
# ``max_fail_attempts``. ``NOT NULL DEFAULT 0`` backfills existing rows to the
# correct "no reclaims observed" state (sqlite requires a DEFAULT for a
# NOT NULL ADD COLUMN).
_MIGRATION_6_RECLAIM_COUNT = (
    "ALTER TABLE dispatcher_tasks "
    "ADD COLUMN reclaim_count INTEGER NOT NULL DEFAULT 0"
)


# Migration 7: indexed timer deadline (``fire_at``) for O(due) timer sweeps.
#
# Without it, ``fire_due_timers`` must full-scan every suspended row and
# canonical-decode its ``wake_on`` blob on each ~1s poll to find due
# ``TimerFired`` waits — O(all suspends) work plus a ``BEGIN IMMEDIATE`` write
# transaction even when nothing is due. The nullable ``fire_at`` column mirrors
# the deadline of a suspended timer wait (NULL for every non-timer suspend and
# every non-suspended state); the partial index over it lets the sweep select
# the due set with ``fire_at <= now`` and skip the write transaction entirely
# when that set is empty.
#
# The adapter maintains one invariant: ``fire_at`` is written in lockstep with
# ``wake_on_canonical`` — set to the ``TimerFired`` deadline whenever a suspend
# installs a timer wait, cleared to NULL on every other write of
# ``wake_on_canonical`` (leave-suspended, non-timer suspend, terminal, ready).
# The backfill goes through the registered ``_noeta_timer_fire_at`` SQL
# function because plain SQL cannot decode the canonical blob; without it an
# in-place upgrade would strand every in-flight ``wait_timer`` suspend at NULL.
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


# Migration 8: widen the fold-baseline index to include the crash-recovery
# seal.
#
# ``StepAttemptAbandoned`` is a third snapshot-shaped fold baseline
# (``state_ref``, like TaskRewound), so ``find_latest_snapshot`` looks up
# ``type IN ('TaskSnapshot', 'TaskRewound', 'StepAttemptAbandoned')``. Same
# constraint as migration 5: a partial index is only chosen when its WHERE
# matches the query predicate exactly, so the index is re-created with the
# widened IN-list. The list is a frozen literal because an applied migration is
# immutable, while the live queries render theirs from
# ``noeta.protocols.event_log.SNAPSHOT_BASELINE_EVENT_TYPES`` — growing that
# constant therefore requires a NEW migration re-widening this index.
# ``tests/test_fix_storage.py`` pins the two in sync via the query plan.
_MIGRATION_8_DROP_SNAPSHOT_INDEX = "DROP INDEX IF EXISTS ix_events_snapshot"

_MIGRATION_8_BASELINE_INDEX = (
    "CREATE INDEX ix_events_snapshot "
    "ON events (task_id, seq DESC) "
    "WHERE type IN ('TaskSnapshot', 'TaskRewound', 'StepAttemptAbandoned')"
)


# Migration 9: targeted-lease-only guard (``reserved``) for fresh subtask
# children.
#
# A freshly-created subtask child is enqueued so its delegation drain /
# background executor can targeted-lease it, but a resident-worker pool's
# untargeted FIFO poll must NOT steal it first: only
# ``subtask_drain._descend_to_child`` seeds a child's goal into its opening
# user message, so an untargeted worker would drive it with an empty message
# history and the provider would reject the request. The boolean ``reserved``
# column (``0``/``1``) is filtered out of the untargeted
# ``lease(task_id=None)`` selection, and the FIRST successful lease clears the
# flag (a one-shot claim) so a later suspend/resume re-enqueue is an ordinary
# untargeted-leaseable task. ``NOT NULL DEFAULT 0`` backfills every existing
# row to un-reserved.
_MIGRATION_9_RESERVED_COLUMN = (
    "ALTER TABLE dispatcher_tasks "
    "ADD COLUMN reserved INTEGER NOT NULL DEFAULT 0"
)


# Migration 10: widen the fold-baseline index to include the conversation
# branch marker.
#
# ``TaskForked`` is a fourth snapshot-shaped fold baseline (``state_ref``, like
# TaskRewound) — it names the history a forked task inherited from the
# conversation it branched off, which is the only way that history folds at all
# (it is not derivable from the fork's own genesis). Same constraint as
# migrations 5 and 8: a partial index is only chosen when its WHERE matches the
# query predicate exactly, so the index is re-created with the widened IN-list.
# The list stays a frozen literal (an applied migration is immutable) while the
# live queries render theirs from
# ``noeta.protocols.event_log.SNAPSHOT_BASELINE_EVENT_TYPES``;
# ``tests/test_fix_storage.py`` pins the two in sync via the query plan.
_MIGRATION_10_DROP_SNAPSHOT_INDEX = "DROP INDEX IF EXISTS ix_events_snapshot"

_MIGRATION_10_BASELINE_INDEX = (
    "CREATE INDEX ix_events_snapshot "
    "ON events (task_id, seq DESC) "
    "WHERE type IN ("
    "'TaskSnapshot', 'TaskRewound', 'StepAttemptAbandoned', 'TaskForked'"
    ")"
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
    Migration(
        version=9,
        description="targeted-lease-only guard (reserved) for subtask children",
        statements=(_MIGRATION_9_RESERVED_COLUMN,),
    ),
    Migration(
        version=10,
        description="widen snapshot index to include TaskForked",
        statements=(
            _MIGRATION_10_DROP_SNAPSHOT_INDEX,
            _MIGRATION_10_BASELINE_INDEX,
        ),
    ),
]


#: Highest ``user_version`` reachable by :func:`apply_migrations`.
SCHEMA_VERSION: int = MIGRATIONS[-1].version


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Advance ``conn``'s schema to :data:`SCHEMA_VERSION`.

    Each iteration re-reads ``PRAGMA user_version`` **inside** its own
    ``BEGIN IMMEDIATE``, which is what makes concurrent initialisation safe:
    reading the version once up front lets two connections opening the same
    empty file both observe ``current=0``, serialise on the write lock, and the
    loser then re-runs version-1 DDL and fails with
    ``table events already exists``. Re-reading under the lock means the loser
    sees the winner's bumped version and exits cleanly.

    Each migration's DDL and its ``PRAGMA user_version`` bump share one
    transaction, so ``user_version`` never advances past a half-applied schema.
    Re-running after success is a no-op.
    """
    # Registered unconditionally: migration 7's backfill needs this decode and
    # plain SQL cannot do it. The registration is cheap, and harmless on a file
    # already at head.
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
