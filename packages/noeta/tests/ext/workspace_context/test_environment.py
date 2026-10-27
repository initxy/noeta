"""The environment block: captured through the task's ExecEnv with hardened
git (a repository's config cannot run a program), shell / OS lines only on
the local env, the model line falling back to the agent's model."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from noeta.events import ExtensionRecorded
from noeta.execenv.base import RunResult
from noeta.execenv.local import LocalExecEnv
from noeta.ext.base import TaskView
from noeta.ext.workspace import Environment
from noeta.loop.state import TaskState


def _view(cwd: str, env: Any, **config: Any) -> TaskView:
    return TaskView(state=TaskState(task_id="t", config={"cwd": cwd, **config}), env=env)


async def _block(ext: Environment, view: TaskView) -> str:
    await ext.on_start(view)
    [rec] = view.pending
    assert isinstance(rec, ExtensionRecorded) and rec.message is not None
    return rec.message.text


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_a_repository_fsmonitor_hook_never_runs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    pwned = tmp_path / "PWNED"
    hook = tmp_path / "hook.sh"
    hook.write_text(f"#!/bin/sh\ntouch {pwned}\nexit 1\n")
    hook.chmod(0o755)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(repo)], check=True)
    (repo / "kept.txt").write_text("k\n")
    subprocess.run([*git, "add", "kept.txt"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    (repo / "a.txt").write_text("a\n")
    subprocess.run(["git", "-C", str(repo), "config", "core.fsmonitor", str(hook)], check=True)
    text = await _block(Environment(), _view(str(repo), LocalExecEnv(str(repo))))
    assert not pwned.exists()
    assert " - Is a git repository: true" in text
    assert "Git branch: trunk" in text
    assert "?? a.txt" in text
    assert " - Shell: " in text and " - OS Version: " in text


class FakeContainer:
    """An ExecEnv that is not the local one; records what it runs."""

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.argv: list[list[str]] = []

    async def run(self, argv: Sequence[str], *, cwd: str | None = None,
                  env: Mapping[str, str] | None = None, stdin: bytes | None = None,
                  timeout: float | None = None, output_cap: int | None = None) -> RunResult:
        self.argv.append(list(argv))
        out = b"main\n" if "rev-parse" in argv else b" M src/x.py\n"
        return RunResult(exit_code=0, stdout=out)

    async def stat(self, path: str) -> object | None:
        return object() if path == "/workspace/.git" else None


async def test_a_container_env_is_asked_and_host_facts_left_out() -> None:
    env = FakeContainer("/workspace")
    text = await _block(Environment(model="agent-model"), _view("/workspace", env))
    assert " - Primary working directory: /workspace" in text
    assert "Git branch: main" in text and " M src/x.py" in text
    assert " - Shell: " not in text and " - OS Version: " not in text
    assert "You are powered by the model agent-model." in text
    for argv in env.argv:
        assert argv[:3] == ["git", "-c", "core.fsmonitor="]
    assert any("status" in a for a in env.argv)


async def test_the_model_line_prefers_the_task_config(tmp_path: Path) -> None:
    env = LocalExecEnv(str(tmp_path))
    text = await _block(Environment(model="agent-model"), _view(str(tmp_path), env, model="cfg"))
    assert "You are powered by the model cfg." in text
    text = await _block(Environment(model="agent-model"), _view(str(tmp_path), env))
    assert "You are powered by the model agent-model." in text
