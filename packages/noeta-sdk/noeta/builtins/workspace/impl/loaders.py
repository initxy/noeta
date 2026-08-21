"""The workspace residents' impure loader half.

The ``NOETA.md``/``AGENTS.md`` discovery convention and the git/platform/date
capture are product content, not kernel mechanism. This module holds
everything that touches the disk, the
subprocess table, or the wall clock:

* :func:`load_instructions` — pre-loop root-file loader (kit search order or
  an override path; missing/empty ⇒ ``None``, zero footprint).
* :func:`discover_instructions` + :func:`build_instructions_discovery` — the
  ``read``-triggered subdirectory walk
  (docs/adr/anchored-content-placement.md) and its Engine hook.
* :func:`build_instructions_preloader` — the resume re-read for discovered
  files.
* :func:`load_environment` — the session-static workspace facts (path, git
  branch/short status, platform, capture date).

All are reached through this plugin's ``session_pack`` factories (the
``instructions`` / ``environment`` packs in the parent ``__init__``) or the
SDK host's doorway for its own record path — nothing imports this module
statically.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, MutableMapping, Optional

from noeta.protocols.content_store import ContentStore
from noeta.protocols.decisions import ToolCall
from noeta.protocols.events import ContextContentRecordedPayload
from noeta.protocols.task import Task
from noeta.protocols.tool import ToolResult
from noeta.runtime.exec_env import ExecEnv
from noeta.runtime.workspace import WorkspaceRoot, path_within


__all__ = [
    "ENVIRONMENT_DRIFT_POLICY",
    "ENVIRONMENT_KIND",
    "ENVIRONMENT_NAME",
    "ENVIRONMENT_VERSION",
    "EnvironmentSnapshot",
    "INSTRUCTIONS_DRIFT_POLICY",
    "INSTRUCTIONS_KIND",
    "INSTRUCTIONS_VERSION",
    "InstructionsSnapshot",
    "build_instructions_discovery",
    "build_instructions_preloader",
    "discover_instructions",
    "load_environment",
    "load_instructions",
]


# --- The two residents' vocabulary (plugin-owned) -------
# The kind keys are this plugin's own, discriminating the generic
# ``ContextContentRecorded`` / ``active_content`` shapes; the kernel never
# names them.

#: The instructions channel kind key — matches ``TaskState.active_content``
#: and ``ContextContentRecorded.kind``.
INSTRUCTIONS_KIND = "instructions"
#: Declared shape version of the rendered body (not its content — content
#: is free to evolve under the ``evolving`` policy).
INSTRUCTIONS_VERSION = "1"
#: The drift policy instructions recordings carry: hash recorded, drift
#: allowed (advisory-only) — the file evolves day to day with the repo.
INSTRUCTIONS_DRIFT_POLICY = "evolving"


@dataclass(frozen=True, slots=True)
class InstructionsSnapshot:
    """Preloaded instructions file contents captured at wiring time.

    ``name`` is the file's basename (e.g. ``"NOETA.md"``) so the View
    source label reads ``instructions:NOETA.md``; ``text`` is the file
    body as read from disk (UTF-8 decoded, unmodified — the wrapping
    tag is the renderer's job, not the loader's). ``None`` is never a
    legal field value; callers that want "no instructions" must
    short-circuit and omit this kind entirely.
    """

    name: str
    text: str


#: The environment channel kind key — matches ``TaskState.active_content``
#: and ``ContextContentRecorded.kind``.
ENVIRONMENT_KIND = "environment"
#: The single resident name (a workspace has exactly one environment
#: block). The View source label reads ``environment:workspace``.
ENVIRONMENT_NAME = "workspace"
#: Declared shape version of the rendered body (not its content — content
#: is free to evolve under the ``evolving`` policy). Bumped to ``"2"`` when
#: the git branch / status / capture-date lines joined the rendered block;
#: to ``"3"`` when the snapshot note joined them (the block is recorded
#: activate-once per task, so the note tells the model the git facts are a
#: task-start snapshot, mirroring Claude Code's git-status wording).
ENVIRONMENT_VERSION = "3"
#: The drift policy environment recordings carry: hash recorded, drift
#: allowed (advisory-only) — an absolute path moves across machines. (Why a
#: content-channel resident and NOT the system prompt: the system prompt is
#: the composer's stable prefix, whose hash is the prompt-cache key; the
#: volatile workspace path belongs in ``semi_stable`` instead.)
ENVIRONMENT_DRIFT_POLICY = "evolving"


@dataclass(frozen=True, slots=True)
class EnvironmentSnapshot:
    """Preloaded workspace facts captured at wiring time, recorded once per
    task (the pack's ``init`` is activate-once — see the last paragraph).

    ``workspace_display`` is the directory string the model is told it is
    working in (relative fs-tool paths resolve against it); ``is_git_repo``
    is whether a ``.git`` entry exists at the root; ``platform`` is the
    host platform tag (``sys.platform``).

    ``git_branch`` / ``git_status`` / ``captured_date`` are a once-at-start
    snapshot of the git branch, ``git status --short`` (truncated) and the
    host date/time, captured at wiring time alongside the rest. They are
    task-static by deliberate choice (mirrors Claude Code's memoized git
    status): the first recording wins for the task's whole life — the pack's
    ``init`` records with ``refresh=False``, so an engine rebuilt in a fresh
    process re-captures a NEW snapshot but never re-records it, and the
    composer keeps resolving the original bytes from the ContentStore. That
    is what keeps the rendered block byte-identical across turns AND resumes
    (the semi_stable prefix the prompt cache rides on); a model that wants
    live state runs ``git status`` itself, and the rendered note says so.
    Each field is the empty string when capture fails or does not apply
    (non-git workspace), and an empty line is omitted from the rendered
    block.
    """

    workspace_display: str
    is_git_repo: bool
    platform: str
    git_branch: str = ""
    git_status: str = ""
    captured_date: str = ""


def load_instructions(
    workspace_dir: Path,
    *,
    filenames: tuple[str, ...],
    override_path: Optional[Path] = None,
    exec_env: Optional[ExecEnv] = None,
) -> Optional[InstructionsSnapshot]:
    """Load the workspace instructions file into a snapshot.

    * ``override_path`` wins when provided — read it (or return
      ``None`` if it does not exist or is empty after stripping).
    * Otherwise walk ``filenames`` (the kit's search order — the candidate
      names are product convention) under ``workspace_dir`` in order; the
      first candidate that exists AND whose UTF-8 content is non-empty
      after stripping wins.
    * Returns ``None`` for a workspace with no instructions file —
      the caller must short-circuit kind registration and activation
      so the empty state has **zero** byte-footprint on the ledger.

    ``exec_env`` (sandbox mode) reads the candidate THROUGH the container —
    ``workspace_dir`` is then the container workdir, so the instructions file
    is the one INSIDE the sandbox. ``None`` keeps the host read
    byte-identical.
    """
    if override_path is not None:
        return _read_one(override_path, exec_env)
    for filename in filenames:
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
            # Any read fault (missing file, a directory in the file's place,
            # permission denied, ...) is "no instructions" — the same
            # forgiving state the sandbox branch above gives a container
            # read fault, so a locked-down NOETA.md/AGENTS.md degrades
            # gracefully instead of crashing session prep.
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
    if not raw.strip():
        return None
    return InstructionsSnapshot(name=path.name, text=raw)


def discover_instructions(
    workspace_root: Path,
    resolved_file: Path,
    *,
    filenames: tuple[str, ...],
    active: tuple[str, ...] = (),
    exec_env: Optional[ExecEnv] = None,
) -> list[InstructionsSnapshot]:
    """Instruction files between ``resolved_file``'s directory and the root.

    The `read`-triggered discovery walk (docs/adr/anchored-content-placement.md):
    for every directory strictly BELOW ``workspace_root`` on the path to
    ``resolved_file`` — shallowest first, so a parent package's conventions
    render before a child's — the first existing, non-empty candidate of
    ``filenames`` (the kit's search order) wins, exactly the root loader's
    precedence. The workspace root itself is excluded: the root file is the
    pre-loop loader's job (and its resident name is the bare basename, not a
    relative path).

    Snapshot names are POSIX workspace-relative paths (``src/pkg/AGENTS.md``)
    — unique per directory, and rendered verbatim as the
    ``<workspace-instructions source=…>`` label. A directory either of whose
    candidate names is already in ``active`` is skipped without probing (it
    already contributed a resident this session). ``resolved_file`` outside
    ``workspace_root`` yields nothing — discovery is fenced to the workspace
    even though reads are not (write authorization is not instruction
    authority). Read faults degrade to "no file", same as the root loader.
    """
    try:
        rel = resolved_file.relative_to(workspace_root)
    except ValueError:
        return []
    found: list[InstructionsSnapshot] = []
    directory = workspace_root
    # rel.parts[:-1] are the directories strictly below the root, shallowest
    # first (the last part is the file itself).
    for part in rel.parts[:-1]:
        directory = directory / part
        names = [
            (directory / filename).relative_to(workspace_root).as_posix()
            for filename in filenames
        ]
        if any(name in active for name in names):
            continue
        for filename, name in zip(filenames, names):
            snapshot = _read_one(directory / filename, exec_env)
            if snapshot is not None:
                found.append(InstructionsSnapshot(name=name, text=snapshot.text))
                break
    return found


def build_instructions_discovery(
    workspace: WorkspaceRoot,
    snapshots: MutableMapping[str, InstructionsSnapshot],
    *,
    filenames: tuple[str, ...],
    content_store: ContentStore,
    render_text: Callable[[InstructionsSnapshot], str],
    exec_env: Optional[ExecEnv] = None,
) -> Callable[[Task, ToolCall, ToolResult], list[ContextContentRecordedPayload]]:
    """The post-tool content-discovery seam for the instructions kind.

    Wired into the Engine as ``content_discovery`` when the host enables
    ``instructions_discovery``. After a successful ``read`` whose path
    resolves INSIDE the workspace (component-wise containment — the fence
    the reads themselves deliberately do not have), it walks
    :func:`discover_instructions`, **puts each new file's rendered bytes into
    the ContentStore** (so the composer's renderer can resolve the name's
    bytes the moment fold activates it — spec §6; ``ref.hash`` equals the
    recorded ``content_hash`` by construction), parks the snapshot in the
    shared ``snapshots`` mapping (still the source for the ``content_hash``
    seam), and returns the activation payloads for ``handle_tool_calls`` to
    gate and emit. Everything degrades to ``[]`` — a discovery fault may only
    omit context, never fail the call.
    """

    def _discover(
        task: Task, call: ToolCall, result: ToolResult
    ) -> list[ContextContentRecordedPayload]:
        if call.tool_name != "Read" or not result.success:
            return []
        target = call.arguments.get("file_path")
        if not isinstance(target, str) or not target:
            return []
        try:
            resolved = workspace.canonicalise(target)
        except Exception:  # noqa: BLE001 — discovery may only omit, never raise.
            return []
        if not path_within(resolved, workspace.root):
            return []
        active = task.state.active_content.get(INSTRUCTIONS_KIND, {})
        payloads: list[ContextContentRecordedPayload] = []
        for snapshot in discover_instructions(
            workspace.root,
            resolved,
            filenames=filenames,
            active=tuple(active),
            exec_env=exec_env,
        ):
            snapshots[snapshot.name] = snapshot
            # Put the rendered bytes so the composer's renderer can resolve
            # them at the recorded hash (spec §6); ``ref.hash`` == the recorded
            # ``content_hash`` (both are sha256 of the rendered text).
            ref = content_store.put(
                render_text(snapshot).encode("utf-8"), media_type="text/markdown"
            )
            payloads.append(
                ContextContentRecordedPayload(
                    kind=INSTRUCTIONS_KIND,
                    name=snapshot.name,
                    version=INSTRUCTIONS_VERSION,
                    content_hash=ref.hash,
                    policy=INSTRUCTIONS_DRIFT_POLICY,
                )
            )
        return payloads

    return _discover


def build_instructions_preloader(
    workspace_dir: Path,
    snapshots: MutableMapping[str, InstructionsSnapshot],
    *,
    exec_env: Optional[ExecEnv] = None,
) -> Callable[[Task], None]:
    """The resume preload for discovered instruction files.

    Wired into the Engine as ``content_preloader`` (called before each step's
    compose — the impure band, so the composer's no-disk red line holds). A
    fresh process resumes with only the root snapshot in the mapping; every
    instructions name the ledger says is active but the mapping lacks is
    re-read as ``workspace_dir / name`` (the root basename and the discovered
    relative paths both resolve this way). A vanished or emptied file leaves
    the name unrendered — the ``evolving`` policy tolerates drift, and a
    degraded preload may only omit. No-op when nothing is missing, so the
    live path pays a set-difference per step and nothing else.
    """

    def _preload(task: Task) -> None:
        for name in task.state.active_content.get(INSTRUCTIONS_KIND, {}):
            if name in snapshots:
                continue
            snapshot = _read_one(workspace_dir / name, exec_env)
            if snapshot is not None:
                snapshots[name] = InstructionsSnapshot(
                    name=name, text=snapshot.text
                )

    return _preload


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
    once per engine build, before anything enters the ledger, so the
    composer's renderer and the pre-loop ``_init`` recording share one
    snapshot — and on the build that seeds the task, record time equals
    compose time by construction.

    Reproducibility scope: the CAPTURE is not reproducible — ``captured_date``
    is a wall clock and ``git_branch`` / ``git_status`` reflect live repo
    state, so a fresh process legitimately captures different values. The
    RECORDING is what stays stable: the pack's ``init`` records activate-once
    (``refresh=False``), so only the task's first capture ever enters the
    ledger and every later compose — same process or a resume in a fresh
    one — resolves those original bytes from the ContentStore. The rendered
    block is therefore byte-identical for the task's whole life, keeping the
    ``semi_stable`` segment prompt-cache-stable across turns and resumes.
    The block still lives in ``semi_stable``, NOT the stable prefix: it
    carries an absolute workspace path, which must not rotate the
    cross-host-shared ``stable_prefix`` hash. ``workspace_display`` /
    ``platform`` do reproduce given the same ``workspace_dir``.

    ``.git`` is probed by mere existence (``.git`` is a directory in a
    normal clone, a gitlink *file* in a worktree / submodule — both count
    as "is a git repository"). The git branch / status subprocesses run
    only for a git repo, and the branch, status and date capture are each
    fully guarded — any failure (no git binary, detached HEAD edge cases,
    timeout, decode error, …) degrades to the empty string and never
    raises. Never returns ``None`` — a workspace always exists.

    ``exec_env`` (sandbox mode) probes ``.git`` and runs git THROUGH the
    container — ``workspace_dir`` is then the container workdir, so the facts
    describe the sandbox's checkout. ``None`` keeps the host reads
    byte-identical.
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
