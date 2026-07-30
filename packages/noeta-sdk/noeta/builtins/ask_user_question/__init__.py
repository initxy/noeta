"""``ask_user_question`` — the structured-HITL control tool as a built-in plugin.

Control-tool-surface S2, D3 + D8: the manifest declares one ``control_tool``
factory (band 300 — the pre-migration schema band). Its mount also publishes the
answer codec as a mount export (D8), so the kernel driver's ``answer`` path can
decode a submitted answer without importing this built-in. ``ask_user_question``
stays a recognized activation name, so ``plugins=("ask_user_question",)`` still
folds to ``Capabilities.ask_user_question=True`` — the plugin name matching the
activation name does not change activation semantics.
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
