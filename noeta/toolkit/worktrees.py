"""The git worktrees noeta makes (``EnterWorktree``, an agent started with
``isolation: "worktree"``): making one on a new branch, telling what removing
it would lose, and removing it with its branch. A git error is never read as
"nothing to lose"."""

from __future__ import annotations

from collections.abc import Sequence

from noeta.core.execenv import ExecEnv

__all__ = ["WORKTREES", "add_worktree", "git", "remove_worktree", "repository_top",
           "unsaved_work"]

#: Where new worktrees go, under the directory that holds them.
WORKTREES = ".noeta/worktrees"


async def git(env: ExecEnv, args: Sequence[str], cwd: str) -> tuple[int, str]:
    """``git args`` in ``cwd``: its exit code and its output (stderr when it
    failed), stripped."""
    out = await env.run(["git", *args], cwd=cwd)
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


async def unsaved_work(env: ExecEnv, path: str, base: str) -> tuple[str, str]:
    """What removing the worktree at ``path`` would lose, in words (empty
    when nothing), and git's error when it could not tell (empty when it
    could): uncommitted files, and commits since ``base``."""
    lost = []
    code, status = await git(env, ["status", "--porcelain"], path)
    if code != 0:
        return "", f"git status: {status or 'failed'}"
    if status:
        lost.append(f"{len(status.splitlines())} uncommitted file(s)")
    code, ahead = await git(env, ["rev-list", "--count", f"{base}..HEAD"], path)
    if code != 0 or not ahead.isdigit():
        return "", f"git rev-list: {ahead or 'failed'}"
    if int(ahead):
        lost.append(f"{ahead} commit(s) the starting branch lacks")
    return " and ".join(lost), ""


async def remove_worktree(env: ExecEnv, top: str, path: str, branch: str) -> str:
    """Remove the worktree at ``path`` and then its ``branch``; ``""`` when
    the worktree is gone, else why not."""
    code, err = await git(env, ["worktree", "remove", "--force", path], top)
    if code != 0:
        return err or "git worktree remove failed"
    await git(env, ["branch", "-D", branch], top)
    return ""
