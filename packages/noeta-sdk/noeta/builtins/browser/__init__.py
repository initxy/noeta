"""``browser`` — the sandbox browser tool pack.

The pack registers on the ``session_pack`` surface rather than contributing
identity ``tool`` s: it is gated on a live sandbox backend plus the ``browser``
capability flag, and neither belongs in an activating agent's ``AgentSpec``.
Band 700 places it between the skills (600) and MCP (800) packs, and tool
insertion order feeds the stable-prefix hash, so the band is part of the
byte-order contract. The ``BrowserBackend`` Protocol lives in this plugin's
``impl``; the ``sandbox`` plugin implements it.
"""

from __future__ import annotations

from noeta.builtins._declare import c
from noeta.client.plugin_manifest import PluginManifest


MANIFEST = PluginManifest(
    name="browser",
    requires_noeta=">=0.4",
    contributions=(
        c(
            "session_pack",
            "browser",
            "noeta.builtins.browser.impl:build_browser_session_pack",
            priority=700,
        ),
    ),
)
