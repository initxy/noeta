"""``governance`` — the default guard stack and the hook observer.

Governance is process-scoped once loaded and never follows activation: a Guard
is an authority, so it must not be something a Task can switch off. The SDK
host wires the stack by resolving ``impl:build_default_guards`` into the kernel
builder's ``guards_factory`` injection.
"""

from __future__ import annotations

from noeta.builtins._declare import c
from noeta.client.plugin_manifest import PluginManifest


MANIFEST = PluginManifest(
    name="governance",
    requires_noeta=">=0.4",
    contributions=(
        c(
            "guard",
            "permission",
            "noeta.builtins.governance.impl.permission:PermissionGuard",
        ),
        c("guard", "budget", "noeta.builtins.governance.impl.budget:BudgetGuard"),
        c(
            "guard",
            "repetition",
            "noeta.builtins.governance.impl.repetition:RepetitionGuard",
        ),
        c("guard", "hook", "noeta.builtins.governance.impl.hook_guard:HookGuard"),
        c(
            "observer",
            "hook",
            "noeta.builtins.governance.impl.hook_observer:HookObserver",
        ),
    ),
)
