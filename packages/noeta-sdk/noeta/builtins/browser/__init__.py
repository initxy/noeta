"""``browser`` — the sandbox browser tool pack.

Browser tools are gated on a live sandbox backend + the ``browser``
capability flag, so the manifest declares no identity ``tool``
contributions — that would merge them into an activating agent's
``AgentSpec``, which the capability flag deliberately does not do. What it
does declare (microkernel phase 3) is the session-construction half: the
``session_pack`` factory (band 700) whose applicability check IS the
backend + flag gate. The pack implementation AND the ``BrowserBackend``
Protocol both live in this plugin's ``impl`` package — the kernel holds no
browser vocabulary; the ``sandbox`` plugin implements the Protocol.
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
