"""``memory`` built-in — file store, tools, and auto-recall (impl).

Microkernel M3: ``noeta.tools.memory`` moved to
:mod:`~noeta.builtins.memory.impl.store` and the store-touching half of
``noeta.execution.memory`` moved to
:mod:`~noeta.builtins.memory.impl.recall`; the kernel keeps only the seams
(``record_memory_index``, the generic ``turn_intake`` provider composition,
and the ``RecallGoalPrelude``).

:func:`build_memory_pack` is the **memory factory** the kernel builder
requires (``build_session_inputs(memory_factory=…)``): the SDK host resolves
it through the plugin loader (:func:`noeta.client.parts.default_memory_factory`)
and the kernel's memory stage calls it with the operator-resolved root
(``None`` ⇒ the impl's global default) — the kernel never imports the store.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from noeta.builtins.memory.impl import store as _store_mod
from noeta.builtins.memory.impl.recall import (
    append_user_message_with_recall,
    memory_reminder_provider,
    recall_memories,
)
from noeta.builtins.memory.impl.store import (
    MemoryArchiveTool,
    MemoryReadTool,
    MemorySearchTool,
    MemoryStore,
    MemoryWriteTool,
    build_memory_tools,
    load_memory_store,
)
from noeta.context.memory import MemoryEntries
from noeta.protocols.tool import Tool


__all__ = [
    "MemoryArchiveTool",
    "MemoryReadTool",
    "MemorySearchTool",
    "MemoryStore",
    "MemoryWriteTool",
    "append_user_message_with_recall",
    "build_memory_pack",
    "build_memory_tools",
    "load_memory_store",
    "memory_reminder_provider",
    "recall_memories",
]


def build_memory_pack(
    *, root: Optional[Path] = None
) -> tuple[MemoryStore, MemoryEntries, dict[str, Tool]]:
    """One session's ``(store, entries-snapshot, tools)`` memory kit.

    The kernel builder's ``memory_factory`` injection target. ``root`` is the
    operator-resolved store root (explicit ``memory_dir`` override >
    ``global_memory_dir``); ``None`` falls back to the impl's
    ``DEFAULT_GLOBAL_MEMORY_DIR`` — read LATE off the store module so a test
    pinning ``noeta.builtins.memory.impl.store.DEFAULT_GLOBAL_MEMORY_DIR``
    stays hermetic. The entries snapshot is taken ONCE here: the composer's
    renderer and the pre-loop ``record_memory_index`` share it, so the
    recorded fingerprint always equals what the model saw.
    """
    resolved = root if root is not None else _store_mod.DEFAULT_GLOBAL_MEMORY_DIR
    memory_store = load_memory_store(root=resolved)
    return memory_store, memory_store.entries(), build_memory_tools(memory_store)


def __getattr__(name: str) -> object:
    # ``DEFAULT_GLOBAL_MEMORY_DIR`` must resolve LATE (tests pin the store
    # module's attribute); a from-import here would freeze the unpatched value.
    if name == "DEFAULT_GLOBAL_MEMORY_DIR":
        return _store_mod.DEFAULT_GLOBAL_MEMORY_DIR
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
