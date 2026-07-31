"""``web`` — the default web tool pack (fetch + search).

Like ``fs``, activating ``web`` is identity-inert: the default tool set of a
bare ``Options`` still comes from ``builtin_tool_classes()`` in the compile path
(byte-identity). This declaration is the reference manifest and the listing
surface.
"""

from __future__ import annotations

from noeta.builtins._declare import c
from noeta.client.plugin_manifest import PluginManifest


MANIFEST = PluginManifest(
    name="web",
    requires_noeta=">=0.4",
    contributions=(
        c("tool", "webfetch", "noeta.builtins.web.impl.fetch:WebFetchTool"),
        c("tool", "web_search", "noeta.builtins.web.impl.search:WebSearchTool"),
        # The session-construction half: band 200 —
        # appends directly after the fs pack, preserving the merged
        # fs-then-web insertion order.
        c(
            "session_pack",
            "web",
            "noeta.builtins.web.impl:build_web_session_pack",
            priority=200,
        ),
    ),
)
