"""Shared stale-reclaim cap decision for the dispatcher adapters (kernel #3).

Both Dispatcher adapters (:class:`noeta.storage.memory.InMemoryDispatcher`
and :class:`noeta.storage.sqlite.dispatcher.SqliteDispatcher`) bound the
poison-task lease → expire → reclaim loop with the same rule: a task that
accrues ``reclaim_max`` CONSECUTIVE no-progress stale-lease reclaims drops to
``terminal`` (``stale_reclaim_exceeded``) instead of requeueing forever — the
reclaim-path analogue of ``max_fail_attempts``. That threshold decision is a
domain rule, so it lives here as the single predicate both adapters route
through (the same reason :mod:`noeta.storage._wake_match` centralises the wake
projection rule); a future adapter cannot silently pick a different cap.

Only the cap *decision* is shared — the reclaim-counter increment/reset and
the terminal ``pending_wakes`` GC are storage-specific mutations (a SQL
``UPDATE``/``DELETE`` versus a dataclass field / ``list.clear()``) with no
shareable common form, so they stay inline in each adapter.
"""

from __future__ import annotations


__all__ = ["reclaim_hits_cap"]


def reclaim_hits_cap(reclaim_count: int, reclaim_max: int) -> bool:
    """True iff a task at ``reclaim_count`` consecutive no-progress reclaims —
    the count AFTER this reclaim's increment — has reached ``reclaim_max`` and
    must drop to ``terminal`` rather than requeue.

    Callers pass the post-increment count so the two adapters agree exactly:
    the in-memory adapter increments its field first and passes it; the sqlite
    adapter passes ``old_count + 1`` (its ``UPDATE`` applies the increment).
    """
    return reclaim_count >= reclaim_max
