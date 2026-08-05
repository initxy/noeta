"""Memory auto-recall — the ``turn_intake`` reminder provider (impl).

Everything that touches a live
:class:`~noeta.builtins.memory.impl.store.MemoryStore` lives here; the kernel
has no memory module at all (final form). The host binds
:func:`memory_reminder_provider` to a live store and prepends the bound
provider to the ONE generic ``intake_reminder_providers`` seam the driver
reads; the kernel driver never sees the store.

The ``memory`` built-in plugin's manifest declares
:func:`memory_reminder_provider` on the ``reminder_provider`` surface (the
listing / reference declaration); the store binding stays host wiring.
"""

from __future__ import annotations

from typing import Any, Optional

from noeta.builtins.memory.impl.index import (
    RECALL_BODY_MAX_BYTES,
    RECALL_TOTAL_MAX_BYTES,
    RecallHit,
    format_recall_text,
    match_memories_tiered,
)
from noeta.builtins.memory.impl.judge import RecallJudge
from noeta.execution.reminders import (
    RecallView,
    Reminder,
    ReminderProvider,
    record_intake_reminders,
)
from noeta.protocols.messages import Block, MessageOrigin
from noeta.builtins.memory.impl.store import MemoryStore


__all__ = [
    "append_user_message_with_recall",
    "memory_reminder_provider",
    "recall_memories",
]


def recall_memories(store: MemoryStore, text: str) -> tuple[RecallHit, ...]:
    """The injector's impure half: read the store NOW, match, load text.

    Reading at call time (not from a wiring-time snapshot) means a
    memory written mid-session by ``memory_write`` is immediately
    recallable — legal because this runs before anything enters the
    ledger. Returns :class:`RecallHit` values in index order; unreadable
    hits are skipped rather than crashing the turn.

    **Only a tier-1 hit costs a body.** A name match is the user's own
    words naming the memory, so the body is loaded and injected as it
    always was. A tier-2 hit — prose overlap against the summary — carries
    just its index summary, and the model pays for the body only if it
    calls ``memory_read``. A memory whose file has gone missing is dropped
    from either tier; a tier-2 entry with an empty summary still rides as
    a bare name, which is enough to read by.

    **And a body is budgeted.** Recall is uninvited context: the model did
    not ask for these bytes and cannot decline them, so a tier-1 hit rides
    inline only while it fits ``RECALL_BODY_MAX_BYTES`` on its own AND
    inside the turn's remaining ``RECALL_TOTAL_MAX_BYTES``. Over either
    line it degrades WHOLE to its index line — the same pointer a tier-2
    hit rides, naming ``memory_read`` for the rest — because half a memory
    reads exactly like a complete one. Budget is spent in hit order, so the
    high-confidence early hits keep their bodies and the tail degrades.
    """
    entries = store.entries()
    summaries = {name: summary for name, summary, _type, _kw in entries}
    hits: list[RecallHit] = []
    spent = 0
    for name, by_name in match_memories_tiered(entries, text):
        if by_name:
            body = store.read(name)
            if body is None:
                continue
            size = len(body.encode("utf-8"))
            if (
                size <= RECALL_BODY_MAX_BYTES
                and spent + size <= RECALL_TOTAL_MAX_BYTES
            ):
                spent += size
                hits.append(RecallHit(name=name, text=body, full=True))
                continue
        hits.append(RecallHit(name=name, text=summaries.get(name, ""), full=False))
    return tuple(hits)


def memory_reminder_provider(
    store: MemoryStore, judge: Optional[RecallJudge] = None
) -> ReminderProvider:
    """The built-in memory auto-recall as a track-A ``reminder_provider``.

    A provider on the ``turn_intake``
    seam: given the intake :class:`~noeta.execution.reminders.RecallView`, it
    reads the store NOW (impure — legal because the output is recorded), matches
    against the incoming message text, and returns at most ONE
    ``Reminder(origin="memory")`` carrying the formatted hits (or nothing on a
    miss). Bound to a live ``store`` at wiring time, exactly like the memory
    tools — the ``memory`` built-in plugin *declares* this provider (the listing
    surface), while the store binding stays host wiring.

    ``judge`` (host-wired from ``Options.recall_model``) is the semantic
    fallback: consulted ONLY when the lexical pass returns nothing — a
    lexical hit never spends the call — and its picks ride as tier-2
    pointers, because a judge is a guess and a guess is worth a pointer,
    not a body. A judged name whose file has meanwhile vanished degrades
    to a bare-name pointer, same as any tier-2 hit with no summary.
    """
    def provider(view: RecallView) -> tuple[Reminder, ...]:
        hits = recall_memories(store, view.text)
        if not hits and judge is not None:
            entries = store.entries()
            if entries:
                summaries = {
                    name: summary for name, summary, _t, _k in entries
                }
                hits = tuple(
                    RecallHit(name=name, text=summaries.get(name, ""), full=False)
                    for name in judge(entries, view.text)
                )
        if not hits:
            return ()
        return (Reminder(text=format_recall_text(hits), origin="memory"),)

    return provider


def append_user_message_with_recall(
    engine: Any,
    task: Any,
    *,
    content: list[Block],
    lease_id: str,
    store: MemoryStore,
    trace_id: Optional[str] = None,
    origin: Optional[MessageOrigin] = None,
) -> Any:
    """The v1 user-message intake seam: retrieve, then ledger both turns.

    Order is load-bearing: retrieval (impure) runs first; the human turn
    lands untagged (role's natural author); hits land as ONE follow-up
    turn tagged ``origin="memory"`` through the Engine's sole
    origin-writer seam. Appending the recall AFTER the user message lets
    the Anthropic adapter merge it into the same wire turn (its
    ``<system-reminder>`` rendering); the ledger itself
    stays provider-neutral. No hits ⇒ exactly the plain
    ``append_user_message`` ledger bytes.

    A thin wrapper over the generic ``turn_intake``
    recording seam (:func:`~noeta.execution.reminders.record_intake_reminders`)
    driven by a single provider — the built-in memory recall
    (:func:`memory_reminder_provider`). The recording order (message, then the
    ``origin="memory"`` follow-up) is preserved verbatim, so the ledger bytes
    are byte-identical to the pre-redesign inline seam.

    ``origin`` is forwarded to the incoming turn's append (the
    driver's ``goal_origin`` passthrough — e.g. an MCP-prompt-expanded goal
    arrives ``origin="system"``); the recall turn's ``origin="memory"`` tag
    is this seam's own and never varies. ``None`` (a human-typed goal)
    keeps the human turn's bytes identical to the plain append.
    """
    return record_intake_reminders(
        engine,
        task,
        content=content,
        lease_id=lease_id,
        providers=(memory_reminder_provider(store),),
        trace_id=trace_id,
        origin=origin,
    )
