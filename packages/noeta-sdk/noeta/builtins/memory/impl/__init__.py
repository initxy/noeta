"""``memory`` built-in — file store, tools, and auto-recall.

The plugin reaches a session only through the generic surfaces: its content
kind and init hook ride a ``session_pack`` contribution and recall rides the
``intake_reminder_providers`` seam, so the kernel never imports the store.

The index resident is frozen per task: the init hook records it first-write-
wins (``refresh=False``), so a ``memory_write`` mid-task never rewrites the
cached prefix. What changed since that snapshot reaches the model through the
``turn_intake`` delta note (:mod:`~noeta.builtins.memory.impl.index_delta`)
instead. The renderer stays pure over ``(folded state, content store)``: it
composes the bytes the ledger's active hash resolves to and never looks at
disk, so the same ledger always composes to the same View.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, cast

from noeta.builtins.memory.impl import store as _store_mod
from noeta.builtins.memory.impl.index import (
    DEFAULT_INDEX_BUDGET_TOKENS,
    MEMORY_BODY_VERSION,
    MEMORY_DRIFT_POLICY,
    MEMORY_INDEX_NAME,
    MEMORY_INDEX_VERSION,
    MEMORY_KIND,
    MemoryEntries,
    memory_content_kind,
    render_memory_index_text,
)
from noeta.builtins.memory.impl.index_delta import (
    memory_index_delta_provider,
)
from noeta.builtins.memory.impl.judge import (
    RecallJudge,
    build_recall_judge,
)
from noeta.builtins.memory.impl.recall import (
    append_user_message_with_recall,
    memory_reminder_provider,
    read_memory_names,
    recall_memories,
    resident_memory_names,
)
from noeta.builtins.memory.impl.store import (
    MEMORY_READ_TOOL_NAME,
    MEMORY_SEARCH_TOOL_NAME,
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
from noeta.protocols.resources import load_markdown
from noeta.protocols.tool import Tool


#: What a read-only pack still offers: the two tools that leave the store as
#: they found it.
READ_TOOLS: frozenset[str] = frozenset(
    {MEMORY_READ_TOOL_NAME, MEMORY_SEARCH_TOOL_NAME}
)

__all__ = [
    "MEMORY_POLICY_READ_ONLY_PROMPT",
    "memory_policy_for",
    "DEFAULT_INDEX_BUDGET_TOKENS",
    "MEMORY_BODY_VERSION",
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
    "RecallJudge",
    "append_user_message_with_recall",
    "build_recall_judge",
    "memory_content_kind",
    "memory_index_delta_provider",
    "build_memory_pack",
    "build_memory_session_pack",
    "build_memory_tools",
    "load_memory_store",
    "memory_reminder_provider",
    "read_memory_names",
    "recall_memories",
    "resident_memory_names",
]


def build_memory_pack(
    *, root: Optional[Path] = None, max_bytes: Optional[int] = None
) -> tuple[MemoryStore, MemoryEntries, dict[str, str], dict[str, Tool]]:
    """One session's ``(store, entries-snapshot, updated dates, tools)`` kit.

    ``root`` is the operator-resolved store root; ``None`` falls back to
    ``DEFAULT_GLOBAL_MEMORY_DIR``, read LATE off the store module so a test
    pinning that attribute stays hermetic. The entries snapshot is the
    **build-time** index fingerprint (what :func:`memory_content_kind` reports
    through the generic ``content_hashes`` seam), and the dates beside it are
    the index's keep order when the store is over budget; what actually enters
    context is whatever the init hook records, which it re-reads from ``store``
    at invocation. ``max_bytes`` is the write tool's page cap (``None`` = none).
    """
    resolved = root if root is not None else _store_mod.DEFAULT_GLOBAL_MEMORY_DIR
    memory_store = load_memory_store(root=resolved)
    entries, updated = memory_store.index_snapshot()
    return (
        memory_store,
        entries,
        updated,
        build_memory_tools(memory_store, max_bytes=max_bytes),
    )


#: The memory-policy fragment the presets append (``noeta.presets``'
#: ``MEMORY_POLICY_PROMPT``), read as a resource — the same bytes, no import
#: edge from a built-in to the presets.
_MEMORY_POLICY_PROMPT = load_markdown("noeta.presets.prompts", "memory-policy", strip=False)
#: The read-only variant (``HostConfig.memory_read_only``): the same opening,
#: naming only the two tools a read-only pack mounts — no write hygiene for
#: tools the model does not have.
MEMORY_POLICY_READ_ONLY_PROMPT = load_markdown(
    __package__, "memory-policy-read-only", strip=False
)


def memory_policy_for(prompt: str, *, read_only: bool) -> str:
    """``prompt`` with its memory-policy fragment matching the pack's mode.

    Applied by the host at Engine build, where the pack's final ``read_only``
    is known, so the compiled spec (and its identity) is the same either way.
    A prompt that ends with the policy fragment (every preset that activates
    ``memory``, and a custom spec that appended it the documented way) gets
    the read-only variant in its place when ``read_only``; any other prompt —
    and every writable pack — is returned unchanged.
    """
    if read_only and prompt.endswith(_MEMORY_POLICY_PROMPT):
        return prompt[: -len(_MEMORY_POLICY_PROMPT)] + MEMORY_POLICY_READ_ONLY_PROMPT
    return prompt


def build_memory_session_pack(ctx: SessionBuildContext) -> PackContribution:
    """The memory pack as a ``session_pack`` contribution.

    Self-gates on the agent's ``memory`` capability flag. The store root
    resolves by precedence: explicit ``memory_dir`` > ``global_memory_dir`` >
    the module default. ``max_bytes`` (the host's ``memory_max_bytes``) caps a
    ``memory_write`` page (fence + body); absent means no cap. ``index_budget_tokens`` (the
    host's ``memory_index_budget_tokens``, derived per model) caps the rendered
    index; absent means :data:`DEFAULT_INDEX_BUDGET_TOKENS`. ``read_only`` (the
    host's ``memory_read_only``) leaves the two tools that change the store out
    of the pack — absent, not refused; the index and recall are the same.
    """
    if not ctx.flag("memory"):
        return EMPTY_CONTRIBUTION
    cfg = ctx.config("memory")
    memory_dir = cfg.get("memory_dir")
    global_memory_dir = cfg.get("global_memory_dir")
    root = memory_dir if memory_dir is not None else global_memory_dir
    store, entries, updated, tools = build_memory_pack(
        root=cast(Optional[Path], root),
        max_bytes=cast(Optional[int], cfg.get("max_bytes")),
    )
    raw_budget = cfg.get("index_budget_tokens")
    if raw_budget is None:
        index_budget = DEFAULT_INDEX_BUDGET_TOKENS
    elif isinstance(raw_budget, int) and not isinstance(raw_budget, bool) and raw_budget > 0:
        index_budget = raw_budget
    else:
        raise ValueError(
            f"memory config: index_budget_tokens must be a positive int, "
            f"got {raw_budget!r}"
        )
    read_only = cfg.get("read_only", False)
    if not isinstance(read_only, bool):
        raise ValueError(
            f"memory config: read_only must be a bool, got {read_only!r}"
        )
    if read_only:
        tools = {name: tool for name, tool in tools.items() if name in READ_TOOLS}
    content_store = ctx.content_store

    def _init(rec: SessionRecorder) -> None:
        """Pre-loop activation of the index resident — first write wins.

        The store is scanned HERE, not closed over from build time: the
        Engine is cached across tasks, so a build-time snapshot could hand a
        new task a stale index. The recorded ``ref.hash`` is the
        rendered-index sha256, so the ledger fully determines the composed
        index and the renderer stays pure.

        ``refresh=False`` (2026-09-25): once the task's index is active, a
        rerun — at a subtask drain, at each new goal — appends nothing even
        when the store changed. A refreshed index would rewrite the cached
        prefix from the index block on for every turn after a
        ``memory_write`` (in a long session, the whole conversation). The
        2026-08-04 D9 guarantee — a page written mid-task reaches the model
        on the next turn — is kept by the ``turn_intake`` delta note
        (:func:`memory_index_delta_provider`) instead. A store empty at seed
        records the index at the first turn it has pages. An empty store
        leaves the ledger untouched.
        """
        live_entries, live_updated = store.index_snapshot()
        if not live_entries:
            return
        body = render_memory_index_text(
            live_entries,
            budget_tokens=index_budget,
            updated=live_updated,
        ).encode("utf-8")
        ref = content_store.put(body, media_type="text/markdown")
        rec.record_content(
            kind=MEMORY_KIND,
            name=MEMORY_INDEX_NAME,
            version=MEMORY_INDEX_VERSION,
            ref=ref,
            policy=MEMORY_DRIFT_POLICY,
            refresh=False,
        )

    return PackContribution(
        tools=tools,
        content_kinds=(
            # Kind band 200 — after skill, before instructions.
            ContentKindContribution(
                200,
                memory_content_kind(
                    entries,
                    budget_tokens=index_budget,
                    updated=updated,
                ),
            ),
        ),
        init=_init,
    )


def __getattr__(name: str) -> object:
    # ``DEFAULT_GLOBAL_MEMORY_DIR`` must resolve LATE (tests pin the store
    # module's attribute); a from-import here would freeze the unpatched value.
    if name == "DEFAULT_GLOBAL_MEMORY_DIR":
        return _store_mod.DEFAULT_GLOBAL_MEMORY_DIR
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
