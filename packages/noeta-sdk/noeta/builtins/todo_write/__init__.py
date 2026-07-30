"""``todo_write`` — the durable-checklist control tool as a built-in plugin.

Control-tool-surface S2, D3: a control tool is a ``control_tool`` surface
contribution. The manifest declares one factory
``(ControlToolBuildContext) -> ControlToolMount | None`` (band 200 — matching
the pre-migration internal schema band); the kernel builder's post-tools mount
loop runs it. ``todo_write`` also stays a recognized activation name
(``_ACTIVATION_CAPABILITY_FLAG``), so ``plugins=("todo_write",)`` still lands
``"todo_write"`` in the compiled ``AgentSpec.plugins`` identity tuple — naming
the plugin the same does not change activation semantics (the feature-flag
branch resolves first).
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
