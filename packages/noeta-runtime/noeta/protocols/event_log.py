"""EventLog Protocol — L0 typed boundary for the append-only task stream.

The EventLog is Noeta's source of truth for "decisions and
causality"; its typed boundary sits at L0, split by
capability into three Protocols:

* :class:`EventLogReader`     — read / find_latest_snapshot; used by fold /
                                 the LLM cursor
* :class:`EventLogWriter`     — emit (business write, lease-checked) +
                                 system_emit (cross-stream system write, no
                                 lease check; replaces the old
                                 ``bypass_lease=True`` flag)
* :class:`EventLogSubscriber` — subscribe / unsubscribe; used by the Observer
                                 framework

Splitting by capability lets a callsite's type hint state its intent directly
("I only read / only write / only subscribe"); import-linter's forbidden
contract can also lock down reverse coupling precisely. InMemory / Shadow /
Phase 2 Sqlite&Postgres are adapters — one concrete class may implement one or
more Protocols (InMemory implements all three, Shadow only Reader+Writer).
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
#: (acceleration), ``TaskRewound`` (conversation rewind) and
#: ``StepAttemptAbandoned`` (crash-recovery seal). This is the single
#: source of truth for the :meth:`EventLogReader.find_latest_snapshot`
#: contract; every adapter's lookup filters on exactly this set. The SQL
#: adapters' partial index (``ix_events_snapshot``) must ALSO list exactly
#: these types in its ``WHERE`` predicate — a partial index is only chosen
#: when its predicate matches the live query — so growing this tuple
#: requires a new migration re-widening the index (see
#: ``storage/sqlite/migrations.py`` migration 8 /
#: ``storage/postgres/migrations.py`` migration 2), pinned by
#: ``tests/test_fix_storage.py``.
SNAPSHOT_BASELINE_EVENT_TYPES: tuple[str, ...] = (
    "TaskSnapshot",
    "TaskRewound",
    "StepAttemptAbandoned",
)


Subscriber = Callable[[EventEnvelope], None]
Unsubscribe = Callable[[], None]


class StopHandle:
    """Idempotent handle wrapping an :data:`Unsubscribe` callable.

    Callers that subscribe to an :class:`EventLogSubscriber` typically
    need exactly two pieces of bookkeeping after the
    ``subscribe(callback)`` call: a reference to the returned
    :data:`Unsubscribe` callable and a stopped flag so subsequent
    teardown is idempotent. ``StopHandle`` collapses both into one
    object — callers' ``stop()`` becomes a one-line delegate
    (``self._handle.stop()``).

    Thread safety: :meth:`stop` is exactly-once even under concurrent
    calls. The check-and-flag step runs under an internal
    :class:`threading.Lock`; the underlying :data:`Unsubscribe` is
    called outside the lock so it cannot block other ``stop()`` /
    ``stopped`` readers. The first thread to enter the critical
    section wins and runs the unsubscribe; every subsequent call
    short-circuits.

    No subscriber base class / mixin / Protocol is introduced — this
    is a lifecycle utility paired with the existing ``Unsubscribe``
    shape, nothing more.
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
        """Call the underlying ``Unsubscribe`` exactly once.

        Safe under concurrent calls — the first thread runs the
        unsubscribe, the rest short-circuit. The unsubscribe call
        itself runs outside the internal lock so a slow teardown
        cannot starve other ``stop()`` callers.
        """
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
    :class:`StopHandle`.

    Returns a handle whose ``stop()`` is safe to call multiple times
    from any thread — useful for any application shutdown / repeated
    teardown path that may try to stop the same subscription more
    than once.
    """
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

        Snapshot is a first-class EventLog event; fold
        acceleration depends on this lookup being fast enough to call
        every Engine step. In-memory backends MAY reverse-scan; SQL
        backends SHOULD index the snapshot type.

        Rewind and the crash-recovery seal generalise this to "the latest
        fold baseline": every type in
        :data:`SNAPSHOT_BASELINE_EVENT_TYPES` carries a ``state_ref``
        rehydration body and re-bases fold through the SAME accelerated
        lookup. Implementations return whichever member of that set has
        the highest seq (a stream with neither marker keeps the original
        behaviour).
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
      (Phase 0: ``ChildLifecycleObserver`` appending ``SubtaskCompleted``
      to the parent stream). No lease check, no idempotency, no
      ``expected_seq``. Callers MUST set ``actor`` so the recorded
      envelope is attributable, and ``origin`` so its live readers — the
      ``AuditObserver`` (which writes ``AuditRecord.origin``) and the
      http_json events API — can classify the writer.
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

        No lease check, no idempotency dedup, no ``expected_seq``.
        Used by Observer-style writers.
        ``actor`` carries the system writer's identity
        (``child_observer``, ``llm``, etc.); ``origin`` names the Noeta
        role (engine / llm / observer / tool / system) and is what the live
        readers (``AuditObserver`` + the http_json events API) key on.
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
      ``BEGIN IMMEDIATE`` transaction (issues 15/16/17). Concurrent
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
    """One row of the task/session catalog (CW5a).

    The minimal, transport-neutral enumeration shape the sessions/tasks list
    needs — ``task_id`` plus the stream-tail bookmarks used for ordering. Pure
    stdlib types (no storage-adapter types leak through this seam); the richer
    lifecycle fields (status / closed / …) are derived by ``noeta.read_models``
    via :func:`noeta.core.fold.fold`, not carried here.
    """

    task_id: str
    last_seq: int
    last_event_time: float


class EventLogTaskIndex(Protocol):
    """Enumerate the task streams a real storage-backed event log holds (CW5a).

    A **separate capability** from :class:`EventLogReader` on purpose: reading a
    *single* task's stream (fold / the LLM cursor) must NOT imply the
    ability to enumerate *every* task. Only genuine storage-backed logs (the
    InMemory / Sqlite adapters) implement this; the narrow per-task readers
    deliberately do not.
    :func:`noeta.read_models` consumes this to build the sessions list without
    reaching into adapter internals.
    """

    def list_task_streams(self) -> list[TaskStreamSummary]:
        """Return one :class:`TaskStreamSummary` per non-empty task stream.

        Ordered most-recently-updated first, with a deterministic tie-break
        (``task_id``) so equal ``last_event_time`` values never produce a
        flaky/unstable list order. Empty streams are skipped.
        """
        ...


# Combined Protocol for callsites that genuinely need read + write
# (Engine, RuntimeLLMClient). Two Protocols intersected
# by inheritance — concrete implementations satisfy both naturally.
class EventLog(EventLogReader, EventLogWriter, Protocol):
    """Read + write combined.

    Engine uses this because it both writes new events and reads the
    stream (for ``_latest_trace_id``). RuntimeLLMClient uses this
    because Normal writes the LLM trio and Resume reads the
    historical cursor. Most callsites should prefer the narrower
    :class:`EventLogReader` or :class:`EventLogWriter`.
    """


class EventLogFull(
    EventLogReader,
    EventLogWriter,
    EventLogSubscriber,
    EventLogTaskIndex,
    Protocol,
):
    """Read + write + subscribe + task-index — full live-adapter shape.

    Issue A (post-Phase-1 cleanup). Reserved for callsites that genuinely
    need every capability at the same wiring seam — concretely
    :func:`noeta.core.wiring.wire_default_observers`,
    :class:`noeta.core.observers.ChildLifecycleObserver`, and the live
    runtime bundle in :mod:`noeta.testing.profile`. The narrow
    :class:`EventLogReader` / :class:`EventLogWriter` /
    :class:`EventLogSubscriber` protocols are the right type at every
    other callsite (single-capability intent stays explicit there);
    don't widen them to :class:`EventLogFull` just because the concrete
    adapter satisfies it.
    """
