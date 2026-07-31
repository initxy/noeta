"""``app`` — the app-preview capability (the ``open_app`` tool).

The tool exists only where the host wires a live ``"app_preview"`` gateway, so
the pack registers on the ``session_pack`` surface rather than contributing an
identity ``tool``: host wiring must not leak into an activating agent's
``AgentSpec``. Band 1000 is the last tool band, so the gateway-backed
``open_app`` shadows a same-named custom tool (band 900) instead of losing to
it.
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
