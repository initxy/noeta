"""The project_status plugin: the project and git branch of the task's
directory as status items, read from real git repositories; a repository's
own config never runs a program; answers reused per directory; shipped, on
in ``main`` and switchable."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from noeta.agents import default_agents
from noeta.core import registry
from noeta.core.extension import StatusItem, TaskView
from noeta.core.loop.state import TaskState
from noeta.host.plugins import PluginAdmin, plugin_infos, switchable
from noeta.plugins import project_status
from noeta.plugins.project_status import ProjectStatus
from noeta.plugins.shell.state import SHELL, ShellState
from noeta.toolkit.shell import GIT_READ_FLAGS

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _view(cwd: str, **ext: Any) -> TaskView:
    return TaskView(state=TaskState(task_id="t", config={"cwd": cwd}, ext=ext))


async def _shown(cwd: str | Path, plugin: ProjectStatus | None = None, **ext: Any) -> list[str]:
    items = await (plugin or ProjectStatus()).ui(_view(str(cwd), **ext))
    assert all(isinstance(i, StatusItem) and i.tone == "normal" for i in items)
    return [i.text for i in items]


def _repo(path: Path, branch: str = "trunk") -> Any:
    """A repository at ``path`` with one commit on ``branch``; returns its
    ``git`` (output as text)."""
    def git(*argv: str) -> str:
        return subprocess.run(["git", "-C", str(path), "-c", "user.name=t",
                               "-c", "user.email=t@t", *argv], check=True,
                              capture_output=True, text=True).stdout.strip()

    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    (path / "kept.txt").write_text("k\n")
    git("add", "kept.txt")
    git("commit", "-q", "-m", "first")
    return git


@needs_git
async def test_the_project_is_the_checkouts_top_folder_and_the_branch_follows(
        tmp_path: Path) -> None:
    _repo(tmp_path / "noeta")
    (tmp_path / "noeta" / "src").mkdir()
    assert await _shown(tmp_path / "noeta") == ["noeta", "⎇ trunk"]
    assert await _shown(tmp_path / "noeta" / "src") == ["noeta", "⎇ trunk"]


@needs_git
async def test_a_detached_head_shows_its_short_commit(tmp_path: Path) -> None:
    git = _repo(tmp_path / "repo")
    commit = git("rev-parse", "HEAD")
    git("checkout", "-q", "--detach")
    assert await _shown(tmp_path / "repo") == ["repo", f"⎇ {commit[:7]}"]


@needs_git
async def test_changes_mark_the_branch_dirty(tmp_path: Path) -> None:
    _repo(tmp_path / "repo")
    (tmp_path / "repo" / "new.txt").write_text("n\n")
    assert await _shown(tmp_path / "repo") == ["repo", "⎇ trunk*"]


@needs_git
async def test_ahead_and_behind_its_upstream(tmp_path: Path) -> None:
    git = _repo(tmp_path / "repo", branch="base")
    git("checkout", "-q", "-b", "trunk", "--track", "base")
    git("commit", "-q", "--allow-empty", "-m", "mine")
    git("commit", "-q", "--allow-empty", "-m", "mine too")
    assert await _shown(tmp_path / "repo") == ["repo", "⎇ trunk ↑2"]
    git("checkout", "-q", "base")
    git("commit", "-q", "--allow-empty", "-m", "theirs")
    git("checkout", "-q", "trunk")
    assert await _shown(tmp_path / "repo") == ["repo", "⎇ trunk ↑2 ↓1"]


@needs_git
async def test_a_linked_worktree_is_its_own_project(tmp_path: Path) -> None:
    git = _repo(tmp_path / "repo")
    git("worktree", "add", "-q", "-b", "feature", str(tmp_path / "repo-feature"))
    assert await _shown(tmp_path / "repo-feature") == ["repo-feature", "⎇ feature"]


@needs_git
async def test_the_shells_directory_wins_over_the_tasks(tmp_path: Path) -> None:
    _repo(tmp_path / "repo")
    _repo(tmp_path / "other", branch="side")
    shell = ShellState(cwd=str(tmp_path / "other"))
    assert await _shown(tmp_path / "repo", **{SHELL: shell}) == ["other", "⎇ side"]


async def test_outside_git_the_directorys_name_alone(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()
    assert await _shown(tmp_path / "plain") == ["plain"]


async def test_a_directory_this_machine_lacks_shows_nothing(tmp_path: Path) -> None:
    assert await _shown(tmp_path / "missing") == []
    assert await _shown("workspace") == []  # not a path of this machine
    assert not await ProjectStatus().ui(TaskView(state=TaskState(task_id="t")))  # no cwd


@needs_git
async def test_a_repository_fsmonitor_never_runs(tmp_path: Path) -> None:
    pwned = tmp_path / "PWNED"
    hook = tmp_path / "hook.sh"
    hook.write_text(f"#!/bin/sh\ntouch {pwned}\nexit 1\n")
    hook.chmod(0o755)
    git = _repo(tmp_path / "repo")
    git("config", "core.fsmonitor", str(hook))
    git("config", "core.hooksPath", str(tmp_path))
    (tmp_path / "post-index-change").write_text(f"#!/bin/sh\ntouch {pwned}\n")
    (tmp_path / "post-index-change").chmod(0o755)
    (tmp_path / "repo" / "kept.txt").write_text("changed\n")
    assert await _shown(tmp_path / "repo") == ["repo", "⎇ trunk*"]
    assert not pwned.exists()


@needs_git
async def test_a_repository_filter_never_runs_and_the_dirty_mark_is_left_out(
        tmp_path: Path) -> None:
    pwned = tmp_path / "PWNED"
    git = _repo(tmp_path / "repo", branch="base")
    git("checkout", "-q", "-b", "trunk", "--track", "base")
    git("commit", "-q", "--allow-empty", "-m", "mine")
    git("config", "filter.x.clean", f"touch {pwned}; cat")
    (tmp_path / "repo" / ".gitattributes").write_text("*.txt filter=x\n")
    (tmp_path / "repo" / "kept.txt").write_text("changed\n")
    assert await _shown(tmp_path / "repo") == ["repo", "⎇ trunk ↑1"]  # no "*"
    assert not pwned.exists()


def lazy_fetching_repo(path: Path, pwned: Path, *, via: str) -> None:
    """A partial clone whose HEAD names a commit it lacks: a read of it
    fetches the commit through a program the repository's own config names,
    which would touch ``pwned`` — its ``core.sshCommand`` (``via="ssh"``),
    an ``ext::`` URL it allows (``"ext"``), or a credential helper asked for
    an ftp proxy's password (``"ftp"``)."""
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(path)], check=True)
    url = {"ssh": "ssh://example.invalid/x", "ext": f"ext::sh -c touch% {pwned}",
           "ftp": "ftp://u@127.0.0.1:1/x"}[via]
    for key, value in (("core.repositoryformatversion", "1"),
                       ("extensions.partialClone", "origin"),
                       ("remote.origin.promisor", "true"),
                       ("remote.origin.url", url),
                       ("protocol.ext.allow", "always"),
                       ("protocol.ftp.allow", "always"),
                       ("core.sshCommand", f"touch {pwned}; false"),
                       ("http.proxy", "http://u@127.0.0.1:1"),
                       ("credential.helper", f"!touch {pwned}; true")):
        subprocess.run(["git", "-C", str(path), "config", key, value], check=True)
    (path / ".git" / "refs" / "heads" / "trunk").write_text("12" * 20 + "\n")


@needs_git
@pytest.mark.parametrize("via", ["ssh", "ext", "ftp"])
async def test_a_partial_clone_never_fetches_through_the_repositorys_programs(
        tmp_path: Path, via: str) -> None:
    pwned = tmp_path / "PWNED"
    lazy_fetching_repo(tmp_path / "repo", pwned, via=via)
    # The trap is real: a plain read runs the repository's program.
    subprocess.run(["git", "-C", str(tmp_path / "repo"), "status"], capture_output=True,
                   timeout=30)
    if via == "ftp" and not pwned.exists():
        pytest.skip("this git cannot fetch over ftp")
    assert pwned.exists()
    pwned.unlink()
    assert await _shown(tmp_path / "repo") == ["repo", "⎇ trunk"]
    assert not pwned.exists()
    # The flags alone hold where git does not know GIT_NO_LAZY_FETCH.
    for read in (["status"], ["log", "-n", "1"], ["rev-list", "--count", "HEAD"]):
        subprocess.run(["git", *GIT_READ_FLAGS, *read], cwd=tmp_path / "repo",
                       capture_output=True, timeout=30)
    assert not pwned.exists()


@needs_git
async def test_an_answer_is_reused_for_a_while(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    git = _repo(tmp_path / "repo")
    plugin = ProjectStatus()
    assert await _shown(tmp_path / "repo", plugin) == ["repo", "⎇ trunk"]
    git("checkout", "-q", "-b", "next")
    assert await _shown(tmp_path / "repo", plugin) == ["repo", "⎇ trunk"]  # reused
    monkeypatch.setattr(project_status, "FRESH_SECONDS", 0.0)
    plugin._seen.clear()
    assert await _shown(tmp_path / "repo", plugin) == ["repo", "⎇ next"]
    git("checkout", "-q", "trunk")
    assert await _shown(tmp_path / "repo", plugin) == ["repo", "⎇ trunk"]  # read again


@needs_git
async def test_git_out_of_time_leaves_the_branch_and_waits_longer(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _repo(tmp_path / "repo")
    (tmp_path / "repo" / "new.txt").write_text("n\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").write_text("#!/bin/sh\nexec sleep 30\n")
    (bin_dir / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setattr(project_status, "GIT_TIMEOUT", 0.2)
    plugin = ProjectStatus()
    assert await _shown(tmp_path / "repo", plugin) == ["repo", "⎇ trunk"]  # from HEAD
    [(until, found)] = plugin._seen.values()
    assert found.slow and until - project_status.time.monotonic() > project_status.FRESH_SECONDS


async def test_without_git_the_branch_comes_from_head(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    assert await _shown(repo) == ["repo", "⎇ main"]
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/.invalid\n")  # reftable's
    assert await _shown(repo) == ["repo"]


def test_shipped_on_in_main_and_switchable() -> None:
    assert registry.sources(registry.PLUGINS).get("project_status") == "package noeta"
    main = next(a for a in default_agents("m") if a.name == "main")
    plugin = next(e for e in main.extensions if e.name == "project_status")
    assert isinstance(plugin, ProjectStatus) and switchable("project_status")
    info = next(p for p in plugin_infos(main, PluginAdmin()) if p["name"] == "project_status")
    assert info["builtin"] and info["switchable"] and info["enabled"]
    assert info["parts"] == ["backend", "ui"]
    assert info["description"].startswith("The project and git branch")
