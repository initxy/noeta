"""``delegation`` — the ``spawn_subagent`` control tool.

The manifest declares one ``control_tool`` factory at band 100, which fixes
this tool's slot in the builder's mount loop so the cross-plugin collision
check iterates in a stable order; the schema and routing byte-orders come from
the mount's own bands. The contribution name is the TOOL name
``spawn_subagent`` — that name is the collision key across plugins — while the
plugin name is the activation name ``delegation``.
"""

from __future__ import annotations

from noeta.builtins._declare import c
from noeta.client.plugin_manifest import PluginManifest


MANIFEST = PluginManifest(
    name="delegation",
    requires_noeta=">=0.4",
    contributions=(
        c(
            "control_tool",
            "spawn_subagent",
            "noeta.builtins.delegation.impl:build_delegation_control_tool",
            priority=100,
        ),
    ),
)
