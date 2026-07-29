"""``skills`` — thin declaration for the skill subsystem.

The skills resource root lands with the skill subsystem (host-wired), so the
manifest carries no contributions; it exists so every built-in capability has
a catalogue entry and the activation name resolves.
"""

from __future__ import annotations

from noeta.client.plugin_manifest import PluginManifest


MANIFEST = PluginManifest(name="skills", requires_noeta=">=0.4")
