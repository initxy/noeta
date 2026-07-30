"""``app`` — the app-preview tool (``open_app``).

The tool is gated on a live host preview gateway (the product's concrete
gateway rides the kernel's backend bag as ``"app_preview"``), so the
manifest declares no identity ``tool`` contributions — that would merge it
into an activating agent's ``AgentSpec``, which the gateway-gated wiring
deliberately does not do. What it does declare (microkernel phase 3) is the
session-construction half: the ``session_pack`` factory (band 1000 — after
the kernel's custom entry, so the host's ``open_app`` is authoritative)
whose applicability check IS the gateway gate. The implementation AND the
``AppPreviewGateway`` / ``AppMount`` seam types all live in this plugin's
``impl`` package (``noeta.builtins.app.impl``) — the kernel holds no
app-preview vocabulary.
"""

from __future__ import annotations

from noeta.builtins._declare import c
from noeta.client.plugin_manifest import PluginManifest


MANIFEST = PluginManifest(
    name="app",
    requires_noeta=">=0.4",
    contributions=(
        c(
            "session_pack",
            "app",
            "noeta.builtins.app.impl:build_app_session_pack",
            priority=1000,
        ),
    ),
)
