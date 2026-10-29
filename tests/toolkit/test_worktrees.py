"""noeta's worktrees: what removing one would lose."""

from __future__ import annotations

import subprocess
from pathlib import Path

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.toolkit.worktrees import add_worktree, unsaved_work


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


async def test_commits_left_on_the_worktree_branch_after_switching_away_count(
        tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    base = _git(repo, "rev-parse", "HEAD")
    env = LocalExecEnv(str(repo))
    tree = repo / ".noeta" / "worktrees" / "w"
    assert await add_worktree(env, str(repo), str(tree), "worktree-w", "HEAD") == ""
    assert await unsaved_work(env, str(tree), base, "worktree-w") == ("", "")
    _git(tree, "commit", "-q", "--allow-empty", "-m", "work")
    # The agent moves to another branch at the start: HEAD has nothing new,
    # but branch -D would drop the commit on worktree-w.
    _git(tree, "checkout", "-q", "-b", "elsewhere", base)
    lost, failed = await unsaved_work(env, str(tree), base, "worktree-w")
    assert failed == "" and lost == "1 commit(s) the starting branch lacks"
    _git(tree, "commit", "-q", "--allow-empty", "-m", "more")
    lost, _ = await unsaved_work(env, str(tree), base, "worktree-w")
    assert lost == "2 commit(s) the starting branch lacks"
