"""``reminders`` — the three compose-time reminders.

Each declaration carries a render ``ref`` into this plugin's ``impl`` package
and an integer ``priority``; the registry renders in ``(priority, name)``
order, so these bands fix the dynamic-suffix tail as todo -> delegation ->
read, spread by 100 to leave room for third-party reminders to interleave.
This manifest is both the listing surface and the resolution source the SDK
build reads before injecting the specs into the kernel builder.
"""

from __future__ import annotations

from noeta.builtins._declare import c
from noeta.client.plugin_manifest import PluginManifest


MANIFEST = PluginManifest(
    name="reminders",
    requires_noeta=">=0.4",
    contributions=(
        c(
            "reminder",
            "unfinished-todos",
            "noeta.builtins.reminders.impl:todo_reminder",
            priority=100,
        ),
        c(
            "reminder",
            "delegation-nudge",
            "noeta.builtins.reminders.impl:delegation_reminder",
            priority=200,
        ),
        c(
            "reminder",
            "read-suggestion",
            "noeta.builtins.reminders.impl:read_suggestion_reminder",
            priority=300,
        ),
    ),
)
