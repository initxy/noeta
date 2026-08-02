"""Process-local pending mid-turn injections — the hot-path signal.

A runtime accelerator only: the durable ``InjectionRequested`` event (folded
into ``GovernanceState.pending_injections``) is the authoritative record, and
this inbox merely lets a live worker thread notice a mid-turn injection in O(1)
at a turn boundary instead of re-folding the log every iteration. The injection
arrives on one thread (an HTTP handler) while the drive runs on another, hence
the lock — the exact shape of :class:`~noeta.runtime.cancellation.CancellationRegistry`.

Why this carries data, where the cancel registry carries only a flag: a cancel
is consumed by the Engine's own thread, which already holds the task; an
injected message is written on the caller's thread and never entered the
Engine's in-memory ``task``, so the drain has nothing to read unless the inbox
hands it the descriptor. A resume folds ``pending_injections`` back from the log
and needs no inbox entry, so a fresh process losing the inbox costs nothing —
the durable contract is untouched.
"""

from __future__ import annotations

import threading
from typing import Any


class InjectionInbox:
    """Thread-safe per-task map of pending injection descriptors.

    Each descriptor is the same ``{messages_ref, count}`` shape fold stores in
    ``GovernanceState.pending_injections``, keyed by ``injection_id``. Insertion
    order is preserved (``dict`` is ordered) so the drain delivers injections in
    arrival order.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, dict[str, dict[str, Any]]] = {}

    def submit(
        self, task_id: str, injection_id: str, descriptor: dict[str, Any]
    ) -> None:
        """Record a pending injection for ``task_id``. Idempotent per id — a
        re-submit of the same ``injection_id`` overwrites with the same
        descriptor, never duplicates."""
        key = str(task_id)
        with self._lock:
            self._pending.setdefault(key, {})[str(injection_id)] = dict(descriptor)

    def snapshot(self, task_id: str) -> dict[str, dict[str, Any]]:
        """A copy of the pending injections for ``task_id`` (arrival order),
        for the drain to read without holding the lock across an emit. Empty
        dict when none — the common no-injection turn boundary."""
        with self._lock:
            pending = self._pending.get(str(task_id))
            if not pending:
                return {}
            return {k: dict(v) for k, v in pending.items()}

    def consume(self, task_id: str, injection_id: str) -> None:
        """Drop one injection once its consuming ``MessagesAppended`` is
        durable. Idempotent; drops the task bucket when it empties so the map
        does not grow without bound on a long-lived server."""
        key = str(task_id)
        with self._lock:
            bucket = self._pending.get(key)
            if bucket is None:
                return
            bucket.pop(str(injection_id), None)
            if not bucket:
                self._pending.pop(key, None)

    def discard(self, task_id: str) -> None:
        """Drop every pending injection for ``task_id`` at conversation
        teardown (mirror of ``CancellationRegistry.discard``). Idempotent."""
        with self._lock:
            self._pending.pop(str(task_id), None)
