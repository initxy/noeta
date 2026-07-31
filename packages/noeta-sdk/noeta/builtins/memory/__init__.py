"""``memory`` — tool pack, policy prompt fragment, recorded auto-recall.

Only the durable identity-plane pieces can be declared here; the index
resident and the recall provider are built per-store by a factory, so their
manifest entries name the factory and the live store binding stays host
wiring.
"""

from __future__ import annotations

from noeta.builtins._declare import c
from noeta.client.plugin_manifest import PluginManifest


MANIFEST = PluginManifest(
    name="memory",
    requires_noeta=">=0.4",
    contributions=(
        c(
            "tool",
            "memory_write",
            "noeta.builtins.memory.impl.store:MemoryWriteTool",
        ),
        c(
            "tool",
            "memory_read",
            "noeta.builtins.memory.impl.store:MemoryReadTool",
        ),
        c(
            "tool",
            "memory_search",
            "noeta.builtins.memory.impl.store:MemorySearchTool",
        ),
        c(
            "tool",
            "memory_archive",
            "noeta.builtins.memory.impl.store:MemoryArchiveTool",
        ),
        c(
            "prompt_fragment",
            "memory-policy",
            "noeta.presets:MEMORY_POLICY_PROMPT",
        ),
        # The ref points at the factory, not a provider: the host binds it to
        # a live store at wiring time, so this entry is the listing surface.
        c(
            "reminder_provider",
            "memory-recall",
            "noeta.builtins.memory.impl.recall:memory_reminder_provider",
            seams=["turn_intake"],
        ),
        # Band 300 places the pack directly after the base fs/web packs; it
        # self-gates on the agent's ``memory`` capability flag.
        c(
            "session_pack",
            "memory",
            "noeta.builtins.memory.impl:build_memory_session_pack",
            priority=300,
        ),
    ),
)
