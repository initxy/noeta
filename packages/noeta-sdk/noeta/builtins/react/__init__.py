"""``react`` — the official decision-mapping policy built-in.

The implementation lives in the sibling ``impl/`` package (microkernel phase
2b): ``ReActPolicy``, the workflow ``OrchestrationPolicy`` /
``StructuredOutputPolicy``, and the builder's ``default_policy_factory`` body
(``build_react_policy_factory``). The manifest stays deliberately
contribution-free: the default policy identity is already baked SDK-side as
``POLICY_REF ("react", "1")`` for every compiled agent (parity pins the
bytes), and the D10 ``policy`` surface exists for plugins that *replace* the
default — the default itself is not a per-agent contribution. The catalogue
entry exists so the capability is listable and the activation name resolves.
"""

from __future__ import annotations

from noeta.client.plugin_manifest import PluginManifest


MANIFEST = PluginManifest(name="react", requires_noeta=">=0.4")
