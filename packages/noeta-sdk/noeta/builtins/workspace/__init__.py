"""``workspace`` — the workspace-context residents (environment + instructions).

The environment block and the instructions-file residents are
content-channel material (renderer prose + hash rule + ``ContentKindSpec``
factory + the ``NOETA.md``/``AGENTS.md`` filename convention + the impure
loaders), not identity contributions — no ``tool`` surface entries. What the
manifest declares (microkernel phase 3) is the session-construction half:
two ``session_pack`` factories, ``instructions`` (band 400) and
``environment`` (band 500), which load their snapshots, contribute the
residents' content kinds, and export the discovery/preloader seams. The SDK
host still reaches the kits (:func:`noeta.client.parts.default_environment_kit`
/ :func:`noeta.client.parts.default_instructions_kit`) and the loaders
(``noeta.client.parts.workspace_impl``) for its own record path — record ==
compose stays true because both halves live in this one plugin.
"""

from __future__ import annotations

from noeta.builtins._declare import c
from noeta.client.plugin_manifest import PluginManifest


MANIFEST = PluginManifest(
    name="workspace",
    requires_noeta=">=0.4",
    contributions=(
        c(
            "session_pack",
            "instructions",
            "noeta.builtins.workspace.impl:build_instructions_session_pack",
            priority=400,
        ),
        c(
            "session_pack",
            "environment",
            "noeta.builtins.workspace.impl:build_environment_session_pack",
            priority=500,
        ),
    ),
)
