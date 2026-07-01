"""HookManager: run Guards in priority order and merge their verdicts.

There are exactly three Guard action points
(``before_tool_call`` / ``before_spawn_subtask`` / ``before_finish``)
and three possible verdicts (``allow`` / ``deny`` / ``require_approval``).
This module owns the loop that calls each registered Guard in
ascending ``priority`` order and returns the first non-allow verdict —
the Engine takes the result and decides what event to write next.

Two defensive behaviours that the Engine relies on:

* A Guard whose ``check`` raises is treated as ``deny`` with a
  synthetic reason, and counts as the deciding non-allow verdict (so
  lower-priority Guards are NOT consulted). This prevents a buggy
  Guard from quietly granting an action it would otherwise have
  blocked.
* The Verdict class itself is re-exported from this module so callers
  written against the issue-01 scaffold (``from noeta.core.hooks import
  Verdict``) keep working without changes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from noeta.protocols.hooks import (
    Guard,
    GuardContext,
    ProposedAction,
    Verdict,
    VerdictResult,
)

__all__ = ["HookManager", "Verdict", "VerdictResult"]

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _GuardEntry:
    guard: Guard
    priority: int


class HookManager:
    """Owns the registered Guards and runs them on each action point."""

    def __init__(self) -> None:
        self._guards: list[_GuardEntry] = []

    def register(
        self, guard: Guard, *, priority: Optional[int] = None
    ) -> None:
        """Register a Guard. ``priority`` falls back to ``guard.priority``."""
        prio = priority if priority is not None else getattr(guard, "priority", 100)
        self._guards.append(_GuardEntry(guard=guard, priority=int(prio)))
        # Keep the list sorted so ``check`` does not re-sort on the hot
        # path. Stable sort preserves registration order within equal
        # priorities so tests are deterministic.
        self._guards.sort(key=lambda e: e.priority)

    def check(
        self, action: ProposedAction, ctx: GuardContext
    ) -> VerdictResult:
        """Run all guards in priority order; return the first non-allow.

        A Guard exception is converted into a ``deny`` carrying the
        guard's name. We log at WARNING (not as a metric, intentionally)
        so operators can see the coverage gap in test logs.
        """
        for entry in self._guards:
            guard = entry.guard
            try:
                result = guard.check(action, ctx)
            except Exception as exc:  # noqa: BLE001 - defensive boundary
                name = getattr(guard, "name", guard.__class__.__name__)
                _log.warning(
                    "guard %r raised %s; treating as deny", name, exc
                )
                return VerdictResult.deny(
                    f"guard {name!r} raised {type(exc).__name__}: {exc}"
                )
            if not result.is_allow:
                return result
        return VerdictResult.allow()
