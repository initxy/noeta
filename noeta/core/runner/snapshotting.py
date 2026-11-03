"""Writing a running task's fold snapshots (``noeta.core.snapshots``): when
one is due, and always folded from stored events, never from the runner's
live state."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

import msgspec

from noeta.core import snapshots
from noeta.core.events import Event, Recorded
from noeta.core.loop.state import fold
from noeta.core.store import Snapshot

if TYPE_CHECKING:
    from noeta.core.runner.task import TaskRun

log = logging.getLogger("noeta.core.runner")

#: A new snapshot is written once the events since the last one hold at
#: least this share of its bytes (and ``Runtime.snapshot_every`` events).
#: Each snapshot rewrites the whole state, so a fixed interval writes bytes
#: growing with the square of the log; this keeps them within about
#: ``1 + 1 / SNAPSHOT_GROWTH`` times the log's, and a claim folds events of
#: at most this share of its snapshot's bytes.
SNAPSHOT_GROWTH = 0.1


def event_bytes(events: Sequence[Event]) -> int:
    """About what ``events`` add to a folded state, by their encoded size."""
    return sum(len(msgspec.json.encode(e)) for e in events)


class SnapshotWriter:
    """One run's snapshot writing. Until :meth:`known` is told of one, the
    run writes none."""

    def __init__(self, run: TaskRun) -> None:
        self.run = run
        #: The newest fold snapshot known, ``(seq, data)``: what the next one
        #: is folded from.
        self._newest: tuple[int, bytes] | None = None
        self._writing: asyncio.Task[None] | None = None
        #: Bytes of the events folded or appended since the newest snapshot.
        self._since = 0

    def known(self, seq: int, data: bytes, folded: Sequence[Recorded]) -> None:
        """The load started from the snapshot ``(seq, data)`` (``seq`` 0:
        the state before the log) and folded ``folded`` after it."""
        self._newest = (seq, data)
        self._since = event_bytes([r.event for r in folded])

    def _due(self) -> bool:
        state = self.run.state
        return self._newest is not None and state.ended is None \
            and state.last_seq - self._newest[0] >= self.run.rt.snapshot_every \
            and self._since >= SNAPSHOT_GROWTH * len(self._newest[1]) \
            and (self._writing is None or self._writing.done())

    def write_loaded(self) -> None:
        """Right after a load the state is a fold of stored events alone: a
        due snapshot is written from it as it is."""
        if not self._due():
            return
        state = self.run.state
        data = snapshots.encode(state)
        if data is None:
            self._newest = None
            return
        self._newest = (state.last_seq, data)
        self._since = 0
        self._writing = asyncio.ensure_future(self._put(state.last_seq, data))

    def count(self, events: Sequence[Event]) -> None:
        """``events`` were appended."""
        if self._newest is not None:
            self._since += event_bytes(events)

    def write_later(self) -> None:
        """After an append: write a snapshot in the background once due."""
        if self._due():
            self._since = 0
            self._writing = asyncio.ensure_future(self._write())

    async def finish(self) -> None:
        """Let a snapshot being written finish (bounded) before the run
        returns, unless the run itself is being cancelled."""
        current = asyncio.current_task()
        if self._writing is None or self._writing.done() \
                or (current is not None and current.cancelling()):
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(self._writing), self.run.rt.lease_seconds)

    async def _write(self) -> None:
        """Fold the stored events after the newest snapshot onto it (never
        the runner's live state: the snapshot must be what a fold of the
        stored log gives) and store the result."""
        assert self._newest is not None
        run = self.run
        seq, data = self._newest
        try:
            state = fold(await run.store.read(run.task_id, after=seq), run.reducers,
                         state=snapshots.decode(data), also=run.also)
        except Exception:  # a snapshot is only ever an optimization
            log.warning("task %s: could not fold a snapshot", run.task_id, exc_info=True)
            return
        new = snapshots.encode(state)
        if new is None:
            self._newest = None
            return
        self._newest = (state.last_seq, new)
        await self._put(state.last_seq, new)

    async def _put(self, seq: int, data: bytes) -> None:
        run = self.run
        try:
            await run.store.put_snapshot(  # type: ignore[attr-defined]
                run.task_id, Snapshot(seq=seq, version=run.fold_version(), data=data))
        except Exception as exc:  # noqa: BLE001 — the next one is written later
            log.warning("task %s: could not store a snapshot: %s", run.task_id, exc)
