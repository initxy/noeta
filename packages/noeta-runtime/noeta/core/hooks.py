"""Run the registered Guards over a proposed action and merge their verdicts.

Guards are consulted in ascending ``priority``. A ``deny`` from any Guard
wins: a ``require_approval`` from an earlier Guard does not stop the scan, so
approving a call can never override a later Guard's denial. Without a deny,
the first ``require_approval`` decides. A Guard whose ``check`` raises counts
as a ``deny``, so a buggy Guard can never quietly grant an action it was meant
to block.
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
    def __init__(self) -> None:
        self._guards: list[_GuardEntry] = []

    def register(
        self, guard: Guard, *, priority: Optional[int] = None
    ) -> None:
        prio = priority if priority is not None else getattr(guard, "priority", 100)
        self._guards.append(_GuardEntry(guard=guard, priority=int(prio)))
        # Sorting on insert keeps ``check`` allocation-free, and the stable sort
        # makes equal priorities resolve in registration order.
        self._guards.sort(key=lambda e: e.priority)

    def check(
        self, action: ProposedAction, ctx: GuardContext
    ) -> VerdictResult:
        pending: Optional[VerdictResult] = None
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
            if result.verdict is Verdict.DENY:
                return result
            if pending is None and not result.is_allow:
                pending = result
        return pending if pending is not None else VerdictResult.allow()
