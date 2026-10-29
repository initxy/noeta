"""``EnterWorktree`` / ``ExitWorktree``: the shell moves into a worktree
under ``.noeta/worktrees/`` and back; only the worktree the tool made is
ever removed, and not with unsaved work unless told to."""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.core.tool import ToolContext
from noeta.plugins.shell import Shell
from noeta.plugins.shell.bash import ShellState, reduce_shell
from noeta.plugins.shell.worktree import EnterWorktreeTool, ExitWorktreeTool
from noeta.toolkit.tools import Workspace, path_within


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("a\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")
    return root


class Driver:
    """Runs the tools with the shell state their records fold into."""

    def __init__(self, root: Path) -> None:
        self.env = LocalExecEnv(str(root))
        self.state = ShellState()

    async def call(self, tool: Any, args: dict[str, Any]) -> tuple[bool, str]:
        recorded: list[tuple[str, str, Any]] = []
        ctx = ToolContext(task_id="t", call_id="c", env=self.env, state={"shell": self.state},
                          record=lambda e, k, d: recorded.append((e, k, d)), root_id="t")
        out = await tool.run(args, ctx)
        for _ext, kind, data in recorded:
            self.state = reduce_shell(self.state, kind, data)
        return out.is_error, "".join(getattr(b, "text", "") for b in out.content)


async def test_enter_then_remove_a_clean_worktree(repo: Path) -> None:
    s = Driver(repo)
    err, text = await s.call(EnterWorktreeTool(), {"name": "feature"})
    path = str(repo / ".noeta" / "worktrees" / "feature")
    assert not err and path in text
    assert s.state.cwd == path and Path(path, "a.txt").is_file()
    assert _git(Path(path), "branch", "--show-current") == "worktree-feature"
    err, text = await s.call(EnterWorktreeTool(), {"name": "other"})
    assert err and "already in the worktree" in text
    err, text = await s.call(ExitWorktreeTool(), {"action": "remove"})
    assert not err and "removed" in text
    assert s.state.cwd == str(repo) and s.state.worktree is None
    assert not Path(path).exists()
    assert "worktree-feature" not in _git(repo, "branch")


async def test_remove_refuses_unsaved_work_unless_discarded(repo: Path) -> None:
    s = Driver(repo)
    await s.call(EnterWorktreeTool(), {"name": "w"})
    tree = repo / ".noeta" / "worktrees" / "w"
    (tree / "new.txt").write_text("x")
    err, text = await s.call(ExitWorktreeTool(), {"action": "remove"})
    assert err and "1 uncommitted file" in text and tree.exists()
    err, _ = await s.call(ExitWorktreeTool(), {"action": "remove", "discard_changes": True})
    assert not err and not tree.exists()


async def test_remove_refuses_when_git_cannot_say_what_would_be_lost(repo: Path) -> None:
    s = Driver(repo)
    await s.call(EnterWorktreeTool(), {"name": "w"})
    tree = repo / ".noeta" / "worktrees" / "w"
    assert s.state.worktree is not None
    # A base git no longer knows: ``rev-list`` fails, so nothing is known.
    s.state = dataclasses.replace(s.state, worktree={**s.state.worktree, "base": "0" * 40})
    err, text = await s.call(ExitWorktreeTool(), {"action": "remove"})
    assert err and "git" in text and tree.exists()
    err, _ = await s.call(ExitWorktreeTool(), {"action": "remove", "discard_changes": True})
    assert not err and not tree.exists()


async def test_keep_and_entering_an_existing_worktree(repo: Path, tmp_path: Path) -> None:
    s = Driver(repo)
    err, text = await s.call(ExitWorktreeTool(), {"action": "keep"})
    assert not err and "No worktree session" in text
    other = repo / "manual"
    _git(repo, "worktree", "add", "-q", "-b", "manual", str(other))
    err, text = await s.call(EnterWorktreeTool(), {"path": str(repo / "nowhere")})
    assert err and "not a worktree" in text
    outside = tmp_path / "outside"
    _git(repo, "worktree", "add", "-q", "-b", "outside", str(outside))
    err, text = await s.call(EnterWorktreeTool(), {"path": str(outside)})
    assert err and "outside the working directory" in text
    err, _ = await s.call(EnterWorktreeTool(), {"path": str(other)})
    assert not err and s.state.cwd == str(other)
    # A worktree entered by path is never removed.
    err, text = await s.call(ExitWorktreeTool(), {"action": "remove"})
    assert not err and other.exists() and s.state.cwd == str(repo)
    err, text = await s.call(EnterWorktreeTool(), {"name": "x", "path": str(other)})
    assert err and "not both" in text


async def test_bad_names_and_no_repository(tmp_path: Path, repo: Path) -> None:
    s = Driver(repo)
    for bad in ("../up", "a b", "x" * 65, "a/./b", "a/.git", "x/.GIT.", "a//b"):
        err, _ = await s.call(EnterWorktreeTool(), {"name": bad})
        assert err
    plain = tmp_path / "plain"
    plain.mkdir()
    err, text = await Driver(plain).call(EnterWorktreeTool(), {})
    assert err and "not in a git repository" in text


def test_the_shell_offers_both_tools_deferred() -> None:
    tools = {t.name: t for t in Shell(background=False)._tools}
    assert tools["EnterWorktree"].deferred and tools["ExitWorktree"].deferred
    assert tools["ExitWorktree"].input_schema["required"] == ["action"]


async def test_a_worktree_entered_from_a_subdirectory_is_usable(repo: Path) -> None:
    """A task started in ``repo/pkg``: Bash and the file tools stay inside
    that directory, so the worktree goes under it and the shell starts in
    the worktree's own ``pkg``."""
    pkg = repo / "pkg"
    pkg.mkdir()
    (pkg / "b.txt").write_text("b\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "pkg")
    s = Driver(pkg)
    err, text = await s.call(EnterWorktreeTool(), {"name": "sub"})
    assert not err, text
    tree = pkg / ".noeta" / "worktrees" / "sub"
    assert s.state.cwd == str(tree / "pkg") and (tree / "pkg" / "b.txt").is_file()
    ws = await Workspace.of(s.env)
    assert path_within(s.state.cwd, ws.root)
    assert await ws.resolve_write(str(tree / "pkg" / "c.txt")) == str(tree / "pkg" / "c.txt")
    err, text = await s.call(ExitWorktreeTool(), {"action": "remove"})
    assert not err and not tree.exists() and s.state.cwd == str(pkg)


async def test_the_made_worktree_is_removed_after_two_switches(repo: Path) -> None:
    s = Driver(repo)
    await s.call(EnterWorktreeTool(), {"name": "made"})
    made = repo / ".noeta" / "worktrees" / "made"
    others = [repo / ".noeta" / "worktrees" / n for n in ("b", "c")]
    for other in others:
        _git(repo, "worktree", "add", "-q", "-b", other.name, str(other))
    for other in others:
        err, _ = await s.call(EnterWorktreeTool(), {"path": str(other)})
        assert not err and s.state.cwd == str(other)
    err, text = await s.call(ExitWorktreeTool(), {"action": "remove"})
    assert not err and "removed" in text
    assert not made.exists() and all(o.exists() for o in others)
    assert s.state.cwd == str(repo)
