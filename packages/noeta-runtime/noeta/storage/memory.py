"""The in-memory reference backend for the L0 storage Protocols.

These adapters are the executable definition of the Protocols' semantics, so
the rules a durable backend must reproduce — the write-protection layers on
:meth:`InMemoryEventLog.emit`, the lease/wake lifecycle, the payload cap — are
spelled out here rather than in prose elsewhere. The durable backends plug into
the same Protocols; :func:`build_stack` is the uniform factory shape every
backend ships.
"""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.content_store import ContentStore
from noeta.protocols.dispatcher import Dispatcher, Lease, LeaseRegistry
from noeta.protocols.errors import (
    ContentNotFound,
    InvalidLease,
    StaleSequence,
    WakeConsumeMismatch,
)
from noeta.protocols.event_log import (
    SNAPSHOT_BASELINE_EVENT_TYPES,
    EventLogFull,
    Subscriber,
    TaskStreamSummary,
    Unsubscribe,
)
from noeta.protocols.events import EventEnvelope, EventOrigin
from noeta.protocols.values import EVENT_PAYLOAD_MAX_BYTES, ContentRef
from noeta.protocols.wake import TimerFired
from noeta.storage.spi import enforce_payload_cap, reclaim_hits_cap, wake_matches


_DEFAULT_SCHEMA_VERSION = 1


def _default_id_factory() -> str:
    return f"evt-{uuid.uuid4().hex}"


# Adapter-local alias the storage contract suite imports; L0 callers should
# reach for ``EVENT_PAYLOAD_MAX_BYTES`` directly.
MAX_PAYLOAD_BYTES = EVENT_PAYLOAD_MAX_BYTES


# ---------------------------------------------------------------------------
# ContentStore
# ---------------------------------------------------------------------------


class InMemoryContentStore:
    """Content-addressed, immutable, dedup-by-hash blob store (in-memory)."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def put(self, body: bytes, *, media_type: str) -> ContentRef:
        digest = hashlib.sha256(body).hexdigest()
        with self._lock:
            # Content-addressed and immutable: an identical hash is an
            # identical body, so an existing entry is never overwritten.
            self._blobs.setdefault(digest, body)
        return ContentRef(hash=digest, size=len(body), media_type=media_type)

    def get(self, ref: ContentRef) -> bytes:
        try:
            return self._blobs[ref.hash]
        except KeyError as exc:
            raise ContentNotFound(ref.hash) from exc

    def get_many(self, refs: Iterable[ContentRef]) -> dict[str, bytes]:
        # There is no round-trip to save here; the batch read exists so this
        # backend stays a drop-in for the durable ones. Missing hashes are
        # omitted rather than raising, which is the Protocol's partial-result
        # contract every backend owes its callers.
        blobs = self._blobs
        out: dict[str, bytes] = {}
        for ref in refs:
            body = blobs.get(ref.hash)
            if body is not None:
                out[ref.hash] = body
        return out

    def __len__(self) -> int:
        return len(self._blobs)


# ---------------------------------------------------------------------------
# EventLog
# ---------------------------------------------------------------------------


def _enforce_payload_cap(envelope: EventEnvelope) -> None:
    # The cap decision belongs to the shared backend rule; only the canonical
    # bytes are computed here, because this adapter never serialises the
    # payload otherwise and the cap must measure what a wire backend stores.
    enforce_payload_cap(
        envelope.task_id, envelope.type, to_canonical_bytes(envelope.payload)
    )


@dataclass
class _StreamState:
    events: list[EventEnvelope] = field(default_factory=list)
    idempotency: dict[tuple[str, str], int] = field(default_factory=dict)


class InMemoryEventLog:
    """Append-only per-task event stream with three-layer write protection.

    :meth:`emit` is the business path: ``(lease_id, idempotency_key)`` dedup,
    optimistic ``expected_seq``, and lease validity, in that order. Dedup runs
    first so a retried write returns its original envelope instead of tripping
    ``expected_seq``.

    ``lease_validator`` is typed as the :class:`LeaseRegistry` Protocol rather
    than a Dispatcher, so the EventLog never imports the Dispatcher type;
    leaving it ``None`` accepts every write unchecked.

    Cross-stream system writes — an Observer appending ``SubtaskCompleted`` to
    the *parent* stream while holding only the child's lease — go through
    :meth:`system_emit`, which skips all three layers and puts ordering and
    idempotency on the caller.
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
        """Late-bind the registry so the two halves of a stack can be built in
        either order."""
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
        """Append one cross-stream system event, unchecked.

        Both ``actor`` and ``origin`` are required because they are not the
        same axis: ``actor`` is the writer's identity, ``origin`` its Noeta
        role (``observer`` / ``engine`` / ``llm`` / ``tool`` / ``system``), and
        readers key on the role, not on the identity string.
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

            # Dedup first: a retried write must return its original envelope
            # rather than trip the ``expected_seq`` assertion below.
            if lease_id is not None and idempotency_key is not None:
                key = (lease_id, idempotency_key)
                if key in stream.idempotency:
                    existing_seq = stream.idempotency[key]
                    return stream.events[existing_seq]

            # Before any state mutation, so an oversized write never advances
            # the stream.
            _enforce_payload_cap(envelope)

            next_seq = len(stream.events)

            if expected_seq is not None and expected_seq != next_seq:
                raise StaleSequence(
                    f"task_id={envelope.task_id}, "
                    f"expected={expected_seq}, actual={next_seq}"
                )

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

        # Outside the lock, and failures are swallowed: an Observer crash must
        # never break the writer that produced the event.
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
                # Several event types carry ``state_ref`` and so are equally
                # valid fold baselines; the reverse scan is what makes the
                # highest-seq baseline win regardless of which type it is.
                if envelope.type in SNAPSHOT_BASELINE_EVENT_TYPES:
                    return envelope
        return None

    # -- task index --------------------------------------------------------

    def list_task_streams(self) -> list[TaskStreamSummary]:
        """Enumerate non-empty task streams, most-recent-update first.

        ``_streams`` is a ``defaultdict``, so a prior ``read()`` of an unknown
        task_id may have materialised an empty stream; those are skipped,
        because a task with no events is not a conversation. The ``task_id``
        tie-break keeps the order stable when timestamps collide.
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
        """Drop a task's whole stream; ``True`` iff a non-empty one was removed.

        A maintenance affordance every backend mirrors, deliberately NOT on the
        L0 Protocols — deletion is a host decision, not part of the append-only
        contract the Engine writes against.
        """
        with self._lock:
            stream = self._streams.pop(task_id, None)
            return bool(stream and stream.events)

    # -- subscribe ---------------------------------------------------------

    def subscribe(self, callback: Subscriber) -> Unsubscribe:
        """Register a sync callback invoked after each successful append."""
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
    # Consecutive stale-lease reclaims with no observed progress in between.
    # Every progress signal — heartbeat, clean release, controlled
    # fail-requeue, force-enqueue — resets it, so only a task that keeps
    # killing its worker silently can reach ``reclaim_max``.
    reclaim_count: int = 0
    wake_on: Any = None
    suspend_reason: str | None = None
    pending_wake_events: list[Any] = field(default_factory=list)
    # Matched wake event waiting to be handed out on the next lease. It
    # SURVIVES that lease: ``lease()`` hands it over but does not clear it, and
    # only a consuming ``release(consumed_wake_event=…)`` does. A crash before
    # that release therefore re-delivers it through ``requeue_stale``, which is
    # what turns at-least-once delivery plus idempotent consumption into
    # exactly-once.
    matched_wake_event: Any = None
    # Targeted-lease-only guard. A freshly enqueued subtask child sets it so an
    # untargeted FIFO poll skips it: only the driver that seeds its goal may
    # claim it. One-shot — the first successful lease clears it, so a later
    # suspend/resume re-enqueue is an ordinary leaseable task.
    reserved: bool = False


class InMemoryDispatcher:
    """In-memory adapter for ``Dispatcher`` + ``LeaseRegistry``.

    One class serves both Protocols because ``is_lease_valid`` has to answer
    from the very state the lease lifecycle mutates. The introspection helpers
    (``task_status`` / ``has_active_lease`` / ``wake_on`` / ``suspend_reason``)
    are on neither Protocol.

    Three caps keep a task from cycling forever, and all three end in a state a
    human has to look at rather than a silent retry:

    * ``heartbeat_max`` — a further heartbeat raises ``InvalidLease`` and
      force-releases the task to ``suspended`` (``lease_quota_exceeded``). The
      default bounds a lease to roughly an hour at a 10s heartbeat.
    * ``max_fail_attempts`` — retryable failures past this drop to ``terminal``.
    * ``reclaim_max`` — consecutive no-progress stale-lease reclaims past this
      drop to ``terminal`` (``stale_reclaim_exceeded``), so a poison task that
      silently kills every worker leasing it cannot requeue forever.
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
        """True iff ``lease_id`` is the active, unexpired lease for
        ``task_id``. The EventLog's read-only hook into the dispatcher —
        it never mutates state.
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
        wake_event the caller did not request: a matched
        wake_event is owned by the single wake → lease handoff that
        produced it.

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
        :class:`Lease`. Lease does **not** clear it — the
        matched wake survives the lease ("matched-in-flight") so a crash
        before the durable ``TaskWoken`` does not lose it; it is cleared
        only by a consuming ``release(consumed_wake_event=...)`` and
        otherwise re-delivered after ``requeue_stale`` — at-least-once
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
            # Do NOT clear matched_wake_event here — it survives the
            # lease; cleared only by a consuming release.
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
                    # Terminal is forever — buffered wakes that never
                    # matched can never drain; GC them.
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

    def wake(self, task_id: str, wake_event: Any, *, reserved: bool = False) -> bool:
        """Deliver a wake event. Returns True iff the task is requeued
        (either because it was suspended and the event matched, or
        because the event matches a wake_on that was set even before
        the suspend handshake — see ``_release_locked`` for the latter).

        On a successful match, the matched ``wake_event`` is recorded
        on ``task.matched_wake_event``; it is handed to the worker on the
        next ``lease()`` but **survives** it,
        cleared only by a consuming
        ``release(consumed_wake_event=…)`` — a crash before that re-delivers
        it via ``requeue_stale``.

        ``reserved=True`` marks the requeued task targeted-lease-only (the
        one-shot guard :meth:`enqueue` documents): an untargeted poll skips it
        until the owning driver's targeted lease claims it and clears the flag.
        Set by a seed-after-wake resume so a resident worker cannot lease the
        woken-but-not-yet-seeded task. Only the matched→ready branch carries it;
        a buffered wake never becomes leaseable.
        """
        with self._lock:
            if task_id not in self._tasks:
                self._tasks[task_id] = _DispatcherTask(task_id=task_id)
            task = self._tasks[task_id]
            if task.status == "suspended" and wake_matches(task.wake_on, wake_event):
                task.matched_wake_event = wake_event
                task.status = "ready"
                task.wake_on = None
                task.suspend_reason = None
                task.reserved = reserved
                self._ready.append(task_id)
                return True
            task.pending_wake_events.append(wake_event)
            return False

    def requeue_stale(self) -> list[str]:
        """Move any leased tasks whose lease expired back to ready.

        Returns the list of task_ids that were requeued. The prior
        lease_id is invalidated; the original worker's writes will
        fail :class:`InvalidLease` on the EventLog.

        Each reclaim increments the task's ``reclaim_count``;
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
                    # Bound the silent lease-expiry loop. The
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
        the **recorded deadline** (byte-stable across re-delivery),
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
        # Validate BEFORE any mutation so a
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
            task.matched_wake_event = None  # OLD matched consumed
        if next_state == "suspended":
            task.wake_on = wake_on
            task.suspend_reason = suspend_reason
            # An un-consumed matched is PRESERVED. A matched wake
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
                # NEW matched (old matched was cleared above).
                for evt in list(task.pending_wake_events):
                    if wake_matches(task.wake_on, evt):
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
            # Terminal is forever — buffered wake events that
            # never matched can never drain now; GC them. The matched
            # wake (exactly-once handoff) is deliberately untouched.
            task.pending_wake_events.clear()


# ---------------------------------------------------------------------------
# Stack factory
# ---------------------------------------------------------------------------


def build_stack() -> tuple[EventLogFull, ContentStore, Dispatcher]:
    """Build the in-memory triple — the uniform backend ``build_stack``.

    No config: the stack lives and dies with the process. Wires the
    triple's one internal invariant itself (the event log takes the
    dispatcher as ``lease_validator``), the same shape as the durable
    backends' factories in the ``storage`` built-in.
    """
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    return event_log, content_store, dispatcher
