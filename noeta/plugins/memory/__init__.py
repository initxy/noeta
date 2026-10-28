"""Auto memory: a per-project directory of markdown files the model writes
with ``Write`` / ``Edit``, indexed by ``MEMORY.md``."""

from noeta.plugins.memory.extension import (
    INDEX_FILE,
    MEMORY_SECTION,
    Memory,
    load_index,
    memory_dir_for,
)

__all__ = ["INDEX_FILE", "MEMORY_SECTION", "Memory", "load_index", "memory_dir_for"]
