"""``skills`` — the skill subsystem built-in.

The implementation lives in the sibling ``impl/`` package (microkernel phase
2a): indexer + registry, the ``run_skill_script`` tool, the allowed-tools
resolver, and the builder's ``skills_factory`` body (``build_skills_kit``).
The manifest stays deliberately contribution-free: skill *selection* is the
kernel's ``skill`` control tool (gated by the ``skill_invocation`` capability
flag), skill *content* is host/workspace material — neither is an identity
``tool`` contribution, and parity pins the compiled bytes. The catalogue
entry exists so the activation name resolves and the capability is listable.
"""

from __future__ import annotations

from noeta.client.plugin_manifest import PluginManifest


MANIFEST = PluginManifest(name="skills", requires_noeta=">=0.4")
