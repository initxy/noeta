"""Memory host glue — activation recording + the recall seam (D5/D6).

The execution-layer counterpart of ``noeta.context.memory`` (pure index
pieces), mirroring how ``noeta.execution.skills`` glues the skill subsystem
to the Engine. Microkernel M3: this module is **seams only** — everything
that touches a live ``MemoryStore`` (store construction, the recall
provider, the store-bound intake wrapper) moved into the ``memory`` built-in
plugin (``noeta.builtins.memory.impl``). What remains is kernel-pure:

* :class:`MemoryIndexKit` — the compose-side bundle (the ``ContentKindSpec``
  factory + the fingerprint rule) the ``memory`` built-in injects. Since the
  kernel final form (spec §4.5) the write-side activation is the memory
  built-in's generic ``init`` hook, recorded through the scoped
  :class:`~noeta.execution.recorder.SeedRecorder` — there is no
  feature-named ``record_memory_index`` seam anymore.
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
from noeta.context.memory import MemoryEntries
from noeta.execution.reminders import (
    ReminderProvider,
    record_intake_reminders,
)
from noeta.protocols.decisions import TaskStatePatch
from noeta.protocols.messages import Block, MessageOrigin, TextBlock


__all__ = [
    "MemoryIndexKit",
    "RecallGoalPrelude",
    "intake_providers",
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
