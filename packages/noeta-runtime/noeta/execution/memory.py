"""Memory host glue — activation recording + the recall seam (D5/D6).

The execution-layer counterpart of ``noeta.context.memory`` (pure index
pieces) and ``noeta.tools.memory`` (store + tools), mirroring how
``noeta.execution.skills`` glues the skill subsystem to the Engine:

* :func:`record_memory_index` — write-side activation: emit ONE
  ``ContextContentRecorded`` (kind ``memory``, policy ``evolving``) so
  fold flips the index resident on in ``TaskState.active_content``.
  Nothing here touches the runtime — the event type, its fold and the
  ``ContentHashesFn`` seam all landed generically in issue 02.
* :func:`append_user_message_with_recall` — the D6 v1 injection seam
  (user-message intake). The injector runs BEFORE anything enters the
  ledger and is allowed to be impure (it reads the store right then);
  its output lands as an ordinary message with ``origin="memory"``
  through the Engine's sole origin-writer seam (D4). A resume
  folds that message back from the ledger and NEVER re-runs retrieval —
  the composer stays a pure function of folded state.

v1 keeps the seam as a plain function the host calls instead of an
"injector" interface — rule of two: the second/third reminder use case
(tool-result intake, task wake) will shape the real abstraction.
Product wiring (presets / noeta-agent) is issue 07's business.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Optional, Sequence

from noeta.context.memory import (
    MEMORY_DRIFT_POLICY,
    MEMORY_INDEX_NAME,
    MEMORY_INDEX_VERSION,
    MEMORY_KIND,
    MemoryEntries,
    RecallHit,
    format_recall_text,
    match_memories_tiered,
    memory_index_hash,
)
from noeta.core.engine import Engine
from noeta.core.fold import apply_event
from noeta.execution.reminders import (
    Reminder,
    ReminderProvider,
    RecallView,
    record_intake_reminders,
)
from noeta.protocols.content_store import ContentStore
from noeta.protocols.decisions import TaskStatePatch
from noeta.protocols.event_log import EventLogWriter
from noeta.protocols.events import ContextContentRecordedPayload
from noeta.protocols.messages import Block, MessageOrigin, TextBlock
from noeta.protocols.task import Task
from noeta.tools.memory import MemoryStore


__all__ = [
    "DEFAULT_GLOBAL_MEMORY_DIR",
    "RecallGoalPrelude",
    "append_user_message_with_recall",
    "intake_providers",
    "load_memory_store",
    "memory_reminder_provider",
    "recall_memories",
    "record_memory_index",
]


#: Memory is pinned to ONE global directory (never per-session
#: workspace), so memories survive a workspace switch and stay cross-scenario.
#: The agent layer configures the root and falls back to this default
#: (``~/.noeta/memories``) when nothing is set; ``expanduser`` resolves ``~``
#: against the running user's home.
DEFAULT_GLOBAL_MEMORY_DIR: Path = Path("~/.noeta/memories").expanduser()


def load_memory_store(*, root: Path) -> MemoryStore:
    """Build the global :class:`MemoryStore` at ``root``.

    ``root`` is the **fixed global** memory directory the agent layer
    supplies (default :data:`DEFAULT_GLOBAL_MEMORY_DIR`) — it is no longer
    derived from the per-session workspace, so reads / writes land in one
    place regardless of which workspace the turn runs in. A missing
    directory is a valid empty store — an unconfigured global dir pays
    nothing (``entries() == ()`` keeps every default flow byte-identical).
    """
    return MemoryStore(root=root)


def record_memory_index(
    event_log: EventLogWriter,
    content_store: ContentStore,
    task: Task,
    *,
    entries: MemoryEntries,
    lease_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> Task:
    """Pre-loop activation of the index resident — write-side only.

    Emits one ``ContextContentRecorded`` carrying the index fingerprint
    (:func:`memory_index_hash` — the same function the kind spec's
    ``hashes`` resolver uses, so the recorded fingerprint and the composed
    bytes share one source of truth) and converges live state through
    ``apply_event``, exactly like the engine-side provenance helpers. Empty
    ``entries`` is a no-op (unconfigured memory leaves the ledger
    untouched), and re-recording an already-active index is dropped
    first-only, like ``emit_skill_content_recorded``.

    Takes the host-owned ``event_log`` / ``content_store`` pair rather
    than reaching into Engine privates; the emitted shape (defaults:
    ``actor="engine"``, ``origin="engine"``) matches the engine-side
    provenance helpers' shape for pre-loop content recordings.
    """
    if not entries:
        return task
    if MEMORY_INDEX_NAME in task.state.active_content.get(MEMORY_KIND, ()):
        return task
    env = event_log.emit(
        task_id=task.task_id,
        type="ContextContentRecorded",
        payload=ContextContentRecordedPayload(
            kind=MEMORY_KIND,
            name=MEMORY_INDEX_NAME,
            version=MEMORY_INDEX_VERSION,
            content_hash=memory_index_hash(entries),
            policy=MEMORY_DRIFT_POLICY,
        ),
        lease_id=lease_id,
        trace_id=trace_id,
    )
    apply_event(task, env, content_store)
    return task


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


def intake_providers(
    store: Optional[MemoryStore],
    extra: Sequence[ReminderProvider] = (),
) -> tuple[ReminderProvider, ...]:
    """The ``turn_intake`` provider list for one append: built-in recall, then plugins.

    ONE composition rule for both intake call sites (the seed path and the
    ``send_goal`` prelude), so they cannot drift. The built-in memory recall goes
    first because it is the pre-existing behaviour and its recorded position is
    pinned by the characterization goldens; the activated plugins' providers
    follow in their own ``(plugin, name)`` order. No store and no plugins ⇒ an
    empty tuple, which records exactly the plain-append bytes.
    """
    providers: list[ReminderProvider] = []
    if store is not None:
        providers.append(memory_reminder_provider(store))
    providers.extend(extra)
    return tuple(providers)


def append_user_message_with_recall(
    engine: Engine,
    task: Task,
    *,
    content: list[Block],
    lease_id: str,
    store: MemoryStore,
    trace_id: Optional[str] = None,
    origin: Optional[MessageOrigin] = None,
) -> Task:
    """The D7 v1 user-message intake seam: retrieve, then ledger both turns.

    Order is load-bearing: retrieval (impure) runs first; the human turn
    lands untagged (role's natural author); hits land as ONE follow-up
    turn tagged ``origin="memory"`` through the Engine's sole
    origin-writer seam. Appending the recall AFTER the user message lets
    the Anthropic adapter merge it into the same wire turn (its
    ``<system-reminder>`` rendering — D4); the ledger itself
    stays provider-neutral. No hits ⇒ exactly the plain
    ``append_user_message`` ledger bytes.

    D7 re-expression: this is now a thin wrapper over the generic
    ``turn_intake`` recording seam (:func:`~noeta.execution.reminders.record_intake_reminders`)
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


@dataclass(frozen=True, slots=True)
class RecallGoalPrelude:
    """``send_goal`` prelude with memory recall.

    Drop-in sibling of :class:`noeta.runtime.worker.AppendMessagePrelude`
    for memory-enabled sessions: a follow-up goal enters the ledger
    through :func:`append_user_message_with_recall`, so resume turns get
    the same D6 intake the opening turn got (the SDK port of the deleted
    runner's ``_goal_prelude`` seam). A goal with no hits ledgers exactly
    the plain-prelude bytes.

    ``origin`` / ``attachment_texts`` / ``activate_skills`` mirror
    :class:`~noeta.runtime.worker.AppendMessagePrelude` field-for-field
    (attachments seed BEFORE the goal as their own ``origin="system"``
    messages and never feed the recall key; the skill-activation patch
    lands AFTER, goal-then-patch order) — only the goal append itself is
    routed through the recall seam, so a memory-enabled session's
    ``send_goal`` differs from the plain prelude solely by the optional
    ``origin="memory"`` follow-up turn.

    ``store`` and ``providers`` are the two provider sources and either may be
    absent. ``store`` is the built-in memory recall (bound to a live store by the
    host, so it cannot be declared as a plain provider); ``providers`` are the
    ``turn_intake`` providers the agent's activated plugins contribute (D7). Both
    run at the same seam, built-in first, so an agent with memory *and* a RAG
    plugin records the built-in recall ahead of the plugin's. With neither, this
    degrades to exactly ``AppendMessagePrelude``'s bytes.
    """

    content: list[Block]
    store: Optional[MemoryStore] = None
    providers: tuple[ReminderProvider, ...] = ()
    origin: Optional[MessageOrigin] = None
    attachment_texts: tuple[str, ...] = ()
    activate_skills: tuple[str, ...] = ()

    #: Recall reads the local store then appends — seed-time safe (D6).
    durable_at_seed: ClassVar[bool] = True

    def __call__(self, engine: Any, task: Any, *, lease_id: str) -> Any:
        for text in self.attachment_texts:
            engine.append_user_message(
                task, content=[TextBlock(text=text)], lease_id=lease_id,
                origin="system",
            )
        task = record_intake_reminders(
            engine, task, content=self.content, lease_id=lease_id,
            providers=intake_providers(self.store, self.providers),
            origin=self.origin,
        )
        if self.activate_skills:
            task = engine.apply_state_patch(
                task,
                patch=TaskStatePatch(activate_skills=list(self.activate_skills)),
                lease_id=lease_id,
            )
        return task
