"""``PostgresDispatcher`` — psycopg-backed adapter for ``Dispatcher`` + ``LeaseRegistry``.

Third Postgres adapter on the same database that ``PostgresEventLog``
and ``PostgresContentStore`` share. Behaviour is pinned by
:class:`noeta.storage.memory.InMemoryDispatcher` and the
storage-backend-neutral contract suite, mirroring
:class:`noeta.storage.sqlite.dispatcher.SqliteDispatcher`
structure-for-structure.

Where sqlite serialises every lifecycle write behind the file-wide
``BEGIN IMMEDIATE`` lock, each write transaction here takes ONE global
``pg_advisory_xact_lock`` for the dispatcher state machine — the FIFO
``ready_order`` allocation and the wake-matching read-modify-write span
multiple tasks, so a per-task lock would not preserve the serial
semantics the contract pins. Dispatcher lifecycle calls are not a hot
path (per step, not per event), so the single lock is not a bottleneck.

Public surface is exactly the ``Dispatcher`` + ``LeaseRegistry`` L0
Protocols plus the same lifecycle helpers (``close`` + context manager)
the other adapters expose.
"""

from __future__ import annotations

import threading
import time
import uuid
from types import TracebackType
from typing import Any, Callable, Mapping, Optional

from noeta.protocols.canonical import from_canonical_bytes, to_canonical_bytes
from noeta.protocols.dispatcher import Lease
from noeta.protocols.errors import InvalidLease, WakeConsumeMismatch
from noeta.protocols.wake import TimerFired
from noeta.storage._reclaim import reclaim_hits_cap
from noeta.storage._wake_match import _matches
from noeta.storage.postgres._connection import (
    _ADVISORY_CLASS_DISPATCHER,
    _open_connection,
)
from noeta.storage.postgres.migrations import apply_migrations


__all__ = ["PostgresDispatcher"]


_VALID_RELEASE_STATES = frozenset({"suspended", "terminal"})


def _serialize_wake(value: Any) -> Optional[bytes]:
    if value is None:
        return None
    return to_canonical_bytes(value)


def _timer_deadline(wake_on: Any) -> Optional[float]:
    """The ``fire_at`` value mirrored onto the row for a timer suspend, or
    ``None`` for every non-timer wake.

    Kept in lockstep with ``wake_on_canonical`` at every write site so
    the indexed timer sweep selects the due set off ``fire_at`` without
    decoding each suspended row. The invariant: ``fire_at`` is non-NULL
    iff the row is a suspended ``TimerFired`` wait carrying this
    deadline.
    """
    return wake_on.fire_at if isinstance(wake_on, TimerFired) else None


def _as_bytes(blob: Any) -> Optional[bytes]:
    """Normalise a BYTEA column value (psycopg may return memoryview)."""
    if blob is None:
        return None
    return bytes(blob)


def _deserialize_wake(blob: Any) -> Any:
    raw = _as_bytes(blob)
    if raw is None:
        return None
    return from_canonical_bytes(raw)


class PostgresDispatcher:
    """psycopg implementation of ``Dispatcher`` + ``LeaseRegistry``.

    Public surface matches the Protocols (plus the standard adapter
    lifecycle helpers). Debug introspection beyond ``task_status`` /
    ``has_active_lease`` is deliberately NOT part of this surface;
    tests reach into ``_conn`` directly when they need the raw rows.
    """

    def __init__(
        self,
        dsn: str,
        *,
        # ``now`` defaults to wall-clock ``time.time``, NOT
        # ``time.monotonic``. ``lease_expires_at`` is a persisted
        # float; comparisons across a process restart only make sense
        # against a wall clock.
        now: Optional[Callable[[], float]] = None,
        heartbeat_max: int = 360,
        max_fail_attempts: int = 3,
        reclaim_max: int = 3,
    ) -> None:
        self._conn = _open_connection(dsn)
        apply_migrations(self._conn)
        self._now = now or time.time
        self._heartbeat_max = heartbeat_max
        self._max_fail_attempts = max_fail_attempts
        self._reclaim_max = reclaim_max
        self._lock = threading.Lock()
        # ``is_lease_valid`` is on the EventLog write path: every
        # ``emit(... lease_id=...)`` calls it from **inside** the
        # EventLog's open transaction. Give it a separate read-only
        # connection + lock (same shape as the sqlite adapter) so a
        # validation read never queues behind a lifecycle write
        # transaction held by another thread on the main connection.
        self._read_conn = _open_connection(dsn)
        self._read_lock = threading.Lock()
        self._closed = False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _begin_locked(self) -> None:
        """Open a transaction holding the global dispatcher advisory lock.

        The sqlite adapter's ``BEGIN IMMEDIATE`` analogue: every
        lifecycle read-modify-write runs serialised behind this one
        transaction-scoped lock (auto-released at COMMIT / ROLLBACK).
        """
        self._conn.execute("BEGIN")
        try:
            self._conn.execute(
                "SELECT pg_advisory_xact_lock(%s, 0)",
                (_ADVISORY_CLASS_DISPATCHER,),
            )
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def _next_ready_order(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(ready_order), 0) + 1 AS next_order "
            "FROM dispatcher_tasks"
        ).fetchone()
        assert row is not None
        return int(row["next_order"])

    def _next_arrival_seq(self, task_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(arrival_seq), -1) + 1 AS next_seq "
            "FROM dispatcher_pending_wakes WHERE task_id = %s",
            (task_id,),
        ).fetchone()
        assert row is not None
        return int(row["next_seq"])

    def _fetch_task(self, task_id: str) -> Optional[Mapping[str, Any]]:
        return self._conn.execute(
            "SELECT * FROM dispatcher_tasks WHERE task_id = %s",
            (task_id,),
        ).fetchone()

    def _fetch_task_by_lease(self, lease_id: str) -> Optional[Mapping[str, Any]]:
        return self._conn.execute(
            "SELECT * FROM dispatcher_tasks WHERE lease_id = %s",
            (lease_id,),
        ).fetchone()

    def _drain_first_matching_pending(
        self, task_id: str, wake_on: Any
    ) -> Optional[bytes]:
        """If a buffered wake event matches ``wake_on``, delete it and
        return its canonical bytes so the caller can persist them on
        the task row as ``matched_wake_event_canonical``. Returns
        ``None`` when no buffered event matches. Caller is responsible
        for transitioning the task row to ``ready`` and clearing wake
        metadata."""
        if wake_on is None:
            return None
        for row in self._conn.execute(
            "SELECT arrival_seq, wake_event_canonical "
            "FROM dispatcher_pending_wakes "
            "WHERE task_id = %s ORDER BY arrival_seq",
            (task_id,),
        ).fetchall():
            wake_event = _deserialize_wake(row["wake_event_canonical"])
            if _matches(wake_on, wake_event):
                self._conn.execute(
                    "DELETE FROM dispatcher_pending_wakes "
                    "WHERE task_id = %s AND arrival_seq = %s",
                    (task_id, int(row["arrival_seq"])),
                )
                return _as_bytes(row["wake_event_canonical"])
        return None

    # ------------------------------------------------------------------
    # Dispatcher Protocol
    # ------------------------------------------------------------------

    def enqueue(self, task_id: str) -> None:
        """Mark ``task_id`` as ready-to-lease.

        Three paths matching the Protocol's idempotency promise:

        * No row → INSERT with a fresh ``ready_order``.
        * Existing row already in ``ready`` → no-op (preserve original
          ``ready_order`` so FIFO is not reshuffled).
        * Existing row in any non-ready status → transition to ready,
          clearing all non-ready columns and assigning a fresh
          ``ready_order``.
        """
        with self._lock:
            self._begin_locked()
            try:
                row = self._fetch_task(task_id)
                if row is None:
                    order = self._next_ready_order()
                    self._conn.execute(
                        "INSERT INTO dispatcher_tasks ("
                        " task_id, status, lease_id, lease_expires_at,"
                        " heartbeat_count, fail_attempts,"
                        " wake_on_canonical, suspend_reason, ready_order"
                        ") VALUES (%s, 'ready', NULL, NULL, 0, 0, NULL, NULL, %s)",
                        (task_id, order),
                    )
                elif row["status"] == "ready":
                    # No-op; FIFO must not be reshuffled.
                    pass
                else:
                    # Non-ready → ready transition: clear every
                    # state-specific field of the prior state in
                    # lockstep with the status flip, including
                    # ``matched_wake_event_canonical``. A stale matched
                    # wake surviving a force-enqueue would let the next
                    # lease deliver a wake_event the caller did not
                    # request (see InMemory.enqueue).
                    order = self._next_ready_order()
                    self._conn.execute(
                        "UPDATE dispatcher_tasks SET "
                        " status = 'ready',"
                        " lease_id = NULL,"
                        " lease_expires_at = NULL,"
                        " heartbeat_count = 0,"
                        " reclaim_count = 0,"
                        " wake_on_canonical = NULL,"
                        " suspend_reason = NULL,"
                        " matched_wake_event_canonical = NULL,"
                        " fire_at = NULL,"
                        " ready_order = %s "
                        "WHERE task_id = %s",
                        (order, task_id),
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def lease(
        self,
        *,
        worker_id: str,
        lease_seconds: float = 30.0,
        task_id: Optional[str] = None,
    ) -> Optional[Lease]:
        """Lease a ready task — FIFO when ``task_id is None``, targeted
        otherwise.

        Targeted-lease semantics (``task_id=<id>``): returns ``None`` if
        the task does not exist or is not currently in the ``ready``
        state. Never raises — diagnosis is the caller's job.

        On success, any ``matched_wake_event_canonical`` queued by a
        prior :meth:`wake` or release-drain is read and handed back on
        :attr:`Lease.wake_event`. It is **NOT** cleared at lease time —
        it survives the lease and is cleared only by a consuming
        ``release(consumed_wake_event=...)``, otherwise re-delivered by
        :meth:`requeue_stale` (at-least-once delivery + idempotent
        consumption = exactly-once).
        """
        del worker_id  # not recorded on the row; reserved for future audit
        with self._lock:
            self._begin_locked()
            try:
                if task_id is None:
                    row = self._conn.execute(
                        "SELECT task_id, matched_wake_event_canonical "
                        "FROM dispatcher_tasks "
                        "WHERE status = 'ready' "
                        "ORDER BY ready_order LIMIT 1"
                    ).fetchone()
                else:
                    row = self._conn.execute(
                        "SELECT task_id, matched_wake_event_canonical "
                        "FROM dispatcher_tasks "
                        "WHERE task_id = %s AND status = 'ready'",
                        (task_id,),
                    ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return None
                leased_task_id = row["task_id"]
                matched_blob = _as_bytes(row["matched_wake_event_canonical"])
                lease_id = f"lease-{uuid.uuid4().hex}"
                expires_at = self._now() + lease_seconds
                self._conn.execute(
                    "UPDATE dispatcher_tasks SET "
                    " status = 'leased',"
                    " lease_id = %s,"
                    " lease_expires_at = %s,"
                    " heartbeat_count = 0,"
                    " suspend_reason = NULL,"
                    " ready_order = NULL "
                    "WHERE task_id = %s",
                    (lease_id, expires_at, leased_task_id),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        wake_event = _deserialize_wake(matched_blob)
        return Lease(
            lease_id=lease_id,
            task_id=leased_task_id,
            expires_at=expires_at,
            wake_event=wake_event,
        )

    def heartbeat(self, lease_id: str, *, lease_seconds: float = 30.0) -> float:
        with self._lock:
            self._begin_locked()
            try:
                row = self._fetch_task_by_lease(lease_id)
                if row is None or row["status"] != "leased":
                    self._conn.execute("ROLLBACK")
                    raise InvalidLease(lease_id)
                if int(row["heartbeat_count"]) >= self._heartbeat_max:
                    # Cap exceeded — force release in the same transaction.
                    # The matched wake is NOT consumed here (no TaskWoken
                    # proof), so it must be PRESERVED and re-delivered,
                    # not stranded. If a matched is in-flight the task
                    # goes back to **ready** (re-deliverable, same as a
                    # non-consuming release); otherwise it suspends on
                    # its preserved wake_on.
                    if row["matched_wake_event_canonical"] is not None:
                        self._conn.execute(
                            "UPDATE dispatcher_tasks SET "
                            " status = 'ready',"
                            " lease_id = NULL,"
                            " lease_expires_at = NULL,"
                            " heartbeat_count = 0,"
                            " reclaim_count = 0,"
                            " wake_on_canonical = NULL,"
                            " suspend_reason = NULL,"
                            " fire_at = NULL,"
                            " ready_order = %s "
                            "WHERE task_id = %s",
                            (self._next_ready_order(), row["task_id"]),
                        )
                    else:
                        self._conn.execute(
                            "UPDATE dispatcher_tasks SET "
                            " status = 'suspended',"
                            " lease_id = NULL,"
                            " lease_expires_at = NULL,"
                            " heartbeat_count = 0,"
                            " reclaim_count = 0,"
                            " suspend_reason = 'lease_quota_exceeded' "
                            "WHERE task_id = %s",
                            (row["task_id"],),
                        )
                    self._conn.execute("COMMIT")
                    raise InvalidLease(lease_id)
                expires_at = self._now() + lease_seconds
                # A successful heartbeat is the leased-task progress
                # signal: reset the stale-reclaim counter.
                self._conn.execute(
                    "UPDATE dispatcher_tasks SET "
                    " heartbeat_count = heartbeat_count + 1,"
                    " reclaim_count = 0,"
                    " lease_expires_at = %s "
                    "WHERE lease_id = %s",
                    (expires_at, lease_id),
                )
                self._conn.execute("COMMIT")
            except InvalidLease:
                raise
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return expires_at

    def release(
        self,
        lease_id: str,
        *,
        next_state: str,
        wake_on: Any = None,
        suspend_reason: Optional[str] = None,
        consumed_wake_event: Any = None,
    ) -> None:
        if next_state not in _VALID_RELEASE_STATES:
            raise ValueError(f"invalid next_state: {next_state}")
        with self._lock:
            self._begin_locked()
            try:
                row = self._fetch_task_by_lease(lease_id)
                if row is None:
                    self._conn.execute("ROLLBACK")
                    raise InvalidLease(lease_id)
                task_id = row["task_id"]

                # --- step 1: validate + clear the OLD matched iff a
                # consuming release presents the exact wake.
                # Mismatch / no-matched → raise + ROLLBACK (commit nothing).
                clear_matched = False
                if consumed_wake_event is not None:
                    stored = _as_bytes(row["matched_wake_event_canonical"])
                    if stored is None or stored != to_canonical_bytes(
                        consumed_wake_event
                    ):
                        self._conn.execute("ROLLBACK")
                        raise WakeConsumeMismatch(
                            f"release(consumed_wake_event=...) on task "
                            f"{task_id!r}: presented wake does not equal the "
                            "stored matched_wake_event"
                        )
                    clear_matched = True
                    self._conn.execute(
                        "UPDATE dispatcher_tasks SET "
                        " matched_wake_event_canonical = NULL "
                        "WHERE task_id = %s",
                        (task_id,),
                    )

                if next_state == "terminal":
                    self._conn.execute(
                        "UPDATE dispatcher_tasks SET "
                        " status = 'terminal',"
                        " lease_id = NULL,"
                        " lease_expires_at = NULL,"
                        " heartbeat_count = 0,"
                        " reclaim_count = 0,"
                        " wake_on_canonical = NULL,"
                        " suspend_reason = %s,"
                        " fire_at = NULL,"
                        " ready_order = NULL "
                        "WHERE task_id = %s",
                        (suspend_reason, task_id),
                    )
                    # Terminal is forever — buffered wakes that never
                    # matched can never drain; GC them. The matched wake
                    # (handoff) is deliberately kept.
                    self._conn.execute(
                        "DELETE FROM dispatcher_pending_wakes WHERE task_id = %s",
                        (task_id,),
                    )
                else:  # next_state == "suspended"
                    wake_blob = _serialize_wake(wake_on)
                    self._conn.execute(
                        "UPDATE dispatcher_tasks SET "
                        " status = 'suspended',"
                        " lease_id = NULL,"
                        " lease_expires_at = NULL,"
                        " heartbeat_count = 0,"
                        " reclaim_count = 0,"
                        " wake_on_canonical = %s,"
                        " suspend_reason = %s,"
                        " fire_at = %s,"
                        " ready_order = NULL "
                        "WHERE task_id = %s",
                        (wake_blob, suspend_reason, _timer_deadline(wake_on), task_id),
                    )
                    # The OLD matched was cleared above iff consuming.
                    matched_present = (
                        not clear_matched
                        and row["matched_wake_event_canonical"] is not None
                    )
                    if matched_present:
                        # An un-consumed matched is PRESERVED and means a
                        # delivery is pending → the task goes back to
                        # **ready** (re-deliverable), not stuck-suspended.
                        order = self._next_ready_order()
                        self._conn.execute(
                            "UPDATE dispatcher_tasks SET "
                            " status = 'ready',"
                            " wake_on_canonical = NULL,"
                            " suspend_reason = NULL,"
                            " fire_at = NULL,"
                            " ready_order = %s "
                            "WHERE task_id = %s",
                            (order, task_id),
                        )
                    else:
                        # No matched: install the NEW wake_on (already set
                        # above) and drain a single matching pending wake →
                        # a possible NEW matched.
                        drained = self._drain_first_matching_pending(task_id, wake_on)
                        if drained is not None:
                            order = self._next_ready_order()
                            self._conn.execute(
                                "UPDATE dispatcher_tasks SET "
                                " status = 'ready',"
                                " wake_on_canonical = NULL,"
                                " suspend_reason = NULL,"
                                " matched_wake_event_canonical = %s,"
                                " fire_at = NULL,"
                                " ready_order = %s "
                                "WHERE task_id = %s",
                                (drained, order, task_id),
                            )
                self._conn.execute("COMMIT")
            except (InvalidLease, ValueError, WakeConsumeMismatch):
                raise
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def release_yield(self, lease_id: str) -> None:
        """Voluntary yield of a seeded lease back to the ready queue.

        Transitions leased→ready WITHOUT incrementing fail_attempts —
        used by transports that seed a task durably under a targeted
        lease and then hand it off to a resident worker pool. Matched
        wakes are preserved.
        """
        with self._lock:
            self._begin_locked()
            try:
                row = self._fetch_task_by_lease(lease_id)
                if row is None:
                    self._conn.execute("ROLLBACK")
                    raise InvalidLease(lease_id)
                task_id = row["task_id"]
                order = self._next_ready_order()
                self._conn.execute(
                    "UPDATE dispatcher_tasks SET "
                    " status = 'ready',"
                    " lease_id = NULL,"
                    " lease_expires_at = NULL,"
                    " heartbeat_count = 0,"
                    " reclaim_count = 0,"
                    " wake_on_canonical = NULL,"
                    " suspend_reason = NULL,"
                    " fire_at = NULL,"
                    " ready_order = %s "
                    "WHERE task_id = %s",
                    (order, task_id),
                )
                self._conn.execute("COMMIT")
            except InvalidLease:
                raise
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def fail(
        self,
        lease_id: str,
        *,
        retryable: bool = False,
        reason: Optional[str] = None,
    ) -> None:
        with self._lock:
            self._begin_locked()
            try:
                row = self._fetch_task_by_lease(lease_id)
                if row is None:
                    self._conn.execute("ROLLBACK")
                    raise InvalidLease(lease_id)
                task_id = row["task_id"]
                attempts = int(row["fail_attempts"])
                if retryable and attempts + 1 < self._max_fail_attempts:
                    # A controlled fail is a progress signal for the
                    # RECLAIM counter (bounding is fail_attempts' job).
                    order = self._next_ready_order()
                    self._conn.execute(
                        "UPDATE dispatcher_tasks SET "
                        " status = 'ready',"
                        " lease_id = NULL,"
                        " lease_expires_at = NULL,"
                        " heartbeat_count = 0,"
                        " reclaim_count = 0,"
                        " fail_attempts = fail_attempts + 1,"
                        " wake_on_canonical = NULL,"
                        " suspend_reason = NULL,"
                        " fire_at = NULL,"
                        " ready_order = %s "
                        "WHERE task_id = %s",
                        (order, task_id),
                    )
                else:
                    final_reason = reason or (
                        "max_attempts_exceeded" if retryable else None
                    )
                    self._conn.execute(
                        "UPDATE dispatcher_tasks SET "
                        " status = 'terminal',"
                        " lease_id = NULL,"
                        " lease_expires_at = NULL,"
                        " heartbeat_count = 0,"
                        " reclaim_count = 0,"
                        " fail_attempts = fail_attempts + %s,"
                        " wake_on_canonical = NULL,"
                        " suspend_reason = %s,"
                        " fire_at = NULL,"
                        " ready_order = NULL "
                        "WHERE task_id = %s",
                        (1 if retryable else 0, final_reason, task_id),
                    )
                    # GC never-matching buffered wakes on the terminal
                    # transition (same as release-terminal).
                    self._conn.execute(
                        "DELETE FROM dispatcher_pending_wakes WHERE task_id = %s",
                        (task_id,),
                    )
                self._conn.execute("COMMIT")
            except InvalidLease:
                raise
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def wake(self, task_id: str, wake_event: Any) -> bool:
        with self._lock:
            self._begin_locked()
            try:
                row = self._fetch_task(task_id)

                if row is None:
                    # Wake-before-enqueue: record the event in
                    # pending_wakes only. No dispatcher_tasks row is
                    # created — the CHECK constraints would require
                    # either ready+ready_order or some other consistent
                    # state, and we don't want to make this task
                    # leaseable yet. enqueue() will materialise the row
                    # later; release(suspended, wake_on=match) will
                    # drain the buffered event.
                    arrival_seq = self._next_arrival_seq(task_id)
                    self._conn.execute(
                        "INSERT INTO dispatcher_pending_wakes ("
                        " task_id, arrival_seq, wake_event_canonical"
                        ") VALUES (%s, %s, %s)",
                        (task_id, arrival_seq, to_canonical_bytes(wake_event)),
                    )
                    self._conn.execute("COMMIT")
                    return False

                if row["status"] == "suspended":
                    wake_on = _deserialize_wake(row["wake_on_canonical"])
                    if _matches(wake_on, wake_event):
                        order = self._next_ready_order()
                        matched_blob = to_canonical_bytes(wake_event)
                        self._conn.execute(
                            "UPDATE dispatcher_tasks SET "
                            " status = 'ready',"
                            " wake_on_canonical = NULL,"
                            " suspend_reason = NULL,"
                            " matched_wake_event_canonical = %s,"
                            " fire_at = NULL,"
                            " ready_order = %s "
                            "WHERE task_id = %s",
                            (matched_blob, order, task_id),
                        )
                        self._conn.execute("COMMIT")
                        return True

                arrival_seq = self._next_arrival_seq(task_id)
                self._conn.execute(
                    "INSERT INTO dispatcher_pending_wakes ("
                    " task_id, arrival_seq, wake_event_canonical"
                    ") VALUES (%s, %s, %s)",
                    (task_id, arrival_seq, to_canonical_bytes(wake_event)),
                )
                self._conn.execute("COMMIT")
                return False
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def requeue_stale(self) -> list[str]:
        """Sweep expired leases back to ready; return the requeued ids.

        Each reclaim increments ``reclaim_count``; at ``reclaim_max``
        consecutive no-progress reclaims the task drops to ``terminal``
        (``stale_reclaim_exceeded``) instead of requeueing.
        Terminal-by-cap tasks are NOT in the returned list.
        """
        now = self._now()
        requeued: list[str] = []
        with self._lock:
            self._begin_locked()
            try:
                stale = self._conn.execute(
                    "SELECT task_id, reclaim_count FROM dispatcher_tasks "
                    "WHERE status = 'leased' AND lease_expires_at <= %s "
                    "ORDER BY lease_expires_at",
                    (now,),
                ).fetchall()
                for row in stale:
                    task_id = row["task_id"]
                    if reclaim_hits_cap(
                        int(row["reclaim_count"]) + 1, self._reclaim_max
                    ):
                        self._conn.execute(
                            "UPDATE dispatcher_tasks SET "
                            " status = 'terminal',"
                            " lease_id = NULL,"
                            " lease_expires_at = NULL,"
                            " heartbeat_count = 0,"
                            " reclaim_count = reclaim_count + 1,"
                            " wake_on_canonical = NULL,"
                            " suspend_reason = 'stale_reclaim_exceeded',"
                            " fire_at = NULL,"
                            " ready_order = NULL "
                            "WHERE task_id = %s",
                            (task_id,),
                        )
                        # Terminal transition GCs buffered wakes.
                        self._conn.execute(
                            "DELETE FROM dispatcher_pending_wakes WHERE task_id = %s",
                            (task_id,),
                        )
                        continue
                    order = self._next_ready_order()
                    self._conn.execute(
                        "UPDATE dispatcher_tasks SET "
                        " status = 'ready',"
                        " lease_id = NULL,"
                        " lease_expires_at = NULL,"
                        " heartbeat_count = 0,"
                        " reclaim_count = reclaim_count + 1,"
                        " ready_order = %s "
                        "WHERE task_id = %s",
                        (order, task_id),
                    )
                    requeued.append(task_id)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return requeued

    def fire_due_timers(self, *, now: float) -> list[str]:
        """Wake every suspended task whose ``TimerFired`` deadline passed.

        ``now`` is a wall-clock epoch timestamp supplied by the caller
        (the same base the Engine used to compute ``fire_at``); the
        delivered wake is the **recorded deadline** blob so re-delivery
        stays byte-identical.

        The due set is selected straight off the partial ``fire_at``
        index. A read-only probe on the separate read connection runs
        FIRST: when nothing is due (the common ~1s poll) it returns
        without opening a write transaction at all, so an idle poll
        never takes the dispatcher advisory lock. The probe/commit race
        is benign — a timer that comes due in the gap is caught by the
        next poll.
        """
        with self._read_lock:
            due = self._read_conn.execute(
                "SELECT 1 FROM dispatcher_tasks WHERE fire_at <= %s LIMIT 1",
                (now,),
            ).fetchone()
        if due is None:
            return []
        fired: list[str] = []
        with self._lock:
            self._begin_locked()
            try:
                rows = self._conn.execute(
                    "SELECT task_id, wake_on_canonical FROM dispatcher_tasks "
                    "WHERE fire_at <= %s ORDER BY fire_at",
                    (now,),
                ).fetchall()
                for row in rows:
                    # ``fire_at`` is written only for TimerFired suspends,
                    # so this set is already the due timers. Still
                    # decode-guard: a blob corrupted AFTER suspend keeps
                    # its ``fire_at``, so skip an undecodable / non-timer
                    # row rather than deliver garbage as its matched wake.
                    try:
                        wake_on = _deserialize_wake(row["wake_on_canonical"])
                    except Exception:  # noqa: BLE001 — one bad row must not stall timers
                        continue
                    if not isinstance(wake_on, TimerFired):
                        continue
                    order = self._next_ready_order()
                    self._conn.execute(
                        "UPDATE dispatcher_tasks SET "
                        " status = 'ready',"
                        " wake_on_canonical = NULL,"
                        " suspend_reason = NULL,"
                        " matched_wake_event_canonical = %s,"
                        " fire_at = NULL,"
                        " ready_order = %s "
                        "WHERE task_id = %s",
                        (
                            _as_bytes(row["wake_on_canonical"]),
                            order,
                            row["task_id"],
                        ),
                    )
                    fired.append(row["task_id"])
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return fired

    # ------------------------------------------------------------------
    # LeaseRegistry Protocol
    # ------------------------------------------------------------------

    def is_lease_valid(self, task_id: str, lease_id: str) -> bool:
        """Read-only single-SELECT path on the hot wire — every
        ``PostgresEventLog.emit`` with a ``lease_id`` calls this from
        inside its own open transaction. No advisory lock; MVCC
        guarantees committed-state visibility, and the read connection's
        own lock just protects connection thread-safety. The read
        connection is intentionally separate from the lifecycle writer
        connection so a validation read never queues behind a lifecycle
        write held by another thread (see ``__init__``).
        """
        with self._read_lock:
            row = self._read_conn.execute(
                "SELECT lease_expires_at FROM dispatcher_tasks "
                "WHERE task_id = %s AND lease_id = %s AND status = 'leased'",
                (task_id, lease_id),
            ).fetchone()
        if row is None:
            return False
        expires_at = row["lease_expires_at"]
        if expires_at is None:
            return False
        return float(expires_at) > self._now()

    # ------------------------------------------------------------------
    # Introspection / maintenance (adapter-only, not on Protocols)
    # ------------------------------------------------------------------

    def task_status(self, task_id: str) -> Optional[str]:
        """Return the dispatcher status for ``task_id`` (``ready`` /
        ``leased`` / ``suspended`` / ``terminal``), or ``None`` if the
        dispatcher holds no row for it. Read-only single SELECT on the
        separate read connection (same reasoning as ``is_lease_valid``)."""
        with self._read_lock:
            row = self._read_conn.execute(
                "SELECT status FROM dispatcher_tasks WHERE task_id = %s",
                (task_id,),
            ).fetchone()
        return None if row is None else str(row["status"])

    def has_active_lease(self, task_id: str) -> bool:
        """True iff a worker currently holds a *live* (non-expired) lease on
        ``task_id``.

        Unlike :meth:`task_status` — which returns the literal ``leased``
        even for a lease whose TTL lapsed after the worker process or step
        thread died — this applies the same expiry test as
        :meth:`is_lease_valid` (``lease_expires_at > now``), so a leaked
        (zombie) lease reads as *not running*. Callers asking "is a worker
        actively running this task right now?" (e.g. the DELETE active
        guard) must use this, not ``task_status() == 'leased'``."""
        with self._read_lock:
            row = self._read_conn.execute(
                "SELECT lease_expires_at FROM dispatcher_tasks "
                "WHERE task_id = %s AND status = 'leased'",
                (task_id,),
            ).fetchone()
        if row is None:
            return False
        expires_at = row["lease_expires_at"]
        if expires_at is None:
            return False
        return float(expires_at) > self._now()

    def restore_task(
        self,
        task_id: str,
        *,
        status: str,
        wake_on: Any = None,
        suspend_reason: Optional[str] = None,
    ) -> None:
        """Adapter-local lifecycle repair used by live conversation rewind.

        ``TaskRewound`` re-bases the EventLog fold to an older
        snapshot-shaped state. The dispatcher row is a lease/wake
        accelerator, so the live rewind command must re-align it with
        that folded baseline without pretending a worker performed a
        normal lease release. This is a maintenance seam, deliberately
        not part of the Dispatcher Protocol.
        """
        if status not in {"ready", "suspended", "terminal"}:
            raise ValueError(f"invalid restore status: {status}")
        with self._lock:
            self._begin_locked()
            try:
                self._conn.execute(
                    "DELETE FROM dispatcher_pending_wakes WHERE task_id = %s",
                    (task_id,),
                )
                if status == "ready":
                    order = self._next_ready_order()
                    self._conn.execute(
                        "INSERT INTO dispatcher_tasks ("
                        " task_id, status, ready_order"
                        ") VALUES (%s, 'ready', %s) "
                        "ON CONFLICT (task_id) DO UPDATE SET "
                        " status = 'ready',"
                        " lease_id = NULL,"
                        " lease_expires_at = NULL,"
                        " heartbeat_count = 0,"
                        " reclaim_count = 0,"
                        " wake_on_canonical = NULL,"
                        " suspend_reason = NULL,"
                        " matched_wake_event_canonical = NULL,"
                        " fire_at = NULL,"
                        " ready_order = excluded.ready_order",
                        (task_id, order),
                    )
                elif status == "terminal":
                    self._conn.execute(
                        "INSERT INTO dispatcher_tasks ("
                        " task_id, status, suspend_reason"
                        ") VALUES (%s, 'terminal', %s) "
                        "ON CONFLICT (task_id) DO UPDATE SET "
                        " status = 'terminal',"
                        " lease_id = NULL,"
                        " lease_expires_at = NULL,"
                        " heartbeat_count = 0,"
                        " reclaim_count = 0,"
                        " wake_on_canonical = NULL,"
                        " suspend_reason = excluded.suspend_reason,"
                        " matched_wake_event_canonical = NULL,"
                        " fire_at = NULL,"
                        " ready_order = NULL",
                        (task_id, suspend_reason),
                    )
                else:
                    wake_blob = _serialize_wake(wake_on)
                    self._conn.execute(
                        "INSERT INTO dispatcher_tasks ("
                        " task_id, status, wake_on_canonical, suspend_reason,"
                        " fire_at"
                        ") VALUES (%s, 'suspended', %s, %s, %s) "
                        "ON CONFLICT (task_id) DO UPDATE SET "
                        " status = 'suspended',"
                        " lease_id = NULL,"
                        " lease_expires_at = NULL,"
                        " heartbeat_count = 0,"
                        " reclaim_count = 0,"
                        " wake_on_canonical = excluded.wake_on_canonical,"
                        " suspend_reason = excluded.suspend_reason,"
                        " matched_wake_event_canonical = NULL,"
                        " fire_at = excluded.fire_at,"
                        " ready_order = NULL",
                        (task_id, wake_blob, suspend_reason, _timer_deadline(wake_on)),
                    )
                    # No buffered-wake redelivery here: the DELETE above
                    # already cleared every pending wake for this task, so
                    # any drain would query an empty table and never
                    # re-ready the task.
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def purge_task(self, task_id: str) -> None:
        """Hard-delete all dispatcher state for ``task_id`` (the task row
        plus any buffered pending wakes). Maintenance affordance backing
        the agent product's "delete session"; not on the Dispatcher
        Protocol. Idempotent — purging an unknown task is a no-op."""
        with self._lock:
            self._begin_locked()
            try:
                self._conn.execute(
                    "DELETE FROM dispatcher_pending_wakes WHERE task_id = %s",
                    (task_id,),
                )
                self._conn.execute(
                    "DELETE FROM dispatcher_tasks WHERE task_id = %s",
                    (task_id,),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------
    # Lifecycle (adapter-only, not on Protocols)
    # ------------------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._conn.close()
            self._read_conn.close()
        finally:
            self._closed = True

    def __enter__(self) -> "PostgresDispatcher":
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.close()
