"""A job is settled once, from the inbox in order: its end makes it ended,
a pickup's ``JOBS_LOST`` makes it lost, and whatever comes after is
dropped. Job logs live in a private per-user directory."""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
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
)
from noeta.core.extension import TaskView
from noeta.core.loop.state import TaskState
from noeta.core.tool import KeptClaim, ToolContext
from noeta.core.types import Text, user
from noeta.plugins.shell import JOBS_LOST, Registry, Shell
from noeta.plugins.shell.job_logs import DEAD_WORKER_LOGS_KEEP_S
from noeta.plugins.shell.jobs import _KEEP_HEAD, _KEEP_TAIL, JOB_ENDED, Job
from noeta.plugins.shell.state import ShellState
from noeta.plugins.shell.tools import STOP_JOB
from noeta.toolkit.notices import NOTICE_GUARD

OTHER = "some-other-host:4242"


def _view(jobs: dict[str, Any]) -> TaskView:
    return TaskView(state=TaskState(task_id="t", ext={"shell": ShellState(jobs=jobs)}))


def _job(**kw: Any) -> dict[str, Any]:
    return {"job_id": "bg-1", "status": "running", **kw}


def _taken(*bodies: Any) -> Recorded:
    return Recorded(seq=9, at=0.0, event=InputTaken(
        items=[InboxItem(seq=i, at=0.0, body=b) for i, b in enumerate(bodies, 1)],
        through=len(bodies)))


def _lost(*jobs: str) -> External:
    return External(name=JOBS_LOST, data={"jobs": list(jobs or ["bg-1"])})


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


async def test_a_lost_job_is_lost_once_and_its_end_is_dropped() -> None:
    ext = Shell(registry=Registry())
    job = _job(tool_use_id="toolu_1", description="make it", output_file="/logs/bg-1.output")
    view = _view({"bg-1": job})
    await ext.on_event(view, _taken(_lost("other"), _lost(), _ended(), _lost()))
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
    view = _view({"bg-1": _job()})
    await ext.on_event(view, _taken(_ended(), _lost()))
    [rec] = view.pending
    assert isinstance(rec, ExtensionRecorded) and rec.kind == "ended"
    assert rec.data == {"job_id": "bg-1", "status": "completed", "exit_code": 0}
    assert _told(view) == ["<task-notification>done</task-notification>"]


async def test_a_quiet_end_is_recorded_without_a_notice() -> None:
    ext = Shell(registry=Registry())
    view = _view({"bg-1": _job()})
    await ext.on_event(view, _taken(_ended(notice=None)))
    [rec] = view.pending
    assert isinstance(rec, ExtensionRecorded) and rec.kind == "ended"
    assert view.enqueued == []


async def test_a_pickup_posts_the_loss_of_jobs_this_worker_does_not_have(
        tmp_path: Path) -> None:
    registry = Registry(log_dir=str(tmp_path / "logs"))
    here = await registry.spawn(env=LocalExecEnv(str(tmp_path)), command="sleep 30",
                                cwd=str(tmp_path), env_vars={}, task_id="t",
                                tool_use_id="c", description="", post=_posted)
    assert isinstance(here, Job)
    ext = Shell(registry=registry)
    view = _view({"bg-1": _job(), here.job_id: _job(job_id=here.job_id),
                  "done": _job(job_id="done", status="ended")})
    await ext.on_start(view)
    assert view.enqueued == [(_lost("bg-1"), None, None)]
    registry.kill_all("t")
    await asyncio.wait_for(here.done.wait(), 10)


async def test_a_pickup_decides_what_a_crash_left_undecided() -> None:
    ext = Shell(registry=Registry())
    taken = _taken(Say(message=user("hi")), _lost())

    async def read_log(_task_id: str, after: int = 0) -> list[Recorded]:
        return [taken]

    view = _view({"bg-1": _job()})
    view.read_log = read_log
    await ext.on_start(view)
    [rec] = view.pending
    assert isinstance(rec, ExtensionRecorded) and rec.kind == "lost"
    assert len(_told(view)) == 1


async def test_a_parents_stop_kills_the_job_here(tmp_path: Path) -> None:
    registry = Registry(log_dir=str(tmp_path / "logs"))
    job = await registry.spawn(env=LocalExecEnv(str(tmp_path)), command="sleep 30",
                               cwd=str(tmp_path), env_vars={}, task_id="t",
                               tool_use_id="c", description="", post=_posted)
    assert isinstance(job, Job)
    view = _view({job.job_id: _job(job_id=job.job_id)})
    await Shell(registry=registry).on_event(
        view, _taken(External(name=STOP_JOB, data={"job_id": job.job_id})))
    await asyncio.wait_for(job.done.wait(), 10)
    assert job.status == "killed" and job.quiet


async def test_the_tools_say_a_job_was_lost(tmp_path: Path) -> None:
    ext = Shell(registry=Registry())
    for status in ("running", "lost"):
        ctx = ToolContext(task_id="t", call_id="c", env=LocalExecEnv(str(tmp_path)),
                          state={"shell": ShellState(jobs={"bg-1": _job(status=status)})},
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
                                task_id="r", tool_use_id="c",
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
    # One directory per worker process inside it.
    assert Path(job.out_path).parent == logs / f"w{os.getpid()}"
    assert stat.S_IMODE(logs.stat().st_mode) == 0o700
    assert stat.S_IMODE(Path(job.out_path).stat().st_mode) == 0o600
    assert Path(job.out_path).read_text() == "secret\n\n[exited with code 0]\n"


async def test_a_new_worker_removes_the_logs_of_dead_workers(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    dead = await asyncio.create_subprocess_exec("true")
    await dead.wait()
    lately = await asyncio.create_subprocess_exec("true")
    await lately.wait()
    for pid in (dead.pid, lately.pid, os.getppid()):
        (logs / f"w{pid}").mkdir(parents=True)
        (logs / f"w{pid}" / "abc.output").write_text("old")
    day_ago = time.time() - DEAD_WORKER_LOGS_KEEP_S - 60
    for path in (logs / f"w{dead.pid}" / "abc.output", logs / f"w{dead.pid}"):
        os.utime(path, (day_ago, day_ago))
    (logs / "unrelated").mkdir()
    job = await _spawn(Registry(log_dir=str(logs)), tmp_path)
    assert isinstance(job, Job)
    await asyncio.wait_for(job.done.wait(), 10)
    assert not (logs / f"w{dead.pid}").exists()
    # A worker that died lately keeps its logs: a lost job's notice names them.
    assert (logs / f"w{lately.pid}" / "abc.output").exists()
    # A live worker's logs, and what noeta did not make, stay.
    assert (logs / f"w{os.getppid()}" / "abc.output").exists() and (logs / "unrelated").exists()
    assert Path(job.out_path).parent == logs / f"w{os.getpid()}"


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
                               cwd=str(tmp_path), env_vars={}, task_id="r",
                               tool_use_id="c", description="", post=_posted,
                               monitor_timeout_ms=60_000 if monitor else None)
    assert isinstance(job, Job)
    await asyncio.wait_for(job.done.wait(), 10)
    # The command ran to its end (nothing killed it for writing on).
    assert job.exit_code == 0
    # A monitor's events come from its stdout alone, capped the same way.
    text = Path(job.events_path or job.out_path).read_text()
    # The cap, past it one read at most (64 KB), then the note.
    kept, note = text.split("\n[output past 1000 bytes was not kept]\n")
    assert 1000 <= len(kept) <= 1000 + 65536 and set(kept) == {"x"}
    assert note == "\n[exited with code 0]\n" or (monitor and note == "")
    if monitor:  # its output file has what it kept of stdout, the note and stderr:
        # each stream keeps its half of the cap there, whatever order the
        # two keepers run in (stderr's line is never crowded out by stdout).
        out = Path(job.out_path).read_text()
        assert out.count("\n[output past 500 bytes was not kept]\n") == 1, out[-300:]
        assert "late\n" in out


async def test_a_monitors_output_file_stays_within_the_cap(tmp_path: Path) -> None:
    # stderr fills the shared output file first; stdout then goes on to its
    # events file only (the output file was written to twice the cap).
    cap = 200_000
    registry = Registry(log_dir=str(tmp_path / "logs"), log_max_bytes=cap)
    command = ("head -c 300000 /dev/zero | tr '\\0' e >&2; "
               "head -c 300000 /dev/zero | tr '\\0' o; echo end")
    job = await registry.spawn(env=LocalExecEnv(str(tmp_path)), command=command,
                               cwd=str(tmp_path), env_vars={}, task_id="r",
                               tool_use_id="c", description="", post=_posted,
                               monitor_timeout_ms=60_000)
    assert isinstance(job, Job)
    await asyncio.wait_for(job.done.wait(), 10)
    out = Path(job.out_path).read_text()
    assert len(out) <= cap + 2 * 65536 + 200
    assert f"[output past {cap // 2} bytes was not kept]" in out
    events = Path(job.events_path or "").read_text()
    assert events.startswith("o" * cap)


def test_a_keeper_ends_with_its_input_while_its_file_still_grows(tmp_path: Path) -> None:
    # A monitor's stderr keeper shares the output file with stdout's: once
    # stderr closes it ends (it read on, spinning, while stdout wrote).
    shared = tmp_path / "out"
    shared.write_text("")
    script = (_KEEP_HEAD
              + f"(timeout 2 sh -c 'while :; do echo x; done' >> {shared}) >/dev/null 2>&1 &\n"
              f"sleep 0.1\nkeep_head 1000000000 {shared} </dev/null\necho done\n")
    started = time.monotonic()
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert done.stdout.strip() == "done"
    assert time.monotonic() - started < 1.5


async def test_a_foreground_log_keeps_its_start_and_its_true_end(tmp_path: Path) -> None:
    # A foreground command's result is the end of its log: past the cap the
    # log keeps its start and a rolling tail, never only the head.
    registry = Registry(log_dir=str(tmp_path / "logs"), log_max_bytes=1000)
    command = "head -c 300000 /dev/zero | tr '\\0' x; echo; echo end"
    job = await registry.spawn(env=LocalExecEnv(str(tmp_path)), command=command,
                               cwd=str(tmp_path), env_vars={}, task_id="r",
                               tool_use_id="c", description="", post=_posted,
                               foreground=True)
    assert isinstance(job, Job)
    await asyncio.wait_for(job.done.wait(), 10)
    assert job.exit_code == 0
    text = Path(job.out_path).read_text()
    head, _, tail = text.partition("\n[output past the first 250 bytes was not kept, up to "
                                   "its last part below]\n")
    assert head == "x" * 250
    assert tail.rstrip("\n").endswith("x\nend") or tail.endswith("x\nend\n")
    assert 250 <= len(tail) <= 1000 + 65536


async def test_a_foreground_keeper_checks_the_size_once_per_several_reads(
        tmp_path: Path) -> None:
    # Two processes per 64 KB read held a fast command's output to ~35 MB/s.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    real_wc = next(p for p in ("/usr/bin/wc", "/bin/wc") if os.path.exists(p))
    (bin_dir / "wc").write_text(f"#!/bin/sh\necho >>{calls}\nexec {real_wc} \"$@\"\n")
    (bin_dir / "wc").chmod(0o755)
    log = tmp_path / "log"
    script = (_KEEP_TAIL + f'head -c 8388608 /dev/zero | keep_tail {64 << 20} "$1"\n')
    proc = await asyncio.create_subprocess_exec(
        "sh", "-c", script, "sh", str(log),
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"})
    assert await proc.wait() == 0
    assert log.stat().st_size == 8388608
    assert len(calls.read_text().splitlines()) < 128 // 4


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
                               cwd=str(tmp_path), env_vars={}, task_id="r",
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
        task_id="r", tool_use_id="c", description="", post=post)
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


async def test_a_job_dies_about_a_lease_after_its_worker_froze(tmp_path: Path) -> None:
    marker = tmp_path / "late"
    registry = Registry(log_dir=str(tmp_path / "logs"))
    job = await registry.spawn(
        env=_ContainerEnv(str(tmp_path)), command=f"sleep 4; touch {marker}",
        cwd=str(tmp_path), env_vars={}, task_id="r", tool_use_id="c",
        description="", post=_posted, kept=KeptClaim(lease_seconds=1.0))
    assert isinstance(job, Job)
    assert registry._keeper is not None
    registry._keeper.cancel()  # the worker froze: nothing re-creates its file
    await asyncio.wait_for(job.done.wait(), 3.5)
    assert job.exit_code != 0
    await asyncio.sleep(1.0)
    assert not marker.exists()


async def test_a_job_lives_while_its_claim_is_kept(tmp_path: Path) -> None:
    job = await Registry(log_dir=str(tmp_path / "logs")).spawn(
        env=_ContainerEnv(str(tmp_path)), command="sleep 2; wait; echo done",
        cwd=str(tmp_path), env_vars={}, task_id="r", tool_use_id="c",
        description="", post=_posted, kept=KeptClaim(lease_seconds=1.0))
    assert isinstance(job, Job) and job.alive_path
    await asyncio.wait_for(job.done.wait(), 10)
    assert job.exit_code == 0
    assert Path(job.out_path).read_text() == "done\n\n[exited with code 0]\n"


class _ContainerEnv(LocalExecEnv):
    """A local env posing as a container: the worker's process is not
    visible to the job's guard."""

    local = False


async def test_a_foreground_job_in_a_container_dies_once_its_worker_stops(
        tmp_path: Path) -> None:
    marker = tmp_path / "late"
    registry = Registry(log_dir=str(tmp_path / "logs"), alive_s=1.0)
    job = await registry.spawn(
        env=_ContainerEnv(str(tmp_path)), command=f"sleep 4; touch {marker}",
        cwd=str(tmp_path), env_vars={}, task_id="r", tool_use_id="c",
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


async def test_a_foreground_job_moved_to_the_background_outlives_its_guard_period(
        tmp_path: Path) -> None:
    # Moved to the background, it keeps its guard's period.
    registry = Registry(log_dir=str(tmp_path / "logs"), alive_s=1.0)
    job = await registry.spawn(
        env=_ContainerEnv(str(tmp_path)), command="sleep 4; echo done",
        cwd=str(tmp_path), env_vars={}, task_id="r", tool_use_id="c",
        description="", post=_posted, foreground=True)
    assert isinstance(job, Job)
    assert registry.to_background(job, None)
    registry.keep_with(job, KeptClaim())
    await asyncio.wait_for(job.done.wait(), 10)
    assert job.exit_code == 0


async def test_an_ended_job_no_longer_listens_for_its_claim_being_lost(
        tmp_path: Path) -> None:
    registry = Registry(log_dir=str(tmp_path / "logs"))
    job = await registry.spawn(
        env=LocalExecEnv(str(tmp_path)), command="sleep 0.2", cwd=str(tmp_path),
        env_vars={}, task_id="r", tool_use_id="c", description="",
        post=_posted)
    assert isinstance(job, Job)
    kept = KeptClaim()
    registry.keep_with(job, kept)
    assert len(kept._on_lost) == 1
    await asyncio.wait_for(job.done.wait(), 5)
    assert kept._on_lost == []


async def test_ended_jobs_are_kept_only_up_to_a_bound(tmp_path: Path) -> None:
    reg = Registry(log_dir=str(tmp_path / "logs"), ended_jobs_keep=3)
    env = LocalExecEnv(str(tmp_path))
    posted: list[Any] = []

    async def post(body: Any, **kw: Any) -> None:
        posted.append(body)

    jobs = []
    for n in range(5):
        job = await reg.spawn(env=env, command="true", cwd=str(tmp_path), env_vars={},
                              task_id="r", tool_use_id=f"toolu_{n}",
                              description="d", post=post)
        assert isinstance(job, Job)
        await job.done.wait()
        jobs.append(job)
    assert len(posted) == 5  # every end was told before it was let go
    assert list(reg.jobs["r"]) == [j.job_id for j in jobs[2:]]
    reg.retire("r")
    await asyncio.gather(*reg._retiring)
    assert not any(Path(j.out_path).exists() for j in jobs)  # dropped ones too


async def test_a_job_let_go_of_keeps_its_log_until_its_root_ends(tmp_path: Path) -> None:
    # Its end notice told the model to read the file: a long session's root
    # forgets old jobs but keeps their output logs, within a byte bound.
    reg = Registry(log_dir=str(tmp_path / "logs"), ended_jobs_keep=3,
                   let_go_logs_max_bytes=60)
    env = LocalExecEnv(str(tmp_path))

    async def post(body: Any, **kw: Any) -> None:
        pass

    jobs = []
    for n in range(8):
        job = await reg.spawn(env=env, command="echo out", cwd=str(tmp_path), env_vars={},
                              task_id="r", tool_use_id=f"toolu_{n}",
                              description="d", post=post)
        assert isinstance(job, Job)
        await job.done.wait()
        await asyncio.gather(*reg._retiring)
        jobs.append(job)
    assert list(reg.jobs["r"]) == [j.job_id for j in jobs[-3:]]
    # Each log is 25 bytes: the two newest of the five let go fit in 60.
    on_disk = [p for p in (tmp_path / "logs").rglob("*") if p.is_file()]
    assert sorted(map(str, on_disk)) == sorted(j.out_path for j in jobs[-5:])
    reg.retire("r")
    await asyncio.gather(*reg._retiring)
    assert [p for p in (tmp_path / "logs").rglob("*") if p.is_file()] == []


async def test_a_long_server_keeps_a_bounded_number_of_roots(tmp_path: Path) -> None:
    # Roots that never end here (idle, or taken by another worker) are let
    # go of, least recently used first, with their logs.
    reg = Registry(log_dir=str(tmp_path / "logs"), tasks_keep=3)
    env = LocalExecEnv(str(tmp_path))
    jobs = []
    for n in range(6):
        job = await reg.spawn(env=env, command="echo out", cwd=str(tmp_path), env_vars={},
                              task_id=f"r{n}", tool_use_id="c",
                              description="d", post=_posted)
        assert isinstance(job, Job)
        await job.done.wait()
        jobs.append(job)
    await asyncio.gather(*reg._retiring)
    assert list(reg.jobs) == ["r3", "r4", "r5"]
    on_disk = [p for p in (tmp_path / "logs").rglob("*") if p.is_file()]
    assert sorted(map(str, on_disk)) == sorted(j.out_path for j in jobs[-3:])


async def test_a_root_with_a_running_job_is_not_let_go_of(tmp_path: Path) -> None:
    reg = Registry(log_dir=str(tmp_path / "logs"), tasks_keep=1)
    env = LocalExecEnv(str(tmp_path))
    long = await reg.spawn(env=env, command="sleep 30", cwd=str(tmp_path), env_vars={},
                           task_id="a", tool_use_id="c", description="d",
                           post=_posted)
    assert isinstance(long, Job)
    short = await reg.spawn(env=env, command="true", cwd=str(tmp_path), env_vars={},
                            task_id="b", tool_use_id="c", description="d",
                            post=_posted)
    assert isinstance(short, Job)
    await short.done.wait()
    assert set(reg.jobs) == {"a", "b"}
    reg.retire("a")
    await asyncio.gather(*reg._retiring)


async def test_a_job_whose_claim_is_lost_leaves_with_its_logs(tmp_path: Path) -> None:
    reg = Registry(log_dir=str(tmp_path / "logs"))
    kept = KeptClaim()
    env = LocalExecEnv(str(tmp_path))
    ended = await reg.spawn(env=env, command="true", cwd=str(tmp_path), env_vars={},
                            task_id="r", tool_use_id="c", description="d",
                            post=_posted)
    assert isinstance(ended, Job)
    await ended.done.wait()
    job = await reg.spawn(env=env, command="sleep 30", cwd=str(tmp_path), env_vars={},
                          task_id="r", tool_use_id="c", description="d",
                          post=_posted, kept=kept)
    assert isinstance(job, Job)
    kept.mark_lost()  # the task left this worker
    await asyncio.wait_for(job.done.wait(), 5)
    await asyncio.gather(*reg._retiring)
    assert list(reg.jobs["r"]) == [ended.job_id]
    assert not Path(job.out_path).exists() and Path(ended.out_path).exists()


async def test_the_stall_watch_survives_an_env_that_fails_to_read(
        caplog: pytest.LogCaptureFixture) -> None:
    class Broken:
        calls = 0

        async def stat(self, path: str) -> None:
            Broken.calls += 1
            if Broken.calls >= 3:
                job.status = "done"
            raise RuntimeError("the container went away")

    job: Any = type("J", (), {"status": "running", "job_id": "j", "out_path": "/x",
                              "env": Broken()})()
    registry = Registry(stall_check_s=0.001)
    with caplog.at_level("WARNING", logger="noeta.plugins.shell"):
        await asyncio.wait_for(registry._stall(job), 5)
    assert Broken.calls == 3
    assert "cannot read the size of job j's output" in caplog.text


def test_a_late_keeper_keeps_its_share_of_a_full_shared_file(tmp_path: Path) -> None:
    # The race that lost a monitor's stderr: stdout's keeper filled the
    # shared output file before stderr's keeper first looked. Each keeper
    # counts only its own bytes, so stderr's line is kept and no second note
    # is written.
    shared = tmp_path / "out"
    shared.write_text("x" * 5000 + "\n[output past 500 bytes was not kept]\n")
    script = _KEEP_HEAD + f"printf 'late\\n' | keep_head 500 {shared}\n"
    subprocess.run(["bash", "-c", script], check=True, timeout=10)
    text = shared.read_text()
    assert text.endswith("late\n") and text.count("was not kept") == 1
