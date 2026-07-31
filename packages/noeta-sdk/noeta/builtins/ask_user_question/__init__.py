"""``ask_user_question`` — the structured human-in-the-loop control tool.

The manifest declares one ``control_tool`` factory at band 300, which fixes
this tool's slot in the builder's mount loop so the cross-plugin collision
check iterates in a stable order; the schema and routing byte-orders come from
the mount's own bands. The mount also publishes the answer codec, so the
kernel driver's ``answer`` path decodes a submitted answer without importing
this built-in.
"""

from __future__ import annotations

from noeta.builtins._declare import c
from noeta.client.plugin_manifest import PluginManifest


MANIFEST = PluginManifest(
    name="ask_user_question",
    requires_noeta=">=0.4",
    contributions=(
        c(
            "control_tool",
            "ask_user_question",
            "noeta.builtins.ask_user_question.impl:build_ask_user_question_control_tool",
            priority=300,
        ),
    ),
)
