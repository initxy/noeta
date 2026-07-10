"""In-memory adapters for the L0 storage Protocols.

Issue 06 hardened the Phase 0 baseline (issue 01) into a real
concurrency-protected backend. The typed boundary was then lifted to
``noeta.protocols.event_log / content_store / dispatcher``;
the classes here are adapters that satisfy those Protocols.

* :class:`InMemoryEventLog` implements ``EventLog`` (Reader + Writer)
  and ``EventLogSubscriber``. Three concurrency layers on
  :meth:`emit` — optimistic ``expected_seq``, lease validity via a
  wired ``LeaseRegistry``, and ``(lease_id, idempotency_key)`` dedup.
  Cross-stream system writes go through :meth:`system_emit` (no lease
  check, no idempotency); this replaces the legacy ``bypass_lease=True``
  flag the earlier ``emit`` carried.
* :class:`InMemoryContentStore` implements ``ContentStore``.
* :class:`InMemoryDispatcher` implements both ``Dispatcher`` (lease
  lifecycle) and ``LeaseRegistry`` (``is_lease_valid`` for EventLog
  backends) in one class.

The InMemory backend is the only Phase-0 implementation; SQL / Postgres
backends in later phases plug into the same Protocols.
"""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.dispatcher import Lease, LeaseRegistry
from noeta.protocols.errors import (
    ContentNotFound,
    InvalidLease,
    PayloadTooLarge,
    StaleSequence,
    WakeConsumeMismatch,
)
from noeta.protocols.event_log import (
    SNAPSHOT_BASELINE_EVENT_TYPES,
    Subscriber,
    TaskStreamSummary,
    Unsubscribe,
)
from noeta.protocols.events import EventEnvelope, EventOrigin
from noeta.protocols.values import EVENT_PAYLOAD_MAX_BYTES, ContentRef
from noeta.protocols.wake import TimerFired
from noeta.storage._reclaim import reclaim_hits_cap
from noeta.storage._wake_match import _matches


_DEFAULT_SCHEMA_VERSION = 1


def _default_id_factory() -> str:
    return f"evt-{uuid.uuid4().hex}"


# Adapter-local alias preserved so existing test imports
# (``from noeta.storage.memory import MAX_PAYLOAD_BYTES``) keep working
# after the canonical constant moved to :mod:`noeta.protocols.values`
# under its more precise name (issue 16). L0 callers should import
# ``EVENT_PAYLOAD_MAX_BYTES`` directly.
MAX_PAYLOAD_BYTES = EVENT_PAYLOAD_MAX_BYTES


# ---------------------------------------------------------------------------
# ContentStore
# ---------------------------------------------------------------------------


class InMemoryContentStore:
    """Content-addressed, immutable, dedup-by-hash blob store (in-memory).

    Phase 0 only.
    """

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def put(self, body: bytes, *, media_type: str) -> ContentRef:
        digest = hashlib.sha256(body).hexdigest()
        with self._lock:
            # Immutable: identical hash means identical body. We never
            # overwrite an existing entry.
            self._blobs.setdefault(digest, body)
        return ContentRef(hash=digest, size=len(body), media_type=media_type)

    def get(self, ref: ContentRef) -> bytes:
        try:
            return self._blobs[ref.hash]
        except KeyError as exc:
            raise ContentNotFound(ref.hash) from exc

    def __len__(self) -> int:
        return len(self._blobs)


# ---------------------------------------------------------------------------
# EventLog
# ---------------------------------------------------------------------------


def _enforce_payload_cap(envelope: EventEnvelope) -> None:
    body = to_canonical_bytes(envelope.payload)
    if len(body) > MAX_PAYLOAD_BYTES:
        raise PayloadTooLarge(
            f"task_id={envelope.task_id}, type={envelope.type}, "
            f"size={len(body)}, cap={MAX_PAYLOAD_BYTES} "
            "(large bodies must go through ContentStore)"
        )


@dataclass
class _StreamState:
    events: list[EventEnvelope] = field(default_factory=list)
    # idempotency cache: (lease_id, idempotency_key) -> seq
    idempotency: dict[tuple[str, str], int] = field(default_factory=dict)


class InMemoryEventLog:
    """Append-only per-task event stream with three-layer write protection.

    Three concurrency layers on :meth:`emit` (the business-path writer):

    1. **Optimistic ``expected_seq``** — caller asserts the next slot
       they intend to claim. Mismatch raises :class:`StaleSequence`.
    2. **Lease validity** — when ``lease_id`` is provided *and* a
       ``LeaseRegistry`` was injected at construction (or via
       :meth:`bind_lease_registry`), the registry must approve the
       (task_id, lease_id) pair. Stale or unknown leases raise
       :class:`InvalidLease`.
    3. **Idempotency** — same ``(lease_id, idempotency_key)`` twice
       returns the originally-assigned envelope silently; no duplicate
       event is appended.

    The :class:`LeaseRegistry` parameter is intentionally a Protocol
    (not a Dispatcher instance) so the EventLog never imports the
    Dispatcher type. Callers that don't need lease enforcement (most
    pure EventLog unit tests) leave it ``None`` and writes are accepted.

    Cross-stream system writes (e.g. a child-completion observer that
    writes ``SubtaskCompleted`` onto the *parent* stream while holding
    only the child's lease) use :meth:`system_emit`. That method skips
    all three concurrency layers — the caller takes responsibility for
    ordering and idempotency.
    """

    def __init__(
        self,
        *,
        lease_validator: LeaseRegistry | None = None,
        clock: Callable[[], float] | None = None,
        id_factory: Callable[[], str] | None = None,
        schema_version: int = _DEFAULT_SCHEMA_VERSION,
    ) -> None:
        self._streams: dict[str, _StreamState] = defaultdict(_StreamState)
        self._subscribers: list[Subscriber] = []
        self._lease_validator = lease_validator
        self._clock = clock or time.time
        self._id_factory = id_factory or _default_id_factory
        self._schema_version = schema_version
        self._lock = threading.Lock()

    # -- wiring ----------------------------------------------------------

    def bind_lease_registry(self, registry: LeaseRegistry) -> None:
        """Late-bind a :class:`LeaseRegistry` (e.g. once the Dispatcher
        exists).

        Useful when the EventLog and Dispatcher are constructed in
        either order. After binding, every :meth:`emit` with a non-
        ``None`` ``lease_id`` is subject to validation.
        """
        self._lease_validator = registry

    # -- writes ----------------------------------------------------------

    def emit(
        self,
        *,
        task_id: str,
        type: str,
        payload: Any,
        lease_id: str | None = None,
        trace_id: str | None = None,
        actor: str = "engine",
        causation_id: str | None = None,
        expected_seq: int | None = None,
        idempotency_key: str | None = None,
        origin: EventOrigin = "engine",
    ) -> EventEnvelope:
        """Append one business event. See class docstring for the three
        concurrency layers this enforces."""
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
        return self._append_impl(
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
        trace_id: str | None = None,
        causation_id: str | None = None,
    ) -> EventEnvelope:
        """Append one cross-stream system event.

        No lease validation, no ``expected_seq``, no idempotency
        dedup. Used by Observer-style writers (Phase 0:
        ``ChildLifecycleObserver`` writing ``SubtaskCompleted`` to the
        parent stream while holding only the child's lease).

        ``actor`` carries the system writer's identity; ``origin``
        names the Noeta role (``observer`` / ``engine`` / ``llm`` /
        ``tool`` / ``system``) and is what the suspend-window
        re-injection keys on, superseding the actor-string
        heuristic.
        """
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
        return self._append_impl(
            envelope,
            lease_id=None,
            expected_seq=None,
            idempotency_key=None,
            require_lease=False,
        )

    def _append_impl(
        self,
        envelope: EventEnvelope,
        *,
        lease_id: str | None,
        expected_seq: int | None,
        idempotency_key: str | None,
        require_lease: bool,
    ) -> EventEnvelope:
        with self._lock:
            stream = self._streams[envelope.task_id]

            # Layer 3: idempotency dedup (returns the cached seq without
            # writing a second event). Evaluate before layer 1 so a
            # retried write does not trip ``expected_seq``.
            if lease_id is not None and idempotency_key is not None:
                key = (lease_id, idempotency_key)
                if key in stream.idempotency:
                    existing_seq = stream.idempotency[key]
                    return stream.events[existing_seq]

            # Layer 0: 4-KB payload cap. Run before any state
            # mutation so an oversized write never advances the stream;
            # we measure the canonicalized JSON shape so the check matches
            # what a real wire backend (SQL/Postgres) would store.
            _enforce_payload_cap(envelope)

            next_seq = len(stream.events)

            # Layer 1: optimistic concurrency on the next slot.
            if expected_seq is not None and expected_seq != next_seq:
                raise StaleSequence(
                    f"task_id={envelope.task_id}, "
                    f"expected={expected_seq}, actual={next_seq}"
                )

            # Layer 2: lease validity.
            if (
                require_lease
                and lease_id is not None
                and self._lease_validator is not None
                and not self._lease_validator.is_lease_valid(envelope.task_id, lease_id)
            ):
                raise InvalidLease(f"task_id={envelope.task_id}, lease_id={lease_id}")

            stamped = envelope.with_seq(next_seq)
            stream.events.append(stamped)

            if lease_id is not None and idempotency_key is not None:
                stream.idempotency[(lease_id, idempotency_key)] = next_seq

        # Notify subscribers outside the lock; failures are silent so
        # an Observer crash never breaks the writer.
        for sub in list(self._subscribers):
            try:
                sub(stamped)
            except Exception:  # noqa: BLE001
                pass

        return stamped

    # -- reads -------------------------------------------------------------

    def read(
        self, task_id: str, *, after_seq: int | None = None
    ) -> list[EventEnvelope]:
        with self._lock:
            events = list(self._streams[task_id].events)
        if after_seq is None:
            return events
        return [e for e in events if e.seq > after_seq]

    def find_latest_snapshot(self, task_id: str) -> EventEnvelope | None:
        with self._lock:
            events = self._streams[task_id].events
            for envelope in reversed(events):
                # TaskRewound / StepAttemptAbandoned are snapshot-shaped fold
                # baselines (carry ``state_ref`` too), so a rewind / attempt
                # seal re-bases fold from the same accelerated lookup. Reverse
                # scan returns whichever baseline has the higher seq.
                if envelope.type in SNAPSHOT_BASELINE_EVENT_TYPES:
                    return envelope
        return None

    # -- task index (CW5a EventLogTaskIndex capability) --------------------

    def list_task_streams(self) -> list[TaskStreamSummary]:
        """Enumerate non-empty task streams, most-recent-update first.

        ``_streams`` is a ``defaultdict`` so a prior ``read()`` on an unknown
        task_id may have materialised an empty stream — those are skipped (a
        task with no events is not a session). Deterministic tie-break on
        ``task_id`` keeps the order stable when ``last_event_time`` ties.
        """
        with self._lock:
            summaries = [
                TaskStreamSummary(
                    task_id=task_id,
                    last_seq=stream.events[-1].seq,
                    last_event_time=stream.events[-1].occurred_at,
                )
                for task_id, stream in self._streams.items()
                if stream.events
            ]
        summaries.sort(key=lambda s: (-s.last_event_time, s.task_id))
        return summaries

    # -- maintenance -------------------------------------------------------

    def purge_task(self, task_id: str) -> bool:
        """Drop a task's whole stream. Mirror of
        :meth:`SqliteEventLog.purge_task` — a GC/maintenance affordance
        (NOT on the L0 Protocols) backing the agent product's "delete
        session". Returns ``True`` iff a non-empty stream was removed."""
        with self._lock:
            stream = self._streams.pop(task_id, None)
            return bool(stream and stream.events)

    # -- subscribe ---------------------------------------------------------

    def subscribe(self, callback: Subscriber) -> Unsubscribe:
        """Register a sync callback invoked after each successful append.

        Returns an unsubscribe function. Subscriber failures are swallowed
        (Observers must not affect the writer).
        """
        self._subscribers.append(callback)

        def _unsubscribe() -> None:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass

        return _unsubscribe


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


@dataclass
class _DispatcherTask:
    task_id: str
    status: str = "ready"  # ready | leased | suspended | terminal
    lease_id: str | None = None
    lease_expires_at: float | None = None
    heartbeat_count: int = 0
    fail_attempts: int = 0
    # Consecutive stale-lease reclaims with no observed progress in
    # between (kernel #3). Incremented by ``requeue_stale``; reset by any
    # progress signal — a successful heartbeat, a clean release, a
    # controlled fail-requeue, or a force-enqueue. At ``reclaim_max`` the
    # task drops to terminal (``stale_reclaim_exceeded``) instead of
    # looping lease → expire → requeue forever.
    reclaim_count: int = 0
    wake_on: Any = None
    suspend_reason: str | None = None
    pending_wake_events: list[Any] = field(default_factory=list)
    # Matched wake event waiting to be handed out on the next lease.
    # Set by ``wake()`` when the event matched the task's ``wake_on``,
    # and by ``_release_locked(suspended)`` when a buffered pending wake
    # matches the freshly-stored ``wake_on``. H2:
    # it **survives the lease** (handed to the worker on
    # ``lease()`` but NOT cleared there — see ``lease`` D1) and is cleared
    # only by a consuming ``release(consumed_wake_event=…)`` (D2). A crash
    # before that consuming release re-delivers it via ``requeue_stale``.
    matched_wake_event: Any = None
    # Targeted-lease-only guard (Dispatcher.enqueue ``reserved``). A freshly
    # enqueued subtask child sets this so an untargeted FIFO poll skips it
    # (only its delegation drain / background executor may targeted-lease it,
    # since only that path seeds its goal). A ONE-SHOT claim guard: the first
    # successful ``lease`` clears it, so a later suspend/resume re-enqueue is an
    # ordinary untargeted-leaseable task.
    reserved: bool = False


class InMemoryDispatcher:
    """In-memory adapter for ``Dispatcher`` + ``LeaseRegistry``.

    Eight lifecycle methods (``enqueue / lease / heartbeat / release /
    fail / wake / requeue_stale / fire_due_timers``) plus the
    ``is_lease_valid`` registry surface used by EventLog backends. The four debug helpers
    (``task_status / wake_on / suspend_reason``) are NOT on either
    Protocol — they are introspection points used only in tests.

    Knobs:

    * ``heartbeat_max`` (default 360, matching the "default
      ~1 hour at 10s heartbeat" rule of thumb): after the cap, any
      further heartbeat raises ``InvalidLease`` and force-releases the
      task to ``suspended`` with reason ``lease_quota_exceeded``.
    * ``max_fail_attempts`` (default 3): retryable failures past this
      number drop the task into ``terminal``.
    * ``reclaim_max`` (default 3): consecutive no-progress stale-lease
      reclaims past this number drop the task into ``terminal`` with
      reason ``stale_reclaim_exceeded`` (kernel #3 — a poison task that
      silently kills its worker must not requeue forever). Reset on any
      progress signal (heartbeat / release / fail-requeue / enqueue).
    """

    def __init__(
        self,
        *,
        now: Callable[[], float] | None = None,
        heartbeat_max: int = 360,
        max_fail_attempts: int = 3,
        reclaim_max: int = 3,
    ) -> None:
        self._now = now or time.monotonic
        self._tasks: dict[str, _DispatcherTask] = {}
        self._ready: list[str] = []
        self._heartbeat_max = heartbeat_max
        self._max_fail_attempts = max_fail_attempts
        self._reclaim_max = reclaim_max
        self._lock = threading.Lock()

    # -- LeaseRegistry ---------------------------------------------------

    def is_lease_valid(self, task_id: str, lease_id: str) -> bool:
        """Return True iff ``lease_id`` is the active lease for
        ``task_id`` and the lease has not expired.

        This is the EventLog's hook into the dispatcher. It is read-only
        and never mutates state.
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.lease_id != lease_id:
                return False
            if task.status != "leased":
                return False
            if task.lease_expires_at is None:
                return False
            return task.lease_expires_at > self._now()

    # -- introspection (test-only; not on Protocol) ----------------------

    def task_status(self, task_id: str) -> str | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return None if task is None else task.status

    def has_active_lease(self, task_id: str) -> bool:
        """True iff a worker currently holds a *live* (non-expired) lease on
        ``task_id`` — the expiry-aware counterpart of
        ``task_status() == 'leased'``. A lease whose TTL lapsed after the
        worker died reads as not-running, so a zombie lease never wedges the
        task as undeletable. Mirrors the sqlite dispatcher."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status != "leased":
                return False
            if task.lease_expires_at is None:
                return False
            return task.lease_expires_at > self._now()

    def wake_on(self, task_id: str) -> Any:
        with self._lock:
            task = self._tasks.get(task_id)
            return None if task is None else task.wake_on

    def suspend_reason(self, task_id: str) -> str | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return None if task is None else task.suspend_reason

    def restore_task(
        self,
        task_id: str,
        *,
        status: str,
        wake_on: Any = None,
        suspend_reason: str | None = None,
    ) -> None:
        """Adapter-local lifecycle repair used by live conversation rewind.

        ``TaskRewound`` re-bases the EventLog fold to an older snapshot-shaped
        state. The dispatcher is only the lease/wake accelerator, so the live
        rewind command must re-align this row with the folded baseline without
        fabricating a lease release. This helper is deliberately not on the L0
        Dispatcher Protocol; normal task progress still goes through
        ``enqueue`` / ``lease`` / ``release`` / ``wake``.
        """
        if status not in {"ready", "suspended", "terminal"}:
            raise ValueError(f"invalid restore status: {status}")
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                task = _DispatcherTask(task_id=task_id)
                self._tasks[task_id] = task
            self._ready = [tid for tid in self._ready if tid != task_id]
            task.lease_id = None
            task.lease_expires_at = None
            task.heartbeat_count = 0
            task.reclaim_count = 0
            task.matched_wake_event = None
            task.pending_wake_events.clear()
            task.wake_on = None
            task.suspend_reason = suspend_reason
            if status == "ready":
                task.status = "ready"
                task.suspend_reason = None
                self._ready.append(task_id)
                return
            if status == "terminal":
                task.status = "terminal"
                return

            task.status = "suspended"
            task.wake_on = wake_on
            # No buffered-wake redelivery here: ``pending_wake_events`` was
            # cleared above, so a drain loop would iterate an empty list and
            # never re-ready the task (mirrors the SQLite adapter).

    # -- Dispatcher lifecycle --------------------------------------------

    def enqueue(self, task_id: str, *, reserved: bool = False) -> None:
        """Mark ``task_id`` as ready-to-lease.

        Idempotent: enqueueing a task that is already ``ready`` is a
        no-op (FIFO order — and its existing ``reserved`` flag — are
        preserved). For any non-ready state
        (``leased``/``suspended``/``terminal``) the lifecycle fields
        of the previous state are cleared in lockstep with the
        ``status='ready'`` transition — including
        ``matched_wake_event``. Letting a stale matched wake survive a
        force-enqueue would let the next ``lease()`` hand out a
        wake_event the caller did not request (B1 invariant: matched
        wake_event is owned by the single wake → lease handoff that
        produced it).

        ``reserved`` (see :meth:`Dispatcher.enqueue`) marks the task
        targeted-lease-only until its first lease claims it.
        """
        with self._lock:
            if task_id not in self._tasks:
                self._tasks[task_id] = _DispatcherTask(
                    task_id=task_id, reserved=reserved
                )
            else:
                task = self._tasks[task_id]
                if task.status != "ready":
                    task.status = "ready"
                    task.lease_id = None
                    task.lease_expires_at = None
                    task.heartbeat_count = 0
                    task.reclaim_count = 0
                    task.wake_on = None
                    task.suspend_reason = None
                    task.matched_wake_event = None
                    task.reserved = reserved
            if task_id not in self._ready:
                self._ready.append(task_id)

    def lease(
        self,
        *,
        worker_id: str,
        lease_seconds: float = 30.0,
        task_id: str | None = None,
    ) -> Lease | None:
        """Lease a ready task.

        ``task_id=None``: pick any ready task in FIFO order (insertion
        order of ``self._ready``).
        ``task_id=<id>``: targeted — only succeed if that specific task
        is currently ready. Returns ``None`` for not-found / not-ready /
        already-leased / suspended / terminal (no exception — diagnosis
        is the caller's job; see ADR ``Dispatcher.lease`` docstring).

        On success, any ``matched_wake_event`` queued by a prior
        :meth:`wake` (or by the pending-wake-drain in
        :meth:`_release_locked`) is handed out on the returned
        :class:`Lease`. H2: lease does **not** clear it — the
        matched wake survives the lease ("matched-in-flight") so a crash
        before the durable ``TaskWoken`` does not lose it; it is cleared
        only by a consuming ``release(consumed_wake_event=...)`` (D2) and
        otherwise re-delivered after ``requeue_stale`` (D3) — at-least-once
        delivery + idempotent consumption = exactly-once.
        """
        with self._lock:
            target_idx: int | None = None
            target_task: _DispatcherTask | None = None
            if task_id is None:
                for idx, ready_id in enumerate(self._ready):
                    candidate = self._tasks[ready_id]
                    # ``reserved`` tasks are targeted-lease-only (a fresh
                    # subtask child its drain/executor must claim first) —
                    # an untargeted FIFO poll skips them.
                    if candidate.status == "ready" and not candidate.reserved:
                        target_idx = idx
                        target_task = candidate
                        break
            else:
                maybe_task = self._tasks.get(task_id)
                if (
                    maybe_task is not None
                    and maybe_task.status == "ready"
                    and task_id in self._ready
                ):
                    target_idx = self._ready.index(task_id)
                    target_task = maybe_task
            if target_task is None or target_idx is None:
                return None
            self._ready.pop(target_idx)
            leased_id = target_task.task_id
            lease_id = f"lease-{uuid.uuid4().hex}"
            expires_at = self._now() + lease_seconds
            target_task.status = "leased"
            target_task.lease_id = lease_id
            target_task.lease_expires_at = expires_at
            target_task.heartbeat_count = 0
            target_task.suspend_reason = None
            # One-shot claim: the child has now been claimed by its owning
            # driver (this can only be a targeted lease — an untargeted poll
            # skips reserved tasks), so clear the guard. A later suspend/resume
            # re-enqueue is then an ordinary untargeted-leaseable task.
            target_task.reserved = False
            # H2 (D1): do NOT clear matched_wake_event here — survives the
            # lease; cleared only by a consuming release (D2).
            wake_event = target_task.matched_wake_event
            return Lease(
                lease_id=lease_id,
                task_id=leased_id,
                expires_at=expires_at,
                wake_event=wake_event,
            )

    def heartbeat(self, lease_id: str, *, lease_seconds: float = 30.0) -> float:
        """Extend a lease window. Enforces ``heartbeat_max`` cap.

        After the cap is reached, the task is force-released to
        ``suspended`` with reason ``lease_quota_exceeded`` and the
        caller's heartbeat raises ``InvalidLease`` (the lease is gone).
        """
        with self._lock:
            task = self._find_task_by_lease(lease_id)
            if task is None or task.status != "leased":
                raise InvalidLease(lease_id)
            if task.heartbeat_count >= self._heartbeat_max:
                # Cap exceeded — force a suspend release inline.
                self._release_locked(
                    task,
                    next_state="suspended",
                    wake_on=task.wake_on,
                    suspend_reason="lease_quota_exceeded",
                )
                raise InvalidLease(lease_id)
            task.heartbeat_count += 1
            # A successful heartbeat is the leased-task progress signal:
            # the worker is alive, so prior stale reclaims are history.
            task.reclaim_count = 0
            task.lease_expires_at = self._now() + lease_seconds
            return task.lease_expires_at

    def release(
        self,
        lease_id: str,
        *,
        next_state: str,
        wake_on: Any = None,
        suspend_reason: str | None = None,
        consumed_wake_event: Any = None,
    ) -> None:
        if next_state not in {"suspended", "terminal"}:
            raise ValueError(f"invalid next_state: {next_state}")
        with self._lock:
            task = self._find_task_by_lease(lease_id)
            if task is None:
                raise InvalidLease(lease_id)
            self._release_locked(
                task,
                next_state=next_state,
                wake_on=wake_on,
                suspend_reason=suspend_reason,
                consumed_wake_event=consumed_wake_event,
            )

    def fail(
        self,
        lease_id: str,
        *,
        retryable: bool = False,
        reason: str | None = None,
    ) -> None:
        """Release the lease on failure. Retryable failures bounded by
        ``max_fail_attempts``; past that, the task drops to terminal.
        """
        with self._lock:
            task = self._find_task_by_lease(lease_id)
            if task is None:
                raise InvalidLease(lease_id)
            task.lease_id = None
            task.lease_expires_at = None
            task.heartbeat_count = 0
            # A controlled fail is a progress signal for the RECLAIM
            # counter (the worker reported back; bounding is
            # ``fail_attempts``' own job).
            task.reclaim_count = 0
            if retryable:
                task.fail_attempts += 1
                if task.fail_attempts >= self._max_fail_attempts:
                    task.status = "terminal"
                    task.suspend_reason = reason or "max_attempts_exceeded"
                    # Kernel #8: terminal is forever — buffered wakes
                    # that never matched can never drain; GC them.
                    task.pending_wake_events.clear()
                else:
                    task.status = "ready"
                    self._ready.append(task.task_id)
            else:
                task.status = "terminal"
                task.suspend_reason = reason
                task.pending_wake_events.clear()

    def release_yield(self, lease_id: str) -> None:
        """Voluntary yield of a seeded lease back to the ready queue.

        Transitions leased→ready WITHOUT incrementing fail_attempts —
        used by transports that seed a task durably under a targeted
        lease and then hand it off to a resident worker pool. A matched
        wake (if any) is preserved.
        """
        with self._lock:
            task = self._find_task_by_lease(lease_id)
            if task is None:
                raise InvalidLease(lease_id)
            task.lease_id = None
            task.lease_expires_at = None
            task.heartbeat_count = 0
            task.reclaim_count = 0
            task.wake_on = None
            task.suspend_reason = None
            task.status = "ready"
            self._ready.append(task.task_id)

    def wake(self, task_id: str, wake_event: Any) -> bool:
        """Deliver a wake event. Returns True iff the task is requeued
        (either because it was suspended and the event matched, or
        because the event matches a wake_on that was set even before
        the suspend handshake — see ``_release_locked`` for the latter).

        On a successful match, the matched ``wake_event`` is recorded
        on ``task.matched_wake_event``; it is handed to the worker on the
        next ``lease()`` but **survives** it (H2),
        cleared only by a consuming
        ``release(consumed_wake_event=…)`` — a crash before that re-delivers
        it via ``requeue_stale``.
        """
        with self._lock:
            if task_id not in self._tasks:
                self._tasks[task_id] = _DispatcherTask(task_id=task_id)
            task = self._tasks[task_id]
            if task.status == "suspended" and _matches(task.wake_on, wake_event):
                task.matched_wake_event = wake_event
                task.status = "ready"
                task.wake_on = None
                task.suspend_reason = None
                self._ready.append(task_id)
                return True
            task.pending_wake_events.append(wake_event)
            return False

    def requeue_stale(self) -> list[str]:
        """Move any leased tasks whose lease expired back to ready.

        Returns the list of task_ids that were requeued. The previously
        held lease_id is invalidated; the original worker's writes will
        fail :class:`InvalidLease` on the EventLog.

        Kernel #3: each reclaim increments the task's ``reclaim_count``;
        at ``reclaim_max`` consecutive no-progress reclaims the task
        drops to ``terminal`` (``stale_reclaim_exceeded``) instead of
        requeueing — the poison-task analogue of ``max_fail_attempts``.
        Terminal-by-cap tasks are NOT in the returned list.
        """
        now = self._now()
        requeued: list[str] = []
        with self._lock:
            for task in self._tasks.values():
                if (
                    task.status == "leased"
                    and task.lease_expires_at is not None
                    and task.lease_expires_at <= now
                ):
                    task.lease_id = None
                    task.lease_expires_at = None
                    task.heartbeat_count = 0
                    # Kernel #3: bound the silent lease-expiry loop. The
                    # counter only resets on a progress signal, so a
                    # poison task that keeps killing its worker without
                    # a heartbeat/fail/release lands terminal here.
                    task.reclaim_count += 1
                    if reclaim_hits_cap(task.reclaim_count, self._reclaim_max):
                        task.status = "terminal"
                        task.suspend_reason = "stale_reclaim_exceeded"
                        task.wake_on = None
                        task.pending_wake_events.clear()
                        continue
                    task.status = "ready"
                    self._ready.append(task.task_id)
                    requeued.append(task.task_id)
        return requeued

    def fire_due_timers(self, *, now: float) -> list[str]:
        """Wake every suspended task whose ``TimerFired`` deadline passed.

        ``now`` is a wall-clock epoch timestamp supplied by the caller —
        deliberately NOT ``self._now`` (which defaults to
        ``time.monotonic``): ``fire_at`` was computed with the Engine's
        wall clock and the two bases must match. The delivered wake is
        the **recorded deadline** (byte-stable across H2 re-delivery),
        not ``TimerFired(fire_at=now)``; matching is the same inclusive
        ``>=`` threshold :func:`matches_wake` pins.
        """
        fired: list[str] = []
        with self._lock:
            for task in self._tasks.values():
                if (
                    task.status == "suspended"
                    and isinstance(task.wake_on, TimerFired)
                    and task.wake_on.fire_at <= now
                ):
                    task.matched_wake_event = task.wake_on
                    task.status = "ready"
                    task.wake_on = None
                    task.suspend_reason = None
                    self._ready.append(task.task_id)
                    fired.append(task.task_id)
        return fired

    # -- maintenance -----------------------------------------------------

    def purge_task(self, task_id: str) -> None:
        """Drop all dispatcher state for ``task_id`` (task row + ready
        queue entry). Mirror of :meth:`SqliteDispatcher.purge_task` — a
        maintenance affordance, not on the Dispatcher Protocol. Idempotent."""
        with self._lock:
            self._tasks.pop(task_id, None)
            self._ready = [t for t in self._ready if t != task_id]

    # -- internal helpers ------------------------------------------------

    def _find_task_by_lease(self, lease_id: str) -> _DispatcherTask | None:
        for task in self._tasks.values():
            if task.lease_id == lease_id:
                return task
        return None

    def _release_locked(
        self,
        task: _DispatcherTask,
        *,
        next_state: str,
        wake_on: Any,
        suspend_reason: str | None,
        consumed_wake_event: Any = None,
    ) -> None:
        # H2 step 1: validate BEFORE any mutation so a
        # mismatch commits nothing (rollback parity with sqlite). Clear the
        # OLD matched iff a consuming release presents the exact wake.
        clear_matched = False
        if consumed_wake_event is not None:
            if (
                task.matched_wake_event is None
                or task.matched_wake_event != consumed_wake_event
            ):
                raise WakeConsumeMismatch(
                    f"release(consumed_wake_event=...) on task "
                    f"{task.task_id!r}: presented wake does not equal the "
                    "stored matched_wake_event"
                )
            clear_matched = True

        task.lease_id = None
        task.lease_expires_at = None
        task.heartbeat_count = 0
        # A clean release is a progress signal — the reclaim counter
        # tracks only consecutive silent lease expiries (kernel #3).
        task.reclaim_count = 0
        task.status = next_state
        if clear_matched:
            task.matched_wake_event = None  # OLD matched consumed (D2)
        if next_state == "suspended":
            task.wake_on = wake_on
            task.suspend_reason = suspend_reason
            # H2 (D5): an un-consumed matched is PRESERVED. A matched wake
            # means a delivery is pending, which supersedes "suspended
            # waiting" — the task goes back to **ready** so the next lease
            # re-delivers it (never stuck-suspended, never overwritten).
            if task.matched_wake_event is not None:
                task.status = "ready"
                task.wake_on = None
                task.suspend_reason = None
                self._ready.append(task.task_id)
            else:
                # No matched: drain a single matching pending wake into a
                # NEW matched (D4 case 4 — old matched was cleared above).
                for evt in list(task.pending_wake_events):
                    if _matches(task.wake_on, evt):
                        task.pending_wake_events.remove(evt)
                        task.matched_wake_event = evt
                        task.status = "ready"
                        task.wake_on = None
                        task.suspend_reason = None
                        self._ready.append(task.task_id)
                        break
        else:
            task.wake_on = None
            task.suspend_reason = suspend_reason
            # Kernel #8: terminal is forever — buffered wake events that
            # never matched can never drain now; GC them. The matched
            # wake (H2 exactly-once handoff) is deliberately untouched.
            task.pending_wake_events.clear()
