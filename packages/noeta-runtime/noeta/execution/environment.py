"""Workspace environment host glue — activation recording.

The execution-layer counterpart of :mod:`noeta.context.environment`
(pure render + hash pieces), mirroring how
:mod:`noeta.execution.instructions` glues the instructions file to the
Engine:

* :func:`load_environment` — impure pre-loop loader: capture the
  session-static workspace facts (the workspace directory string, whether
  a ``.git`` entry exists, the host platform) into an
  :class:`EnvironmentSnapshot`. Unlike instructions, a workspace always
  exists, so this NEVER returns ``None`` — the environment resident is
  always registered and always activated.
* :func:`record_environment` — write-side activation: emit ONE
  ``ContextContentRecorded`` (kind ``environment``, policy ``evolving``)
  so fold flips the resident on in ``TaskState.active_content``. Nothing
  here touches the runtime — the event type, its fold and the
  ``ContentHashesFn`` seam all landed generically.

v1 keeps the seam as plain functions the host calls instead of an
interface — rule of two. Product wiring is handled by the same
``prepare()`` / resume helpers that activate skills, memory and
instructions.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from noeta.context.environment import (
    ENVIRONMENT_DRIFT_POLICY,
    ENVIRONMENT_KIND,
    ENVIRONMENT_NAME,
    ENVIRONMENT_VERSION,
    EnvironmentSnapshot,
    environment_content_hash,
)
from noeta.core.fold import apply_event
from noeta.protocols.content_store import ContentStore
from noeta.protocols.event_log import EventLogWriter
from noeta.protocols.events import ContextContentRecordedPayload
from noeta.protocols.task import Task
from noeta.tools.fs.exec_env import ExecEnv


__all__ = [
    "load_environment",
    "record_environment",
]

#: Upper bound on the captured ``git status --short`` body. The status of a
#: large dirty tree can run unbounded; a model only needs the gist for
#: orientation (and can run ``git status`` itself for the rest), so cap it.
_GIT_STATUS_MAX_BYTES = 2048


def load_environment(
    workspace_dir: Path, *, exec_env: Optional[ExecEnv] = None
) -> EnvironmentSnapshot:
    """Capture the session-static workspace facts into a snapshot.

    Impure (reads the workspace path string, probes ``.git`` on disk,
    reads ``sys.platform``, and — when it is a git repo — spawns git to
    read the branch / short status, plus reads the host clock) but called
    ONCE pre-loop — before anything enters the ledger — so the composer's
    renderer and the pre-loop :func:`record_environment` share one
    snapshot, and record time equals compose time by construction.

    Reproducibility scope: the snapshot is memoized for the whole session,
    so the rendered bytes are identical *across steps within one session*
    (the semi-stable segment stays KV-cache-stable). They are NOT guaranteed
    to reproduce *across sessions*: ``captured_date`` is a wall clock and
    ``git_branch`` / ``git_status`` reflect live repo state, so a resume in a
    fresh process legitimately re-renders different bytes for those lines.
    That is why the environment resident carries the ``evolving`` drift policy
    (``content_hash`` recorded as advisory provenance, free to move) and lives
    in ``semi_stable``, NOT the stable prefix — only the stable prefix (system
    + tools) is under the hard cross-step byte-reproducibility constraint.
    ``workspace_display`` / ``platform`` do reproduce given the same
    ``workspace_dir``.

    ``.git`` is probed by mere existence (``.git`` is a directory in a
    normal clone, a gitlink *file* in a worktree / submodule — both count
    as "is a git repository"). The git branch / status subprocesses run
    only for a git repo, and the branch, status and date capture are each
    fully guarded — any failure (no git binary, detached HEAD edge cases,
    timeout, decode error, …) degrades to the empty string and never
    raises. Never returns ``None`` — a workspace always exists.

    ``exec_env`` (sandbox mode) probes ``.git`` and runs git THROUGH the
    container — ``workspace_dir`` is then the container workdir, so the facts
    describe the sandbox's checkout (this fixes the v1 bug where the loader
    probed a container path on the host filesystem). ``None`` keeps the host
    reads byte-identical.
    """
    git_marker = workspace_dir / ".git"
    is_git_repo = (
        exec_env.exists(git_marker) if exec_env is not None else git_marker.exists()
    )
    git_branch = ""
    git_status = ""
    if is_git_repo:
        git_branch = _git_branch(workspace_dir, exec_env)
        git_status = _git_status(workspace_dir, exec_env)
    return EnvironmentSnapshot(
        workspace_display=str(workspace_dir),
        is_git_repo=is_git_repo,
        platform=sys.platform,
        git_branch=git_branch,
        git_status=git_status,
        captured_date=_captured_date(),
    )


def _run_git(
    workspace_dir: Path, args: list[str], exec_env: Optional[ExecEnv] = None
) -> str:
    """Run a read-only git command in ``workspace_dir``, "" on any failure.

    Captured once pre-loop, so a short timeout is fine; any non-zero exit,
    missing binary, timeout or decode problem degrades to "". ``exec_env``
    (sandbox mode) runs git INSIDE the container (cwd = the container workdir).
    """
    if exec_env is not None:
        try:
            outcome = exec_env.run_argv(
                ["git", *args],
                cwd=workspace_dir,
                timeout_s=5,
                output_cap=_GIT_STATUS_MAX_BYTES * 8,
            )
        except Exception:  # noqa: BLE001 — capture is best-effort, never fatal.
            return ""
        if outcome.timed_out or outcome.returncode != 0:
            return ""
        return outcome.stdout.decode("utf-8", errors="replace")
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(workspace_dir),
            capture_output=True,
            timeout=5,
            check=False,
        )
    except Exception:  # noqa: BLE001 — capture is best-effort, never fatal.
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8", errors="replace")


def _git_branch(workspace_dir: Path, exec_env: Optional[ExecEnv] = None) -> str:
    args = ["rev-parse", "--abbrev-ref", "HEAD"]
    # Keep the local path a 2-positional-arg call so it is byte-identical to
    # before (and existing ``_run_git`` fakes that take (wd, args) still work);
    # only sandbox mode threads the ExecEnv.
    out = (
        _run_git(workspace_dir, args, exec_env)
        if exec_env is not None
        else _run_git(workspace_dir, args)
    )
    return out.strip()


def _git_status(workspace_dir: Path, exec_env: Optional[ExecEnv] = None) -> str:
    args = ["status", "--short"]
    status = (
        _run_git(workspace_dir, args, exec_env)
        if exec_env is not None
        else _run_git(workspace_dir, args)
    )
    if len(status.encode("utf-8")) > _GIT_STATUS_MAX_BYTES:
        clipped = status.encode("utf-8")[:_GIT_STATUS_MAX_BYTES]
        # Drop a trailing partial multibyte char from the byte cut.
        status = clipped.decode("utf-8", errors="ignore")
    return status.rstrip("\n")


def _captured_date() -> str:
    try:
        return datetime.now().astimezone().isoformat(timespec="seconds")
    except Exception:  # noqa: BLE001 — clock read is best-effort.
        return ""


def record_environment(
    event_log: EventLogWriter,
    content_store: ContentStore,
    task: Task,
    *,
    snapshot: Optional[EnvironmentSnapshot],
    lease_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> Task:
    """Pre-loop activation of the environment resident — write-side only.

    Emits one ``ContextContentRecorded`` carrying the content fingerprint
    (:func:`environment_content_hash` — same function the kind spec's
    ``hashes`` resolver uses, so the recorded fingerprint and the composed
    bytes share one source of truth) and converges live state through
    ``apply_event``, exactly like ``record_instructions`` /
    ``record_memory_index``.

    ``snapshot is None`` is a no-op (defensive symmetry with the other
    residents — :func:`load_environment` never produces ``None``), and
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
            content_hash=environment_content_hash(snapshot),
            policy=ENVIRONMENT_DRIFT_POLICY,
        ),
        lease_id=lease_id,
        trace_id=trace_id,
    )
    apply_event(task, env, content_store)
    return task
