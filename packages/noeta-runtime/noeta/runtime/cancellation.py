"""Process-local set of cancelled root task ids — the cancel cascade.

A runtime accelerator only: the durable ``TaskCancelled`` event is the
authoritative record, and this set merely lets a live worker thread notice a
cancel in O(1) at a turn boundary instead of re-folding the log. A resume
that folds the log injects no ``cancelled`` predicate, so the registry has
zero effect on the durable contract. The cancel arrives on one thread (an
HTTP handler) while the drive runs on another, hence the lock.
"""

from __future__ import annotations

import threading


class CancellationRegistry:
    """Thread-safe set of cancelled (root) task ids."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled: set[str] = set()

    def request(self, task_id: str) -> None:
        """Mark ``task_id`` cancelled. Idempotent."""
        with self._lock:
            self._cancelled.add(str(task_id))

    def is_cancelled(self, task_id: str) -> bool:
        with self._lock:
            return str(task_id) in self._cancelled

    def discard(self, task_id: str) -> None:
        """Drop a mark once its tree is torn down — without this the set
        grows without bound on a long-lived server."""
        with self._lock:
            self._cancelled.discard(str(task_id))
