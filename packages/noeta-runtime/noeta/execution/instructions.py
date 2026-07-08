"""Instructions file host glue — activation recording.

The execution-layer counterpart of :mod:`noeta.context.instructions`
(pure render + hash pieces), mirroring how
:mod:`noeta.execution.memory` glues the memory subsystem to the Engine:

* :func:`load_instructions` — impure pre-loop loader: read the first
  existing, non-empty candidate from ``<workspace>/NOETA.md`` or
  ``<workspace>/AGENTS.md`` (or an override path) into an
  :class:`InstructionsSnapshot`. Missing / empty files are a valid
  "no instructions" state (returns ``None``) so a default workspace
  pays nothing.
* :func:`record_instructions` — write-side activation: emit ONE
  ``ContextContentRecorded`` (kind ``instructions``, policy
  ``evolving``) so fold flips the resident on in
  ``TaskState.active_content``. Nothing here touches the runtime —
  the event type, its fold and the ``ContentHashesFn`` seam all
  landed generically.

v1 keeps the seam as plain functions the host calls instead of an
interface — rule of two. Product wiring is handled by the same
``prepare()`` / resume helpers that activate skills and memory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from noeta.context.instructions import (
    INSTRUCTIONS_DRIFT_POLICY,
    INSTRUCTIONS_KIND,
    INSTRUCTIONS_VERSION,
    InstructionsSnapshot,
    instructions_content_hash,
)
from noeta.core.fold import apply_event
from noeta.protocols.content_store import ContentStore
from noeta.protocols.event_log import EventLogWriter
from noeta.protocols.events import ContextContentRecordedPayload
from noeta.protocols.task import Task
from noeta.tools.fs.exec_env import ExecEnv


__all__ = [
    "DEFAULT_INSTRUCTIONS_FILENAMES",
    "load_instructions",
    "record_instructions",
]


#: Workspace-root search order for the instructions file. The first
#: existing, non-empty candidate wins. NOETA.md is canonical (the
#: project's CLAUDE.md counterpart); AGENTS.md is a common GitHub /
#: repo convention supported as a fallback.
DEFAULT_INSTRUCTIONS_FILENAMES = ("NOETA.md", "AGENTS.md")


def load_instructions(
    workspace_dir: Path,
    *,
    override_path: Optional[Path] = None,
    exec_env: Optional[ExecEnv] = None,
) -> Optional[InstructionsSnapshot]:
    """Load the workspace instructions file into a snapshot.

    * ``override_path`` wins when provided — read it (or return
      ``None`` if it does not exist or is empty after stripping).
    * Otherwise walk :data:`DEFAULT_INSTRUCTIONS_FILENAMES` under
      ``workspace_dir`` in order; the first candidate that exists AND
      whose UTF-8 content is non-empty after stripping wins.
    * Returns ``None`` for a workspace with no instructions file —
      the caller must short-circuit kind registration and activation
      so the empty state has **zero** byte-footprint on the ledger.

    ``exec_env`` (sandbox mode) reads the candidate THROUGH the container —
    ``workspace_dir`` is then the container workdir, so the instructions file
    is the one INSIDE the sandbox (this fixes the v1 bug where the loader read a
    container path against the host filesystem). ``None`` keeps the host read
    byte-identical.
    """
    if override_path is not None:
        return _read_one(override_path, exec_env)
    for filename in DEFAULT_INSTRUCTIONS_FILENAMES:
        candidate = workspace_dir / filename
        snapshot = _read_one(candidate, exec_env)
        if snapshot is not None:
            return snapshot
    return None


def _read_one(
    path: Path, exec_env: Optional[ExecEnv] = None
) -> Optional[InstructionsSnapshot]:
    if exec_env is not None:
        try:
            # A missing / non-file path surfaces as an OSError subclass
            # (AioSandboxExecEnv maps not_found → FileNotFoundError); any read
            # fault is treated as "no instructions", the same forgiving state
            # as a missing host file.
            raw = exec_env.read_text(path, encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
    else:
        try:
            raw = path.read_text(encoding="utf-8")
        except (FileNotFoundError, IsADirectoryError):
            return None
    if not raw.strip():
        return None
    return InstructionsSnapshot(name=path.name, text=raw)


def record_instructions(
    event_log: EventLogWriter,
    content_store: ContentStore,
    task: Task,
    *,
    snapshot: Optional[InstructionsSnapshot],
    lease_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> Task:
    """Pre-loop activation of the instructions resident — write-side only.

    Emits one ``ContextContentRecorded`` carrying the content
    fingerprint (:func:`instructions_content_hash` — same function the
    kind spec's ``hashes`` resolver uses, so the recorded fingerprint and
    the composed bytes share one source of truth) and converges live state
    through ``apply_event``, exactly like the engine-side provenance helpers.

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
            content_hash=instructions_content_hash(snapshot),
            policy=INSTRUCTIONS_DRIFT_POLICY,
        ),
        lease_id=lease_id,
        trace_id=trace_id,
    )
    apply_event(task, env, content_store)
    return task
