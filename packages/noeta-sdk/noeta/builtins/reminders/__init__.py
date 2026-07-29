"""``reminders`` — track B (D8): the three compose-time reminders.

Migrated onto the ``reminder`` surface. Each declaration carries the render
``ref`` and the integer ``priority`` that keeps the composed dynamic-suffix
tail byte-identical to the pre-migration append order (todo -> delegation ->
read). The composer's default registry wires these three directly (like fs/web
tools sourced from ``BUILTIN_TOOL_CLASSES``); this manifest is the reference /
listing surface.
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
            "noeta.context.reminders:todo_reminder",
            priority=100,
        ),
        c(
            "reminder",
            "delegation-nudge",
            "noeta.context.reminders:delegation_reminder",
            priority=200,
        ),
        c(
            "reminder",
            "read-suggestion",
            "noeta.context.reminders:read_suggestion_reminder",
            priority=300,
        ),
    ),
)
