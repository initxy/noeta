"""noeta's worktrees: what removing one would lose."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.toolkit.worktrees import (
    add_worktree,
    after_jobs,
    job_runs_in,
    job_runs_under,
    unsaved_work,
)


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


async def test_untracked_files_count_though_git_is_told_to_hide_them(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    _git(repo, "config", "status.showUntrackedFiles", "no")
    base = _git(repo, "rev-parse", "HEAD")
    env = LocalExecEnv(str(repo))
    tree = repo / ".noeta" / "worktrees" / "w"
    assert await add_worktree(env, str(repo), str(tree), "worktree-w", "HEAD") == ""
    (tree / "new.txt").write_text("work\n")
    assert await unsaved_work(env, str(tree), base, "worktree-w") == (
        "1 uncommitted file(s)", "")


async def test_noetas_own_git_runs_no_hook_and_no_fsmonitor(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    base = _git(repo, "rev-parse", "HEAD")
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {tmp_path}/hook-ran\n")
    hook.chmod(0o755)
    _git(repo, "config", "core.fsmonitor", f"touch {tmp_path}/fsmonitor-ran; false")
    env = LocalExecEnv(str(repo))
    tree = repo / ".noeta" / "worktrees" / "w"
    assert await add_worktree(env, str(repo), str(tree), "worktree-w", "HEAD") == ""
    assert await unsaved_work(env, str(tree), base, "worktree-w") == ("", "")
    assert not (tmp_path / "hook-ran").exists()
    assert not (tmp_path / "fsmonitor-ran").exists()


class _Remote:
    """An env on another machine: its paths name its own filesystem."""

    local = False

    def __init__(self, container: str | None = None) -> None:
        self.cwd = "/workspace"
        if container is not None:
            self.container = container


async def test_a_job_counts_only_on_its_own_filesystem() -> None:
    a, b = _Remote(), _Remote()
    path = "/workspace/.noeta/worktrees/w"
    ended = job_runs_in(a, path + "/sub")  # type: ignore[arg-type]
    ran: list[str] = []

    async def mark(name: str) -> None:
        ran.append(name)

    try:
        assert job_runs_under(a, path)  # type: ignore[arg-type]
        assert not job_runs_under(b, path)  # type: ignore[arg-type]
        # Two envs' paths alike: one's queued removal does not replace the other's.
        after_jobs(a, path, lambda: mark("a"))  # type: ignore[arg-type]
        after_jobs(b, path, lambda: mark("b"))  # type: ignore[arg-type]
        await asyncio.sleep(0)
        assert ran == ["b"]
    finally:
        ended()
    await asyncio.sleep(0)
    assert ran == ["b", "a"]
    # Two env objects of one container, and two local envs, are one filesystem.
    ended = job_runs_in(_Remote("c1"), path)  # type: ignore[arg-type]
    try:
        assert job_runs_under(_Remote("c1"), path)  # type: ignore[arg-type]
        assert not job_runs_under(_Remote("c2"), path)  # type: ignore[arg-type]
    finally:
        ended()
    ended = job_runs_in(LocalExecEnv("/"), path)
    try:
        assert job_runs_under(LocalExecEnv("/tmp"), path)
    finally:
        ended()
