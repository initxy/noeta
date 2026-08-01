"""``SqliteDispatcher`` — sqlite3-backed adapter for ``Dispatcher`` + ``LeaseRegistry``.

Shares the sqlite file the sibling adapters open. Migration 3 owns
``dispatcher_tasks`` (one row per task carrying status + lease + suspend
metadata, with CHECK constraints physicalising the state-machine invariants so
a direct write bypassing this class is rejected) and
``dispatcher_pending_wakes`` (per-task FIFO of wake events, deliberately with
**no FK**, because ``wake(unknown, ...)`` may legitimately arrive before any
``enqueue`` creates the task row). Behaviour is pinned by
:class:`noeta.storage.memory.InMemoryDispatcher`.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path
from types import TracebackType
from typing import Any, Callable, Optional, Union

from noeta.protocols.canonical import from_canonical_bytes, to_canonical_bytes
from noeta.protocols.dispatcher import DEFAULT_QUEUE, Lease
from noeta.protocols.errors import InvalidLease, WakeConsumeMismatch
from noeta.protocols.wake import TimerFired
from noeta.storage.spi import reclaim_hits_cap, wake_matches

from noeta.builtins.storage.impl.sqlite._connection import _open_connection
from noeta.builtins.storage.impl.sqlite._transaction import _begin_immediate_with_retry
from noeta.builtins.storage.impl.sqlite.migrations import apply_migrations


__all__ = ["SqliteDispatcher"]


_VALID_RELEASE_STATES = frozenset({"suspended", "terminal"})


def _serialize_wake(value: Any) -> Optional[bytes]:
    if value is None:
        return None
    return to_canonical_bytes(value)


def _timer_deadline(wake_on: Any) -> Optional[float]:
    """The ``fire_at`` value mirrored onto the row for a timer suspend, or
    ``None`` for every non-timer wake.

    Kept in lockstep with ``wake_on_canonical`` at every write site so the
    indexed timer sweep (migration 7) selects the due set off ``fire_at``
    without decoding each suspended row. The invariant: ``fire_at`` is
    non-NULL iff the row is a suspended ``TimerFired`` wait carrying this
    deadline.
    """
    return wake_on.fire_at if isinstance(wake_on, TimerFired) else None


def _deserialize_wake(blob: Optional[bytes]) -> Any:
    if blob is None:
        return None
    return from_canonical_bytes(blob)


class SqliteDispatcher:
    """sqlite3 implementation of ``Dispatcher`` + ``LeaseRegistry``.

    Beyond the two Protocols the surface holds only the maintenance seams a
    host genuinely needs (status probes, ``restore_task``, ``purge_task``) plus
    lifecycle helpers. Finer-grained introspection — ``wake_on`` decoding,
    pending-wake enumeration — is deliberately absent; tests that need the
    underlying rows read ``_conn`` directly.
    """

    def __init__(
        self,
        path: Union[str, Path],
        *,
        # ``now`` defaults to wall-clock ``time.time``, NOT
        # ``time.monotonic``. ``lease_expires_at`` is a persisted
        # float; comparisons across a process restart only make sense
        # against a wall clock. InMemoryDispatcher's monotonic default
        # is fine because its state evaporates on restart, but a
        # persistent dispatcher must survive reboots.
        now: Optional[Callable[[], float]] = None,
        heartbeat_max: int = 360,
        max_fail_attempts: int = 3,
        reclaim_max: int = 3,
    ) -> None:
        target = str(path)
        self._conn = _open_connection(path)
        apply_migrations(self._conn)
        self._now = now or time.time
        self._heartbeat_max = heartbeat_max
        self._max_fail_attempts = max_fail_attempts
        self._reclaim_max = reclaim_max
        self._lock = threading.Lock()
        # ``is_lease_valid`` sits on the EventLog write path: every
        # ``emit(... lease_id=...)`` calls it from **inside** the EventLog's own
        # ``BEGIN IMMEDIATE``. Routing the validator through ``self._conn`` +
        # ``self._lock`` would deadlock — an EventLog thread holding the file's
        # writer lock waits on ``self._lock``, held by a thread itself blocked
        # acquiring that same writer lock for a dispatcher lifecycle method.
        # A separate read connection + lock runs validation under WAL's
        # concurrent-reader semantics instead, independent of the writer lock.
        # ``:memory:`` cannot have a second connection (each
        # ``sqlite3.connect(":memory:")`` opens a fresh empty DB), so it shares
        # the one connection; the deadlock cannot arise there anyway, as there
        # is no file-level writer lock to contend for.
        if target == ":memory:":
            self._read_conn: sqlite3.Connection = self._conn
            self._read_lock: threading.Lock = self._lock
        else:
            self._read_conn = _open_connection(path)
            self._read_lock = threading.Lock()
        self._closed = False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _next_ready_order(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(ready_order), 0) + 1 FROM dispatcher_tasks"
        ).fetchone()
        return int(row[0])

    def _next_arrival_seq(self, task_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(arrival_seq), -1) + 1 "
            "FROM dispatcher_pending_wakes WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return int(row[0])

    def _fetch_task(self, task_id: str) -> Optional[sqlite3.Row]:
        row = self._conn.execute(
            "SELECT * FROM dispatcher_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return row  # type: ignore[no-any-return]

    def _fetch_task_by_lease(self, lease_id: str) -> Optional[sqlite3.Row]:
        row = self._conn.execute(
            "SELECT * FROM dispatcher_tasks WHERE lease_id = ?",
            (lease_id,),
        ).fetchone()
        return row  # type: ignore[no-any-return]

    def _drain_first_matching_pending(
        self, task_id: str, wake_on: Any
    ) -> Optional[bytes]:
        """Delete the first buffered wake matching ``wake_on`` and return its
        canonical bytes, or ``None`` when none matches. The caller owns
        transitioning the task row to ``ready`` and clearing wake metadata."""
        if wake_on is None:
            return None
        for row in self._conn.execute(
            "SELECT arrival_seq, wake_event_canonical FROM dispatcher_pending_wakes "
            "WHERE task_id = ? ORDER BY arrival_seq",
            (task_id,),
        ).fetchall():
            wake_event = _deserialize_wake(row["wake_event_canonical"])
            if wake_matches(wake_on, wake_event):
                self._conn.execute(
                    "DELETE FROM dispatcher_pending_wakes "
                    "WHERE task_id = ? AND arrival_seq = ?",
                    (task_id, int(row["arrival_seq"])),
                )
                return bytes(row["wake_event_canonical"])
        return None

    # ------------------------------------------------------------------
    # Dispatcher Protocol
    # ------------------------------------------------------------------

    def enqueue(
        self,
        task_id: str,
        *,
        reserved: bool = False,
        queue: Optional[str] = None,
        parent_task_id: Optional[str] = None,
    ) -> None:
        """Mark ``task_id`` as ready-to-lease.

        Three paths matching the Protocol's idempotency promise:

        * No row → INSERT with a fresh ``ready_order``, born on its queue
          (explicit ``queue`` > ``parent_task_id``'s stored queue >
          ``DEFAULT_QUEUE``).
        * Existing row already in ``ready`` → no-op, preserving both the
          original ``ready_order`` and the existing ``reserved`` flag so FIFO
          is not reshuffled.
        * Existing row in any non-ready status → transition to ready,
          clearing all non-ready columns and assigning a fresh
          ``ready_order``. The stored ``queue`` is untouched — a row's queue
          is immutable after birth.

        ``reserved`` (see :meth:`Dispatcher.enqueue`) marks the task
        targeted-lease-only until its first lease claims it.
        """
        reserved_val = 1 if reserved else 0
        with self._lock:
            _begin_immediate_with_retry(self._conn)
            try:
                row = self._fetch_task(task_id)
                if row is None:
                    if queue is None and parent_task_id is not None:
                        parent = self._fetch_task(parent_task_id)
                        queue = parent["queue"] if parent is not None else None
                    order = self._next_ready_order()
                    self._conn.execute(
                        "INSERT INTO dispatcher_tasks ("
                        " task_id, status, lease_id, lease_expires_at,"
                        " heartbeat_count, fail_attempts,"
                        " wake_on_canonical, suspend_reason, ready_order,"
                        " reserved, queue"
                        ") VALUES (?, 'ready', NULL, NULL, 0, 0, NULL, NULL, ?, ?, ?)",
                        (
                            task_id,
                            order,
                            reserved_val,
                            queue if queue is not None else DEFAULT_QUEUE,
                        ),
                    )
                elif row["status"] == "ready":
                    # No-op; FIFO must not be reshuffled.
                    pass
                else:
                    # Clear every field belonging to the prior state in lockstep
                    # with the status flip, ``matched_wake_event_canonical``
                    # included: a stale matched wake surviving a force-enqueue
                    # would make the next lease deliver a wake_event nobody
                    # asked for.
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
                        " ready_order = ?,"
                        " reserved = ? "
                        "WHERE task_id = ?",
                        (order, reserved_val, task_id),
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
        queue: str = DEFAULT_QUEUE,
    ) -> Optional[Lease]:
        """Lease a ready task — per-``queue`` FIFO when ``task_id is None``,
        targeted (``queue`` ignored) otherwise.

        Targeted-lease semantics (``task_id=<id>``): returns ``None`` if the
        task does not exist or is not currently ``ready``. Never raises —
        diagnosing which of those it was is the caller's job.

        On success, any ``matched_wake_event_canonical`` queued by a prior
        :meth:`wake` or release-drain is handed back on
        :attr:`Lease.wake_event` but **NOT** cleared: it survives the lease and
        is cleared only by a consuming ``release(consumed_wake_event=...)``,
        otherwise re-delivered by :meth:`requeue_stale`. At-least-once delivery
        plus idempotent consumption is what makes it effectively exactly-once.
        """
        # sqlite is single-host by design (ADR ``multi-host-lease-fencing``):
        # one worker pool per store, so there is no cross-host lease audit to
        # answer and ``worker_id`` is deliberately not recorded — not a gap to
        # fill later. Postgres is multi-host and does record it per lease.
        del worker_id
        with self._lock:
            _begin_immediate_with_retry(self._conn)
            try:
                if task_id is None:
                    # ``reserved`` tasks are targeted-lease-only (a fresh
                    # subtask child its drain/executor must claim first) —
                    # skip them in the untargeted FIFO selection, which never
                    # crosses queues (ADR ``worker-queue-routing``).
                    row = self._conn.execute(
                        "SELECT task_id, matched_wake_event_canonical "
                        "FROM dispatcher_tasks "
                        "WHERE status = 'ready' AND reserved = 0 "
                        " AND queue = ? "
                        "ORDER BY ready_order LIMIT 1",
                        (queue,),
                    ).fetchone()
                else:
                    row = self._conn.execute(
                        "SELECT task_id, matched_wake_event_canonical "
                        "FROM dispatcher_tasks "
                        "WHERE task_id = ? AND status = 'ready'",
                        (task_id,),
                    ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return None
                leased_task_id = row["task_id"]
                matched_blob = row["matched_wake_event_canonical"]
                lease_id = f"lease-{uuid.uuid4().hex}"
                expires_at = self._now() + lease_seconds
                # ``matched_wake_event_canonical`` is deliberately left in
                # place: the matched wake stays in flight across the lease so a
                # crash before the durable TaskWoken cannot lose it. Only a
                # consuming release clears it; anything else re-delivers it.
                self._conn.execute(
                    "UPDATE dispatcher_tasks SET "
                    " status = 'leased',"
                    " lease_id = ?,"
                    " lease_expires_at = ?,"
                    " heartbeat_count = 0,"
                    " suspend_reason = NULL,"
                    " ready_order = NULL,"
                    # One-shot claim: this lease (only a targeted one can
                    # reach a reserved row) claims the child for its owning
                    # driver, so clear the guard. A later suspend/resume
                    # re-enqueue is then an ordinary untargeted-leaseable task.
                    " reserved = 0 "
                    "WHERE task_id = ?",
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
            _begin_immediate_with_retry(self._conn)
            try:
                row = self._fetch_task_by_lease(lease_id)
                if row is None or row["status"] != "leased":
                    self._conn.execute("ROLLBACK")
                    raise InvalidLease(lease_id)
                if int(row["heartbeat_count"]) >= self._heartbeat_max:
                    # Cap exceeded — force release in the same transaction.
                    # There is no TaskWoken proof here, so a matched wake is
                    # NOT consumed: it must be preserved and re-delivered
                    # rather than stranded. With one in flight the task returns
                    # to **ready** (re-deliverable, like a non-consuming
                    # release); otherwise it suspends on its preserved wake_on.
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
                            " ready_order = ? "
                            "WHERE task_id = ?",
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
                            "WHERE task_id = ?",
                            (row["task_id"],),
                        )
                    self._conn.execute("COMMIT")
                    raise InvalidLease(lease_id)
                expires_at = self._now() + lease_seconds
                # A successful heartbeat is the leased-task progress signal, so
                # it resets the stale-reclaim counter.
                self._conn.execute(
                    "UPDATE dispatcher_tasks SET "
                    " heartbeat_count = heartbeat_count + 1,"
                    " reclaim_count = 0,"
                    " lease_expires_at = ? "
                    "WHERE lease_id = ?",
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
            _begin_immediate_with_retry(self._conn)
            try:
                row = self._fetch_task_by_lease(lease_id)
                if row is None:
                    self._conn.execute("ROLLBACK")
                    raise InvalidLease(lease_id)
                task_id = row["task_id"]

                # Clear the old matched wake only when a consuming release
                # presents the exact same wake. A mismatch (or none stored)
                # raises and rolls back, committing nothing.
                clear_matched = False
                if consumed_wake_event is not None:
                    stored = row["matched_wake_event_canonical"]
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
                        "WHERE task_id = ?",
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
                        " suspend_reason = ?,"
                        " fire_at = NULL,"
                        " ready_order = NULL "
                        "WHERE task_id = ?",
                        (suspend_reason, task_id),
                    )
                    # Terminal is forever, so buffered wakes that never matched
                    # can never drain — GC them. The matched wake is kept: it
                    # is still owed to whoever consumes it.
                    self._conn.execute(
                        "DELETE FROM dispatcher_pending_wakes WHERE task_id = ?",
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
                        " wake_on_canonical = ?,"
                        " suspend_reason = ?,"
                        " fire_at = ?,"
                        " ready_order = NULL "
                        "WHERE task_id = ?",
                        (wake_blob, suspend_reason, _timer_deadline(wake_on), task_id),
                    )
                    matched_present = (
                        not clear_matched
                        and row["matched_wake_event_canonical"] is not None
                    )
                    if matched_present:
                        # An un-consumed matched wake means a delivery is still
                        # pending, so the task goes back to **ready** where it
                        # can be re-delivered, not into a stuck suspend.
                        order = self._next_ready_order()
                        self._conn.execute(
                            "UPDATE dispatcher_tasks SET "
                            " status = 'ready',"
                            " wake_on_canonical = NULL,"
                            " suspend_reason = NULL,"
                            " fire_at = NULL,"
                            " ready_order = ? "
                            "WHERE task_id = ?",
                            (order, task_id),
                        )
                    else:
                        # Nothing matched yet: the new wake_on is already
                        # installed above, so drain one matching buffered wake,
                        # which may itself become the new matched wake.
                        drained = self._drain_first_matching_pending(task_id, wake_on)
                        if drained is not None:
                            order = self._next_ready_order()
                            self._conn.execute(
                                "UPDATE dispatcher_tasks SET "
                                " status = 'ready',"
                                " wake_on_canonical = NULL,"
                                " suspend_reason = NULL,"
                                " matched_wake_event_canonical = ?,"
                                " fire_at = NULL,"
                                " ready_order = ? "
                                "WHERE task_id = ?",
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

        Transitions leased→ready WITHOUT incrementing fail_attempts — used
        by transports that seed a task durably under a targeted lease and
        then hand it off to a resident worker pool. Any matched wake is
        preserved (same discipline as a retryable-fail requeue), so a wake
        delivered before the yield is not lost.
        """
        with self._lock:
            _begin_immediate_with_retry(self._conn)
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
                    " ready_order = ? "
                    "WHERE task_id = ?",
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
            _begin_immediate_with_retry(self._conn)
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
                        " ready_order = ? "
                        "WHERE task_id = ?",
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
                        " fail_attempts = fail_attempts + ?,"
                        " wake_on_canonical = NULL,"
                        " suspend_reason = ?,"
                        " fire_at = NULL,"
                        " ready_order = NULL "
                        "WHERE task_id = ?",
                        (1 if retryable else 0, final_reason, task_id),
                    )
                    # Terminal transition GCs buffered wakes that can never
                    # drain, same as release-terminal.
                    self._conn.execute(
                        "DELETE FROM dispatcher_pending_wakes WHERE task_id = ?",
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
            _begin_immediate_with_retry(self._conn)
            try:
                row = self._fetch_task(task_id)

                if row is None:
                    # Wake-before-enqueue: buffer the event and create no
                    # ``dispatcher_tasks`` row. Any row the CHECK constraints
                    # would accept implies a consistent state (ready +
                    # ready_order, say), which would make the task leaseable
                    # before it exists. ``enqueue`` materialises the row later
                    # and ``release(suspended, wake_on=...)`` drains this.
                    arrival_seq = self._next_arrival_seq(task_id)
                    self._conn.execute(
                        "INSERT INTO dispatcher_pending_wakes ("
                        " task_id, arrival_seq, wake_event_canonical"
                        ") VALUES (?, ?, ?)",
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
                            " matched_wake_event_canonical = ?,"
                            " fire_at = NULL,"
                            " ready_order = ?,"
                            # Targeted-lease-only guard for a seed-after-wake
                            # resume; one-shot, cleared by the owning driver's
                            # targeted lease.
                            " reserved = ? "
                            "WHERE task_id = ?",
                            (matched_blob, order, 1 if reserved else 0, task_id),
                        )
                        self._conn.execute("COMMIT")
                        return True

                arrival_seq = self._next_arrival_seq(task_id)
                self._conn.execute(
                    "INSERT INTO dispatcher_pending_wakes ("
                    " task_id, arrival_seq, wake_event_canonical"
                    ") VALUES (?, ?, ?)",
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
        (``stale_reclaim_exceeded``) instead of requeueing — otherwise a task
        that kills its worker every time loops forever. Terminal-by-cap tasks
        are NOT in the returned list.
        """
        now = self._now()
        requeued: list[str] = []
        with self._lock:
            _begin_immediate_with_retry(self._conn)
            try:
                stale = self._conn.execute(
                    "SELECT task_id, reclaim_count FROM dispatcher_tasks "
                    "WHERE status = 'leased' AND lease_expires_at <= ? "
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
                            "WHERE task_id = ?",
                            (task_id,),
                        )
                        # Terminal transition GCs buffered wakes.
                        self._conn.execute(
                            "DELETE FROM dispatcher_pending_wakes WHERE task_id = ?",
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
                        " ready_order = ? "
                        "WHERE task_id = ?",
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
        delivered wake is the **recorded deadline** blob so timer re-delivery
        stays byte-identical.

        The due set is selected straight off the indexed ``fire_at`` column
        (migration 7) — ``fire_at`` mirrors each suspended timer's deadline
        and is NULL for every non-timer / non-suspended row, so the partial
        index makes the sweep an O(due) range hit rather than an
        O(all-suspends) full scan. A read-only probe on the concurrent-reader
        connection runs FIRST: when nothing is due (the common ~1s poll) it
        returns without opening ``BEGIN IMMEDIATE`` at all, so an idle poll
        never takes the write lock. The probe/commit race is benign — a timer
        that comes due in the gap is caught by the next poll.
        """
        with self._read_lock:
            due = self._read_conn.execute(
                "SELECT 1 FROM dispatcher_tasks WHERE fire_at <= ? LIMIT 1",
                (now,),
            ).fetchone()
        if due is None:
            return []
        fired: list[str] = []
        with self._lock:
            _begin_immediate_with_retry(self._conn)
            try:
                rows = self._conn.execute(
                    "SELECT task_id, wake_on_canonical FROM dispatcher_tasks "
                    "WHERE fire_at <= ? ORDER BY fire_at",
                    (now,),
                ).fetchall()
                for row in rows:
                    # ``fire_at`` is written only for TimerFired suspends, so
                    # this set is already the due timers. Still decode-guard:
                    # a blob corrupted AFTER suspend keeps its ``fire_at``, so
                    # skip an undecodable / non-timer row rather than deliver
                    # garbage as its matched wake. The guard now runs over the
                    # DUE rows only (usually none), not every suspend.
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
                        " matched_wake_event_canonical = ?,"
                        " fire_at = NULL,"
                        " ready_order = ? "
                        "WHERE task_id = ?",
                        (
                            bytes(row["wake_on_canonical"]),
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
        ``SqliteEventLog.emit`` with a ``lease_id`` calls this. No
        ``BEGIN IMMEDIATE``; WAL guarantees committed-state visibility
        across connections, and the read connection's own lock just
        protects ``sqlite3.Connection`` thread-safety. The read
        connection is intentionally separate from the lifecycle
        writer connection so EventLog ``BEGIN IMMEDIATE`` callers
        cannot deadlock against dispatcher lifecycle methods (see
        ``__init__`` for the full reasoning).
        """
        with self._read_lock:
            row = self._read_conn.execute(
                "SELECT lease_expires_at FROM dispatcher_tasks "
                "WHERE task_id = ? AND lease_id = ? AND status = 'leased'",
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
                "SELECT status FROM dispatcher_tasks WHERE task_id = ?",
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
        guard) must use this, not ``task_status() == 'leased'``; otherwise a
        dead lease wedges the task as permanently undeletable. Read-only
        single SELECT on the read connection (same reasoning as
        ``is_lease_valid``)."""
        with self._read_lock:
            row = self._read_conn.execute(
                "SELECT lease_expires_at FROM dispatcher_tasks "
                "WHERE task_id = ? AND status = 'leased'",
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

        ``TaskRewound`` re-bases the EventLog fold to an older snapshot-shaped
        state. The dispatcher row is a lease/wake accelerator, so the live
        rewind command must re-align it with that folded baseline without
        pretending a worker performed a normal lease release. This is a
        maintenance seam, deliberately not part of the Dispatcher Protocol.
        """
        if status not in {"ready", "suspended", "terminal"}:
            raise ValueError(f"invalid restore status: {status}")
        with self._lock:
            _begin_immediate_with_retry(self._conn)
            try:
                self._conn.execute(
                    "DELETE FROM dispatcher_pending_wakes WHERE task_id = ?",
                    (task_id,),
                )
                if status == "ready":
                    order = self._next_ready_order()
                    self._conn.execute(
                        "INSERT INTO dispatcher_tasks ("
                        " task_id, status, ready_order"
                        ") VALUES (?, 'ready', ?) "
                        "ON CONFLICT(task_id) DO UPDATE SET "
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
                        ") VALUES (?, 'terminal', ?) "
                        "ON CONFLICT(task_id) DO UPDATE SET "
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
                        ") VALUES (?, 'suspended', ?, ?, ?) "
                        "ON CONFLICT(task_id) DO UPDATE SET "
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
                    # No buffered-wake redelivery here: the DELETE above already
                    # cleared every pending wake for this task, so any drain
                    # would query an empty table and never re-ready the task.
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
            _begin_immediate_with_retry(self._conn)
            try:
                self._conn.execute(
                    "DELETE FROM dispatcher_pending_wakes WHERE task_id = ?",
                    (task_id,),
                )
                self._conn.execute(
                    "DELETE FROM dispatcher_tasks WHERE task_id = ?",
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
            # ``self._read_conn is self._conn`` for ``:memory:``; in
            # that case ``conn.close()`` already closed it and a
            # second close on the same handle is a no-op for sqlite3.
            if self._read_conn is not self._conn:
                self._read_conn.close()
        finally:
            self._closed = True

    def __enter__(self) -> "SqliteDispatcher":
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.close()
