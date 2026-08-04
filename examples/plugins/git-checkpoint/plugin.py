"""Undo for agent file edits: a git snapshot before every mutating tool call.

Demonstrated SDK capability: the ``observer`` surface. An observer is a
process-scoped subscriber on the EventLog, so loading this plugin arms
checkpointing for every agent with no activation to forget. Observers are
wiring, never identity — enabling one leaves the compiled agent and its cache
prefix untouched.

Every snapshot goes through a scratch ``GIT_INDEX_FILE`` and lands on a ref
outside ``refs/heads/*``, because the one thing this must never do is disturb the
user's own branch, ``HEAD``, staging area, or ``git log``. Chaining each
checkpoint onto the previous one gives the ref an ordered history without a
branch.

Per ``docs/adr/guard-observer-hooks.md`` an observer failure must never reach the
writer, so the observer path swallows everything: a missing or broken repo
degrades to "no checkpoint recorded", never a failed turn.
:func:`restore_checkpoint` is an explicit operator call and does raise.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional, Sequence

from noeta.sdk import PluginBuilder


#: Outside ``refs/heads/*`` on purpose: not a branch, never moves ``HEAD``, never
#: shows up in the user's ``git log``.
DEFAULT_CHECKPOINT_REF = "refs/noeta/checkpoints"

#: The built-in tools that mutate the workspace. Read-only tools are excluded —
#: a snapshot per ``read`` would bury the useful checkpoints in noise.
DEFAULT_MUTATING_TOOLS: tuple[str, ...] = ("Write", "Edit")

#: Checkpointing on *start* rather than completion is what makes the snapshot an
#: undo point: it captures the tree the call is about to change.
_TOOL_CALL_STARTED = "ToolCallStarted"

#: An explicit identity, so a checkpoint succeeds in a repo with no ``user.name``
#: configured and can never be mistaken for one of the user's own commits.
_CHECKPOINT_AUTHOR = "noeta-checkpoint"
_CHECKPOINT_EMAIL = "noeta-checkpoint@localhost"

_log = logging.getLogger("noeta.plugins.git_checkpoint")


__all__ = [
    "GitCheckpointObserver",
    "GitCheckpointError",
    "restore_checkpoint",
    "OBSERVER",
    "plugin",
    "DEFAULT_CHECKPOINT_REF",
    "DEFAULT_MUTATING_TOOLS",
]


class GitCheckpointError(RuntimeError):
    """A checkpoint or restore git operation failed.

    Only ever escapes :func:`restore_checkpoint`; the observer path swallows it
    along with everything else, per the guard-observer ADR.
    """


def _git(
    repo_path: Path,
    *args: str,
    index: Optional[Path] = None,
    check: bool = True,
) -> str:
    """Run one ``git`` command in ``repo_path``; return its stripped stdout.

    ``index`` is exported as ``GIT_INDEX_FILE`` so the command works on a scratch
    index and the user's real ``.git/index`` is neither read nor written.
    ``check=False`` is for probes that legitimately fail (does the ref exist
    yet?), where a non-zero exit is an answer rather than an error.

    The checkpoint identity is exported on every call rather than configured
    once: git refuses to auto-detect an author in a repo with no ``user.name``,
    which would make checkpointing fail exactly in the throwaway repos it is most
    useful in.
    """
    env: dict[str, str] = {
        "GIT_AUTHOR_NAME": _CHECKPOINT_AUTHOR,
        "GIT_AUTHOR_EMAIL": _CHECKPOINT_EMAIL,
        "GIT_COMMITTER_NAME": _CHECKPOINT_AUTHOR,
        "GIT_COMMITTER_EMAIL": _CHECKPOINT_EMAIL,
    }
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

    Returns the new checkpoint commit sha.
    """
    scratch = Path(tempfile.mkdtemp(prefix="noeta-ckpt-"))
    index = scratch / "index"
    try:
        # Starting from an *absent* index, ``add -A`` records every non-ignored
        # path, so the tree is a faithful snapshot rather than a diff against
        # whatever the user happened to have staged.
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

    Subscriber callbacks fire *outside* the EventLog writer lock and may run
    concurrently from several writer threads, so the record operation is
    serialised on an instance lock — concurrent ``update-ref`` calls would
    otherwise interleave and break the checkpoint parent chain. Each thread
    still snapshots into its own scratch index, so only the ref advance
    contends.

    Every exception is swallowed and logged (guard-observer ADR: an observer
    must never break the writer).
    """

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
            # Degrade to "no checkpoint" rather than failing the agent's turn:
            # losing an undo point is recoverable, losing the turn is not.
            _log.warning(
                "git-checkpoint observer skipped a checkpoint: %s", exc
            )


def restore_checkpoint(
    repo_path: Any,
    *,
    ref: str = DEFAULT_CHECKPOINT_REF,
    commit: Optional[str] = None,
) -> str:
    """Write a checkpoint's tree back into the working tree; return its sha.

    Restores the tip of ``ref`` unless a specific ``commit`` is given. Files
    created *after* the checkpoint are left in place: an undo that also deleted
    unrelated new work would be worse than the mistake it reverses.

    Raises :class:`GitCheckpointError` on failure — this is an explicit operator
    call, so a silent no-op would be the dangerous outcome.
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


#: Carried on the manifest so operator tooling can list the knobs. Descriptive
#: only — nothing in the loader reads it, so it must be kept true by hand.
CONFIG_SCHEMA = {
    "env": {
        "NOETA_GIT_CHECKPOINT_REPO": "workspace git repo to checkpoint (default: cwd)",
        "NOETA_GIT_CHECKPOINT_REF": f"checkpoint ref (default: {DEFAULT_CHECKPOINT_REF})",
    }
}


#: The configured Observer the manifest ships. Built once at import, so a host
#: must set ``NOETA_GIT_CHECKPOINT_REPO`` *before* loading the plugin — the
#: default of ``cwd`` is rarely the workspace a real host means. A distributed
#: install resolves it through the ``ref`` below; a single-file load caches this
#: very object, so the two paths agree without a second import.
OBSERVER = GitCheckpointObserver(
    os.environ.get("NOETA_GIT_CHECKPOINT_REPO", os.getcwd()),
    ref=os.environ.get("NOETA_GIT_CHECKPOINT_REF", DEFAULT_CHECKPOINT_REF),
)


#: The builder *is* this plugin's manifest, and its name is the plugin identity
#: — the enable-list key, not the filename. ``python -m noeta.sdk.plugin_check``
#: derives TOML from it and verifies the shipped ``noeta-plugin.toml`` matches,
#: which is what stops the two from drifting.
plugin = PluginBuilder(
    "git-checkpoint", requires_noeta=">=0.4", config_schema=CONFIG_SCHEMA
)
plugin.contribute("observer", OBSERVER, name="git_checkpoint", ref="git_checkpoint:OBSERVER")
