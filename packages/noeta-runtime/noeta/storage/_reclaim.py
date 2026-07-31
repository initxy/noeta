"""The stale-reclaim cap decision shared by the Dispatcher adapters.

A task that accrues ``reclaim_max`` CONSECUTIVE no-progress stale-lease reclaims
drops to ``terminal`` instead of requeueing forever — the reclaim-path analogue
of ``max_fail_attempts``, and the only thing bounding a poison task that
silently kills every worker that leases it. The threshold is a domain rule, so
it lives here as one predicate instead of a constant each adapter picks for
itself; only the *decision* is shared, because the counter update and the
terminal ``pending_wakes`` GC are storage-specific mutations with no common form.
"""

from __future__ import annotations


__all__ = ["reclaim_hits_cap"]


def reclaim_hits_cap(reclaim_count: int, reclaim_max: int) -> bool:
    """True iff the task must drop to ``terminal`` rather than requeue.

    ``reclaim_count`` is the count AFTER this reclaim's increment. Requiring
    post-increment is what lets an adapter that increments a field and one that
    increments inside a SQL ``UPDATE`` agree on the same boundary.
    """
    return reclaim_count >= reclaim_max
