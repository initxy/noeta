"""``presets`` — the official agents contributed on the ``agent`` surface.

A contribution resolves its ``ref`` as ``module:attribute``, so only the two
standalone :class:`AgentDefinition` s can be declared here; the three roster
subagents live inside ``noeta.presets.OFFICIAL_SUBAGENTS`` — a dict, not module
attributes — and are wired from there.
"""

from __future__ import annotations

from noeta.builtins._declare import c
from noeta.client.plugin_manifest import PluginManifest


MANIFEST = PluginManifest(
    name="presets",
    requires_noeta=">=0.4",
    contributions=(
        c("agent", "web", "noeta.presets:WEB_SUBAGENT"),
        c("agent", "__consolidation__", "noeta.presets:CONSOLIDATION_AGENT"),
    ),
)
