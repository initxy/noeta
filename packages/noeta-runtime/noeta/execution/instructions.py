"""Instructions file host glue — activation recording.

The execution-layer counterpart of :mod:`noeta.context.instructions`
(pure render + hash pieces), mirroring how
:mod:`noeta.execution.memory` glues the memory subsystem to the Engine.
Since microkernel phase 3 (D10) this module is record-seam only:

* :func:`record_instructions` — write-side activation: emit ONE
  ``ContextContentRecorded`` (kind ``instructions``, policy
  ``evolving``) so fold flips the resident on in
  ``TaskState.active_content``. Nothing here touches the runtime —
  the event type, its fold and the ``ContentHashesFn`` seam all
  landed generically.

The impure loader half (``load_instructions`` — the ``NOETA.md`` /
``AGENTS.md`` convention — plus the ``read``-triggered discovery walk and
the resume preloader) is product content and lives in the ``workspace``
built-in plugin (``noeta.builtins.workspace.impl.loaders``), reached
through that plugin's ``session_pack`` contribution (compose side, which
also exports the Engine's ``content_discovery`` / ``content_preloader``
hooks) and the SDK host's doorway (record side).

v1 keeps the seam as plain functions the host calls instead of an
interface — rule of two. Product wiring is handled by the same
``prepare()`` / resume helpers that activate skills and memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from noeta.context.content_channel import ContentKindSpec
from noeta.context.instructions import (
    INSTRUCTIONS_DRIFT_POLICY,
    INSTRUCTIONS_KIND,
    INSTRUCTIONS_VERSION,
    InstructionsSnapshot,
)
from noeta.core.fold import apply_event
from noeta.protocols.content_store import ContentStore
from noeta.protocols.event_log import EventLogWriter
from noeta.protocols.events import ContextContentRecordedPayload
from noeta.protocols.task import Task
from typing import Optional


__all__ = [
    "InstructionsKit",
    "record_instructions",
]


@dataclass(frozen=True)
class InstructionsKit:
    """What one session build consumes from the instructions resident.

    The SkillsKit pattern (phase 2c): the tag-block renderer, the hash
    rule, the ``ContentKindSpec`` factory AND the candidate-filename
    search order (``NOETA.md`` / ``AGENTS.md`` — a product convention,
    not kernel vocabulary) live in the ``workspace`` built-in plugin
    (``noeta.builtins.workspace.impl:build_instructions_kit``); the
    kernel receives the bundle at its record seam so the pre-loop loader,
    the mid-loop discovery hook and the compose-time renderer all share
    a single source of truth.
    """

    #: ``{name: snapshot} -> ContentKindSpec`` — the registry item factory
    #: (the mapping is deliberately mutable and shared with discovery).
    content_kind_from: Callable[
        [Mapping[str, InstructionsSnapshot]], ContentKindSpec
    ]
    #: ``snapshot -> sha256(rendered bytes)`` — the recorded fingerprint.
    content_hash: Callable[[InstructionsSnapshot], str]
    #: Workspace-root search order for instruction files; the first
    #: existing, non-empty candidate wins (per directory).
    filenames: tuple[str, ...]


def record_instructions(
    event_log: EventLogWriter,
    content_store: ContentStore,
    task: Task,
    *,
    snapshot: Optional[InstructionsSnapshot],
    kit: InstructionsKit,
    lease_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> Task:
    """Pre-loop activation of the instructions resident — write-side only.

    Emits one ``ContextContentRecorded`` carrying the content fingerprint
    (``kit.content_hash`` — the same kit whose ``content_kind_from``
    the composer renders from, so the recorded fingerprint and the composed
    bytes share one source of truth) and converges live state through
    ``apply_event``, exactly like the engine-side provenance helpers.

    ``snapshot is None`` is a no-op (unconfigured workspace leaves
    the ledger untouched), and re-recording an already-active
    instructions name is dropped first-only, like
    ``record_memory_index`` / ``emit_skill_content_recorded``.
    """
    if snapshot is None:
        return task
    if snapshot.name in task.state.active_content.get(INSTRUCTIONS_KIND, ()):
        return task
    env = event_log.emit(
        task_id=task.task_id,
        type="ContextContentRecorded",
        payload=ContextContentRecordedPayload(
            kind=INSTRUCTIONS_KIND,
            name=snapshot.name,
            version=INSTRUCTIONS_VERSION,
            content_hash=kit.content_hash(snapshot),
            policy=INSTRUCTIONS_DRIFT_POLICY,
        ),
        lease_id=lease_id,
        trace_id=trace_id,
    )
    apply_event(task, env, content_store)
    return task
