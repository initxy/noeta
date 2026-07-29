"""``presets`` — the official agents (D11).

The two standalone official ``AgentDefinition`` s are declared by ref; the
three main-roster subagents ride ``noeta.presets`` directly (they live in a
dict, not module attributes).
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
