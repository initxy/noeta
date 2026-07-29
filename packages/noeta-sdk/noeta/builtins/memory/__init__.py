"""``memory`` — tool pack + policy prompt fragment + recorded auto-recall.

Tool pack plus the policy prompt fragment (D10: ``MEMORY_POLICY_PROMPT``
migrates onto the ``prompt_fragment`` surface). The memory index
content-channel resident is built per-store by a factory, so it stays
host-wired; the two durable, identity-plane pieces (tools + policy prompt) are
declared here.
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
        # Track A (D7): the memory auto-recall re-expressed as a recorded
        # ``reminder_provider`` on the ``turn_intake`` seam. The ref points at the
        # factory (bound to a live store at wiring time, like the memory tools);
        # this declaration is the listing / reference surface.
        c(
            "reminder_provider",
            "memory-recall",
            "noeta.builtins.memory.impl.recall:memory_reminder_provider",
            seams=["turn_intake"],
        ),
    ),
)
