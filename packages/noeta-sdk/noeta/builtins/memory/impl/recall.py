"""Memory auto-recall — the ``turn_intake`` reminder provider (impl).

Everything that touches a live
:class:`~noeta.builtins.memory.impl.store.MemoryStore` lives here; the kernel
has no memory module at all (final form). The host binds
:func:`memory_reminder_provider` to a live store and prepends the bound
provider to the ONE generic ``intake_reminder_providers`` seam the driver
reads; the kernel driver never sees the store.

A recalled **body** enters a task once: a tier-1 hit rides as a
``ResidentActivation`` of the ``memory`` content kind (activate-once, so the
same name on a later goal of the same task costs nothing, and the resident
survives compaction by re-hanging after the summary), while pointers — tier-2
hits, the ``related`` neighbours of a tier-1 hit, judge picks, over-budget
bodies — ride the ``origin="memory"`` follow-up turn as before. A name already resident in the task is silent in both tiers —
and so is a memory the model loaded itself with ``memory_read`` while that
read still sits in the history the model sees (the seam's
``RecallView.visible_history``, which stops at the compaction boundary): the
page is already in context as a tool result, so recall must neither inject the
body again nor point at a page the model has already read. Once a compaction
summary swallows the read, the page is recallable again.

The ``memory`` built-in plugin's manifest declares
:func:`memory_reminder_provider` on the ``reminder_provider`` surface (the
listing / reference declaration); the store binding stays host wiring.
"""

from __future__ import annotations

from typing import Any, Collection, Iterable, Mapping, Optional

from noeta.builtins.memory.impl.index import (
    DEFAULT_RECALL_MAX_HITS,
    MEMORY_BODY_VERSION,
    MEMORY_DRIFT_POLICY,
    MEMORY_INDEX_NAME,
    MEMORY_KIND,
    RECALL_BODY_MAX_BYTES,
    RECALL_TOTAL_MAX_BYTES,
    RecallHit,
    format_recall_text,
    match_memories_tiered,
)
from noeta.builtins.memory.impl.judge import RecallJudge
from noeta.execution.reminders import (
    IntakeItem,
    RecallView,
    Reminder,
    ReminderProvider,
    ResidentActivation,
    record_intake_reminders,
)
from noeta.protocols.messages import (
    Block,
    MessageOrigin,
    ToolResultBlock,
    ToolUseBlock,
)
from noeta.builtins.memory.impl.store import MEMORY_READ_TOOL_NAME, MemoryStore


__all__ = [
    "append_user_message_with_recall",
    "memory_reminder_provider",
    "read_memory_names",
    "recall_memories",
    "resident_memory_names",
]


def resident_memory_names(task_state: Any) -> frozenset[str]:
    """The memory names already active as residents of this task.

    Read off the generic activation map ``TaskState.active_content`` under the
    ``memory`` kind, minus the index (a resident of the kind, never a memory).
    ``None`` / a state without the map (a seam that cannot supply it, a stub
    task) reads as nothing resident.
    """
    active = getattr(task_state, "active_content", None)
    if not isinstance(active, Mapping):
        return frozenset()
    names = active.get(MEMORY_KIND, {})
    return frozenset(n for n in names if n != MEMORY_INDEX_NAME)


def read_memory_names(history: Iterable[Any]) -> frozenset[str]:
    """The memories the model loaded itself with ``memory_read`` in ``history``.

    Pairs each ``memory_read`` ``ToolUseBlock`` with its ``ToolResultBlock``
    on ``call_id`` and keeps the name only when the read succeeded — a failed
    read (no such memory, an invalid name) loaded nothing, so the name stays
    recallable. Called on the seam's ``RecallView.visible_history`` (the
    rolling history past the compaction boundary), never on the whole ledger:
    a read whose result a summary has swallowed is a page the model no longer
    has, and recall must serve it again. A truncated read still counts — what
    the model saw is longer than any body recall would inject.
    """
    requested: dict[str, str] = {}
    names: set[str] = set()
    for message in history:
        for block in getattr(message, "content", ()):
            if (
                isinstance(block, ToolUseBlock)
                and block.tool_name == MEMORY_READ_TOOL_NAME
                and isinstance(block.arguments, Mapping)
                and isinstance(block.arguments.get("name"), str)
            ):
                requested[block.call_id] = block.arguments["name"]
            elif (
                isinstance(block, ToolResultBlock)
                and block.success
                and block.call_id in requested
            ):
                names.add(requested[block.call_id])
    return frozenset(names)


def recall_memories(
    store: MemoryStore,
    text: str,
    *,
    resident: Collection[str] = (),
    exclude: Collection[str] = (),
    judge: Optional[RecallJudge] = None,
) -> tuple[RecallHit, ...]:
    """The injector's impure half: read the store NOW, match, load text.

    Reading at call time (not from a wiring-time snapshot) means a
    memory written mid-session by ``memory_write`` is immediately
    recallable — legal because this runs before anything enters the
    ledger. Returns :class:`RecallHit` values best first (the matcher's
    order), at most ``DEFAULT_RECALL_MAX_HITS`` of them whatever their
    source; unreadable hits are skipped rather than crashing the turn.

    **Only a tier-1 hit costs a body.** A name match is the user's own
    words naming the memory, so the body is loaded and injected as it
    always was. A tier-2 hit — one shared name word, or prose overlap
    against the summary — carries just its index summary, and the model pays
    for the body only if it calls ``memory_read``. A memory whose file has gone missing is dropped
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

    **And a resident is silent.** ``resident`` names the memories already in
    context: the residents of this task (:func:`resident_memory_names`) and
    the pages the model loaded itself with ``memory_read``
    (:func:`read_memory_names`). They are out of every candidate set, so they
    neither take a hit slot nor spend budget — the body is already there, as
    a resident placed by its anchor and re-hung across compaction, or as the
    tool result the model asked for. ``exclude`` is the host's standing
    version of the same thing (``HostConfig.recall_exclude``): a page it
    rides into context by its own means. Both still count when the matcher
    decides which name tokens are common, so what is in context never changes
    the tier of another page. A memory that happens to be named like the
    index resident can never ride full (it would overwrite the index's
    activation), so it degrades to a pointer.

    **A named page brings its neighbours.** For each tier-1 hit, in hit order,
    the pages its fence lists under ``related`` join as pointers while the cap
    has room: one hop, never a body, skipping names that do not exist, are
    already hits, or are out of the candidate set. Links are followed from
    tier 1 alone — a pointer is already a guess, and a guess's neighbour is
    noise.

    **The judge fills in when nothing was named.** ``judge`` (the semantic
    fallback) is consulted only when tier 1 is empty AND the pointers have not
    already filled the cap — a tier-1 hit never spends the call, and neither
    does a turn whose picks would be dropped. It chooses among the pages that
    are not already pointers, and its picks ride after them as pointers too.
    """
    entries = store.entries()
    skip = frozenset(resident) | frozenset(exclude)
    summaries = {name: summary for name, summary, _type, _kw in entries}

    def pointer(name: str) -> RecallHit:
        return RecallHit(name=name, text=summaries.get(name, ""), full=False)

    hits: list[RecallHit] = []
    named: list[str] = []
    spent = 0
    for name, by_name in match_memories_tiered(entries, text, exclude=skip):
        if by_name and name != MEMORY_INDEX_NAME:
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
            else:
                hits.append(pointer(name))
        else:
            hits.append(pointer(name))
        if by_name:
            named.append(name)

    taken = {hit.name for hit in hits}
    room = DEFAULT_RECALL_MAX_HITS - len(hits)
    if named:
        for source in named:
            for other in store.related(source):
                if room <= 0:
                    break
                if other in taken or other in skip or other not in summaries:
                    continue
                hits.append(pointer(other))
                taken.add(other)
                room -= 1
    elif judge is not None and room > 0:
        candidates = tuple(
            e for e in entries if e[0] not in skip and e[0] not in taken
        )
        if candidates:
            # Held to the candidates it was shown: the bound judge already
            # drops a hallucinated slug, but the seam takes any callable, and
            # an excluded name must stay silent whoever picks it.
            offered = {e[0] for e in candidates}
            picks = dict.fromkeys(
                n for n in judge(candidates, text) if n in offered
            )
            hits.extend(pointer(n) for n in list(picks)[:room])
    return tuple(hits)


def memory_reminder_provider(
    store: MemoryStore,
    judge: Optional[RecallJudge] = None,
    *,
    exclude: Collection[str] = (),
) -> ReminderProvider:
    """The built-in memory auto-recall as a ``reminder_provider``.

    A provider on the ``turn_intake``
    seam: given the intake :class:`~noeta.execution.reminders.RecallView`, it
    reads the store NOW (impure — legal because the output is recorded), matches
    against the incoming message text, and returns one
    ``ResidentActivation`` per new tier-1 body (the ``memory`` kind,
    activate-once per task — the seam records it right after the goal, and
    the kind's renderer places it) followed by at most ONE
    ``Reminder(origin="memory")`` carrying the pointer hits (or nothing on a
    miss). Names already resident in the task (``view.task_state``'s
    activation map) are silent in both tiers, which is what makes a
    long-lived task's eleventh goal cost nothing for a memory its first goal
    already recalled; so are the pages the model loaded itself with
    ``memory_read`` while that read is still in ``view.visible_history``, so
    a goal that names a page the model just read neither injects the body a
    second time nor points at it. Bound to a live ``store`` at wiring time, exactly like
    the memory tools — the ``memory`` built-in plugin *declares* this provider
    (the listing surface), while the store binding stays host wiring.

    ``judge`` (host-wired from ``Options.recall_model``) is the semantic
    fallback: consulted ONLY when the text named no page and the lexical
    pointers leave room under the cap — a tier-1 hit never spends the call —
    and its picks ride as tier-2 pointers, because a judge is a guess and a
    guess is worth a pointer, not a body (:func:`recall_memories` holds the
    rule). A judged name whose file has meanwhile vanished degrades to a
    bare-name pointer, same as any tier-2 hit with no summary.

    ``exclude`` (host-wired from ``HostConfig.recall_exclude``) names the
    pages recall never surfaces, in any tier: ones the host already rides
    into context by its own means. The index still lists them and
    ``memory_read`` still reads them.
    """
    def provider(view: RecallView) -> tuple[IntakeItem, ...]:
        resident = resident_memory_names(view.task_state) | read_memory_names(
            getattr(view, "visible_history", ())
        )
        hits = recall_memories(
            store, view.text, resident=resident, exclude=exclude, judge=judge
        )
        if not hits:
            return ()
        items: list[IntakeItem] = [
            ResidentActivation(
                kind=MEMORY_KIND,
                name=hit.name,
                body=hit.text.encode("utf-8"),
                version=MEMORY_BODY_VERSION,
                policy=MEMORY_DRIFT_POLICY,
            )
            for hit in hits
            if hit.full
        ]
        pointers = tuple(hit for hit in hits if not hit.full)
        if pointers:
            items.append(
                Reminder(text=format_recall_text(pointers), origin="memory")
            )
        return tuple(items)

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
    """The v1 user-message intake seam: retrieve, then ledger the turn(s).

    Order is load-bearing: retrieval (impure) runs first; the human turn
    lands untagged (role's natural author); each new tier-1 body lands as a
    ``memory``-kind resident activation (``Engine.record_content``), and
    pointer hits as ONE follow-up turn tagged ``origin="memory"`` through
    the Engine's sole origin-writer seam. Recording the residents right
    after the user message anchors them there, and appending the pointer
    turn after that lets the Anthropic adapter merge it into the same wire
    turn (its ``<system-reminder>`` rendering); the ledger itself stays
    provider-neutral. No hits ⇒ exactly the plain ``append_user_message``
    ledger bytes.

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
