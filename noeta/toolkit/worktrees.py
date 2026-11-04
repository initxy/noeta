"""The git worktrees noeta makes (``EnterWorktree``, an agent started with
``isolation: "worktree"``): making one on a new branch, telling what removing
it would lose, and removing it with its branch. A git error is never read as
"nothing to lose"."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence

from noeta.core.execenv import ExecEnv
from noeta.toolkit.local_env import is_local

__all__ = ["WORKTREES", "add_worktree", "after_jobs", "cancel_after_jobs", "git", "job_runs_in",
           "job_runs_under", "remove_worktree", "repository_top", "unsaved_work"]

#: Where new worktrees go, under the directory that holds them.
WORKTREES = ".noeta/worktrees"
#: Global flags for git noeta runs itself: no hook, no fsmonitor command.
GIT_NO_HOOKS = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=")

#: The working directories of this process's running shell jobs, counted by
#: (filesystem, path) (the shell plugin keeps it). A job keeps its task's
#: claim on this worker (a sub-agent's jobs end when it hands back), so the
#: task that would remove a worktree one runs in runs here and sees it.
_JOB_DIRS: Counter[tuple[object, str]] = Counter()
#: (filesystem, worktree path) -> what runs once no job works under it
#: (:func:`after_jobs`).
_AFTER_JOBS: dict[tuple[object, str], Callable[[], Awaitable[object]]] = {}
#: The ones running (a strong reference until they finish).
_RUNNING: set[asyncio.Task[object]] = set()
log = logging.getLogger(__name__)


def _filesystem(env: ExecEnv) -> object:
    """Which filesystem ``env``'s paths name: this machine's for every local
    env (a task's env is made per run, so a parent and its agent hold
    different objects), a container's by its name, else the env itself."""
    if is_local(env):
        return "local"
    container = getattr(env, "container", None)
    return ("container", container) if isinstance(container, str) else id(env)


def job_runs_in(env: ExecEnv, cwd: str) -> Callable[[], None]:
    """Count a job working in ``cwd`` of ``env`` until the returned call
    (once)."""
    key = (_filesystem(env), cwd)
    _JOB_DIRS[key] += 1
    done = False

    def ended() -> None:
        nonlocal done
        if done:
            return
        done = True
        _JOB_DIRS[key] -= 1
        if _JOB_DIRS[key] <= 0:
            del _JOB_DIRS[key]
        _run_due()

    return ended


def after_jobs(env: ExecEnv, path: str, then: Callable[[], Awaitable[object]]) -> None:
    """Run ``then`` once no job of this process works in ``path`` of ``env``
    or below it (when the last one ends, or is killed with its root)."""
    _AFTER_JOBS[(_filesystem(env), path)] = then
    _run_due()


def cancel_after_jobs(env: ExecEnv, path: str) -> None:
    """Drop what :func:`after_jobs` queued for ``path`` of ``env`` (someone
    works there again)."""
    _AFTER_JOBS.pop((_filesystem(env), path), None)


def _run_due() -> None:
    for key in [k for k in _AFTER_JOBS if not _runs_under(*k)]:
        then = _AFTER_JOBS.pop(key)
        try:
            task = asyncio.get_running_loop().create_task(_logged(key[1], then))
        except RuntimeError:  # no loop: nothing can run it now
            continue
        _RUNNING.add(task)
        task.add_done_callback(_RUNNING.discard)


async def _logged(path: str, then: Callable[[], Awaitable[object]]) -> object:
    try:
        return await then()
    except Exception:
        log.exception("worktree %s: what was to follow its jobs failed", path)
        return None


def job_runs_under(env: ExecEnv, path: str) -> bool:
    """Whether a job of this process works in ``path`` of ``env`` or below
    it."""
    return _runs_under(_filesystem(env), path)


def _runs_under(filesystem: object, path: str) -> bool:
    top = path.rstrip("/") + "/"
    return any(fs == filesystem and (d == path or d.startswith(top)) for fs, d in _JOB_DIRS)


async def git(env: ExecEnv, args: Sequence[str], cwd: str) -> tuple[int, str]:
    """``git args`` in ``cwd``: its exit code and its output (stderr when it
    failed), stripped. With no hook and no fsmonitor command (what a
    sandboxed command could have planted in the repository)."""
    out = await env.run(["git", *GIT_NO_HOOKS, *args], cwd=cwd)
    text = out.stdout if out.exit_code == 0 else out.stderr or out.stdout
    return out.exit_code, text.decode(errors="replace").strip()


async def repository_top(env: ExecEnv, cwd: str) -> str | None:
    """The top directory of the repository ``cwd`` is in, else ``None``."""
    code, top = await git(env, ["rev-parse", "--show-toplevel"], cwd)
    return top if code == 0 and top else None


async def add_worktree(env: ExecEnv, top: str, path: str, branch: str, base: str) -> str:
    """Make a worktree at ``path`` on the new ``branch`` from ``base``;
    ``""`` when made, else why not."""
    code, err = await git(env, ["worktree", "add", "-b", branch, path, base], top)
    return "" if code == 0 else err or "git worktree add failed"


async def unsaved_work(env: ExecEnv, path: str, base: str, branch: str) -> tuple[str, str]:
    """What removing the worktree at ``path`` and its ``branch`` would lose,
    in words (empty when nothing), and git's error when it could not tell
    (empty when it could): uncommitted files, and commits since ``base`` on
    ``HEAD`` or on ``branch`` (the worktree may have switched away from it)."""
    lost = []
    # Untracked files count whatever status.showUntrackedFiles says.
    code, status = await git(env, ["status", "--porcelain", "--untracked-files=normal"], path)
    if code != 0:
        return "", f"git status: {status or 'failed'}"
    if status:
        lost.append(f"{len(status.splitlines())} uncommitted file(s)")
    code, ahead = await git(env, ["rev-list", "--count", "HEAD", f"refs/heads/{branch}",
                                  "--not", base, "--"], path)
    if code != 0 or not ahead.isdigit():
        return "", f"git rev-list: {ahead or 'failed'}"
    if int(ahead):
        lost.append(f"{ahead} commit(s) the starting branch lacks")
    return " and ".join(lost), ""


async def remove_worktree(env: ExecEnv, top: str, path: str,
                          branch: str) -> tuple[str, str]:
    """Remove the worktree at ``path`` and then its ``branch``: why the
    worktree is still there (``""`` when it is gone) and why the branch is
    (``""`` when it was deleted, or when the worktree stayed)."""
    code, err = await git(env, ["worktree", "remove", "--force", path], top)
    if code != 0:
        return err or "git worktree remove failed", ""
    code, err = await git(env, ["branch", "-D", branch], top)
    return "", "" if code == 0 else err or "git branch -D failed"
