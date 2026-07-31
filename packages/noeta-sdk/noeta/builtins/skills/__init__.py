"""``skills`` — the skill subsystem built-in.

The implementation lives in the sibling ``impl/`` package (microkernel phase
2a): indexer + registry, the ``run_skill_script`` tool, the allowed-tools
resolver, and the kit assembly (``build_skills_kit``). It declares:

* the session-construction half — the ``session_pack`` factory the kernel
  builder's generic loop calls (band 600, microkernel phase 3); disabling the
  built-in removes the pack (the ``None``-factory special case is gone).

The ``skill`` **control tool** is no longer a separate manifest surface: the
session pack contributes it through ``PackContribution.control_tools`` as a
factory closed over its own merged registry (band 400 — byte order still
pinned by the S0 golden; spec §5: no kit, menu, or registry crosses into
kernel code). Skill *selection* thus rides the same contribution as skill
*content*. ``skill_invocation`` stays a recognized non-plugin activation
name.
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
