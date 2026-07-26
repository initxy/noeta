"""``git-checkpoint`` — a first-party example Noeta plugin.

The plugin contributes a single :class:`~noeta.protocols.event_log.Subscriber`
(a post-commit event Observer) that snapshots the workspace every time the
agent starts a *mutating* file tool call (``write`` / ``edit`` / ``apply_patch``
by default). Each snapshot is recorded as a commit on a dedicated ref
(``refs/noeta/checkpoints``) so the agent's mutation history is undoable
without ever touching the user's branch, ``HEAD``, or staging area:

* The snapshot is built through a **temporary index** (``GIT_INDEX_FILE``
  pointing at a scratch file), so the user's real ``.git/index`` is never
  read or written.
* The commit is written with ``git commit-tree`` and published with
  ``git update-ref`` onto ``refs/noeta/checkpoints`` — a ref outside
  ``refs/heads/*``, so it is not a branch, does not move ``HEAD``, and never
  appears in the user's ``git log``.
* Checkpoints chain: each new checkpoint's parent is the previous checkpoint,
  so the ref carries the full ordered snapshot history.

:func:`restore_checkpoint` is the inverse: it writes a checkpoint's tree back
into the working tree (again via a temporary index, so ``HEAD`` and the real
index stay put). It overwrites the recorded files; files created after the
checkpoint are left in place (a non-destructive restore).

Guard-observer contract (``docs/adr/guard-observer-hooks.md``): an Observer
failure must **never** flow back to the writer. The observer therefore
swallows every exception (logging at ``warning``) — a broken or missing git
repo degrades to "no checkpoint recorded", never a failed agent turn.
:func:`restore_checkpoint`, by contrast, is an explicit operator call and
*does* raise on failure.

The plugin's identity name is ``git-checkpoint`` (``noeta_plugin_name``); the
factory reads its workspace repo path and tuning from the operator ``config``
dict (see :func:`noeta_plugin`).
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence


#: Factory name override consumed by the loader (``load_plugins``) so the
#: plugin's identity is ``git-checkpoint`` regardless of the file's stem.
noeta_plugin_name = "git-checkpoint"

#: The dedicated ref checkpoints are recorded on. Deliberately outside
#: ``refs/heads/*`` so it is never a branch and never moves ``HEAD``.
DEFAULT_CHECKPOINT_REF = "refs/noeta/checkpoints"

#: The built-in tool names that mutate the workspace. A checkpoint is taken
#: when one of these starts (see ``BUILTIN_TOOL_CLASSES`` in
#: ``noeta.client.parts``: ``edit`` / ``write`` / ``apply_patch``).
DEFAULT_MUTATING_TOOLS: tuple[str, ...] = ("write", "edit", "apply_patch")

#: The event type a mutating tool call raises at its start
#: (``noeta.protocols.events.ToolCallStartedPayload``).
_TOOL_CALL_STARTED = "ToolCallStarted"

#: The checkpoint commit's author/committer identity. Set explicitly so a
#: checkpoint succeeds even when the repo has no ``user.name`` / ``user.email``
#: configured, and so checkpoints are attributable and never masquerade as the
#: user's own commits.
_CHECKPOINT_AUTHOR = "noeta-checkpoint"
_CHECKPOINT_EMAIL = "noeta-checkpoint@localhost"

_log = logging.getLogger("noeta.plugins.git_checkpoint")


__all__ = [
    "GitCheckpointObserver",
    "GitCheckpointError",
    "restore_checkpoint",
    "noeta_plugin",
    "DEFAULT_CHECKPOINT_REF",
    "DEFAULT_MUTATING_TOOLS",
]


class GitCheckpointError(RuntimeError):
    """A checkpoint or restore git operation failed.

    Raised only by :func:`restore_checkpoint` (an explicit operator call).
    The Observer path swallows this and every other exception per the
    guard-observer ADR.
    """


def _git(
    repo_path: Path,
    *args: str,
    index: Optional[Path] = None,
    check: bool = True,
) -> str:
    """Run one ``git`` command in ``repo_path``; return its stripped stdout.

    ``index``, when given, is exported as ``GIT_INDEX_FILE`` so the command
    operates on a scratch index instead of the repo's real ``.git/index``.
    ``check=False`` returns ``""`` on a non-zero exit instead of raising (used
    to probe whether the checkpoint ref exists yet).
    """
    env: dict[str, str] = {}
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    result = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
        env={**_os_environ(), **env},
    )
    if check and result.returncode != 0:
        raise GitCheckpointError(
            f"git {' '.join(args)} failed in {repo_path}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip()


def _os_environ() -> dict[str, str]:
    import os

    return dict(os.environ)


def _record_checkpoint(
    repo_path: Path,
    ref: str,
    tool_name: str,
    task_id: str,
    seq: int,
) -> str:
    """Snapshot the working tree into a checkpoint commit; advance ``ref``.

    Uses a throwaway index so the user's real index is untouched, and
    ``update-ref`` on ``ref`` (never a branch) so ``HEAD`` and the user's
    branch do not move. Returns the new checkpoint commit sha.
    """
    scratch = Path(tempfile.mkdtemp(prefix="noeta-ckpt-"))
    index = scratch / "index"
    try:
        # Stage the entire working tree into the scratch index. Starting from
        # an absent index, ``add -A`` records every non-ignored path, so the
        # resulting tree is a faithful snapshot of the worktree.
        _git(repo_path, "add", "-A", index=index)
        tree = _git(repo_path, "write-tree", index=index)

        parent = _git(
            repo_path,
            "rev-parse",
            "--verify",
            "--quiet",
            f"{ref}^{{commit}}",
            check=False,
        )
        message = f"noeta checkpoint: {tool_name} (task {task_id} seq {seq})"
        commit_args = ["commit-tree", tree, "-m", message]
        if parent:
            commit_args += ["-p", parent]
        commit = _git(
            repo_path,
            *commit_args,
            index=index,
        )
        _git(repo_path, "update-ref", ref, commit)
        return commit
    finally:
        _rmtree(scratch)


def _rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


class GitCheckpointObserver:
    """Post-commit Observer that checkpoints the workspace on mutating tools.

    An instance is a plain :class:`~noeta.protocols.event_log.Subscriber` —
    a ``callable(EventEnvelope) -> None`` the host subscribes to the EventLog.
    It fires on every append; it acts only on a ``ToolCallStarted`` whose
    ``tool_name`` is in ``mutating_tools``.

    Thread-safety mirrors the built-in Observers: subscriber callbacks fire
    outside the EventLog writer lock and may run concurrently from several
    writer threads, so the whole record operation runs under an instance
    ``threading.Lock``. That serialises ``update-ref`` and keeps the parent
    chain consistent (each thread still snapshots into its own scratch index).

    Every exception is swallowed and logged at ``warning`` (guard-observer
    ADR: an Observer must never break the writer). A missing/broken repo just
    means no checkpoint is recorded.
    """

    #: Stable Observer name (the built-in Observers expose ``name`` too).
    name = "git_checkpoint"

    def __init__(
        self,
        repo_path: Any,
        *,
        ref: str = DEFAULT_CHECKPOINT_REF,
        mutating_tools: Sequence[str] = DEFAULT_MUTATING_TOOLS,
    ) -> None:
        self.repo_path = Path(repo_path)
        self.ref = ref
        self.mutating_tools = frozenset(mutating_tools)
        self._lock = threading.Lock()

    def __call__(self, env: Any) -> None:
        try:
            if getattr(env, "type", None) != _TOOL_CALL_STARTED:
                return
            payload = getattr(env, "payload", None)
            tool_name = getattr(payload, "tool_name", None)
            if tool_name not in self.mutating_tools:
                return
            with self._lock:
                commit = _record_checkpoint(
                    self.repo_path,
                    self.ref,
                    tool_name,
                    getattr(env, "task_id", "unknown"),
                    getattr(env, "seq", 0),
                )
            _log.debug(
                "recorded checkpoint %s on %s for %s",
                commit,
                self.ref,
                tool_name,
            )
        except Exception as exc:  # noqa: BLE001 — guard-observer: never raise
            # An Observer failure must never flow back to the writer
            # (docs/adr/guard-observer-hooks.md). Degrade to "no checkpoint".
            _log.warning(
                "git-checkpoint observer skipped a checkpoint: %s", exc
            )


def restore_checkpoint(
    repo_path: Any,
    *,
    ref: str = DEFAULT_CHECKPOINT_REF,
    commit: Optional[str] = None,
) -> str:
    """Write a checkpoint's tree back into the working tree.

    Restores the tip of ``ref`` unless a specific ``commit`` (any git
    commit-ish reachable in the repo) is given. Like the record path, this
    uses a throwaway index, so the user's real index and ``HEAD`` are left
    untouched — only the working-tree files recorded in the checkpoint are
    overwritten. Files created after the checkpoint are left in place (a
    non-destructive restore).

    Returns the restored commit sha. Unlike the Observer, this is an explicit
    operator call and **raises** :class:`GitCheckpointError` on failure.
    """
    repo_path = Path(repo_path)
    target = commit if commit is not None else ref
    sha = _git(
        repo_path,
        "rev-parse",
        "--verify",
        f"{target}^{{commit}}",
    )
    scratch = Path(tempfile.mkdtemp(prefix="noeta-restore-"))
    index = scratch / "index"
    try:
        _git(repo_path, "read-tree", sha, index=index)
        _git(repo_path, "checkout-index", "-a", "-f", index=index)
        return sha
    finally:
        _rmtree(scratch)


def noeta_plugin(api: Any, config: Optional[dict] = None) -> None:
    """Plugin factory — contribute the checkpoint Observer.

    Operator ``config`` keys (all optional):

    * ``repo_path`` — the workspace git repo to checkpoint. Defaults to the
      current working directory (a server host injects the real workspace
      root here).
    * ``ref`` — the checkpoint ref (default ``refs/noeta/checkpoints``).
    * ``mutating_tools`` — the tool names that trigger a checkpoint
      (default ``("write", "edit", "apply_patch")``).

    The Observer is *wiring-layer* (``Options.observers``), so it does not
    enter ``AgentSpec`` identity: enabling checkpointing never changes the
    compiled agent or its cache prefix.
    """
    cfg = dict(config or {})
    repo_path = cfg.get("repo_path", Path.cwd())
    ref = cfg.get("ref", DEFAULT_CHECKPOINT_REF)
    mutating_tools = cfg.get("mutating_tools", DEFAULT_MUTATING_TOOLS)
    api.add_observer(
        GitCheckpointObserver(
            repo_path,
            ref=ref,
            mutating_tools=mutating_tools,
        )
    )
