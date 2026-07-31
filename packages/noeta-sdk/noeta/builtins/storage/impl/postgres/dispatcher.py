"""``PostgresDispatcher`` — psycopg-backed adapter for ``Dispatcher`` + ``LeaseRegistry``.

Every lifecycle write transaction takes ONE global
``pg_advisory_xact_lock`` for the dispatcher state machine: the FIFO
``ready_order`` allocation and the wake-matching read-modify-write both span
multiple tasks, so a per-task lock would not preserve the serial semantics the
storage contract pins. Lifecycle calls happen per step rather than per event,
which is what makes a single global lock affordable.
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
from noeta.storage.spi import reclaim_hits_cap, wake_matches

from noeta.builtins.storage.impl.postgres._connection import (
    _ADVISORY_CLASS_DISPATCHER,
    _DB_NOW_SQL,
    _open_connection,
)
from noeta.builtins.storage.impl.postgres.migrations import apply_migrations


__all__ = ["PostgresDispatcher"]


_VALID_RELEASE_STATES = frozenset({"suspended", "terminal"})


def _serialize_wake(value: Any) -> Optional[bytes]:
    if value is None:
        return None
    return to_canonical_bytes(value)


def _timer_deadline(wake_on: Any) -> Optional[float]:
    """The ``fire_at`` value mirrored onto the row for a timer suspend, or
    ``None`` for every non-timer wake.

    Every write site must keep this in lockstep with ``wake_on_canonical``:
    the invariant ``fire_at IS NOT NULL`` iff the row is a suspended
    ``TimerFired`` wait is what lets the indexed timer sweep pick the due set
    without decoding each suspended row.
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

    Debug introspection beyond ``task_status`` / ``has_active_lease`` is
    deliberately absent from this surface; tests reach into ``_conn`` when
    they need raw rows.
    """

    def __init__(
        self,
        dsn: str,
        *,
        # Wall-clock ``time.time``, NOT ``time.monotonic``:
        # ``lease_expires_at`` is persisted, and comparing it across a
        # process restart only makes sense against a wall clock.
        now: Optional[Callable[[], float]] = None,
        heartbeat_max: int = 360,
        max_fail_attempts: int = 3,
        reclaim_max: int = 3,
        row_lock_timeout_ms: int = 5_000,
    ) -> None:
        self._conn = _open_connection(dsn)
        apply_migrations(self._conn)
        # Without an injected ``now`` the expiry math runs on the database
        # clock (``_DB_NOW_SQL``), the one clock every host shares; an
        # injected ``now`` keeps the deterministic client-side comparisons
        # the contract tests drive.
        self._db_clock = now is None
        self._now = now or time.time
        self._heartbeat_max = heartbeat_max
        self._max_fail_attempts = max_fail_attempts
        self._reclaim_max = reclaim_max
        # Upper bound on a lifecycle transaction's wait for a dispatcher-row
        # lock. The EventLog's in-transaction fence probe
        # (``SELECT ... FOR SHARE``) holds a row lock for the length of an
        # emit, so a wedged emit (GC pause, SIGSTOP, dead client) would
        # otherwise pin the global dispatcher advisory lock fleet-wide behind
        # e.g. a blocked ``requeue_stale`` UPDATE. On timeout the transaction
        # aborts (``psycopg.errors.LockNotAvailable``), the caller's sweep
        # retries next poll, and every other lifecycle op proceeds. Normal
        # emits commit in milliseconds; reaching this bound means the emitter
        # is already pathological.
        self._row_lock_timeout_ms = row_lock_timeout_ms
        self._lock = threading.Lock()
        # ``is_lease_valid`` sits on the EventLog write path: every
        # ``emit(... lease_id=...)`` calls it from **inside** the EventLog's
        # open transaction. It gets its own connection + lock so a validation
        # read never queues behind a lifecycle write transaction another
        # thread is holding on the main connection.
        self._read_conn = _open_connection(dsn)
        self._read_lock = threading.Lock()
        self._closed = False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _begin_locked(self) -> None:
        """Open a transaction holding the global dispatcher advisory lock.

        Every lifecycle read-modify-write is serialised behind this one
        transaction-scoped lock, auto-released at COMMIT / ROLLBACK.

        The advisory acquisition itself waits unboundedly, since host-to-host
        serialisation is the normal case; the ``SET LOCAL lock_timeout``
        issued AFTER it bounds only subsequent row-lock waits — an UPDATE
        queued behind an emit's ``FOR SHARE`` fence probe — so a wedged
        emitter cannot pin the global lock indefinitely.
        """
        self._conn.execute("BEGIN")
        try:
            self._conn.execute(
                "SELECT pg_advisory_xact_lock(%s, 0)",
                (_ADVISORY_CLASS_DISPATCHER,),
            )
            self._conn.execute(
                f"SET LOCAL lock_timeout = '{int(self._row_lock_timeout_ms)}ms'"
            )
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def _expiry_sql_and_param(
        self, lease_seconds: float
    ) -> tuple[str, float]:
        """Return ``(sql_fragment, param)`` for a lease-expiry computation.

        In DB-clock mode the server computes ``clock_timestamp() +
        lease_seconds``, so every host agrees on the expiry instant whatever
        its own clock says. The fragment is an SQL expression, not always a
        placeholder, so the caller must embed it in the statement text rather
        than pass it as a parameter.
        """
        if self._db_clock:
            return f"{_DB_NOW_SQL} + %s", lease_seconds
        return "%s", self._now() + lease_seconds

    def _now_clause(self) -> tuple[str, tuple]:
        """Return ``(sql_expression, params)`` for "now", so that every
        predicate comparing against it renders the same clock base."""
        if self._db_clock:
            return _DB_NOW_SQL, ()
        return "%s", (self._now(),)

    def _next_ready_order(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(ready_order), 0) + 1 AS next_order "
            "FROM dispatcher_tasks"
        ).fetchone()
        if row is None:
            raise RuntimeError("_next_ready_order: COALESCE(MAX()) returned no row")
        return int(row["next_order"])

    def _next_arrival_seq(self, task_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(arrival_seq), -1) + 1 AS next_seq "
            "FROM dispatcher_pending_wakes WHERE task_id = %s",
            (task_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"_next_arrival_seq({task_id}): COALESCE(MAX()) returned no row"
            )
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
        """If a buffered wake event matches ``wake_on``, delete it and return
        its canonical bytes; ``None`` when nothing matches.

        Deliberately leaves the task row alone: the caller owns the
        transition to ``ready`` and the clearing of wake metadata, and must
        persist the returned bytes as ``matched_wake_event_canonical``.
        """
        if wake_on is None:
            return None
        for row in self._conn.execute(
            "SELECT arrival_seq, wake_event_canonical "
            "FROM dispatcher_pending_wakes "
            "WHERE task_id = %s ORDER BY arrival_seq",
            (task_id,),
        ).fetchall():
            wake_event = _deserialize_wake(row["wake_event_canonical"])
            if wake_matches(wake_on, wake_event):
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

    def enqueue(self, task_id: str, *, reserved: bool = False) -> None:
        """Mark ``task_id`` as ready-to-lease.

        Idempotent as the Protocol requires: a task already in ``ready``
        keeps its ``ready_order`` and its ``reserved`` flag untouched, so
        re-enqueueing cannot reshuffle FIFO position. ``reserved`` marks the
        task targeted-lease-only until its first lease claims it.
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
                        " wake_on_canonical, suspend_reason, ready_order,"
                        " reserved"
                        ") VALUES (%s, 'ready', NULL, NULL, 0, 0, NULL, NULL, %s, %s)",
                        (task_id, order, reserved),
                    )
                elif row["status"] == "ready":
                    # No-op; FIFO must not be reshuffled.
                    pass
                else:
                    # Clear every field belonging to the prior state in
                    # lockstep with the status flip, including
                    # ``matched_wake_event_canonical``: a matched wake
                    # surviving a force-enqueue would let the next lease
                    # deliver a wake_event the caller never requested.
                    order = self._next_ready_order()
                    self._conn.execute(
                        "UPDATE dispatcher_tasks SET "
                        " status = 'ready',"
                        " lease_id = NULL,"
                        " worker_id = NULL,"
                        " lease_expires_at = NULL,"
                        " heartbeat_count = 0,"
                        " reclaim_count = 0,"
                        " wake_on_canonical = NULL,"
                        " suspend_reason = NULL,"
                        " matched_wake_event_canonical = NULL,"
                        " fire_at = NULL,"
                        " ready_order = %s,"
                        " reserved = %s "
                        "WHERE task_id = %s",
                        (order, reserved, task_id),
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

        A targeted lease returns ``None`` when the task does not exist or is
        not in ``ready``; it never raises, because diagnosis is the caller's
        job. A queued ``matched_wake_event_canonical`` is handed back on
        :attr:`Lease.wake_event` but is **NOT** cleared here: only a
        consuming ``release(consumed_wake_event=...)`` clears it, so an
        interrupted worker's wake is re-delivered by :meth:`requeue_stale`.
        At-least-once delivery plus idempotent consumption is what makes the
        pair exactly-once.
        """
        with self._lock:
            self._begin_locked()
            try:
                if task_id is None:
                    # ``reserved`` tasks are targeted-lease-only — a fresh
                    # subtask child its own driver must claim first — so the
                    # untargeted FIFO selection skips them.
                    row = self._conn.execute(
                        "SELECT task_id, matched_wake_event_canonical "
                        "FROM dispatcher_tasks "
                        "WHERE status = 'ready' AND reserved = false "
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
                expiry_sql, expiry_param = self._expiry_sql_and_param(
                    lease_seconds
                )
                if self._db_clock:
                    updated = self._conn.execute(
                        "UPDATE dispatcher_tasks SET "
                        " status = 'leased',"
                        " lease_id = %s,"
                        f" lease_expires_at = {expiry_sql},"
                        " heartbeat_count = 0,"
                        " suspend_reason = NULL,"
                        " worker_id = %s,"
                        " ready_order = NULL,"
                        # One-shot claim: only a targeted lease can reach a
                        # reserved row, so reaching here means the owning
                        # driver has claimed the child and the guard is spent.
                        " reserved = false "
                        "WHERE task_id = %s "
                        "RETURNING lease_expires_at",
                        (lease_id, expiry_param, worker_id, leased_task_id),
                    ).fetchone()
                    if updated is None:
                        raise RuntimeError(
                            f"lease(): UPDATE RETURNING returned no row "
                            f"for task {leased_task_id}"
                        )
                    expires_at = float(updated["lease_expires_at"])
                else:
                    expires_at = expiry_param
                    self._conn.execute(
                        "UPDATE dispatcher_tasks SET "
                        " status = 'leased',"
                        " lease_id = %s,"
                        f" lease_expires_at = {expiry_sql},"
                        " heartbeat_count = 0,"
                        " suspend_reason = NULL,"
                        " worker_id = %s,"
                        " ready_order = NULL,"
                        # One-shot claim (see the db_clock branch above).
                        " reserved = false "
                        "WHERE task_id = %s",
                        (lease_id, expiry_param, worker_id, leased_task_id),
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
                    # Force release in the same transaction. The matched wake
                    # is NOT consumed here (there is no TaskWoken proof), so
                    # it must be preserved and re-delivered rather than
                    # stranded: a task with one in flight goes back to
                    # **ready**, otherwise it suspends on its preserved
                    # wake_on.
                    if row["matched_wake_event_canonical"] is not None:
                        self._conn.execute(
                            "UPDATE dispatcher_tasks SET "
                            " status = 'ready',"
                            " lease_id = NULL,"
                            " worker_id = NULL,"
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
                            " worker_id = NULL,"
                            " lease_expires_at = NULL,"
                            " heartbeat_count = 0,"
                            " reclaim_count = 0,"
                            " suspend_reason = 'lease_quota_exceeded' "
                            "WHERE task_id = %s",
                            (row["task_id"],),
                        )
                    self._conn.execute("COMMIT")
                    raise InvalidLease(lease_id)
                expiry_sql, expiry_param = self._expiry_sql_and_param(
                    lease_seconds
                )
                # A successful heartbeat is the only progress signal a leased
                # task gives, so it resets the stale-reclaim counter.
                if self._db_clock:
                    updated = self._conn.execute(
                        "UPDATE dispatcher_tasks SET "
                        " heartbeat_count = heartbeat_count + 1,"
                        " reclaim_count = 0,"
                        f" lease_expires_at = {expiry_sql} "
                        "WHERE lease_id = %s "
                        "RETURNING lease_expires_at",
                        (expiry_param, lease_id),
                    ).fetchone()
                    if updated is None:
                        raise RuntimeError(
                            f"heartbeat({lease_id}): UPDATE RETURNING "
                            f"returned no row"
                        )
                    expires_at = float(updated["lease_expires_at"])
                else:
                    expires_at = expiry_param
                    self._conn.execute(
                        "UPDATE dispatcher_tasks SET "
                        " heartbeat_count = heartbeat_count + 1,"
                        " reclaim_count = 0,"
                        f" lease_expires_at = {expiry_sql} "
                        "WHERE lease_id = %s",
                        (expiry_param, lease_id),
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

                # The stored matched wake is cleared only when a consuming
                # release presents that exact wake; a mismatch rolls back so
                # the call commits nothing at all.
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
                        " worker_id = NULL,"
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
                    # Terminal is forever, so buffered wakes can never drain
                    # and are collected here. The matched wake is deliberately
                    # kept — it is the handoff to whoever reads the result.
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
                        " worker_id = NULL,"
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
                    matched_present = (
                        not clear_matched
                        and row["matched_wake_event_canonical"] is not None
                    )
                    if matched_present:
                        # An un-consumed matched wake means a delivery is
                        # still pending, so the task goes back to **ready**
                        # rather than suspending on a wake it already has.
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
                        # The new wake_on is installed above; a buffered wake
                        # that already matches it must be drained now, or the
                        # task would suspend on an event that has arrived.
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

        Goes leased→ready WITHOUT incrementing ``fail_attempts``, because a
        transport that seeds a task under a targeted lease and hands it to a
        worker pool has not failed anything. Matched wakes are preserved.
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
                    " worker_id = NULL,"
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
                    # A controlled fail counts as progress for the reclaim
                    # counter; bounding retries is ``fail_attempts``' job.
                    order = self._next_ready_order()
                    self._conn.execute(
                        "UPDATE dispatcher_tasks SET "
                        " status = 'ready',"
                        " lease_id = NULL,"
                        " worker_id = NULL,"
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
                        " worker_id = NULL,"
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
                    # Terminal is forever, so buffered wakes can never drain.
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

    def wake(self, task_id: str, wake_event: Any, *, reserved: bool = False) -> bool:
        with self._lock:
            self._begin_locked()
            try:
                row = self._fetch_task(task_id)

                if row is None:
                    # Wake-before-enqueue: buffer the event without creating
                    # a ``dispatcher_tasks`` row, since any row satisfying
                    # the CHECK constraints would also make the task
                    # leaseable before anything has enqueued it. A later
                    # ``enqueue`` materialises the row and a subsequent
                    # suspend drains the buffered event.
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
                    if wake_matches(wake_on, wake_event):
                        order = self._next_ready_order()
                        matched_blob = to_canonical_bytes(wake_event)
                        self._conn.execute(
                            "UPDATE dispatcher_tasks SET "
                            " status = 'ready',"
                            " wake_on_canonical = NULL,"
                            " suspend_reason = NULL,"
                            " matched_wake_event_canonical = %s,"
                            " fire_at = NULL,"
                            " ready_order = %s,"
                            # Targeted-lease-only guard for a seed-after-wake
                            # resume; one-shot, cleared by the owning
                            # driver's targeted lease.
                            " reserved = %s "
                            "WHERE task_id = %s",
                            (matched_blob, order, reserved, task_id),
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

        Each reclaim increments ``reclaim_count``, and at ``reclaim_max``
        consecutive no-progress reclaims the task drops to ``terminal``
        instead of requeueing, so a task that kills every worker that leases
        it cannot loop forever. Tasks terminated by that cap are NOT in the
        returned list.
        """
        requeued: list[str] = []
        with self._lock:
            self._begin_locked()
            try:
                now_expr, now_params = self._now_clause()
                stale = self._conn.execute(
                    "SELECT task_id, reclaim_count FROM dispatcher_tasks "
                    "WHERE status = 'leased' "
                    f"AND lease_expires_at <= {now_expr} "
                    "ORDER BY lease_expires_at",
                    now_params,
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
                            " worker_id = NULL,"
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
                        # Terminal is forever, so buffered wakes can never
                        # drain.
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
                        " worker_id = NULL,"
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

        ``now`` is a wall-clock epoch supplied by the caller on the same base
        the Engine used to compute ``fire_at``, and the delivered wake is the
        **recorded deadline** blob so re-delivery stays byte-identical. In
        DB-clock mode the parameter is **ignored**: both the probe and the
        sweep use the database's ``clock_timestamp()``, so every host answers
        "is it due?" identically whatever its own clock says.

        The read-only probe on the separate read connection runs FIRST so an
        idle poll — the common case, roughly once a second — returns without
        opening a write transaction or touching the dispatcher advisory lock.
        The probe/commit race is benign: a timer coming due in the gap is
        caught by the next poll.
        """
        with self._read_lock:
            if self._db_clock:
                due = self._read_conn.execute(
                    "SELECT 1 FROM dispatcher_tasks "
                    f"WHERE fire_at <= {_DB_NOW_SQL} LIMIT 1"
                ).fetchone()
            else:
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
                if self._db_clock:
                    rows = self._conn.execute(
                        "SELECT task_id, wake_on_canonical "
                        "FROM dispatcher_tasks "
                        f"WHERE fire_at <= {_DB_NOW_SQL} ORDER BY fire_at"
                    ).fetchall()
                else:
                    rows = self._conn.execute(
                        "SELECT task_id, wake_on_canonical "
                        "FROM dispatcher_tasks "
                        "WHERE fire_at <= %s ORDER BY fire_at",
                        (now,),
                    ).fetchall()
                for row in rows:
                    # ``fire_at`` is written only for TimerFired suspends, so
                    # this set is already the due timers. The decode guard
                    # still matters: a blob corrupted after suspend keeps its
                    # ``fire_at``, and skipping that row is better than
                    # delivering garbage as its matched wake.
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
        """Read-only single-SELECT path on the hot wire: every
        ``PostgresEventLog.emit`` carrying a ``lease_id`` calls this from
        inside its own open transaction.

        No advisory lock is taken — MVCC already guarantees committed-state
        visibility, and the read connection's lock only protects connection
        thread-safety.
        """
        return self._lease_is_active(task_id, lease_id=lease_id)

    def _lease_is_active(
        self, task_id: str, *, lease_id: Optional[str] = None
    ) -> bool:
        """Shared expiry check backing :meth:`is_lease_valid` and
        :meth:`has_active_lease`, so the two can never disagree about
        whether a lease is live."""
        with self._read_lock:
            now_expr, now_params = self._now_clause()
            if lease_id is not None:
                lease_clause = "AND lease_id = %s"
                params: tuple = (task_id, lease_id, *now_params)
            else:
                lease_clause = ""
                params = (task_id, *now_params)
            row = self._read_conn.execute(
                "SELECT 1 FROM dispatcher_tasks "
                "WHERE task_id = %s " + lease_clause + " "
                "AND status = 'leased' "
                f"AND lease_expires_at > {now_expr}",
                params,
            ).fetchone()
            return row is not None

    # ------------------------------------------------------------------
    # Introspection / maintenance (adapter-only, not on Protocols)
    # ------------------------------------------------------------------

    def task_status(self, task_id: str) -> Optional[str]:
        """Return the dispatcher status for ``task_id`` (``ready`` /
        ``leased`` / ``suspended`` / ``terminal``), or ``None`` if the
        dispatcher holds no row for it."""
        with self._read_lock:
            row = self._read_conn.execute(
                "SELECT status FROM dispatcher_tasks WHERE task_id = %s",
                (task_id,),
            ).fetchone()
        return None if row is None else str(row["status"])

    def has_active_lease(self, task_id: str) -> bool:
        """True iff a worker currently holds a *live* (non-expired) lease on
        ``task_id``.

        :meth:`task_status` returns the literal ``leased`` even for a lease
        whose TTL lapsed when the worker process died, so any caller asking
        "is a worker running this task right now?" must use this instead —
        it applies the expiry test, and a zombie lease reads as not running.
        """
        return self._lease_is_active(task_id)

    def restore_task(
        self,
        task_id: str,
        *,
        status: str,
        wake_on: Any = None,
        suspend_reason: Optional[str] = None,
    ) -> None:
        """Adapter-local lifecycle repair used by conversation rewind.

        ``TaskRewound`` re-bases the EventLog fold to an earlier state, and
        the dispatcher row — a lease/wake accelerator — has to be re-aligned
        with that baseline without pretending a worker performed a normal
        release. Deliberately not part of the Dispatcher Protocol.
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
                        " worker_id = NULL,"
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
                        " worker_id = NULL,"
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
                        " worker_id = NULL,"
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
                    # No buffered-wake drain here: the DELETE above already
                    # cleared every pending wake for this task, so a drain
                    # would query an empty table.
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def purge_task(self, task_id: str) -> None:
        """Hard-delete all dispatcher state for ``task_id`` (the task row plus
        any buffered pending wakes). A maintenance affordance kept off the
        Dispatcher Protocol; purging an unknown task is a no-op."""
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
