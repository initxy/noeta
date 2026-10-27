"""Cross-session memory: a directory of markdown files, four tools, the
index, auto-recall and the consolidation curator."""

from noeta.plugins.memory.extension import (
    CONSOLIDATION_AGENT_NAME,
    MEMORY_POLICY_PROMPT,
    MEMORY_POLICY_READ_ONLY_PROMPT,
    Memory,
    consolidation_agent,
    with_memory_policy,
)
from noeta.plugins.memory.store import DEFAULT_GLOBAL_MEMORY_DIR, MemoryStore

__all__ = [
    "CONSOLIDATION_AGENT_NAME",
    "DEFAULT_GLOBAL_MEMORY_DIR",
    "MEMORY_POLICY_PROMPT",
    "MEMORY_POLICY_READ_ONLY_PROMPT",
    "Memory",
    "MemoryStore",
    "consolidation_agent",
    "with_memory_policy",
]
