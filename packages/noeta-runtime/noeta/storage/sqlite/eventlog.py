"""``SqliteEventLog`` — sqlite3-backed adapter for the L0 EventLog Protocols.

Issue 15. First real persistent EventLog backend; behaviour pinned by
:class:`noeta.storage.memory.InMemoryEventLog` (which remains the
reference implementation for unit / Engine integration tests, untouched
by this issue).

Three concurrency layers on :meth:`emit` match the InMemory adapter:

1. **Idempotency** — same ``(task_id, lease_id, idempotency_key)`` twice
   returns the originally-assigned envelope without writing a new one.
2. **4-KB payload cap** — canonical bytes computed once and
   re-used for the INSERT, so the cap check and the persisted bytes
   share the same single-source serialisation path.
3. **Optimistic ``expected_seq``** — caller asserts the next slot they
   intend to claim. Mismatch raises :class:`StaleSequence`.
4. **Lease validity** — when ``lease_id`` is provided and a
   ``LeaseRegistry`` was injected, the registry must approve the
   ``(task_id, lease_id)`` pair.

Every write runs inside ``BEGIN IMMEDIATE`` so two writers cannot race
on ``MAX(seq)``. Subscribers fire **after** ``COMMIT`` and **outside**
the adapter lock; subscriber callbacks are free to issue further
``emit / system_emit`` calls (the ``ChildLifecycleObserver`` pattern)
because the original transaction is already durable by then.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path
from types import TracebackType
from typing import Any, Callable, Optional, Union

from noeta.protocols.canonical import (
    from_canonical_bytes,
    restore_dataclass,
    to_canonical_bytes,
)
from noeta.protocols.dispatcher import LeaseRegistry
from noeta.protocols.errors import (
    InvalidLease,
    PayloadTooLarge,
    StaleSequence,
)
from noeta.protocols.event_log import (
    Subscriber,
    TaskStreamSummary,
    Unsubscribe,
)
from noeta.protocols.events import (
    AssistantThinkingRecordedPayload,
    BackgroundShellExitedPayload,
    BackgroundShellKilledPayload,
    BackgroundShellLostPayload,
    BackgroundShellPolledPayload,
    BackgroundShellStartedPayload,
    BackgroundSubagentDeliveredPayload,
    BackgroundSubagentStartedPayload,
    CompactedPayload,
    CompactionRequestedPayload,
    ContextPlanComposedPayload,
    ConversationClosedPayload,
    ConversationReopenedPayload,
    EventEnvelope,
    AgentBoundPayload,
    EventOrigin,
    LLMRequestFinishedPayload,
    TaskHostBoundPayload,
    LLMRequestStartedPayload,
    LLMResponseRecordedPayload,
    LeaseGrantedPayload,
    McpProvenanceRecordedPayload,
    McpServerSkippedPayload,
    MessageSelection,
    MessagesAppendedPayload,
    ModelBoundPayload,
    ContextContentRecordedPayload,
    SkillContentRecordedPayload,
    StepTransitionMarkedPayload,
    SubtaskCompletedPayload,
    SubtaskDeniedPayload,
    SubtaskSpawnedPayload,
    TaskCancelledPayload,
    TaskCompletedPayload,
    TaskCreatedPayload,
    TaskFailedPayload,
    TaskRewoundPayload,
    TaskSnapshotPayload,
    TaskStartedPayload,
    TaskStatePatchedPayload,
    TaskSuspendedPayload,
    TaskWokenPayload,
    ToolCallApprovalRequestedPayload,
    ToolCallApprovalResolvedPayload,
    ToolCallDeniedPayload,
    ToolCallFinishedPayload,
    ToolCallStartedPayload,
    ToolResultRecordedPayload,
    ToolSchemaRecordedPayload,
    UserQuestionAnsweredPayload,
    UserQuestionRequestedPayload,
)
from noeta.protocols.messages import Usage
from noeta.protocols.values import EVENT_PAYLOAD_MAX_BYTES
from noeta.storage.sqlite._connection import _open_connection
from noeta.storage.sqlite.migrations import apply_migrations


__all__ = ["MAX_PAYLOAD_BYTES", "SqliteEventLog"]


# Adapter-local alias preserved so existing call sites and tests can
# keep importing ``MAX_PAYLOAD_BYTES`` from this module. The canonical
# L0 name is :data:`noeta.protocols.values.EVENT_PAYLOAD_MAX_BYTES`
# (issue 16 sign-off pinned the precise event-payload naming to avoid
# confusion with the unrelated absence of any cap on ContentStore).
MAX_PAYLOAD_BYTES = EVENT_PAYLOAD_MAX_BYTES


_DEFAULT_SCHEMA_VERSION = 1


def _default_id_factory() -> str:
    return f"evt-{uuid.uuid4().hex}"


# Adapter-local map from event-type string to the typed payload
# dataclass constructor. ``from_canonical_bytes`` restores nested
# ``__canonical_tag__``-bearing values (``ContentRef``, ``Message``,
# ``WakeCondition``, ``SubtaskResult``, ``ContextPlan``, ``ViewSegment``)
# automatically; this table covers the outer payload classes that do
# *not* carry a tag and would otherwise read back as plain dicts.
#
# A test in the contract suite reflects ``noeta.protocols.events`` and
# fails CI the moment a new ``*Payload`` class lands without a matching
# entry here, so the mapping cannot drift silently.
def _restore_llm_request_started_payload(d: Any) -> LLMRequestStartedPayload:
    """Restore ``LLMRequestStarted`` tolerating MS1's optional ``selection``.

    Three deterministic shapes for ``selection``:
      * absent / ``None`` → ``None`` (pre-MS1 old-shape payload);
      * an already-typed :class:`MessageSelection` (the normal sqlite read
        path: ``from_canonical_bytes`` rehydrates the tagged value before
        this restorer runs) → kept as-is;
      * a plain (untagged) dict — old-ish / handwritten / fixture bodies →
        rebuilt from the fixed five fields; a missing required key is a
        ``KeyError`` (fail loud — a malformed body is not silently dropped).
    Any other shape fails loud.
    """
    sel = d.get("selection")
    if sel is None:
        selection: Optional[MessageSelection] = None
    elif isinstance(sel, MessageSelection):
        selection = sel
    elif isinstance(sel, dict):
        selection = MessageSelection(
            strategy=sel["strategy"],
            candidates=sel["candidates"],
            selected=sel["selected"],
            dropped=sel["dropped"],
            limit=sel["limit"],
            # ③ (D-3f): additive prune/summarize counters — ``.get`` so a
            # pre-③ dict body restores with the byte-safe defaults.
            pruned=sel.get("pruned", 0),
            summarized=sel.get("summarized", 0),
        )
    else:
        raise TypeError(
            f"LLMRequestStarted.selection: unexpected shape {type(sel)!r}"
        )
    return LLMRequestStartedPayload(
        call_id=d["call_id"],
        model=d["model"],
        request_ref=d["request_ref"],
        input_tokens=d.get("input_tokens", 0),
        selection=selection,
    )


def _restore_llm_request_finished_payload(d: Any) -> LLMRequestFinishedPayload:
    """Restore ``LLMRequestFinished`` tolerating the optional ``usage`` added in foundation phase A.

    Three deterministic shapes for ``usage`` (mirrors the selection
    three-state restorer above):
      * absent / ``None`` → empty ``Usage()`` (old-shape payload from
        before foundation phase A — the dataclass default also covers this,
        but we are explicit so the intent survives refactors);
      * an already-typed :class:`Usage` (the normal sqlite read path:
        ``from_canonical_bytes`` does NOT rehydrate ``Usage`` because it
        carries no tag, so in practice this is the dict branch — kept for
        symmetry / defensiveness) → kept as-is;
      * a plain (untagged) dict — the sqlite canonical body → rebuilt
        from its stored fields. Unknown keys (e.g. a legacy bare-dict
        ``input_tokens`` / ``total_tokens`` from a hand-written fixture)
        are dropped rather than crashing ``Usage(**d)``.
    """
    raw = d.get("usage")
    if raw is None:
        usage = Usage()
    elif isinstance(raw, Usage):
        usage = raw
    elif isinstance(raw, dict):
        known = {
            "uncached",
            "cache_read",
            "cache_write",
            "output",
            "reasoning_tokens",
        }
        usage = Usage(**{k: v for k, v in raw.items() if k in known})
    else:
        raise TypeError(
            f"LLMRequestFinished.usage: unexpected shape {type(raw)!r}"
        )
    return LLMRequestFinishedPayload(
        call_id=d["call_id"],
        success=d["success"],
        cost_usd=d.get("cost_usd", 0.0),
        latency_ms=d.get("latency_ms", 0),
        usage=usage,
    )


_PAYLOAD_RESTORERS: dict[str, Callable[[Any], Any]] = {
    "TaskCreated":         lambda d: TaskCreatedPayload(**d),
    "TaskStarted":         lambda d: TaskStartedPayload(**d),
    "TaskStatePatched":    lambda d: TaskStatePatchedPayload(**d),
    "MessagesAppended":    lambda d: MessagesAppendedPayload(**d),
    "TaskSnapshot":        lambda d: TaskSnapshotPayload(**d),
    "TaskRewound":         lambda d: TaskRewoundPayload(**d),
    "ContextPlanComposed": lambda d: ContextPlanComposedPayload(**d),
    "TaskCompleted":       lambda d: TaskCompletedPayload(**d),
    "TaskFailed":          lambda d: TaskFailedPayload(**d),
    "ToolCallStarted":     lambda d: ToolCallStartedPayload(**d),
    "ToolResultRecorded":  lambda d: ToolResultRecordedPayload(**d),
    "ToolCallFinished":    lambda d: ToolCallFinishedPayload(**d),
    "SubtaskSpawned":      lambda d: SubtaskSpawnedPayload(**d),
    "StepTransitionMarked": lambda d: StepTransitionMarkedPayload(**d),
    "CompactionRequested": lambda d: CompactionRequestedPayload(**d),
    "Compacted":           lambda d: CompactedPayload(**d),
    "SubtaskCompleted":    lambda d: SubtaskCompletedPayload(**d),
    "SubtaskDenied":       lambda d: SubtaskDeniedPayload(**d),
    "TaskSuspended":       lambda d: TaskSuspendedPayload(**d),
    "TaskWoken":           lambda d: TaskWokenPayload(**d),
    "ToolCallDenied":      lambda d: ToolCallDeniedPayload(**d),
    "ToolCallApprovalRequested": lambda d: ToolCallApprovalRequestedPayload(**d),
    "ToolCallApprovalResolved":  lambda d: ToolCallApprovalResolvedPayload(**d),
    "UserQuestionRequested": lambda d: UserQuestionRequestedPayload(**d),
    "UserQuestionAnswered": lambda d: UserQuestionAnsweredPayload(**d),
    "LLMRequestStarted":   lambda d: _restore_llm_request_started_payload(d),
    "LLMResponseRecorded": lambda d: LLMResponseRecordedPayload(**d),
    "AssistantThinkingRecorded": lambda d: AssistantThinkingRecordedPayload(**d),
    "LLMRequestFinished":  lambda d: _restore_llm_request_finished_payload(d),
    "TaskCancelled":       lambda d: TaskCancelledPayload(**d),
    "ModelBound":          lambda d: ModelBoundPayload(**d),
    # ``restore_dataclass`` (not ``**d``) so an old recording that still
    # carries the retired verify-era ``*_fingerprint`` keys folds/resumes
    # instead of crashing on an unexpected keyword (R1 tolerance).
    "AgentBound":          lambda d: restore_dataclass(AgentBoundPayload, d),
    "TaskHostBound":       lambda d: restore_dataclass(TaskHostBoundPayload, d),
    "ConversationClosed":  lambda d: ConversationClosedPayload(**d),
    "ConversationReopened": lambda d: ConversationReopenedPayload(**d),
    "LeaseGranted":        lambda d: LeaseGrantedPayload(**d),
    "ToolSchemaRecorded":  lambda d: ToolSchemaRecordedPayload(**d),
    "SkillContentRecorded": lambda d: SkillContentRecordedPayload(**d),
    "ContextContentRecorded": lambda d: ContextContentRecordedPayload(**d),
    "McpServerSkipped":    lambda d: McpServerSkippedPayload(**d),
    "McpProvenanceRecorded": lambda d: McpProvenanceRecordedPayload(**d),
    "BackgroundShellStarted": lambda d: BackgroundShellStartedPayload(**d),
    "BackgroundShellPolled":  lambda d: BackgroundShellPolledPayload(**d),
    "BackgroundShellExited":  lambda d: BackgroundShellExitedPayload(**d),
    "BackgroundShellKilled":  lambda d: BackgroundShellKilledPayload(**d),
    "BackgroundShellLost":    lambda d: BackgroundShellLostPayload(**d),
    "BackgroundSubagentStarted":   lambda d: BackgroundSubagentStartedPayload(**d),
    "BackgroundSubagentDelivered": lambda d: BackgroundSubagentDeliveredPayload(**d),
}


def _restore_payload(event_type: str, body: Any) -> Any:
    restorer = _PAYLOAD_RESTORERS.get(event_type)
    if restorer is None:
        # Forward-compatibility: an event type we don't yet know about
        # passes through as the canonical dict. New typed payload
        # classes must register here; the contract suite enforces it.
        return body
    return restorer(body)


def _enforce_payload_cap(task_id: str, event_type: str, body: bytes) -> None:
    if len(body) > EVENT_PAYLOAD_MAX_BYTES:
        raise PayloadTooLarge(
            f"task_id={task_id}, type={event_type}, "
            f"size={len(body)}, cap={EVENT_PAYLOAD_MAX_BYTES} "
            "(large bodies must go through ContentStore)"
        )


class SqliteEventLog:
    """sqlite3-backed implementation of ``EventLog`` + ``EventLogSubscriber``.

    Public surface deliberately equals the L0 Protocols (``emit``,
    ``system_emit``, ``read``, ``find_latest_snapshot``, ``subscribe``)
    plus :meth:`bind_lease_registry` (mirroring InMemory) and
    :meth:`close` (adapter-level resource release; not on the
    Protocol, callers that wire SqliteEventLog know to call it).
    """

    def __init__(
        self,
        path: Union[str, Path],
        *,
        lease_validator: Optional[LeaseRegistry] = None,
        clock: Optional[Callable[[], float]] = None,
        id_factory: Optional[Callable[[], str]] = None,
        schema_version: int = _DEFAULT_SCHEMA_VERSION,
    ) -> None:
        self._conn = _open_connection(path)
        apply_migrations(self._conn)
        self._lease_validator = lease_validator
        self._clock = clock or time.time
        self._id_factory = id_factory or _default_id_factory
        self._schema_version = schema_version
        self._subscribers: list[Subscriber] = []
        # ``threading.Lock`` (not RLock) — same-thread re-entry into
        # ``emit`` (e.g. an application bug calling emit from inside
        # emit) deadlocks rather than corrupting the seq counter.
        # Subscriber-driven re-emit is safe because callbacks run
        # *after* lock release; see ``_notify`` below.
        self._lock = threading.Lock()
        self._closed = False

    # -- wiring ----------------------------------------------------------

    def bind_lease_registry(self, registry: LeaseRegistry) -> None:
        """Late-bind a :class:`LeaseRegistry`.

        Mirrors the InMemory adapter so wiring helpers that previously
        constructed an EventLog without the Dispatcher and patched it
        in later keep working without backend-specific branches.
        """
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
        # cap check and the BLOB INSERT, so the persisted payload is
        # byte-identical to what the cap saw (single canonical path).
        body = to_canonical_bytes(envelope.payload)

        stamped: EventEnvelope
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                # Idempotency dedup (runs before the seq check so a
                # retry doesn't accidentally trip StaleSequence).
                if lease_id is not None and idempotency_key is not None:
                    cached = self._conn.execute(
                        "SELECT seq FROM idempotency "
                        "WHERE task_id = ? AND lease_id = ? "
                        "AND idempotency_key = ?",
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
                    "FROM events WHERE task_id = ?",
                    (envelope.task_id,),
                ).fetchone()
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
                    and not self._lease_validator.is_lease_valid(
                        envelope.task_id, lease_id
                    )
                ):
                    raise InvalidLease(
                        f"task_id={envelope.task_id}, lease_id={lease_id}"
                    )

                stamped = envelope.with_seq(next_seq)
                self._conn.execute(
                    "INSERT INTO events ("
                    " task_id, seq, id, type, schema_version, occurred_at,"
                    " actor, trace_id, correlation_id, causation_id,"
                    " origin, payload_canonical"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                        ") VALUES (?, ?, ?, ?)",
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
        # take the same lock: otherwise a reader could interleave with
        # a writer's ``BEGIN IMMEDIATE`` block and either see uncommitted
        # state or race the sqlite3 driver's per-connection state.
        with self._lock:
            if after_seq is None:
                rows = self._conn.execute(
                    "SELECT * FROM events WHERE task_id = ? ORDER BY seq",
                    (task_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM events WHERE task_id = ? AND seq > ? "
                    "ORDER BY seq",
                    (task_id, int(after_seq)),
                ).fetchall()
            return [_row_to_envelope(row) for row in rows]

    def find_latest_snapshot(self, task_id: str) -> Optional[EventEnvelope]:
        with self._lock:
            # TaskRewound is a snapshot-shaped fold baseline
            # (``state_ref`` too) — take whichever of {TaskSnapshot, TaskRewound}
            # has the higher seq so a rewind re-bases fold from the same lookup.
            # The ``ix_events_snapshot`` partial index (migration 5) is keyed
            # on exactly this ``type IN ('TaskSnapshot', 'TaskRewound')``
            # predicate, so this lookup is an indexed single-row hit rather
            # than a reverse PRIMARY KEY walk whose cost grew with the tail
            # since the last baseline.
            row = self._conn.execute(
                "SELECT * FROM events "
                "WHERE task_id = ? AND type IN ('TaskSnapshot', 'TaskRewound') "
                "ORDER BY seq DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            return _row_to_envelope(row)

    def list_task_streams(self) -> list[TaskStreamSummary]:
        """Enumerate task streams, most-recent-update first (CW5a).

        One ``GROUP BY task_id`` pass over the events table. The
        ``MAX(occurred_at) DESC, task_id ASC`` ordering gives a deterministic
        tie-break so equal timestamps never reorder flakily. A row only exists
        when the task has ≥1 event, so empty streams are naturally absent.
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
                task_id=row[0],
                last_seq=int(row[1]),
                last_event_time=float(row[2]),
            )
            for row in rows
        ]

    def _fetch_envelope(self, task_id: str, seq: int) -> EventEnvelope:
        # Callers already hold ``self._lock`` (only ``_append`` invokes
        # this, from inside its BEGIN IMMEDIATE block). No nested
        # acquire — that would deadlock on a non-reentrant Lock.
        row = self._conn.execute(
            "SELECT * FROM events WHERE task_id = ? AND seq = ?",
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
        session" command — deliberately NOT on the L0 ``EventLog`` Protocols
        (the record/fold path is append-only and never deletes). ``content`` blobs the events
        referenced are intentionally left untouched: that table is addressed
        by hash and shared across tasks, so reclaiming orphaned blobs is a
        separate offline GC concern (see the ``ContentStore`` Protocol).

        Returns ``True`` iff at least one ``events`` row was removed.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    "DELETE FROM events WHERE task_id = ?", (task_id,)
                )
                removed = cur.rowcount
                self._conn.execute(
                    "DELETE FROM idempotency WHERE task_id = ?", (task_id,)
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return removed > 0

    # -- lifecycle -------------------------------------------------------

    def close(self) -> None:
        """Close the underlying sqlite3 connection.

        Idempotent. Not part of the L0 Protocols — application wiring
        that constructs a :class:`SqliteEventLog` is responsible for
        calling this at shutdown (or using ``with contextlib.closing(...)``).
        """
        if self._closed:
            return
        try:
            self._conn.close()
        finally:
            self._closed = True

    def __enter__(self) -> "SqliteEventLog":
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.close()


def _row_to_envelope(row: sqlite3.Row) -> EventEnvelope:
    body_blob = row["payload_canonical"]
    canonical_body = from_canonical_bytes(body_blob)
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
