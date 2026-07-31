"""``skills`` — the skill subsystem built-in.

A single ``session_pack`` contribution is the whole manifest; the
implementation lives in the sibling ``impl/`` package. The pack also
contributes the ``skill`` control tool as a factory closed over its own merged
registry, so no registry, menu, or kit ever crosses into kernel code.
"""

from __future__ import annotations

from noeta.builtins._declare import c
from noeta.client.plugin_manifest import PluginManifest


MANIFEST = PluginManifest(
    name="skills",
    requires_noeta=">=0.4",
    contributions=(
        c(
            "session_pack",
            "skills",
            "noeta.builtins.skills.impl:build_skills_session_pack",
            priority=600,
        ),
    ),
)
