"""``memory`` built-in — file store, tools, and auto-recall (impl).

Microkernel M3: ``noeta.tools.memory`` moved to
:mod:`~noeta.builtins.memory.impl.store` and the store-touching half of
``noeta.execution.memory`` moved to
:mod:`~noeta.builtins.memory.impl.recall`; the kernel keeps only the seams
(``record_memory_index``, the generic ``turn_intake`` provider composition,
and the ``RecallGoalPrelude``).

:func:`build_memory_session_pack` is this plugin's ``session_pack``
contribution (microkernel phase 3): the SDK host resolves it from the
manifest (``noeta.client.parts.default_session_packs``) and the kernel
builder's generic pack loop calls it — the kernel never imports the store.
:func:`build_memory_pack` remains the underlying kit constructor the pack
(and the consolidation path) build from.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, cast

from noeta.builtins.memory.impl import store as _store_mod
from noeta.builtins.memory.impl.index import (
    build_memory_index_kit,
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
from noeta.context.memory import (
    MEMORY_DRIFT_POLICY,
    MEMORY_INDEX_NAME,
    MEMORY_INDEX_VERSION,
    MEMORY_KIND,
    MemoryEntries,
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
    "MemoryArchiveTool",
    "MemoryReadTool",
    "MemorySearchTool",
    "MemoryStore",
    "MemoryWriteTool",
    "append_user_message_with_recall",
    "build_memory_index_kit",
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


def build_memory_session_pack(ctx: SessionBuildContext) -> PackContribution:
    """The memory pack as a ``session_pack`` contribution (microkernel phase 3).

    The manifest-declared factory (band 300). Self-gates on the agent's
    ``memory`` capability flag; the store root comes from this plugin's own
    config entry (explicit ``memory_dir`` override > ``global_memory_dir`` >
    the impl's global default — the same precedence the kernel's memory stage
    applied). The store handle and the load-time index snapshot ride the
    exports so the composer's renderer and the pre-loop
    ``record_memory_index`` share one snapshot, one fingerprint.
    """
    if not ctx.flag("memory"):
        return EMPTY_CONTRIBUTION
    cfg = ctx.config("memory")
    memory_dir = cfg.get("memory_dir")
    global_memory_dir = cfg.get("global_memory_dir")
    root = memory_dir if memory_dir is not None else global_memory_dir
    store, entries, tools = build_memory_pack(root=cast(Optional[Path], root))
    content_store = ctx.content_store
    # The index resident (kind band 200 — after skill, before instructions):
    # rendered from the SAME entries snapshot the exports carry, so the
    # composed bytes and the recorded fingerprint share one source.
    index_kit = build_memory_index_kit()

    def _init(rec: SessionRecorder) -> None:
        """Pre-loop activation of the index resident (spec §4.5).

        Serialises the SAME entries the composer's renderer holds into the
        ContentStore and records the resulting ref, so the ledger fully
        determines the composed index (law 2): ``ref.hash`` equals the
        rendered-index sha256 the ``evolving`` fingerprint always carried, so
        the ``ContextContentRecorded`` payload matches the retired
        ``record_memory_index`` call (the envelope now attributes
        ``actor="plugin:memory"``). Empty entries leave the ledger untouched.
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
            ContentKindContribution(200, index_kit.content_kind(entries)),
        ),
        init=_init,
        memory_store=store,
        memory_entries=entries,
    )


def __getattr__(name: str) -> object:
    # ``DEFAULT_GLOBAL_MEMORY_DIR`` must resolve LATE (tests pin the store
    # module's attribute); a from-import here would freeze the unpatched value.
    if name == "DEFAULT_GLOBAL_MEMORY_DIR":
        return _store_mod.DEFAULT_GLOBAL_MEMORY_DIR
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
