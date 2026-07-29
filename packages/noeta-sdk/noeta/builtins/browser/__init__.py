"""``browser`` — thin declaration for the sandbox browser tool pack.

Browser tools are gated on a live sandbox backend (host-wired), so the
manifest carries no contributions; it exists so every built-in capability has
a catalogue entry and the activation name resolves.
"""

from __future__ import annotations

from noeta.client.plugin_manifest import PluginManifest


MANIFEST = PluginManifest(name="browser", requires_noeta=">=0.4")
