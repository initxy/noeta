"""``governance`` — the default guard / observer hooks.

D6: process-scoped once loaded, never following activation. These are
governance authority.
"""

from __future__ import annotations

from noeta.builtins._declare import c
from noeta.client.plugin_manifest import PluginManifest


MANIFEST = PluginManifest(
    name="governance",
    requires_noeta=">=0.4",
    contributions=(
        c("guard", "permission", "noeta.guards.permission:PermissionGuard"),
        c("guard", "budget", "noeta.guards.budget:BudgetGuard"),
        c("guard", "repetition", "noeta.guards.repetition:RepetitionGuard"),
        c("guard", "hook", "noeta.guards.hook:HookGuard"),
        c("observer", "hook", "noeta.observers.hook:HookObserver"),
    ),
)
