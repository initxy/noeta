"""Process-local cancellation registry (cancel-cascade).

A live, in-process set of cancelled root task ids. The control-plane
``cancel`` writes the durable ``TaskCancelled`` event AND marks the root
here; the Engine polls it — through a per-call ``cancelled`` predicate the
delegation drain binds to ``is_cancelled(root_id)`` — at its turn
boundaries to abandon an in-flight child's result.

This is a RUNTIME accelerator only. The authoritative record of a cancel
is the ``TaskCancelled`` event in the log; this registry just lets a live
worker thread notice the cancel in O(1) without re-folding the log on
every turn. A resume that folds the log reconstructs state without it (no
predicate is injected on that path), so it has zero effect on the durable
contract.

Thread-safe: the cancel arrives on one thread (an HTTP handler) while the
workflow runs on another (the synchronous drive thread), so the set is
guarded by a lock.
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
        """Drop a cancelled mark once its tree has been torn down — keeps
        the set from growing without bound on a long-lived server."""
        with self._lock:
            self._cancelled.discard(str(task_id))
