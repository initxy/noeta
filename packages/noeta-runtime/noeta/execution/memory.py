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
from typing import Any, ClassVar, Optional

from noeta.context.memory import (
    MEMORY_DRIFT_POLICY,
    MEMORY_INDEX_NAME,
    MEMORY_INDEX_VERSION,
    MEMORY_KIND,
    MemoryEntries,
    format_recall_text,
    match_memories,
    memory_index_hash,
)
from noeta.core.engine import Engine
from noeta.core.fold import apply_event
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
    "load_memory_store",
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


def recall_memories(
    store: MemoryStore, text: str
) -> tuple[tuple[str, str], ...]:
    """The injector's impure half: read the store NOW, match, load bodies.

    Reading at call time (not from a wiring-time snapshot) means a
    memory written mid-session by ``memory_write`` is immediately
    recallable — legal because this runs before anything enters the
    ledger. Returns ``(name, full_body)`` pairs in index order;
    unreadable hits are skipped rather than crashing the turn.
    """
    hits: list[tuple[str, str]] = []
    for name in match_memories(store.entries(), text):
        body = store.read(name)
        if body is not None:
            hits.append((name, body))
    return tuple(hits)


def _recall_key(content: list[Block]) -> str:
    """Derive the recall match key from a user turn's ``content``.

    D5: only the ``TextBlock`` texts feed retrieval — images
    ride the turn but never participate in recall. Concatenated in block
    order (newline-joined) so a multi-block text turn matches the same as
    the old single-string call.
    """
    return "\n".join(b.text for b in content if isinstance(b, TextBlock))


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
    """The D6 v1 user-message intake seam: retrieve, then ledger both turns.

    Order is load-bearing: retrieval (impure) runs first; the human turn
    lands untagged (role's natural author); hits land as ONE follow-up
    turn tagged ``origin="memory"`` through the Engine's sole
    origin-writer seam. Appending the recall AFTER the user message lets
    the Anthropic adapter merge it into the same wire turn (its
    ``<system-reminder>`` rendering — D4); the ledger itself
    stays provider-neutral. No hits ⇒ exactly the plain
    ``append_user_message`` ledger bytes.

    D5: the seam now carries ``content: list[Block]``; the
    recall key is the concatenated ``TextBlock`` text (:func:`_recall_key`)
    so images ride the turn but never drive retrieval. The human turn
    appends ``content`` as-is; the memory-hit turn still appends the
    single ``[TextBlock(format_recall_text(hits))]`` with
    ``origin="memory"``.

    ``origin`` is forwarded to the incoming turn's append (the
    driver's ``goal_origin`` passthrough — e.g. an MCP-prompt-expanded goal
    arrives ``origin="system"``); the recall turn's ``origin="memory"`` tag
    is this seam's own and never varies. ``None`` (a human-typed goal)
    keeps the human turn's bytes identical to the plain append.
    """
    hits = recall_memories(store, _recall_key(content))
    task = engine.append_user_message(
        task, content=content, lease_id=lease_id, trace_id=trace_id,
        origin=origin,
    )
    if hits:
        task = engine.append_user_message(
            task,
            content=[TextBlock(text=format_recall_text(hits))],
            lease_id=lease_id,
            trace_id=trace_id,
            origin="memory",
        )
    return task


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
    """

    content: list[Block]
    store: MemoryStore
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
        task = append_user_message_with_recall(
            engine, task, content=self.content, lease_id=lease_id,
            store=self.store, origin=self.origin,
        )
        if self.activate_skills:
            task = engine.apply_state_patch(
                task,
                patch=TaskStatePatch(activate_skills=list(self.activate_skills)),
                lease_id=lease_id,
            )
        return task
