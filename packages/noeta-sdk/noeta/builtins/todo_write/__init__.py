"""``todo_write`` — the durable-checklist control tool.

The manifest declares one ``control_tool`` factory at band 200, which fixes
this tool's slot in the builder's mount loop so the cross-plugin collision
check iterates in a stable order; the schema and routing byte-orders come from
the mount's own bands. The plugin name doubles as a capability-flag activation
name, and the factory self-gates on that flag — mounting is enablement.
"""

from __future__ import annotations

from noeta.builtins._declare import c
from noeta.client.plugin_manifest import PluginManifest


MANIFEST = PluginManifest(
    name="todo_write",
    requires_noeta=">=0.4",
    contributions=(
        c(
            "control_tool",
            "todo_write",
            "noeta.builtins.todo_write.impl:build_todo_write_control_tool",
            priority=200,
        ),
    ),
)
