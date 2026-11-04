"""Background shells under the runner: an end notice wakes an idle task into
a turn and is recorded; cancelling and turn ends kill jobs silently, a
stopped turn leaves them running; a task with a running job keeps its claim
on its worker over its waits, and a job whose worker is gone is marked lost
by the next worker to pick the task up."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.errors import StoreUnavailable
from noeta.core.events import ExtensionRecorded, External, TaskCreated, ToolFinished
from noeta.core.loop.state import fold
from noeta.core.runner import Runtime
from noeta.core.store import NewTask, Post
from noeta.plugins.fs import FileTools
from noeta.plugins.shell import JOB_ENDED, Registry, Shell
from noeta.testing import ScriptedProvider, reply
from noeta.toolkit.notices import NOTICE_GUARD


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def _no_sleep(_: float) -> None:
    return None


def _setup(store: SqliteStore, provider: ScriptedProvider, tmp: Path,
           **kw: Any) -> tuple[Runtime, Shell]:
    registry = kw.pop("registry", None) or Registry(log_dir=str(tmp / "logs"))
    ext = Shell(registry=registry, **kw)
    agent = Agent(name="a", model="m", extensions=[ext, FileTools()])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 sleep=_no_sleep)
    return rt, ext


async def _jobs(client: Client, tid: str, ext: Shell) -> dict[str, Any]:
    st = fold(await client.events(tid), {ext.name: ext.reduce}, {ext.name: ext.initial_state()})
    jobs: dict[str, Any] = st.ext[ext.name].jobs
    return jobs


async def _settle(ext: Shell) -> None:
    for jobs in list(ext.registry.jobs.values()):
        for job in list(jobs.values()):
            await asyncio.wait_for(job.done.wait(), 10)
            assert job.sender is not None
            await asyncio.wait_for(job.sender, 10)


def _bg(command: str) -> tuple[str, dict[str, Any]]:
    return ("Bash", {"command": command, "run_in_background": True})


@contextlib.asynccontextmanager
async def _serving(rt: Runtime) -> AsyncIterator[None]:
    stop = asyncio.Event()
    serving = asyncio.create_task(rt.serve(stop))
    try:
        yield
    finally:
        stop.set()
        await serving


async def _until(check: Callable[[], Awaitable[bool]], timeout: float = 10.0) -> None:
    async def loop() -> None:
        while not await check():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(loop(), timeout)


def _released(client: Client, tid: str) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        row = await client.get(tid)
        return row is not None and row.status == "waiting" and row.owner is None

    return check


def _kept_waiting(client: Client, tid: str, rt: Runtime) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        row = await client.get(tid)
        return row is not None and row.status == "waiting" and row.owner == rt.worker

    return check


async def test_an_end_notice_wakes_the_idle_task(store: SqliteStore, tmp_path: Path) -> None:
    # The job outlives the turn, so its notice wakes an idle task (an instant
    # job could finish mid-turn and ride in the same turn instead).
    provider = ScriptedProvider([reply("", _bg("sleep 0.3; echo done-here")),
                                 reply("started it"), reply("it finished")])
    rt, ext = _setup(store, provider, tmp_path)
    client = Client(store)
    tid = await client.create("a", "go", config={"cwd": str(tmp_path)})
    await rt.run_until_idle()  # the task goes on waiting with its claim kept
    await _settle(ext)
    await _until(_released(client, tid))
    notice = provider.requests[2].messages[-1]
    assert notice.origin == "system"
    assert notice.text.startswith(f"{NOTICE_GUARD}\n\n<task-notification>\n<task-id>")
    assert "<output-file>" in notice.text
    assert notice.text.endswith("completed (exit code 0)</summary>\n</task-notification>")
    [job] = (await _jobs(client, tid, ext)).values()
    assert job["status"] == "completed" and job["command"] == "sleep 0.3; echo done-here"
    row = await client.get(tid)
    assert row.waiting_on == "input" and row.owner is None


async def test_cancelling_the_task_kills_its_jobs_silently(store: SqliteStore,
                                                           tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", _bg("sleep 30")), reply("ok")])
    rt, ext = _setup(store, provider, tmp_path)
    client = Client(store)
    tid = await client.create("a", "go", config={"cwd": str(tmp_path)})
    async with _serving(rt):
        await _until(_kept_waiting(client, tid, rt))
        [job] = ext.registry.jobs[tid].values()
        await client.cancel(tid)
        await asyncio.wait_for(job.done.wait(), 10)
        assert job.sender is not None
        await asyncio.wait_for(job.sender, 10)
    assert job.status == "killed" and job.quiet
    assert (await client.get(tid)).status == "ended"
    inbox = await store.inbox(tid)
    assert not any("task-notification" in str(i.body) for i in inbox)
    # The ended task's jobs and logs are dropped once they are gone.
    for _ in range(100):
        if tid not in ext.registry.jobs:
            break
        await asyncio.sleep(0.02)
    assert tid not in ext.registry.jobs
    assert not Path(job.out_path).exists()


async def test_stopping_the_turn_leaves_its_jobs_running(store: SqliteStore,
                                                         tmp_path: Path) -> None:
    # Claude Code's background tasks outlive an interrupt.
    provider = ScriptedProvider([reply("", _bg("sleep 30"), ("Bash", {"command": "sleep 1"}))])
    rt, ext = _setup(store, provider, tmp_path)
    client = Client(store)
    tid = await client.create("a", "go", config={"cwd": str(tmp_path)})
    async with _serving(rt):
        async def started() -> bool:
            return any(not j.foreground for j in ext.registry.jobs.get(tid, {}).values())

        await _until(started)
        await client.cancel(tid, turn_only=True)
        await _until(_kept_waiting(client, tid, rt))
        [job] = [j for j in ext.registry.jobs[tid].values() if not j.foreground]
        await asyncio.sleep(0.1)
        assert job.status == "running" and not job.done.is_set()
        await client.cancel(tid)
        await asyncio.wait_for(job.done.wait(), 10)
    assert job.status == "killed"


async def test_jobs_end_with_the_turn_when_configured(store: SqliteStore,
                                                      tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", _bg("sleep 30")), reply("done")])
    rt, ext = _setup(store, provider, tmp_path, background_ends_with_turn=True)
    client = Client(store)
    tid = await client.create("a", "go", config={"cwd": str(tmp_path)})
    await rt.run_until_idle()
    [result] = [r.event.result for r in await client.events(tid)
                if isinstance(r.event, ToolFinished)]
    assert "stopped when you give your final response" in result.text
    [job] = ext.registry.jobs[tid].values()
    await asyncio.wait_for(job.done.wait(), 10)
    assert job.status == "killed" and job.quiet


async def test_a_task_with_a_job_stays_with_its_worker_until_that_worker_is_gone(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = str(tmp_path / "t.db")
    here, there = SqliteStore(path), SqliteStore(path)  # two worker processes
    provider = ScriptedProvider([reply("", _bg("sleep 30")), reply("ok"), reply("told")])
    rt, ext = _setup(here, provider, tmp_path)
    rt.lease_seconds = 0.3
    rt2, ext2 = _setup(there, provider, tmp_path / "2")
    client = Client(there)
    tid = await client.create("a", "go", config={"cwd": str(tmp_path)})
    async with _serving(rt):
        await _until(_kept_waiting(client, tid, rt))
        await client.send(tid, "status?")  # heard by polling: another process's post
        await asyncio.sleep(0.5)  # longer than a lease: the first worker renews it
        assert not await rt2.run_once()

        async def answered() -> bool:
            return len(provider.requests) == 3  # by the first worker

        await _until(answered)

        # The first worker loses the store: its lease runs out, and the job
        # dies with the claim (nothing posts its end).
        async def unreachable(*_a: Any, **_k: Any) -> Any:
            raise StoreUnavailable("gone")

        monkeypatch.setattr(here, "renew", unreachable)
        [job] = ext.registry.jobs[tid].values()
        await asyncio.wait_for(job.done.wait(), 10)
    assert job.status == "killed" and job.fenced
    provider.add(reply("lost it"))
    await client.send(tid, "and now?")
    await asyncio.sleep(rt.lease_seconds)
    await rt2.run_until_idle()
    [state] = (await _jobs(client, tid, ext2)).values()
    assert state["status"] == "lost"
    *_, said, told = provider.requests[-1].messages
    assert said.text == "and now?"
    assert told.origin == "system" and told.text.startswith(f"{NOTICE_GUARD}\n\n")
    assert "<status>failed</status>" in told.text
    assert 'Background command "sleep 30" was lost: the worker process' in told.text
    await here.close()
    await there.close()


async def test_the_claim_is_given_back_when_the_job_ends(store: SqliteStore,
                                                         tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", _bg("sleep 0.2")), reply("ok"), reply("noted"),
                                 reply("again")])
    rt, ext = _setup(store, provider, tmp_path)
    client = Client(store)
    tid = await client.create("a", "go", config={"cwd": str(tmp_path)})
    await rt.run_until_idle()
    await _settle(ext)
    await _until(_released(client, tid))  # the end came in while the claim was kept
    row = await client.get(tid)
    assert row is not None and row.status == "waiting" and row.owner is None
    assert [j["status"] for j in (await _jobs(client, tid, ext)).values()] == ["completed"]
    rt2, _ext2 = _setup(store, provider, tmp_path)
    await client.send(tid, "next")
    assert await rt2.run_once()


async def test_a_killed_job_is_recorded(store: SqliteStore, tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", _bg("sleep 30")), reply("ok"), reply("again")])
    rt, ext = _setup(store, provider, tmp_path)
    client = Client(store)
    tid = await client.create("a", "go", config={"cwd": str(tmp_path)})
    async with _serving(rt):
        await _until(_kept_waiting(client, tid, rt))
        ext.registry.kill_all(tid)
        await _until(_released(client, tid))
    [job] = (await _jobs(client, tid, ext)).values()
    assert job["status"] == "killed"


class Delegate:
    name = "Delegate"
    description = "Hand work to a sub-agent."
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    async def run(self, input: dict[str, Any], ctx: Any) -> Any:
        from noeta.core.tool import Park, ToolOutput

        child = ctx.spawn("worker", input["prompt"], config={"cwd": input["cwd"]})
        return ToolOutput(park=Park(on="children", keys=[child]))

    async def resume(self, input: dict[str, Any], ctx: Any, outcome: Any) -> Any:
        from noeta.core.tool import text_output

        [ended] = outcome.values()
        return text_output(ended.answer)


async def test_a_sub_agents_job_dies_with_it(store: SqliteStore, tmp_path: Path) -> None:
    provider = ScriptedProvider([
        reply("", ("Delegate", {"prompt": "start it", "cwd": str(tmp_path)})),  # root
        reply("", _bg("sleep 30")),                                             # child
        reply("child started it"),                                              # child ends
        reply("ok"),                                                            # root
    ])
    registry = Registry(log_dir=str(tmp_path / "logs"))
    shells = Shell(registry=registry)
    root_agent = Agent(name="a", model="m", tools=[Delegate()], extensions=[shells])
    worker = Agent(name="worker", model="m", one_shot=True, extensions=[shells, FileTools()])
    rt = Runtime(store=store, agents={"a": root_agent, "worker": worker},
                 provider_for=lambda _m: provider, sleep=_no_sleep)
    client = Client(store)
    tid = await client.create("a", "go", config={"cwd": str(tmp_path)})
    await rt.run_until_idle()
    [child] = await client.tasks(parent_id=tid)
    assert child.status == "ended"
    for _ in range(100):
        if child.task_id not in registry.jobs:
            break
        await asyncio.sleep(0.02)
    assert child.task_id not in registry.jobs
    assert len(provider.requests) == 4
    assert not any("task-notification" in str(i.body) for i in await store.inbox(tid))


def test_a_continuation_drops_the_running_jobs_of_its_source() -> None:
    ext = Shell(registry=Registry())
    st = ext.reduce(None, "cwd", {"cwd": "/w/sub"})
    st = ext.reduce(st, "started", {"job_id": "a"})
    st = ext.reduce(st, "started", {"job_id": "b"})
    st = ext.reduce(st, "ended", {"job_id": "b", "status": "completed"})
    out = ext.continue_state(st)
    assert out.cwd == "/w/sub" and list(out.jobs) == ["b"]


async def test_a_job_whose_end_was_posted_is_never_lost_on_another_worker(
        store: SqliteStore, tmp_path: Path) -> None:
    """Its end is in the inbox before the claim is given back, so the next
    worker settles the job as ended: the loss it posts for jobs it does not
    have comes after."""
    provider = ScriptedProvider([reply("it finished")])
    rt, ext = _setup(store, provider, tmp_path)
    client = Client(store)
    await store.create(NewTask(task_id="t", created=TaskCreated(agent="a")))
    claim = await store.claim("w-old", ["default"], 30)
    assert claim is not None
    await store.append(claim, [ExtensionRecorded(extension="shell", kind="started", data={
        "job_id": "j1", "command": "make", "description": "make", "tool_use_id": "toolu_1",
        "output_file": "/logs/j1.output"})], expected_seq=1)
    await store.post(Post(task_id="t", body=External(name=JOB_ENDED, data={
        "job_id": "j1", "status": "completed", "exit_code": 0,
        "notice": f"{NOTICE_GUARD}\n\n<task-notification>done</task-notification>"})))
    await store.release(claim)
    await rt.run_until_idle()
    [job] = (await _jobs(client, "t", ext)).values()
    assert job["status"] == "completed"
    assert "<task-notification>done" in provider.requests[0].messages[-1].text


async def test_a_worker_stopping_tells_the_job_loss_with_the_next_message(
        store: SqliteStore, tmp_path: Path) -> None:
    """Closing a front end kills its jobs (as Claude Code does on exit) but
    starts no turn: the waiting task hears of the loss along with the
    person's next message."""
    provider = ScriptedProvider([reply("", _bg("sleep 30")), reply("started it"),
                                 reply("answered")])
    rt, ext = _setup(store, provider, tmp_path)
    client = Client(store)
    tid = await client.create("a", "go", config={"cwd": str(tmp_path)})
    async with _serving(rt):
        await _until(_kept_waiting(client, tid, rt))
        [job] = ext.registry.jobs[tid].values()
    await asyncio.wait_for(job.done.wait(), 10)
    after_close = Runtime(store=store, agents=rt.agents, provider_for=rt.provider_for,
                          sleep=_no_sleep)
    await after_close.run_until_idle()
    row = await client.get(tid)
    assert row is not None and row.status == "waiting" and row.owner is None
    assert len(provider.requests) == 2
    await client.send(tid, "what happened?")
    await after_close.run_until_idle()
    assert len(provider.requests) == 3
    *_, said, notice = provider.requests[2].messages
    assert said.text == "what happened?"
    assert notice.origin == "system" and "<status>failed</status>" in notice.text
    assert [j["status"] for j in (await _jobs(client, tid, ext)).values()] == ["lost"]
