"""``memory`` built-in — file store, tools, and auto-recall.

The plugin reaches a session only through the generic surfaces: its content
kind and init hook ride a ``session_pack`` contribution and recall rides the
``intake_reminder_providers`` seam, so the kernel never imports the store.
The entries snapshot is taken once per session and shared by the composer's
renderer and the init hook, which is what keeps the recorded fingerprint
equal to what the model saw.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, cast

from noeta.builtins.memory.impl import store as _store_mod
from noeta.builtins.memory.impl.index import (
    MEMORY_DRIFT_POLICY,
    MEMORY_INDEX_NAME,
    MEMORY_INDEX_VERSION,
    MEMORY_KIND,
    MemoryEntries,
    memory_content_kind,
    render_memory_index_text,
)
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
from noeta.execution.session_pack import (
    EMPTY_CONTRIBUTION,
    ContentKindContribution,
    PackContribution,
    SessionBuildContext,
    SessionRecorder,
)
from noeta.protocols.tool import Tool


__all__ = [
    "MEMORY_DRIFT_POLICY",
    "MEMORY_INDEX_NAME",
    "MEMORY_INDEX_VERSION",
    "MEMORY_KIND",
    "MemoryArchiveTool",
    "MemoryEntries",
    "MemoryReadTool",
    "MemorySearchTool",
    "MemoryStore",
    "MemoryWriteTool",
    "append_user_message_with_recall",
    "memory_content_kind",
    "build_memory_pack",
    "build_memory_session_pack",
    "build_memory_tools",
    "load_memory_store",
    "memory_reminder_provider",
    "recall_memories",
]


def build_memory_pack(
    *, root: Optional[Path] = None
) -> tuple[MemoryStore, MemoryEntries, dict[str, Tool]]:
    """One session's ``(store, entries-snapshot, tools)`` memory kit.

    ``root`` is the operator-resolved store root; ``None`` falls back to
    ``DEFAULT_GLOBAL_MEMORY_DIR``, read LATE off the store module so a test
    pinning that attribute stays hermetic. The entries snapshot is taken ONCE
    here so the renderer and the init hook cannot disagree.
    """
    resolved = root if root is not None else _store_mod.DEFAULT_GLOBAL_MEMORY_DIR
    memory_store = load_memory_store(root=resolved)
    return memory_store, memory_store.entries(), build_memory_tools(memory_store)


def build_memory_session_pack(ctx: SessionBuildContext) -> PackContribution:
    """The memory pack as a ``session_pack`` contribution.

    Self-gates on the agent's ``memory`` capability flag. The store root
    resolves by precedence: explicit ``memory_dir`` > ``global_memory_dir`` >
    the module default.
    """
    if not ctx.flag("memory"):
        return EMPTY_CONTRIBUTION
    cfg = ctx.config("memory")
    memory_dir = cfg.get("memory_dir")
    global_memory_dir = cfg.get("global_memory_dir")
    root = memory_dir if memory_dir is not None else global_memory_dir
    store, entries, tools = build_memory_pack(root=cast(Optional[Path], root))
    content_store = ctx.content_store

    def _init(rec: SessionRecorder) -> None:
        """Pre-loop activation of the index resident.

        Serialises the SAME entries the composer's renderer holds, so the
        ledger fully determines the composed index and ``ref.hash`` is the
        rendered-index sha256. Empty entries leave the ledger untouched.
        """
        if not entries:
            return
        body = render_memory_index_text(entries).encode("utf-8")
        ref = content_store.put(body, media_type="text/markdown")
        rec.record_content(
            kind=MEMORY_KIND,
            name=MEMORY_INDEX_NAME,
            version=MEMORY_INDEX_VERSION,
            ref=ref,
            policy=MEMORY_DRIFT_POLICY,
        )

    return PackContribution(
        tools=tools,
        content_kinds=(
            # Kind band 200 — after skill, before instructions.
            ContentKindContribution(200, memory_content_kind(entries)),
        ),
        init=_init,
    )


def __getattr__(name: str) -> object:
    # ``DEFAULT_GLOBAL_MEMORY_DIR`` must resolve LATE (tests pin the store
    # module's attribute); a from-import here would freeze the unpatched value.
    if name == "DEFAULT_GLOBAL_MEMORY_DIR":
        return _store_mod.DEFAULT_GLOBAL_MEMORY_DIR
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
