"""Dispatcher and LeaseRegistry Protocols — the scheduling boundary.

One worker, one lease, one stretch: the Dispatcher owns Task enqueue, lease
grant, wake dispatch, and stale reclaim. :class:`LeaseRegistry` is split out
because the single-writer invariant makes an EventLog backend validate a
``lease_id`` on every write, and that backend has no business knowing about
enqueue or wake — narrowing its reverse dependency to one method keeps the
dependency graph acyclic. A single class may implement both Protocols.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Optional, Protocol


__all__ = ["DEFAULT_QUEUE", "Dispatcher", "Lease", "LeaseRegistry"]


#: The queue a task lands on when no explicit queue is given and no parent row
#: exists to inherit from. Single-pool deployments never see another name.
DEFAULT_QUEUE: Final[str] = "default"


@dataclass(frozen=True, slots=True)
class Lease:
    """Short-lived exclusive grant of a Task to a Worker.

    Dispatcher mints leases via :meth:`Dispatcher.lease`; the Worker
    presents ``lease_id`` to EventLog on every emit so the write is
    attributed to a known leaseholder.

    ``wake_event`` is populated when the Task being leased had a
    matched wake event waiting (set by a prior ``dispatcher.wake(...)``
    or by the ``release(suspended)`` pending-wake-drain). The matched
    event is **NOT** consumed at lease time — it survives the
    lease ("matched-in-flight") so a crash before the durable
    ``TaskWoken`` does not lose it. It is cleared only by a **consuming
    release** (``release(consumed_wake_event=<this>)``, after ``TaskWoken``
    is durable) and is otherwise **re-delivered** by ``requeue_stale``
    after a crash (at-least-once delivery + idempotent consumption =
    exactly-once). Workers MUST forward the value into
    ``Engine.note_woken(task, wake_event=...)`` before the first
    ``run_one_step`` to write the durable ``TaskWoken`` envelope, then
    pass it back as ``consumed_wake_event`` on the eventual ``release``.
    """

    lease_id: str
    task_id: str
    expires_at: float
    wake_event: Optional[Any] = None


class LeaseRegistry(Protocol):
    """Read-only view of "is this lease still live?".

    EventLog backends use this to validate writes without coupling to
    the full Dispatcher surface. The single method is intentionally
    side-effect-free so a backend can call it from inside its write
    lock without re-entrancy concerns.
    """

    def is_lease_valid(self, task_id: str, lease_id: str) -> bool:
        """Return True iff ``lease_id`` is the live lease for ``task_id``.

        "Live" means the lease has not been released, has not expired,
        and the Dispatcher considers the holding worker still active.
        """
        ...


class Dispatcher(Protocol):
    """Scheduling + lease lifecycle.

    Worker calls follow ``enqueue → lease → (heartbeat*) → release / fail``;
    the wake half is asynchronous (``wake`` requeues a suspended Task).
    ``requeue_stale`` is the recovery sweep that moves leased Tasks
    whose lease expired back to ready, and ``fire_due_timers`` is the
    timer sweep that wakes ``wait_timer`` suspends whose deadline
    passed; the Worker daemon runs both on an interval.

    Introspection helpers (``task_status`` / ``wake_on`` / ``suspend_reason``)
    are deliberately NOT on this Protocol: they are test-only affordances of an
    adapter, and production code that wants task state folds the EventLog,
    which is the single source of truth.
    """

    def enqueue(
        self,
        task_id: str,
        *,
        reserved: bool = False,
        queue: Optional[str] = None,
        parent_task_id: Optional[str] = None,
    ) -> None:
        """Mark ``task_id`` as ready-to-lease.

        Idempotent: enqueueing an already-ready task is a no-op.

        ``queue`` / ``parent_task_id`` decide the queue a **new** row is born
        on (see :data:`DEFAULT_QUEUE`): an explicit ``queue`` wins; else the
        stored queue of ``parent_task_id``'s row (child inheritance — the
        observer and the background-subagent submit pass it); else the
        default. A row's queue is **immutable after birth** — both arguments
        are ignored for an existing row, whatever its status. Routing is
        dispatcher-local state, never task identity (ADR
        ``worker-queue-routing``).

        ``reserved=True`` marks the task as **targeted-lease-only**: an
        untargeted ``lease(task_id=None)`` FIFO poll SKIPS it, so only a
        targeted ``lease(task_id=<id>)`` can claim it. It exists for a task
        enqueued before it has been seeded: a bare ``run_leased_task`` step
        does not seed, so a resident-worker pool that stole the task would
        drive it with an empty message history the provider rejects. Both
        reservers are seeders — a freshly-created subtask child (whose goal
        only ``subtask_drain._descend_to_child`` writes) and a freshly-created
        root task (whose ``ModelBound`` + opening goal message only
        ``InteractionDriver.seed_start``'s tail writes).

        The flag is a ONE-SHOT claim guard: the first successful ``lease``
        CLEARS it, so once seeded, a task re-entering the ready queue (a seed
        ``release_yield``'d to the pool, or a suspend/approval resume) is an
        ordinary untargeted-leaseable task.
        """
        ...

    def lease(
        self,
        *,
        worker_id: str,
        lease_seconds: float = 30.0,
        task_id: Optional[str] = None,
        queue: str = DEFAULT_QUEUE,
    ) -> Optional[Lease]:
        """Try to acquire a lease on a ready Task.

        ``task_id=None`` (default): pick the first ready Task **on
        ``queue``** in FIFO order (``ready_order`` ascending). There is no
        wildcard claim — an untargeted poll never crosses queues, so a pool
        can only ever drive work routed to it (ADR ``worker-queue-routing``).

        ``task_id=<id>``: targeted lease — ``queue`` is ignored; the caller
        already knows exactly which task it wants.

        ``task_id=<id>``: targeted lease — return a :class:`Lease`
        only if that specific Task is in the ready queue and not
        already leased. Returns ``None`` for any of: not enqueued,
        not yet existent, currently leased by another worker,
        suspended, terminal. **Never raises** on not-found — the
        caller distinguishes "task does not exist" from "task exists
        but is in a non-ready state" with its own diagnostics path
        (typical pattern: ``event_log.read(task_id)`` + ``fold``).

        ``lease_seconds`` is the initial deadline; the worker MUST
        call :meth:`heartbeat` before expiry to extend it.

        The returned ``Lease.wake_event`` is the matched wake (if one was
        queued); it is **not** consumed at lease time — see :class:`Lease` for
        the consume-at-release / re-delivery contract, and
        :func:`noeta.protocols.wake.matches_wake` for the projection-matching
        invariant that produced it.
        """
        ...

    def heartbeat(self, lease_id: str, *, lease_seconds: float = 30.0) -> float:
        """Extend ``lease_id``'s deadline; return the new expires_at.

        Raises:
            noeta.protocols.errors.InvalidLease — lease is unknown,
                released, expired, or the heartbeat cap was reached.
        """
        ...

    def release(
        self,
        lease_id: str,
        *,
        next_state: str,
        wake_on: Any = None,
        suspend_reason: Optional[str] = None,
        consumed_wake_event: Any = None,
    ) -> None:
        """Release ``lease_id`` and transition the Task.

        ``next_state`` is one of ``"suspended"`` / ``"terminal"``.
        When suspending, ``wake_on`` carries the typed WakeCondition
        the Dispatcher matches against incoming wake events;
        ``suspend_reason`` is a short tag for observability.

        ``consumed_wake_event``: when a woken lease
        finishes after writing a durable ``TaskWoken``, the worker passes
        the lease's wake event here so the Dispatcher clears the task's
        ``matched_wake_event`` — but **only if it equals the stored matched
        event**. If it is set but does NOT equal the stored matched (or no
        matched is stored), the Dispatcher raises
        :class:`noeta.protocols.errors.WakeConsumeMismatch` and commits
        **nothing** (rollback). ``None`` (every non-consuming release:
        skipped / terminal-without-wake / fail / heartbeat-cap) never
        touches ``matched_wake_event`` and never raises — so an
        un-consumed wake is preserved and re-delivered (at-least-once),
        making delivery+consume exactly-once across crashes.

        On a ``suspended`` release that BOTH clears a stored matched
        (``consumed_wake_event``) AND installs a new ``wake_on``,
        the order is fixed: validate+clear the old matched FIRST, then
        install the new ``wake_on`` and drain pending wakes against it
        (which may set a fresh matched) — the old matched and the new
        wake_on are never mixed in one slot.

        Raises:
            noeta.protocols.errors.InvalidLease — lease is unknown.
            noeta.protocols.errors.WakeConsumeMismatch — ``consumed_wake_event``
                is set but does not equal the stored matched event.
            ValueError — ``next_state`` is not one of the two valid
                values.
        """
        ...

    def release_yield(self, lease_id: str) -> None:
        """Yield a freshly-seeded lease back to the ready queue.

        Used by transports that seed a task durably (create task, append
        seed events) under a targeted lease and then hand the task off to
        a resident worker pool. Transitions ``leased → ready`` with a
        fresh ``ready_order``, clearing lease fields but preserving any
        ``matched_wake_event_canonical`` (so a wake delivered before the
        yield is not lost). Does NOT increment ``fail_attempts`` — this
        is a voluntary yield, not a failure.

        Raises:
            noeta.protocols.errors.InvalidLease — lease is unknown.
        """
        ...

    def fail(
        self,
        lease_id: str,
        *,
        retryable: bool = False,
        reason: Optional[str] = None,
    ) -> None:
        """Release ``lease_id`` on failure.

        Retryable failures requeue the Task up to the backend's
        ``max_fail_attempts``; past that, the Task drops to terminal.
        Non-retryable failures drop straight to terminal.
        """
        ...

    def wake(self, task_id: str, wake_event: Any, *, reserved: bool = False) -> bool:
        """Deliver a wake event to a suspended Task.

        Returns True iff the Task was requeued (either because the
        event matched a live ``wake_on``, or because the event was
        buffered against a not-yet-suspended Task and matches on
        eventual suspend).

        ``reserved=True`` marks the requeued Task **targeted-lease-only**
        (same one-shot guard as :meth:`enqueue`), so an untargeted
        ``lease(task_id=None)`` FIFO poll SKIPS it until the driver that owns
        this wake claims it with a targeted lease. A resume that appends the
        command's message only after its own targeted lease sets this: between
        the wake and that lease the Task is ready-but-unseeded, and a
        resident-worker pool leasing it there would re-drive the turn WITHOUT
        the command's input. Only the matched→ready transition carries the
        flag; a buffered wake never becomes leaseable, so there is nothing to
        reserve.
        """
        ...

    def requeue_stale(self) -> list[str]:
        """Sweep expired leases back to ready; return the requeued ids.

        Workers that fail to heartbeat before deadline have their
        leases reclaimed by this call. EventLog writes against the
        old ``lease_id`` then raise ``InvalidLease`` against the
        registry.
        """
        ...

    def fire_due_timers(self, *, now: float) -> list[str]:
        """Deliver ``TimerFired`` wakes to every suspended Task whose
        timer deadline has passed; return the woken ids.

        This is the timer producer half of the ``wait_timer`` Decision
        branch: the suspend records ``wake_on=TimerFired(fire_at=...)``
        (an absolute wall-clock epoch deadline computed by the Engine's
        clock), and a periodic sweep — the Worker daemon's timer poll,
        alongside :meth:`requeue_stale` — calls this to flip due Tasks
        back to ready.

        ``now`` is a wall-clock epoch timestamp supplied by the caller.
        It is deliberately a parameter, NOT the backend's internal
        clock: it must share the time base the Engine used to compute
        ``fire_at`` (``time.time``), whereas e.g. the in-memory
        backend's internal ``now`` defaults to ``time.monotonic``.

        The delivered wake event is the **recorded deadline**
        (``TimerFired(fire_at=<stored deadline>)``, satisfying the
        inclusive ``>=`` threshold at the equality boundary), not
        ``TimerFired(fire_at=now)`` — so a re-delivery after a crash
        hands the worker a byte-identical wake event and the durable
        ``TaskWoken`` reconciliation matches by equality.
        """
        ...
