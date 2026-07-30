"""``skills`` — the skill subsystem built-in.

The implementation lives in the sibling ``impl/`` package (microkernel phase
2a): indexer + registry, the ``run_skill_script`` tool, the allowed-tools
resolver, and the kit assembly (``build_skills_kit``). The manifest carries
no identity contributions: skill *selection* is the kernel's ``skill``
control tool (gated by the ``skill_invocation`` capability flag), skill
*content* is host/workspace material — neither is an identity ``tool``
contribution, and parity pins the compiled bytes. What it does declare
(microkernel phase 3) is the session-construction half: the ``session_pack``
factory the kernel builder's generic loop calls (band 600). Disabling the
built-in removes the pack — the ``None``-factory special case is gone.
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
