"""``governance`` — the default guard / observer hooks.

D6: process-scoped once loaded, never following activation. These are
governance authority. Microkernel M2: the refs point into this plugin's own
``impl`` package (the enforcement classes moved out of ``noeta.guards`` /
``noeta.observers.hook``); the SDK host wires the default stack by resolving
``impl:build_default_guards`` (:func:`noeta.client.parts.default_guards_factory`)
into the kernel builder's ``guards_factory`` injection.
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
