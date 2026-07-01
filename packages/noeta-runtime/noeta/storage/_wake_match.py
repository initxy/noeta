"""Shared wake-matching helper for the storage adapters.

Both Dispatcher adapters (:class:`noeta.storage.memory.InMemoryDispatcher`
and :class:`noeta.storage.sqlite.dispatcher.SqliteDispatcher`) need to
decide whether a pending wake event satisfies a task's ``wake_on``
condition. That decision is the projection-matching invariant and it
lives in :mod:`noeta.protocols.wake`; the adapters MUST NOT carry their
own match logic. This module is the single ``_matches`` both adapters
route through so contract-suite parametrisation across the two backends
cannot drift, and so a future adapter cannot silently diverge.
"""

from __future__ import annotations

from typing import Any

from noeta.protocols.wake import matches_wake


__all__ = ["_matches"]


def _matches(wake_on: Any, event: Any) -> bool:
    """Wake matching delegates to the L0 ``matches_wake`` helper.

    Adapter implementations MUST NOT carry their own match logic — the
    projection-matching invariant (SubtaskCompleted on subtask_id;
    SubtaskGroupCompleted on group_id; HumanResponseReceived on handle;
    TimerFired on ``event.fire_at >= condition.fire_at``) is a domain
    rule and lives in :mod:`noeta.protocols.wake`. The wake-resume issue
    tightened this: every Dispatcher routes through ``matches_wake`` so a
    future adapter cannot silently diverge.
    """
    if wake_on is None or event is None:
        return False
    return matches_wake(wake_on, event)
