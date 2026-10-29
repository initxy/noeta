"""A job is settled once, from the inbox in order: its end makes it ended,
the loss of the hold it ran under makes it lost, and whatever comes after
is dropped. Job logs live in a private per-user directory."""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.core.events import (
    ExtensionRecorded,
    External,
    InboxItem,
    InputTaken,
    Recorded,
    Say,
    TaskEnded,
)
from noeta.core.extension import TaskView
from noeta.core.holds import TreeHold
from noeta.core.loop.state import TaskState
from noeta.core.store import HOLD_LOST
from noeta.core.tool import ToolContext
from noeta.core.types import Text, user
from noeta.plugins.shell import Registry, Shell
from noeta.plugins.shell.bash import ShellState
from noeta.plugins.shell.jobs import JOB_ENDED, Job
from noeta.toolkit.notices import NOTICE_GUARD

OTHER = "some-other-host:4242"


def _view(jobs: dict[str, Any]) -> TaskView:
    return TaskView(state=TaskState(task_id="t", ext={"shell": ShellState(jobs=jobs)}))


def _job(worker: str, **kw: Any) -> dict[str, Any]:
    return {"job_id": "bg-1", "status": "running", "worker": worker, **kw}


def _taken(*bodies: Any) -> Recorded:
    return Recorded(seq=9, at=0.0, event=InputTaken(
        items=[InboxItem(seq=i, at=0.0, body=b) for i, b in enumerate(bodies, 1)],
        through=len(bodies)))


def _lost(epoch: int = 7, worker: str = OTHER) -> External:
    return External(name=HOLD_LOST, data={"worker": worker, "epoch": epoch})


def _ended(notice: str | None = "<task-notification>done</task-notification>") -> External:
    return External(name=JOB_ENDED, data={"job_id": "bg-1", "status": "completed",
                                          "exit_code": 0, "notice": notice})


def _told(view: TaskView) -> list[str]:
    out = []
    for body, key, target in view.enqueued:
        assert isinstance(body, Say) and body.when == "now" and key == "shell:notice:bg-1"
        assert target is None
        assert body.message.origin == "system"
        out.append(body.message.text)
    return out


async def test_a_job_whose_hold_is_lost_is_lost_once_and_its_end_is_dropped() -> None:
    ext = Shell(registry=Registry())
    job = _job(OTHER, hold=7, tool_use_id="toolu_1", description="make it",
               output_file="/logs/bg-1.output")
    view = _view({"bg-1": job})
    await ext.on_event(view, _taken(_lost(epoch=6), _lost(), _ended(), _lost()))
    [rec] = view.pending
    assert isinstance(rec, ExtensionRecorded) and (rec.kind, rec.data) == ("lost",
                                                                          {"job_id": "bg-1"})
    assert rec.message is None
    [text] = _told(view)
    assert text == (
        f"{NOTICE_GUARD}\n\n<task-notification>\n<task-id>bg-1</task-id>\n"
        "<tool-use-id>toolu_1</tool-use-id>\n<output-file>/logs/bg-1.output</output-file>\n"
        "<status>failed</status>\n<summary>Background command \"make it\" was lost: the "
        "worker process running it stopped, so its further output and exit status are "
        "unknown</summary>\n</task-notification>")
    later = _view(ext.reduce(ShellState(jobs={"bg-1": job}), "lost", rec.data).jobs)
    await ext.on_event(later, _taken(_ended()))
    assert later.pending == [] and later.enqueued == []


async def test_a_job_that_ended_is_not_lost_after() -> None:
    ext = Shell(registry=Registry())
    view = _view({"bg-1": _job(OTHER, hold=7)})
    await ext.on_event(view, _taken(_ended(), _lost()))
    [rec] = view.pending
    assert isinstance(rec, ExtensionRecorded) and rec.kind == "ended"
    assert rec.data == {"job_id": "bg-1", "status": "completed", "exit_code": 0}
    assert _told(view) == ["<task-notification>done</task-notification>"]


async def test_a_quiet_end_is_recorded_without_a_notice() -> None:
    ext = Shell(registry=Registry())
    view = _view({"bg-1": _job(OTHER, hold=7)})
    await ext.on_event(view, _taken(_ended(notice=None)))
    [rec] = view.pending
    assert isinstance(rec, ExtensionRecorded) and rec.kind == "ended"
    assert view.enqueued == []


def _sub_agent_log(*, ended: bool, epoch: int = 7) -> list[Recorded]:
    started = ExtensionRecorded(extension="shell", kind="started", data=_job(
        OTHER, hold=epoch, tool_use_id="toolu_9", description="crunch"))
    out = [Recorded(seq=2, at=0.0, event=started)]
    if ended:
        out.append(Recorded(seq=3, at=0.0, event=TaskEnded(status="done")))
    return out


def _root_of(children: dict[str, list[Recorded]]) -> TaskView:
    view = _view({})

    async def child_tasks(task_id: str) -> list[str]:
        return [c for c in children if c.rsplit(".", 1)[0] == task_id]

    async def read_log(task_id: str, after: int = 0) -> list[Recorded]:
        return children[task_id]

    view.child_tasks, view.read_log = child_tasks, read_log
    return view


async def test_the_root_tells_the_loss_of_a_job_whose_sub_agent_ended() -> None:
    ext = Shell(registry=Registry())
    view = _root_of({"t.a": _sub_agent_log(ended=True), "t.a.b": [],
                     "t.c": _sub_agent_log(ended=False)})
    await ext.on_event(view, _taken(_lost()))
    [text] = _told(view)
    assert "<tool-use-id>toolu_9</tool-use-id>" in text and "<status>failed</status>" in text
    assert view.pending == []  # nothing of the root's own changes


async def test_only_the_lost_hold_and_only_the_root() -> None:
    ext = Shell(registry=Registry())
    other_hold = _root_of({"t.a": _sub_agent_log(ended=True, epoch=8)})
    await ext.on_event(other_hold, _taken(_lost()))
    assert other_hold.enqueued == []
    child = _root_of({"t.a": _sub_agent_log(ended=True)})
    child.state.parent_id = "p"
    await ext.on_event(child, _taken(_lost()))
    assert child.enqueued == []


async def test_a_pickup_decides_what_a_crash_left_undecided() -> None:
    ext = Shell(registry=Registry())
    taken = _taken(Say(message=user("hi")), _lost())

    async def read_log(_task_id: str, after: int = 0) -> list[Recorded]:
        return [taken]

    view = _view({"bg-1": _job(OTHER, hold=7)})
    view.read_log = read_log
    await ext.on_start(view)
    [rec] = view.pending
    assert isinstance(rec, ExtensionRecorded) and rec.kind == "lost"
    assert len(_told(view)) == 1


async def test_the_tools_say_a_job_was_lost(tmp_path: Path) -> None:
    ext = Shell(registry=Registry())
    for status in ("running", "lost"):
        ctx = ToolContext(task_id="t", call_id="c", env=LocalExecEnv(str(tmp_path)),
                          state={"shell": ShellState(jobs={"bg-1": _job(OTHER, status=status)})},
                          record=lambda *_: None, root_id="t")
        for tool in ext.tools(_view({})):
            if tool.name == "TaskStop":
                out = await tool.run({"task_id": "bg-1"}, ctx)
                body = "".join(b.text for b in out.content if isinstance(b, Text))
                assert out.is_error and "was lost" in body, body


async def _posted(_body: Any, task_id: str | None = None, **_kw: Any) -> None:
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
    # The temp directory on disk, never the small RAM-backed runtime one.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(runtime))
    job = await _spawn(Registry(), tmp_path)
    assert isinstance(job, Job)
    await asyncio.wait_for(job.done.wait(), 10)
    logs = runtime / f"noeta-background-{os.getuid()}"
    assert Path(job.out_path).parent == logs
    assert stat.S_IMODE(logs.stat().st_mode) == 0o700
    assert stat.S_IMODE(Path(job.out_path).stat().st_mode) == 0o600
    assert Path(job.out_path).read_text() == "secret\n\n[exited with code 0]\n"


async def test_a_planted_log_directory_is_refused(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = tmp_path / "run"
    runtime.mkdir()
    monkeypatch.setenv("TMPDIR", str(runtime))
    elsewhere = tmp_path / "attacker"
    elsewhere.mkdir()
    (runtime / f"noeta-background-{os.getuid()}").symlink_to(elsewhere)
    refused = await _spawn(Registry(), tmp_path)
    assert isinstance(refused, str) and "not a directory owned by the current user" in refused
    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize("monitor", [False, True])
async def test_a_log_stops_growing_at_its_cap_and_says_so(tmp_path: Path, monitor: bool) -> None:
    registry = Registry(log_dir=str(tmp_path / "logs"), log_max_bytes=1000)
    command = "head -c 300000 /dev/zero | tr '\\0' x; echo; echo late >&2; echo end"
    job = await registry.spawn(env=LocalExecEnv(str(tmp_path)), command=command,
                               cwd=str(tmp_path), env_vars={}, root_id="r", spawner_id="r",
                               tool_use_id="c", description="", post=_posted,
                               monitor_timeout_ms=60_000 if monitor else None)
    assert isinstance(job, Job)
    await asyncio.wait_for(job.done.wait(), 10)
    # The command ran to its end (nothing killed it for writing on).
    assert job.exit_code == 0
    text = Path(job.out_path).read_text()
    # The cap, past it one read at most (64 KB), then the note.
    kept, note = text.split("\n[output past 1000 bytes was not kept]\n")
    assert 1000 <= len(kept) <= 1000 + 65536 and set(kept) == {"x"}
    assert note == "\n[exited with code 0]\n" or (monitor and note == "")


async def test_a_job_counts_as_running_until_its_end_is_posted(tmp_path: Path) -> None:
    # Whoever waits for a tree's jobs to finish (a front end before its next
    # prompt) must find the end in the inbox once none runs: a job whose
    # command exited but whose end is not posted yet still counts.
    entered, posting = asyncio.Event(), asyncio.Event()

    async def slow_post(_body: Any, task_id: str | None = None, **_kw: Any) -> None:
        entered.set()
        await posting.wait()

    registry = Registry(log_dir=str(tmp_path / "logs"))
    job = await registry.spawn(env=LocalExecEnv(str(tmp_path)), command="true",
                               cwd=str(tmp_path), env_vars={}, root_id="r", spawner_id="r",
                               tool_use_id="c", description="", post=slow_post)
    assert isinstance(job, Job)
    await asyncio.wait_for(entered.wait(), 10)
    assert job.status == "exited"
    assert registry.running("r") == [job]
    posting.set()
    await asyncio.wait_for(job.done.wait(), 10)
    assert registry.running("r") == []


async def test_a_loose_existing_directory_is_tightened(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir(mode=0o755)
    logs.chmod(0o755)
    job = await _spawn(Registry(log_dir=str(logs)), tmp_path)
    assert isinstance(job, Job)
    await asyncio.wait_for(job.done.wait(), 10)
    assert stat.S_IMODE(logs.stat().st_mode) == 0o700


_WORKER = """
import asyncio, sys
from noeta.backends.execenvs.local import LocalExecEnv
from noeta.plugins.shell import Registry

async def main(work, marker):
    async def post(*_a, **_k):
        return None
    job = await Registry(log_dir=work + "/logs").spawn(
        env=LocalExecEnv(work), command=f"sleep 1.5; touch {marker}", cwd=work, env_vars={},
        root_id="r", spawner_id="r", tool_use_id="c", description="", post=post)
    print("started", flush=True)
    await asyncio.sleep(60)

asyncio.run(main(sys.argv[1], sys.argv[2]))
"""


async def test_a_job_dies_with_its_worker_process(tmp_path: Path) -> None:
    marker = tmp_path / "late"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", _WORKER, str(tmp_path), str(marker),
        stdout=asyncio.subprocess.PIPE)
    assert proc.stdout is not None
    assert await asyncio.wait_for(proc.stdout.readline(), 30) == b"started\n"
    proc.kill()
    await proc.wait()
    await asyncio.sleep(2.5)  # past the end of the command
    assert not marker.exists()


async def test_a_job_dies_once_its_hold_goes_stale(tmp_path: Path) -> None:
    marker = tmp_path / "late"
    tree = TreeHold(root_id="r", worker="w", epoch=1, seconds=1.0)
    job = await Registry(log_dir=str(tmp_path / "logs")).spawn(
        env=LocalExecEnv(str(tmp_path)), command=f"sleep 4; touch {marker}",
        cwd=str(tmp_path), env_vars={}, root_id="r", spawner_id="r", tool_use_id="c",
        description="", post=_posted, tree=tree)
    assert isinstance(job, Job)
    tree.renewed -= 60  # the worker froze: nothing renewed its hold
    await asyncio.wait_for(job.done.wait(), 3.5)
    assert job.exit_code != 0
    await asyncio.sleep(1.0)
    assert not marker.exists()


async def test_a_job_lives_while_its_hold_is_fresh(tmp_path: Path) -> None:
    tree = TreeHold(root_id="r", worker="w", epoch=1, seconds=1.0)
    job = await Registry(log_dir=str(tmp_path / "logs")).spawn(
        env=LocalExecEnv(str(tmp_path)), command="sleep 2; wait; echo done",
        cwd=str(tmp_path), env_vars={}, root_id="r", spawner_id="r", tool_use_id="c",
        description="", post=_posted, tree=tree)
    assert isinstance(job, Job)
    while not job.done.is_set():
        tree.renewed = time.monotonic()
        await asyncio.sleep(0.1)
    assert job.exit_code == 0
    assert Path(job.out_path).read_text() == "done\n\n[exited with code 0]\n"


class _ContainerEnv(LocalExecEnv):
    """A local env posing as a container: the worker's process is not
    visible to the job's guard."""

    local = False


async def test_a_foreground_job_in_a_container_dies_once_its_worker_stops(
        tmp_path: Path) -> None:
    marker = tmp_path / "late"
    registry = Registry(log_dir=str(tmp_path / "logs"), foreground_alive_s=1.0)
    job = await registry.spawn(
        env=_ContainerEnv(str(tmp_path)), command=f"sleep 4; touch {marker}",
        cwd=str(tmp_path), env_vars={}, root_id="r", spawner_id="r", tool_use_id="c",
        description="", post=_posted, foreground=True)
    assert isinstance(job, Job) and job.alive_path
    await asyncio.sleep(0.6)
    assert job.status == "running"  # kept alive while the worker runs
    assert registry._keeper is not None
    registry._keeper.cancel()  # the worker froze: nothing re-creates its file
    await asyncio.wait_for(job.done.wait(), 3.5)
    assert job.exit_code != 0
    await asyncio.sleep(1.0)
    assert not marker.exists()
