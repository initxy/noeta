"""Memory auto-recall — the ``turn_intake`` reminder provider (impl).

Microkernel M3: moved here from ``noeta.execution.memory``, which keeps the
kernel *seams* only — ``record_memory_index`` (pure activation recording over
``noeta.context.memory``), the generic provider composition, and the
``RecallGoalPrelude`` — while everything that touches a live
:class:`~noeta.builtins.memory.impl.store.MemoryStore` lives here. The host
binds :func:`memory_reminder_provider` to a live store and hands the bound
provider through its ``memory_recall_context`` seam; the kernel driver never
sees the store.

The ``memory`` built-in plugin's manifest declares
:func:`memory_reminder_provider` on the ``reminder_provider`` surface (the
listing / reference declaration); the store binding stays host wiring.
"""

from __future__ import annotations

from typing import Any, Optional

from noeta.builtins.memory.impl.index import (
    RecallHit,
    format_recall_text,
    match_memories_tiered,
)
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
    """
    entries = store.entries()
    summaries = {name: summary for name, summary, _type in entries}
    hits: list[RecallHit] = []
    for name, by_name in match_memories_tiered(entries, text):
        if not by_name:
            hits.append(RecallHit(name=name, text=summaries.get(name, ""), full=False))
            continue
        body = store.read(name)
        if body is not None:
            hits.append(RecallHit(name=name, text=body, full=True))
    return tuple(hits)


def memory_reminder_provider(store: MemoryStore) -> ReminderProvider:
    """The built-in memory auto-recall as a track-A ``reminder_provider`` (D7).

    Re-expresses the former inline recall as a provider on the ``turn_intake``
    seam: given the intake :class:`~noeta.execution.reminders.RecallView`, it
    reads the store NOW (impure — legal because the output is recorded), matches
    against the incoming message text, and returns at most ONE
    ``Reminder(origin="memory")`` carrying the formatted hits (or nothing on a
    miss). Bound to a live ``store`` at wiring time, exactly like the memory
    tools — the ``memory`` built-in plugin *declares* this provider (the listing
    surface), while the store binding stays host wiring.
    """
    def provider(view: RecallView) -> tuple[Reminder, ...]:
        hits = recall_memories(store, view.text)
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
    """The D7 v1 user-message intake seam: retrieve, then ledger both turns.

    Order is load-bearing: retrieval (impure) runs first; the human turn
    lands untagged (role's natural author); hits land as ONE follow-up
    turn tagged ``origin="memory"`` through the Engine's sole
    origin-writer seam. Appending the recall AFTER the user message lets
    the Anthropic adapter merge it into the same wire turn (its
    ``<system-reminder>`` rendering — D4); the ledger itself
    stays provider-neutral. No hits ⇒ exactly the plain
    ``append_user_message`` ledger bytes.

    D7 re-expression: a thin wrapper over the generic ``turn_intake``
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
