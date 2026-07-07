"""``PostgresEventLog`` — psycopg-backed adapter for the L0 EventLog Protocols.

Second persistent EventLog backend; behaviour pinned by
:class:`noeta.storage.memory.InMemoryEventLog` and the
storage-backend-neutral contract suite, mirroring
:class:`noeta.storage.sqlite.eventlog.SqliteEventLog` structure-for-structure.

The same concurrency layers on :meth:`emit`:

1. **Idempotency** — same ``(task_id, lease_id, idempotency_key)`` twice
   returns the originally-assigned envelope without writing a new one.
2. **4-KB payload cap** — canonical bytes computed once and re-used for
   the INSERT, so the cap check and the persisted bytes share the same
   single-source serialisation path.
3. **Optimistic ``expected_seq``** — caller asserts the next slot they
   intend to claim. Mismatch raises :class:`StaleSequence`.
4. **Lease validity** — when ``lease_id`` is provided and a
   ``LeaseRegistry`` was injected, the lease must check out. Unlike
   the other adapters this one probes ``dispatcher_tasks`` first with
   an in-tx ``SELECT ... FOR SHARE`` (the multi-host fence: the
   row-share lock holds off any concurrent lease-clearing UPDATE until
   this emit commits, closing the zombie-append window — see ADR
   multi-host-lease-fencing.md D1); only when the probe finds no row
   is the registry consulted (mixed wirings keep registry semantics).

Where sqlite wraps every write in the file-wide ``BEGIN IMMEDIATE``
lock, Postgres is MVCC: each write transaction takes a per-task-stream
``pg_advisory_xact_lock`` first, so two writers cannot race on
``MAX(seq)`` within a stream while appends to different streams stay
concurrent. Subscribers fire **after** ``COMMIT`` and **outside** the
adapter lock; subscriber callbacks are free to issue further
``emit / system_emit`` calls (the ``ChildLifecycleObserver`` pattern)
because the original transaction is already durable by then.
"""

from __future__ import annotations

import threading
import time
import uuid
from types import TracebackType
from typing import Any, Callable, Mapping, Optional

from noeta.protocols.canonical import from_canonical_bytes, to_canonical_bytes
from noeta.protocols.dispatcher import LeaseRegistry
from noeta.protocols.errors import (
    InvalidLease,
    StaleSequence,
)
from noeta.protocols.event_log import (
    SNAPSHOT_BASELINE_EVENT_TYPES,
    Subscriber,
    TaskStreamSummary,
    Unsubscribe,
)
from noeta.protocols.events import EventEnvelope, EventOrigin
from noeta.protocols.values import EVENT_PAYLOAD_MAX_BYTES

from noeta.storage._payload_restore import (
    _enforce_payload_cap,
    _restore_payload,
)
from noeta.storage.postgres._connection import (
    _ADVISORY_CLASS_EVENTS,
    _DB_NOW_SQL,
    _open_connection,
)
from noeta.storage.postgres.migrations import apply_migrations

# The ``find_latest_snapshot`` predicate, rendered once from the protocol
# constant so the query can never drift from the contract set (the
# ``ix_events_snapshot`` partial index must keep matching it textually —
# see the migration notes).
_BASELINE_TYPES_SQL = "(" + ", ".join(
    f"'{t}'" for t in SNAPSHOT_BASELINE_EVENT_TYPES
) + ")"


__all__ = ["MAX_PAYLOAD_BYTES", "PostgresEventLog"]


# Adapter-local alias mirroring the sqlite module; the canonical L0 name
# is :data:`noeta.protocols.values.EVENT_PAYLOAD_MAX_BYTES`.
MAX_PAYLOAD_BYTES = EVENT_PAYLOAD_MAX_BYTES


_DEFAULT_SCHEMA_VERSION = 1


def _default_id_factory() -> str:
    return f"evt-{uuid.uuid4().hex}"


class PostgresEventLog:
    """psycopg implementation of ``EventLog`` + ``EventLogSubscriber``.

    Public surface deliberately equals the L0 Protocols (``emit``,
    ``system_emit``, ``read``, ``find_latest_snapshot``, ``subscribe``)
    plus :meth:`bind_lease_registry` (mirroring InMemory) and
    :meth:`close` (adapter-level resource release; not on the Protocol,
    callers that wire PostgresEventLog know to call it).
    """

    def __init__(
        self,
        dsn: str,
        *,
        lease_validator: Optional[LeaseRegistry] = None,
        clock: Optional[Callable[[], float]] = None,
        id_factory: Optional[Callable[[], str]] = None,
        schema_version: int = _DEFAULT_SCHEMA_VERSION,
        _emit_pause: Optional[Callable[[], None]] = None,
    ) -> None:
        self._conn = _open_connection(dsn)
        apply_migrations(self._conn)
        self._lease_validator = lease_validator
        # No injected ``clock`` (production) → the in-tx fence probe's
        # expiry predicate runs on the database clock, matching the
        # dispatcher's D2 clock base; an injected clock keeps the
        # deterministic client-side comparison for tests.
        self._db_clock = clock is None
        self._clock = clock or time.time
        # Test-only seam: invoked between the fence probe and the event
        # INSERT so multi-host tests can hold an emit transaction open
        # across a concurrent reclaim deterministically. Never set in
        # production wiring.
        self._emit_pause = _emit_pause
        self._id_factory = id_factory or _default_id_factory
        self._schema_version = schema_version
        self._subscribers: list[Subscriber] = []
        # ``threading.Lock`` (not RLock) — same-thread re-entry into
        # ``emit`` deadlocks rather than corrupting the seq counter.
        # Subscriber-driven re-emit is safe because callbacks run
        # *after* lock release; see ``_notify`` below.
        self._lock = threading.Lock()
        self._closed = False

    # -- wiring ----------------------------------------------------------

    def bind_lease_registry(self, registry: LeaseRegistry) -> None:
        """Late-bind a :class:`LeaseRegistry` (mirrors the other adapters)."""
        self._lease_validator = registry

    # -- writes ----------------------------------------------------------

    def emit(
        self,
        *,
        task_id: str,
        type: str,
        payload: Any,
        lease_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        actor: str = "engine",
        causation_id: Optional[str] = None,
        expected_seq: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        origin: EventOrigin = "engine",
    ) -> EventEnvelope:
        envelope = EventEnvelope.build(
            task_id=task_id,
            type=type,
            payload=payload,
            id=self._id_factory(),
            actor=actor,
            trace_id=trace_id,
            causation_id=causation_id,
            schema_version=self._schema_version,
            occurred_at=self._clock(),
            origin=origin,
        )
        return self._append(
            envelope,
            lease_id=lease_id,
            expected_seq=expected_seq,
            idempotency_key=idempotency_key,
            require_lease=True,
        )

    def system_emit(
        self,
        *,
        task_id: str,
        type: str,
        payload: Any,
        actor: str,
        origin: EventOrigin,
        trace_id: Optional[str] = None,
        causation_id: Optional[str] = None,
    ) -> EventEnvelope:
        envelope = EventEnvelope.build(
            task_id=task_id,
            type=type,
            payload=payload,
            id=self._id_factory(),
            actor=actor,
            trace_id=trace_id,
            causation_id=causation_id,
            schema_version=self._schema_version,
            occurred_at=self._clock(),
            origin=origin,
        )
        return self._append(
            envelope,
            lease_id=None,
            expected_seq=None,
            idempotency_key=None,
            require_lease=False,
        )

    def _lock_stream(self, task_id: str) -> None:
        """Serialise writers of one task stream for the open transaction.

        ``hashtext`` maps the task id onto the advisory objid; a hash
        collision between two task ids only over-serialises those two
        streams (correctness is unaffected). Auto-released at COMMIT /
        ROLLBACK.
        """
        self._conn.execute(
            "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
            (_ADVISORY_CLASS_EVENTS, task_id),
        )

    def _append(
        self,
        envelope: EventEnvelope,
        *,
        lease_id: Optional[str],
        expected_seq: Optional[int],
        idempotency_key: Optional[str],
        require_lease: bool,
    ) -> EventEnvelope:
        # Serialise once: the same canonical bytes feed both the 4-KB
        # cap check and the BYTEA INSERT, so the persisted payload is
        # byte-identical to what the cap saw (single canonical path).
        body = to_canonical_bytes(envelope.payload)

        stamped: EventEnvelope
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._lock_stream(envelope.task_id)
                # Idempotency dedup (runs before the seq check so a
                # retry doesn't accidentally trip StaleSequence).
                if lease_id is not None and idempotency_key is not None:
                    cached = self._conn.execute(
                        "SELECT seq FROM idempotency "
                        "WHERE task_id = %s AND lease_id = %s "
                        "AND idempotency_key = %s",
                        (envelope.task_id, lease_id, idempotency_key),
                    ).fetchone()
                    if cached is not None:
                        existing = self._fetch_envelope(
                            envelope.task_id, int(cached["seq"])
                        )
                        self._conn.execute("COMMIT")
                        return existing

                _enforce_payload_cap(envelope.task_id, envelope.type, body)

                next_seq_row = self._conn.execute(
                    "SELECT COALESCE(MAX(seq), -1) + 1 AS next_seq "
                    "FROM events WHERE task_id = %s",
                    (envelope.task_id,),
                ).fetchone()
                if next_seq_row is None:
                    raise RuntimeError(
                        f"_append({envelope.task_id}): COALESCE(MAX()) "
                        f"returned no row"
                    )
                next_seq = int(next_seq_row["next_seq"])

                if expected_seq is not None and expected_seq != next_seq:
                    raise StaleSequence(
                        f"task_id={envelope.task_id}, "
                        f"expected={expected_seq}, actual={next_seq}"
                    )

                if (
                    require_lease
                    and lease_id is not None
                    and self._lease_validator is not None
                ):
                    # In-tx fence probe (ADR multi-host-lease-fencing.md
                    # D1): select the dispatcher row FOR SHARE so a
                    # concurrent reclaim / release / heartbeat-cap
                    # UPDATE blocks until this emit commits or rolls
                    # back — no zombie write can land after a new lease
                    # generation started. A returned row proves the
                    # lease current in THIS database; zero rows fall
                    # back to the bound registry, which keeps mixed
                    # wirings (e.g. an InMemoryDispatcher validating
                    # this log) on their exact registry semantics.
                    if self._db_clock:
                        held = self._conn.execute(
                            "SELECT 1 FROM dispatcher_tasks "
                            "WHERE task_id = %s AND lease_id = %s "
                            "AND status = 'leased' "
                            f"AND lease_expires_at > {_DB_NOW_SQL} "
                            "FOR SHARE",
                            (envelope.task_id, lease_id),
                        ).fetchone()
                    else:
                        held = self._conn.execute(
                            "SELECT 1 FROM dispatcher_tasks "
                            "WHERE task_id = %s AND lease_id = %s "
                            "AND status = 'leased' "
                            "AND lease_expires_at > %s "
                            "FOR SHARE",
                            (envelope.task_id, lease_id, self._clock()),
                        ).fetchone()
                    if held is None and not self._lease_validator.is_lease_valid(
                        envelope.task_id, lease_id
                    ):
                        raise InvalidLease(
                            f"task_id={envelope.task_id}, lease_id={lease_id}"
                        )

                if self._emit_pause is not None:
                    self._emit_pause()

                stamped = envelope.with_seq(next_seq)
                self._conn.execute(
                    "INSERT INTO events ("
                    " task_id, seq, id, type, schema_version, occurred_at,"
                    " actor, trace_id, correlation_id, causation_id,"
                    " origin, payload_canonical"
                    ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        stamped.task_id,
                        stamped.seq,
                        stamped.id,
                        stamped.type,
                        stamped.schema_version,
                        stamped.occurred_at,
                        stamped.actor,
                        stamped.trace_id,
                        stamped.correlation_id,
                        stamped.causation_id,
                        stamped.origin,
                        body,
                    ),
                )
                if lease_id is not None and idempotency_key is not None:
                    self._conn.execute(
                        "INSERT INTO idempotency ("
                        " task_id, lease_id, idempotency_key, seq"
                        ") VALUES (%s, %s, %s, %s)",
                        (
                            stamped.task_id,
                            lease_id,
                            idempotency_key,
                            stamped.seq,
                        ),
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

        # Notify subscribers outside the lock and after COMMIT so a
        # subscriber that re-enters ``emit`` (e.g. the cross-stream
        # ChildLifecycleObserver pattern) opens its own transaction
        # cleanly. Subscriber exceptions are swallowed.
        self._notify(stamped)
        return stamped

    # -- reads -----------------------------------------------------------

    def read(
        self, task_id: str, *, after_seq: Optional[int] = None
    ) -> list[EventEnvelope]:
        # Reads share the single connection with writers, so they must
        # take the same lock — the connection carries per-transaction
        # state that a concurrent writer would otherwise interleave.
        with self._lock:
            if after_seq is None:
                rows = self._conn.execute(
                    "SELECT * FROM events WHERE task_id = %s ORDER BY seq",
                    (task_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM events WHERE task_id = %s AND seq > %s "
                    "ORDER BY seq",
                    (task_id, int(after_seq)),
                ).fetchall()
            return [_row_to_envelope(row) for row in rows]

    def find_latest_snapshot(self, task_id: str) -> Optional[EventEnvelope]:
        with self._lock:
            # TaskRewound / StepAttemptAbandoned are snapshot-shaped fold
            # baselines (``state_ref`` too) — take whichever of the three
            # has the higher seq so a rewind / attempt seal re-bases fold
            # from the same lookup. ``ix_events_snapshot`` is partial on
            # exactly this predicate (migration 2).
            row = self._conn.execute(
                "SELECT * FROM events "
                f"WHERE task_id = %s AND type IN {_BASELINE_TYPES_SQL} "
                "ORDER BY seq DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            return _row_to_envelope(row)

    def list_task_streams(self) -> list[TaskStreamSummary]:
        """Enumerate task streams, most-recent-update first.

        One ``GROUP BY task_id`` pass; ``MAX(occurred_at) DESC, task_id
        ASC`` gives a deterministic tie-break so equal timestamps never
        reorder flakily. A row only exists when the task has ≥1 event,
        so empty streams are naturally absent.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT task_id, MAX(seq) AS last_seq, "
                "MAX(occurred_at) AS last_event_time "
                "FROM events GROUP BY task_id "
                "ORDER BY last_event_time DESC, task_id ASC"
            ).fetchall()
        return [
            TaskStreamSummary(
                task_id=row["task_id"],
                last_seq=int(row["last_seq"]),
                last_event_time=float(row["last_event_time"]),
            )
            for row in rows
        ]

    def _fetch_envelope(self, task_id: str, seq: int) -> EventEnvelope:
        # Callers already hold ``self._lock`` (only ``_append`` invokes
        # this, from inside its transaction). No nested acquire — that
        # would deadlock on a non-reentrant Lock.
        row = self._conn.execute(
            "SELECT * FROM events WHERE task_id = %s AND seq = %s",
            (task_id, seq),
        ).fetchone()
        if row is None:
            # Should never happen: idempotency table points at a row
            # we just verified exists; surface loudly if it does.
            raise RuntimeError(
                f"idempotency cache references missing event "
                f"task_id={task_id}, seq={seq}"
            )
        return _row_to_envelope(row)

    # -- subscribe -------------------------------------------------------

    def subscribe(self, callback: Subscriber) -> Unsubscribe:
        self._subscribers.append(callback)

        def _unsubscribe() -> None:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass

        return _unsubscribe

    def _notify(self, envelope: EventEnvelope) -> None:
        for sub in list(self._subscribers):
            try:
                sub(envelope)
            except Exception:  # noqa: BLE001 — don't break writer
                pass

    # -- maintenance -----------------------------------------------------

    def purge_task(self, task_id: str) -> bool:
        """Hard-delete every row this task owns (events + idempotency).

        A GC/maintenance affordance backing the agent product's "delete
        session" command — deliberately NOT on the L0 ``EventLog``
        Protocols (the record/fold path is append-only and never
        deletes). ``content`` blobs are intentionally left untouched:
        that table is addressed by hash and shared across tasks.

        Returns ``True`` iff at least one ``events`` row was removed.
        """
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._lock_stream(task_id)
                cur = self._conn.execute(
                    "DELETE FROM events WHERE task_id = %s", (task_id,)
                )
                removed = cur.rowcount
                self._conn.execute(
                    "DELETE FROM idempotency WHERE task_id = %s", (task_id,)
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return removed > 0

    # -- lifecycle -------------------------------------------------------

    def close(self) -> None:
        """Close the underlying psycopg connection.

        Idempotent. Not part of the L0 Protocols — application wiring
        that constructs a :class:`PostgresEventLog` is responsible for
        calling this at shutdown.
        """
        if self._closed:
            return
        try:
            self._conn.close()
        finally:
            self._closed = True

    def __enter__(self) -> "PostgresEventLog":
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.close()


def _row_to_envelope(row: Mapping[str, Any]) -> EventEnvelope:
    # psycopg may hand BYTEA back as ``memoryview``; normalise to bytes
    # before the canonical decode.
    canonical_body = from_canonical_bytes(bytes(row["payload_canonical"]))
    payload = _restore_payload(row["type"], canonical_body)
    return EventEnvelope(
        id=row["id"],
        task_id=row["task_id"],
        seq=int(row["seq"]),
        type=row["type"],
        schema_version=int(row["schema_version"]),
        occurred_at=float(row["occurred_at"]),
        actor=row["actor"],
        trace_id=row["trace_id"],
        correlation_id=row["correlation_id"],
        causation_id=row["causation_id"],
        payload=payload,
        origin=row["origin"],
    )
