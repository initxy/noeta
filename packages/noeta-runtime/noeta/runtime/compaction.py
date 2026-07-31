"""Application-driven snapshot trigger that restores fold acceleration.

A stream that never hits the Engine's own snapshot triggers (suspend,
terminate, mid-tool-loop threshold) has nothing recent to fold from, so this
Worker emits a fresh ``TaskSnapshot`` once one has grown more than
``max_uncompacted_events`` past its latest snapshot — as a lease-free
maintenance writer (``lease_id=None``, ``origin='system'``), which is safe
only because a snapshot re-derives state rather than advancing it. The append
is fenced by ``expected_seq``: the fold invariant is that a ``TaskSnapshot``
at seq S covers events 0..S-1, so losing the race to a concurrent writer must
report ``compacted=False`` rather than record a snapshot of a stale prefix.
Nothing is pruned — older snapshot refs stay reachable, and EventLog /
ContentStore GC is out of scope.
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
    task_id: str
    compacted: bool
    events_since_latest_snapshot: int
    latest_snapshot_seq_before: Optional[int]
    new_snapshot_seq: Optional[int]


def _latest_snapshot_seq(events: list[EventEnvelope]) -> Optional[int]:
    for env in reversed(events):
        if env.type == "TaskSnapshot":
            return int(env.seq)
    return None


def _latest_trace_id(events: list[EventEnvelope]) -> str:
    """Inherit the latest event's trace so the snapshot is attributed to the
    activity context that made it necessary."""
    return events[-1].trace_id if events else "trace-unknown"


class CompactionWorker:
    """Single-process, synchronous compaction trigger.

    Stateless across calls by design: each call re-reads the EventLog and
    makes its own decision, so nothing it caches can go stale under a
    concurrent writer.
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
        """Emit a new ``TaskSnapshot`` iff the gap since the latest snapshot
        is at or above ``max_uncompacted_events``.

        ``compacted=False`` also covers the lost race: a concurrent writer
        landing an event between the read and the append aborts the snapshot
        rather than recording one that does not cover the stream it claims.
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
        # Optimisation only: the ``expected_seq`` check below is what makes
        # a lost race safe. Short-circuiting here merely saves a known-stale
        # ``ContentStore.put`` round-trip, so do not mistake it for the
        # correctness gate.
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
            # The stream advanced under us; dropping the snapshot is what
            # protects the fold invariant. The next ``compact_if_needed``
            # re-reads and folds the new tail, so nothing is lost by giving
            # up here.
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
