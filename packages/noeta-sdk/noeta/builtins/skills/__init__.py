"""``skills`` — the skill subsystem built-in.

The implementation lives in the sibling ``impl/`` package (microkernel phase
2a): indexer + registry, the ``run_skill_script`` tool, the allowed-tools
resolver, and the kit assembly (``build_skills_kit``). Two things it declares:

* the session-construction half — the ``session_pack`` factory the kernel
  builder's generic loop calls (band 600, microkernel phase 3); disabling the
  built-in removes the pack (the ``None``-factory special case is gone); and
* the ``skill`` **control tool** — a ``control_tool`` contribution (band 400,
  control-tool-surface S2b) whose factory reproduces the pre-migration internal
  ``_skill_mount``, self-gating on ``skill_invocation`` + a non-empty indexed
  menu. Skill *selection* thus rides the same plugin as skill *content*; the
  ``skill`` schema + translate + ``skill.md`` moved beside the impl (byte-order
  pinned by the S0 golden). ``skill_invocation`` stays a recognized non-plugin
  activation name.
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
        c(
            "control_tool",
            "skill",
            "noeta.builtins.skills.impl:build_skills_control_tool",
            priority=400,
        ),
    ),
)
