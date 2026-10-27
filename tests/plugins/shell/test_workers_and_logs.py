"""Review fixes: a job running in another worker is reported as such (not
lost), and job logs live in a private per-user directory."""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.execenv.local import LocalExecEnv
from noeta.core.events import ExtensionRecorded
from noeta.core.extension import TaskView
from noeta.core.loop.state import TaskState
from noeta.core.tool import ToolContext
from noeta.core.types import Text
from noeta.plugins.shell import Registry, Shell
from noeta.plugins.shell.bash import ShellState
from noeta.plugins.shell.jobs import Job

OTHER = "some-other-host:4242"


def _view(jobs: dict[str, Any]) -> TaskView:
    return TaskView(state=TaskState(task_id="t", ext={"shell": ShellState(jobs=jobs)}))


def _job(worker: str, **kw: Any) -> dict[str, Any]:
    return {"job_id": "bg-1", "status": "running", "worker": worker, **kw}


async def test_a_job_in_another_worker_is_noted_once_not_lost() -> None:
    ext = Shell(registry=Registry())
    view = _view({"bg-1": _job(OTHER)})
    await ext.on_start(view)
    [rec] = view.pending
    assert isinstance(rec, ExtensionRecorded) and rec.kind == "elsewhere"
    assert rec.message is not None and rec.message.origin == "system"
    assert rec.message.text == ("Background job bg-1 is running in another worker process; "
                                "it cannot be read or stopped from here.")
    state = ext.reduce(ShellState(jobs={"bg-1": _job(OTHER)}), "elsewhere", rec.data)
    assert state.jobs["bg-1"]["status"] == "running"
    again = _view(state.jobs)
    await ext.on_start(again)
    assert again.pending == []


async def test_a_job_whose_worker_is_gone_is_lost() -> None:
    import socket

    registry = Registry()
    ext = Shell(registry=registry)
    dead = f"{socket.gethostname()}:{2 ** 22 + 12345}"
    for worker in (dead, registry.worker):
        view = _view({"bg-1": _job(worker)})
        await ext.on_start(view)
        [rec] = view.pending
        assert isinstance(rec, ExtensionRecorded) and rec.kind == "lost"


async def test_the_tools_say_where_the_job_runs(tmp_path: Path) -> None:
    registry = Registry()
    ext = Shell(registry=registry)
    ctx = ToolContext(task_id="t", call_id="c", env=LocalExecEnv(str(tmp_path)),
                      state={"shell": ShellState(jobs={"bg-1": _job(OTHER)})},
                      record=lambda *_: None, root_id="t")
    for tool in ext.tools(_view({})):
        if tool.name in ("BashOutput", "KillShell"):
            out = await tool.run({"bash_id": "bg-1", "shell_id": "bg-1"}, ctx)
            body = "".join(b.text for b in out.content if isinstance(b, Text))
            assert out.is_error and "running in another worker process" in body, body


async def _posted(_body: Any, task_id: str | None = None) -> None:
    return None


async def _spawn(registry: Registry, tmp_path: Path) -> Job | str:
    env = LocalExecEnv(str(tmp_path))
    return await registry.spawn(env=env, command="echo secret", cwd=str(tmp_path), env_vars={},
                                root_id="r", spawner_id="r", tool_use_id="c",
                                description="", post=_posted)


async def test_logs_go_to_a_private_per_user_directory(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = tmp_path / "run"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    job = await _spawn(Registry(), tmp_path)
    assert isinstance(job, Job)
    await asyncio.wait_for(job.done.wait(), 10)
    logs = runtime / f"noeta-background-{os.getuid()}"
    assert Path(job.out_path).parent == logs
    assert stat.S_IMODE(logs.stat().st_mode) == 0o700
    assert stat.S_IMODE(Path(job.out_path).stat().st_mode) == 0o600
    assert Path(job.out_path).read_text() == "secret\n"


async def test_a_planted_log_directory_is_refused(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = tmp_path / "run"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    elsewhere = tmp_path / "attacker"
    elsewhere.mkdir()
    (runtime / f"noeta-background-{os.getuid()}").symlink_to(elsewhere)
    refused = await _spawn(Registry(), tmp_path)
    assert isinstance(refused, str) and "not a directory owned by the current user" in refused
    assert list(elsewhere.iterdir()) == []


async def test_a_loose_existing_directory_is_tightened(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir(mode=0o755)
    logs.chmod(0o755)
    job = await _spawn(Registry(log_dir=str(logs)), tmp_path)
    assert isinstance(job, Job)
    await asyncio.wait_for(job.done.wait(), 10)
    assert stat.S_IMODE(logs.stat().st_mode) == 0o700
