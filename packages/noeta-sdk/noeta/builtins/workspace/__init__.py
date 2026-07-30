"""``workspace`` — thin declaration for the workspace-context material.

The environment block and the instructions-file residents are host-wired
content-channel material (renderer prose + hash rule + ``ContentKindSpec``
factory + the ``NOETA.md``/``AGENTS.md`` filename convention), not identity
contributions — so the manifest carries no contributions (the browser/app
precedent); it exists so every built-in capability has a catalogue entry and
the activation name resolves. The material lives in this plugin's ``impl``
package (phase 2c: ``noeta.builtins.workspace.impl`` —
``build_environment_kit`` / ``build_instructions_kit``), resolved by the SDK
host through :func:`noeta.client.parts.default_environment_kit` /
:func:`noeta.client.parts.default_instructions_kit` into the kernel
builder's ``environment_kit`` / ``instructions_kit`` injections.
"""

from __future__ import annotations

from noeta.client.plugin_manifest import PluginManifest


MANIFEST = PluginManifest(name="workspace", requires_noeta=">=0.4")
