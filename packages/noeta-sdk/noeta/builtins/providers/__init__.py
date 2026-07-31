"""``providers`` built-in — the three Noeta-shape LLM adapters.

The ``provider`` surface is single-valued and host-wired: the host picks
exactly one adapter through ``Options.provider`` / ``HostConfig``, so this
manifest contributes nothing to that surface — merging three adapters onto a
single-valued surface would be a self-collision. Hosts construct an adapter
from ``noeta.sdk.providers``.
"""

from __future__ import annotations

from noeta.client.plugin_manifest import PluginManifest


MANIFEST = PluginManifest(name="providers", requires_noeta=">=0.4")
