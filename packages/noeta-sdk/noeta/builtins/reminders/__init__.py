"""``reminders`` — track B (D8): the three compose-time reminders.

Migrated onto the ``reminder`` surface. Each declaration carries the render
``ref`` (into this plugin's own ``impl`` package — microkernel M2) and the
integer ``priority`` that keeps the composed dynamic-suffix tail
byte-identical to the pre-migration append order (todo -> delegation -> read).
The SDK client build resolves these refs through
:func:`noeta.client.parts.default_reminder_specs` and injects the resolved
specs into the kernel builder (``base_reminders``) — this manifest is both the
listing surface and the resolution source.
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
