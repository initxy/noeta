"""Workspace environment host glue — activation recording.

The execution-layer counterpart of :mod:`noeta.context.environment`
(pure render + hash pieces), mirroring how
:mod:`noeta.execution.instructions` glues the instructions file to the
Engine. Since microkernel phase 3 (D10) this module is record-seam only:

* :func:`record_environment` — write-side activation: emit ONE
  ``ContextContentRecorded`` (kind ``environment``, policy ``evolving``)
  so fold flips the resident on in ``TaskState.active_content``. Nothing
  here touches the runtime — the event type, its fold and the
  ``ContentHashesFn`` seam all landed generically.

The impure loader half (``load_environment`` — git/platform/date capture)
is product content and lives in the ``workspace`` built-in plugin
(``noeta.builtins.workspace.impl.loaders``), reached through that plugin's
``session_pack`` contribution (compose side) and the SDK host's doorway
(record side).

v1 keeps the seam as plain functions the host calls instead of an
interface — rule of two. Product wiring is handled by the same
``prepare()`` / resume helpers that activate skills, memory and
instructions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from noeta.context.content_channel import ContentKindSpec
from noeta.context.environment import (
    ENVIRONMENT_DRIFT_POLICY,
    ENVIRONMENT_KIND,
    ENVIRONMENT_NAME,
    ENVIRONMENT_VERSION,
    EnvironmentSnapshot,
)
from noeta.core.fold import apply_event
from noeta.protocols.content_store import ContentStore
from noeta.protocols.event_log import EventLogWriter
from noeta.protocols.events import ContextContentRecordedPayload
from noeta.protocols.task import Task


__all__ = [
    "EnvironmentKit",
    "record_environment",
]


@dataclass(frozen=True)
class EnvironmentKit:
    """What one session build consumes from the environment resident.

    The SkillsKit pattern (phase 2c): the renderer prose, the hash rule and
    the ``ContentKindSpec`` factory are product material and live in the
    ``workspace`` built-in plugin
    (``noeta.builtins.workspace.impl:build_environment_kit``); the kernel
    receives the bundle at its record seam so the compose-time renderer and
    the record-time fingerprint share a single source of truth.
    """

    #: ``snapshot -> ContentKindSpec`` — the registry item factory.
    content_kind: Callable[[EnvironmentSnapshot], ContentKindSpec]
    #: ``snapshot -> sha256(rendered bytes)`` — the recorded fingerprint.
    content_hash: Callable[[EnvironmentSnapshot], str]


def record_environment(
    event_log: EventLogWriter,
    content_store: ContentStore,
    task: Task,
    *,
    snapshot: Optional[EnvironmentSnapshot],
    kit: EnvironmentKit,
    lease_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> Task:
    """Pre-loop activation of the environment resident — write-side only.

    Emits one ``ContextContentRecorded`` carrying the content fingerprint
    (``kit.content_hash`` — the same kit whose ``content_kind`` the
    composer renders from, so the recorded fingerprint and the
    composed bytes share one source of truth) and converges live state
    through ``apply_event``, exactly like ``record_instructions`` /
    ``record_memory_index``.

    ``snapshot is None`` is a no-op (defensive symmetry with the other
    residents — the workspace plugin's loader never produces ``None``), and
    re-recording an already-active environment name is dropped first-only,
    like ``record_instructions``.
    """
    if snapshot is None:
        return task
    if ENVIRONMENT_NAME in task.state.active_content.get(ENVIRONMENT_KIND, ()):
        return task
    env = event_log.emit(
        task_id=task.task_id,
        type="ContextContentRecorded",
        payload=ContextContentRecordedPayload(
            kind=ENVIRONMENT_KIND,
            name=ENVIRONMENT_NAME,
            version=ENVIRONMENT_VERSION,
            content_hash=kit.content_hash(snapshot),
            policy=ENVIRONMENT_DRIFT_POLICY,
        ),
        lease_id=lease_id,
        trace_id=trace_id,
    )
    apply_event(task, env, content_store)
    return task
