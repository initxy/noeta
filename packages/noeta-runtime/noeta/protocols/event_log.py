"""EventLog Protocol — the append-only task stream, Noeta's source of truth for
decisions and causality.

The boundary is split by capability (read, write, subscribe, task-index) so a
callsite's type hint states its intent — "I only read" — and import-linter can
lock reverse coupling down precisely. One adapter class may implement several
of these Protocols; callers should still ask for the narrowest one they need.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

from noeta.protocols.events import EventEnvelope, EventOrigin


__all__ = [
    "SNAPSHOT_BASELINE_EVENT_TYPES",
    "EventLog",
    "EventLogFull",
    "EventLogReader",
    "EventLogSubscriber",
    "EventLogTaskIndex",
    "EventLogWriter",
    "StopHandle",
    "Subscriber",
    "TaskStreamSummary",
    "Unsubscribe",
    "subscribe_with_stop",
]


#: The fold-baseline event types — every type whose payload carries a
#: ``state_ref`` rehydration body and re-bases the fold: ``TaskSnapshot``
#: (acceleration), ``TaskRewound`` (conversation rewind),
#: ``StepAttemptAbandoned`` (crash-recovery seal) and ``TaskForked`` (the
#: history a branched conversation inherits). This is the single source of
#: truth for the :meth:`EventLogReader.find_latest_snapshot` contract; every
#: adapter's lookup filters on exactly this set. The SQL adapters' partial
#: index (``ix_events_snapshot``) must ALSO list exactly these types in its
#: ``WHERE`` predicate — a partial index is only chosen when its predicate
#: matches the live query — so growing this tuple requires a migration that
#: re-widens the index.
SNAPSHOT_BASELINE_EVENT_TYPES: tuple[str, ...] = (
    "TaskSnapshot",
    "TaskRewound",
    "StepAttemptAbandoned",
    "TaskForked",
)


Subscriber = Callable[[EventEnvelope], None]
Unsubscribe = Callable[[], None]


class StopHandle:
    """Idempotent handle wrapping an :data:`Unsubscribe` callable.

    It collapses the two things every subscriber ends up tracking — the
    unsubscribe callable and a stopped flag — into one object, so teardown
    paths that may fire twice stay safe.

    :meth:`stop` is exactly-once even under concurrent calls: the
    check-and-flag step runs under an internal :class:`threading.Lock`, while
    the underlying :data:`Unsubscribe` is called outside it so a slow teardown
    cannot block other ``stop()`` / ``stopped`` callers.
    """

    __slots__ = ("_unsubscribe", "_stopped", "_lock")

    def __init__(self, unsubscribe: Unsubscribe) -> None:
        self._unsubscribe = unsubscribe
        self._stopped = False
        self._lock = threading.Lock()

    @property
    def stopped(self) -> bool:
        return self._stopped

    def stop(self) -> None:
        """Call the underlying ``Unsubscribe`` exactly once."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            unsubscribe = self._unsubscribe
        unsubscribe()


def subscribe_with_stop(
    subscriber: "EventLogSubscriber", callback: Subscriber
) -> StopHandle:
    """Wrap ``subscriber.subscribe(callback)`` in an idempotent
    :class:`StopHandle`."""
    return StopHandle(subscriber.subscribe(callback))


class EventLogReader(Protocol):
    """Read side of a task's EventLog stream.

    All methods are pure: no IO outside the stream itself, no clock /
    random / network. Multiple readers may share one underlying log.
    """

    def read(
        self, task_id: str, *, after_seq: Optional[int] = None
    ) -> list[EventEnvelope]:
        """Return events for ``task_id`` in append order.

        ``after_seq`` filters to events strictly past that seq (``None``
        returns the whole stream). Backends MAY return a snapshot copy
        — callers must not assume identity stability across calls.
        """
        ...

    def find_latest_snapshot(self, task_id: str) -> Optional[EventEnvelope]:
        """Return the most recent fold-baseline envelope, or ``None``.

        A baseline is any member of :data:`SNAPSHOT_BASELINE_EVENT_TYPES`;
        implementations return whichever has the highest seq. Fold acceleration
        depends on this lookup being cheap enough to call every Engine step, so
        in-memory backends MAY reverse-scan while SQL backends SHOULD index
        those types.
        """
        ...


class EventLogWriter(Protocol):
    """Write side of a task's EventLog stream.

    Two emit methods:

    * :meth:`emit` is the business-path write. The backend MUST enforce
      lease validity (when a ``lease_validator`` is wired) and MAY use
      ``expected_seq`` / ``idempotency_key`` for optimistic concurrency
      and retry dedup.
    * :meth:`system_emit` is the cross-stream system write — used when
      an Observer writes onto a stream it does not hold the lease for
      (``ChildLifecycleObserver`` appending ``SubtaskCompleted`` to the
      parent stream). No lease check, no idempotency, no ``expected_seq``.
      Callers MUST set ``actor`` so the recorded envelope is attributable,
      and ``origin`` so readers such as ``AuditObserver`` (which writes
      ``AuditRecord.origin``) can classify the writer.
    """

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
        """Append one business event.

        Raises:
            noeta.protocols.errors.InvalidLease — ``lease_id`` is not
                live (backends with a wired ``LeaseRegistry`` only).
            noeta.protocols.errors.StaleSequence — ``expected_seq`` did
                not match the next slot.
            noeta.protocols.errors.PayloadTooLarge — canonical bytes of
                ``payload`` exceeded the 4-KB envelope cap.

        Idempotency: if ``(lease_id, idempotency_key)`` was previously
        seen on this stream, returns the originally-assigned envelope
        without writing a new one.
        """
        ...

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
        """Append one cross-stream system event.

        No lease check, no idempotency dedup, no ``expected_seq``. ``actor``
        carries the system writer's identity (``child_observer``, ``llm``, …);
        ``origin`` names the Noeta role (engine / llm / observer / tool /
        system) that readers such as ``AuditObserver`` key on.
        """
        ...


class EventLogSubscriber(Protocol):
    """Subscribe to every successful append on the log.

    Delivery semantics that every adapter MUST honour:

    * **Synchronous before emit returns.** ``subscribe`` registers a
      callback; the callback runs synchronously before the originating
      ``emit`` / ``system_emit`` call returns. Workers can rely on
      observer side-effects (e.g. ``ChildLifecycleObserver`` writing
      ``SubtaskCompleted`` to a parent stream) being visible by the
      time the child's emit has returned.
    * **After the append is durable.** Notification fires *after* the
      append has been committed to the underlying store. Observers
      only ever see envelopes that the EventLog itself has accepted.
    * **Outside the adapter writer lock.** Notification fires after
      the adapter has released its writer lock / committed its
      ``BEGIN IMMEDIATE`` transaction. Concurrent
      writers from different threads can therefore enter the same
      observer callback at the same time — observers are responsible
      for their own thread-safety. No global ordering of callbacks
      across writers is promised; observers that need ordering should
      key off ``(task_id, seq)`` on the envelope.
    * **Exceptions are swallowed**. A subscriber callback
      that raises must not break the writer — observers do not get
      to abort a Task by misbehaving.
    """

    def subscribe(self, callback: Subscriber) -> Unsubscribe:
        """Register a callback; return an ``unsubscribe()`` function."""
        ...


@dataclass(frozen=True, slots=True)
class TaskStreamSummary:
    """One row of the task catalog.

    The minimal, transport-neutral enumeration shape a task list needs —
    ``task_id`` plus the stream-tail bookmarks it orders by. Pure stdlib types,
    so no storage-adapter type leaks through this seam; richer lifecycle fields
    (status / closed / …) are derived by ``noeta.read_models`` through
    :func:`noeta.core.fold.fold` rather than carried here.
    """

    task_id: str
    last_seq: int
    last_event_time: float


class EventLogTaskIndex(Protocol):
    """Enumerate the task streams a storage-backed event log holds.

    A **separate capability** from :class:`EventLogReader` on purpose: reading a
    *single* task's stream (fold, the LLM cursor) must NOT imply the ability to
    enumerate *every* task, so the narrow per-task readers do not implement it.
    ``noeta.read_models`` consumes this to build a task list without reaching
    into adapter internals.
    """

    def list_task_streams(self) -> list[TaskStreamSummary]:
        """Return one :class:`TaskStreamSummary` per non-empty task stream.

        Ordered most-recently-updated first, with a deterministic tie-break
        (``task_id``) so equal ``last_event_time`` values never produce a
        flaky/unstable list order. Empty streams are skipped.
        """
        ...


class EventLog(EventLogReader, EventLogWriter, Protocol):
    """Read + write combined.

    For the few callsites that genuinely do both — the Engine writes events and
    reads the stream for its trace id; ``RuntimeLLMClient`` writes the LLM trio
    live and reads the historical cursor on resume. Everywhere else, prefer the
    narrower :class:`EventLogReader` or :class:`EventLogWriter`.
    """


class EventLogFull(
    EventLogReader,
    EventLogWriter,
    EventLogSubscriber,
    EventLogTaskIndex,
    Protocol,
):
    """Read + write + subscribe + task-index — the full live-adapter shape.

    Reserved for callsites that genuinely need every capability at the same
    wiring seam: :func:`noeta.core.wiring.wire_default_observers`,
    :class:`noeta.core.observers.ChildLifecycleObserver`, and the live runtime
    bundle in :mod:`noeta.testing.profile`. Do not widen a narrow annotation to
    this one just because the concrete adapter satisfies it — the narrow
    Protocols are what keeps single-capability intent explicit.
    """
