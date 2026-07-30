"""Memory host glue — activation recording + the recall seam (D5/D6).

The execution-layer counterpart of ``noeta.context.memory`` (pure index
pieces), mirroring how ``noeta.execution.skills`` glues the skill subsystem
to the Engine. Microkernel M3: this module is **seams only** — everything
that touches a live ``MemoryStore`` (store construction, the recall
provider, the store-bound intake wrapper) moved into the ``memory`` built-in
plugin (``noeta.builtins.memory.impl``). What remains is kernel-pure:

* :func:`record_memory_index` — write-side activation: emit ONE
  ``ContextContentRecorded`` (kind ``memory``, policy ``evolving``) so
  fold flips the index resident on in ``TaskState.active_content``.
  Nothing here touches the runtime — the event type, its fold and the
  ``ContentHashesFn`` seam all landed generically in issue 02.
* :func:`intake_providers` — the ONE composition rule for a turn's
  ``turn_intake`` provider list: the host-bound built-in recall provider
  (obtained through the host's ``memory_recall_context`` seam, which binds
  the impl's ``memory_reminder_provider`` to a live store SDK-side) ahead
  of the activated plugins' providers.
* :class:`RecallGoalPrelude` — the ``send_goal`` prelude that routes a
  follow-up goal through the recording seam with those providers.

The host seam contract (microkernel M3): ``memory_recall_context(agent,
task_id)`` returns ``(recall_provider, entries)`` — a bound
``ReminderProvider`` plus the load-time index snapshot — or ``None`` for a
memory-off agent. The kernel never sees the store behind the provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, ClassVar, Optional, Sequence

from noeta.context.content_channel import ContentKindSpec
from noeta.context.memory import (
    MEMORY_DRIFT_POLICY,
    MEMORY_INDEX_NAME,
    MEMORY_INDEX_VERSION,
    MEMORY_KIND,
    MemoryEntries,
)
from noeta.core.fold import apply_event
from noeta.execution.reminders import (
    ReminderProvider,
    record_intake_reminders,
)
from noeta.protocols.content_store import ContentStore
from noeta.protocols.decisions import TaskStatePatch
from noeta.protocols.event_log import EventLogWriter
from noeta.protocols.events import ContextContentRecordedPayload
from noeta.protocols.messages import Block, MessageOrigin, TextBlock
from noeta.protocols.task import Task


__all__ = [
    "MemoryIndexKit",
    "RecallGoalPrelude",
    "intake_providers",
    "record_memory_index",
]


@dataclass(frozen=True)
class MemoryIndexKit:
    """What one session build consumes from the memory index resident.

    The SkillsKit pattern (phase 2c): the index renderer prose, the hash
    rule and the ``ContentKindSpec`` factory are product material and live
    in the ``memory`` built-in plugin
    (``noeta.builtins.memory.impl:build_memory_index_kit``); the kernel
    receives them as one injected bundle so the compose-time renderer and
    the record-time fingerprint share a single source of truth.
    """

    #: ``entries -> ContentKindSpec`` — the registry item factory.
    content_kind: Callable[[MemoryEntries], ContentKindSpec]
    #: ``entries -> sha256(rendered index bytes)`` — the recorded fingerprint.
    content_hash: Callable[[MemoryEntries], str]


def record_memory_index(
    event_log: EventLogWriter,
    content_store: ContentStore,
    task: Task,
    *,
    entries: MemoryEntries,
    kit: MemoryIndexKit,
    lease_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> Task:
    """Pre-loop activation of the index resident — write-side only.

    Emits one ``ContextContentRecorded`` carrying the index fingerprint
    (``kit.content_hash`` — the same injected kit whose ``content_kind``
    the composer renders from, so the recorded fingerprint and the
    composed bytes share one source of truth) and converges live state
    through ``apply_event``, exactly like the engine-side provenance
    helpers. Empty ``entries`` is a no-op (unconfigured memory leaves the
    ledger untouched), and re-recording an already-active index is dropped
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
            content_hash=kit.content_hash(entries),
            policy=MEMORY_DRIFT_POLICY,
        ),
        lease_id=lease_id,
        trace_id=trace_id,
    )
    apply_event(task, env, content_store)
    return task


def intake_providers(
    recall: Optional[ReminderProvider],
    extra: Sequence[ReminderProvider] = (),
) -> tuple[ReminderProvider, ...]:
    """The ``turn_intake`` provider list for one append: built-in recall, then plugins.

    ONE composition rule for both intake call sites (the seed path and the
    ``send_goal`` prelude), so they cannot drift. The built-in memory recall goes
    first because it is the pre-existing behaviour and its recorded position is
    pinned by the characterization goldens; the activated plugins' providers
    follow in their own ``(plugin, name)`` order. No recall provider and no
    plugins ⇒ an empty tuple, which records exactly the plain-append bytes.

    Microkernel M3: ``recall`` is the already-bound provider the host's
    ``memory_recall_context`` seam returns (the impl's
    ``memory_reminder_provider(store)``), not a store — the kernel composes
    providers, never touches a store.
    """
    providers: list[ReminderProvider] = []
    if recall is not None:
        providers.append(recall)
    providers.extend(extra)
    return tuple(providers)


@dataclass(frozen=True, slots=True)
class RecallGoalPrelude:
    """``send_goal`` prelude with memory recall.

    Drop-in sibling of :class:`noeta.runtime.worker.AppendMessagePrelude`
    for memory-enabled sessions: a follow-up goal enters the ledger
    through the ``turn_intake`` recording seam, so resume turns get
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

    ``recall`` and ``providers`` are the two provider sources and either may be
    absent. ``recall`` is the built-in memory recall, already bound to a live
    store by the host (microkernel M3 — the kernel never sees the store);
    ``providers`` are the ``turn_intake`` providers the agent's activated
    plugins contribute (D7). Both run at the same seam, built-in first, so an
    agent with memory *and* a RAG plugin records the built-in recall ahead of
    the plugin's. With neither, this degrades to exactly
    ``AppendMessagePrelude``'s bytes.
    """

    content: list[Block]
    recall: Optional[ReminderProvider] = None
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
            providers=intake_providers(self.recall, self.providers),
            origin=self.origin,
        )
        if self.activate_skills:
            task = engine.apply_state_patch(
                task,
                patch=TaskStatePatch(activate_skills=list(self.activate_skills)),
                lease_id=lease_id,
            )
        return task
