"""``delegation`` — the ``spawn_subagent`` control tool as a built-in plugin.

Control-tool-surface S2, D3: the manifest declares one ``control_tool`` factory
(band 100 — the pre-migration schema band; ``spawn_subagent`` renders FIRST in
the schema list). The contribution NAME is the tool name ``spawn_subagent``
(collision key), while the plugin NAME is ``delegation`` (the activation name
that lands ``"delegation"`` in the identity tuple). ``plugins=("delegation",)``
still folds delegation ON — the feature-flag branch resolves before the loaded
plugin set, so the plugin sharing the activation name is inert to folding.
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
