"""Built-in Guards.

:class:`BudgetGuard` enforces resource
caps on a Task; :class:`PermissionGuard` enforces tool / agent
allowlists with fail-closed risk-level handling. Both target the
``noeta.protocols.hooks.Guard`` Protocol; the host application
explicitly registers whichever instances it wants on its
``HookManager`` (there is no auto-wire — the canonical default profile
is assembled by ``noeta.execution.builder``, consumed by the
apps/noeta-agent host).

The Guards are intentionally isolated to ``noeta.protocols`` imports
(see ``.importlinter:guards-only-protocols``) so they cannot reach
into Engine, runtime adapters, storage, or providers — they are
boundary policy, not Engine internals.
"""

from __future__ import annotations

from noeta.guards.budget import Budget, BudgetGuard
from noeta.guards.permission import PermissionGuard, PermissionPolicy
from noeta.guards.repetition import RepetitionGuard, RepetitionPolicy


__all__ = [
    "Budget",
    "BudgetGuard",
    "PermissionGuard",
    "PermissionPolicy",
    "RepetitionGuard",
    "RepetitionPolicy",
]
