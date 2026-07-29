"""``providers`` — the three noeta-shape LLM adapters.

The ``provider`` surface is single-valued and **host-wired** (D3 / D6): the
host selects exactly one adapter through ``Options.provider`` / ``HostConfig``,
so the three adapters are NOT merged onto the single-valued surface (that
would be a self-collision). This is a declaration-only reference manifest; the
adapter refs are documented for host discovery:

* ``noeta.providers.anthropic:AnthropicProvider``
* ``noeta.providers.openai_compat:OpenAICompatProvider``
* ``noeta.providers.openai_responses:OpenAIResponsesProvider``
"""

from __future__ import annotations

from noeta.client.plugin_manifest import PluginManifest


MANIFEST = PluginManifest(name="providers", requires_noeta=">=0.4")
