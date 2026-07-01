"""``CompactionWorker`` — single-process, synchronous snapshot trigger.

Issue 20. Application-driven helper that emits a fresh ``TaskSnapshot``
event whenever a task stream has accumulated more events than
``max_uncompacted_events`` since its latest snapshot, restoring
``fold`` snapshot-accelerated coverage for streams that fell through
Engine's natural snapshot triggers (suspend / terminate / mid-tool-loop
threshold).

What this Worker is and is not:

* It writes snapshots via :meth:`EventLog.emit` with ``lease_id=None``
  and ``expected_seq=as_of_seq + 1`` (issue 20 P1 rev). The
  expected-seq optimistic lock closes the TOCTOU window between
  ``read → fold`` and the actual append: a concurrent writer that
  slips an event into the stream between Worker's last read and the
  append raises :class:`StaleSequence`, and Worker reports
  ``compacted=False`` rather than emit a stale snapshot. This keeps
  the fold invariant intact — a TaskSnapshot at seq S always covers
  events 0..S-1.
* The Worker still presents as a maintenance writer:
  ``origin='system'``, ``actor='compaction'``. ``lease_id=None`` lets
  the EventLog skip the lease check the same way ``system_emit``
  would (see :meth:`InMemoryEventLog._append_impl`'s ``require_lease``
  branch).
* It is **not** a resumable lifecycle event source: a resume folds
  the event stream itself, treating ``origin='system'
  type='TaskSnapshot'`` envelopes as a fold-acceleration cache rather
  than a lifecycle event to re-derive (issue 20 §G5).
* It is **not** a GC mechanism. ``ContentStore.put`` on a duplicate
  body is idempotent (issue 16 dedup-by-hash); older snapshot refs
  remain reachable. EventLog /
  ContentStore GC is left to a future ADR.
* It is **not** asynchronous. Each ``compact_if_needed`` call is one
  synchronous decision. Phase 2 daemon scheduling layers on top.
* It is **currently unwired** in production: nothing on the live host
  path constructs a ``CompactionWorker`` — it is exercised only by its
  own tests and kept as the Phase-2 daemon seam (the periodic scheduler
  that would call ``compact_if_needed`` is not built yet).

Serialisation path is exactly ``fold → serialize_task_state →
ContentStore.put`` — the same single canonical path Engine
``_write_snapshot`` uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from noeta.core.fold import fold
from noeta.core.snapshot import serialize_task_state, snapshot_media_type
from noeta.protocols.content_store import ContentStore
from noeta.protocols.errors import StaleSequence
from noeta.protocols.event_log import EventLog
from noeta.protocols.events import EventEnvelope, TaskSnapshotPayload


__all__ = ["CompactionResult", "CompactionWorker"]


@dataclass(frozen=True, slots=True)
class CompactionResult:
    """Outcome of a single ``CompactionWorker.compact_if_needed`` call."""

    task_id: str
    compacted: bool
    events_since_latest_snapshot: int
    latest_snapshot_seq_before: Optional[int]
    new_snapshot_seq: Optional[int]


def _latest_snapshot_seq(events: list[EventEnvelope]) -> Optional[int]:
    """Return the seq of the most recent ``TaskSnapshot`` in ``events``,
    or ``None`` if no snapshot has been emitted yet."""
    for env in reversed(events):
        if env.type == "TaskSnapshot":
            return int(env.seq)
    return None


def _latest_trace_id(events: list[EventEnvelope]) -> str:
    """Inherit trace_id from the latest event so the compaction event
    is attributed to the most recent activity context."""
    return events[-1].trace_id if events else "trace-unknown"


class CompactionWorker:
    """Single-process, synchronous compaction trigger.

    Application code drives the Worker explicitly:
    ``worker.compact_if_needed(task_id)``. Returns a
    :class:`CompactionResult` describing whether a new
    ``TaskSnapshot`` event was emitted and what gap was observed.

    The Worker is intentionally **stateless across calls**: each call
    re-reads the EventLog, makes its own decision, and returns.
    """

    name = "compaction"

    def __init__(
        self,
        *,
        event_log: EventLog,
        content_store: ContentStore,
        max_uncompacted_events: int = 50,
        actor: str = "compaction",
    ) -> None:
        self._event_log = event_log
        self._content_store = content_store
        self._max = int(max_uncompacted_events)
        self._actor = actor

    def compact_if_needed(self, task_id: str) -> CompactionResult:
        """Inspect the stream and emit a new ``TaskSnapshot`` iff the
        gap since the latest snapshot is at or above
        ``max_uncompacted_events``.

        ``compacted=False`` covers three cases: (a) the stream is
        empty / has no ``TaskCreated`` yet, (b) the gap is below the
        threshold, (c) a concurrent writer landed an event between
        Worker's last read and the snapshot append, raising
        :class:`StaleSequence` from the EventLog's ``expected_seq``
        check. Case (c) protects the fold invariant — a snapshot at
        seq S always covers events 0..S-1 — by aborting instead of
        emitting a stale snapshot.
        """
        events = self._event_log.read(task_id)
        if not events:
            return CompactionResult(
                task_id=task_id,
                compacted=False,
                events_since_latest_snapshot=0,
                latest_snapshot_seq_before=None,
                new_snapshot_seq=None,
            )

        latest_snapshot_seq = _latest_snapshot_seq(events)
        if latest_snapshot_seq is None:
            gap = len(events)
        else:
            gap = int(events[-1].seq) - latest_snapshot_seq

        if gap < self._max:
            return CompactionResult(
                task_id=task_id,
                compacted=False,
                events_since_latest_snapshot=gap,
                latest_snapshot_seq_before=latest_snapshot_seq,
                new_snapshot_seq=None,
            )

        return self._emit_snapshot(task_id, gap, latest_snapshot_seq, events)

    def _emit_snapshot(
        self,
        task_id: str,
        gap: int,
        latest_snapshot_seq_before: Optional[int],
        events: list[EventEnvelope],
    ) -> CompactionResult:
        as_of_seq = int(events[-1].seq)
        task = fold(self._event_log, self._content_store, task_id)
        # Race guard A — optimisation only: if a concurrent writer
        # already appended events between ``read`` and the fold
        # completing, the body we are about to put is known stale.
        # ``emit`` would catch this via expected_seq below, but we
        # save a wasted ``ContentStore.put`` by short-circuiting here.
        # (The put is idempotent on hash; the saving is a round-trip,
        # not a correctness fix — that lives in the ``expected_seq``
        # check.)
        if self._stream_advanced(task_id, as_of_seq):
            return self._noop_due_to_race(
                task_id, gap, latest_snapshot_seq_before
            )
        ref = self._content_store.put(
            serialize_task_state(task),
            media_type=snapshot_media_type(),
        )
        try:
            env = self._event_log.emit(
                task_id=task_id,
                type="TaskSnapshot",
                payload=TaskSnapshotPayload(state_ref=ref),
                lease_id=None,
                expected_seq=as_of_seq + 1,
                actor=self._actor,
                origin="system",
                trace_id=_latest_trace_id(events),
            )
        except StaleSequence:
            # The atomic seq check rejected our snapshot because the
            # stream advanced under us between our read and the
            # append. This is the definitive correctness gate: not
            # emitting protects the fold invariant. Callers can retry
            # via the next ``compact_if_needed`` call, which re-reads
            # and folds the new tail before trying again.
            return self._noop_due_to_race(
                task_id, gap, latest_snapshot_seq_before
            )
        return CompactionResult(
            task_id=task_id,
            compacted=True,
            events_since_latest_snapshot=gap,
            latest_snapshot_seq_before=latest_snapshot_seq_before,
            new_snapshot_seq=int(env.seq),
        )

    def _stream_advanced(self, task_id: str, as_of_seq: int) -> bool:
        fresh = self._event_log.read(task_id)
        return bool(fresh) and int(fresh[-1].seq) != as_of_seq

    def _noop_due_to_race(
        self,
        task_id: str,
        gap: int,
        latest_snapshot_seq_before: Optional[int],
    ) -> CompactionResult:
        return CompactionResult(
            task_id=task_id,
            compacted=False,
            events_since_latest_snapshot=gap,
            latest_snapshot_seq_before=latest_snapshot_seq_before,
            new_snapshot_seq=None,
        )
