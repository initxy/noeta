"""The single wake-match predicate every Dispatcher adapter routes through.

Whether a pending wake event satisfies a task's ``wake_on`` condition is the
projection-matching invariant, a domain rule owned by
:mod:`noeta.protocols.wake`. An adapter MUST NOT carry its own copy of that
logic, or the backends diverge under one shared contract suite.
"""

from __future__ import annotations

from typing import Any

from noeta.protocols.wake import matches_wake


__all__ = ["_matches"]


def _matches(wake_on: Any, event: Any) -> bool:
    """A missing condition or a missing event never matches; the rest is L0."""
    if wake_on is None or event is None:
        return False
    return matches_wake(wake_on, event)
