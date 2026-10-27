"""Background shells under the runner: an end notice wakes an idle task into
a turn and is recorded; stopping, cancelling and turn ends kill jobs
silently; a job the process forgot (a restart) is marked lost."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.agent import Agent
from noeta.client import Client
from noeta.events import ToolFinished
from noeta.ext.background_shell import BackgroundShells, Registry
from noeta.ext.fs import FileTools
from noeta.loop.state import fold
from noeta.runner import Runtime
from noeta.store.sqlite import SqliteStore
from noeta.testing import ScriptedProvider, reply


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def _no_sleep(_: float) -> None:
    return None


def _setup(store: SqliteStore, provider: ScriptedProvider, tmp: Path,
           **kw: Any) -> tuple[Runtime, BackgroundShells]:
    registry = kw.pop("registry", None) or Registry(log_dir=str(tmp / "logs"))
    ext = BackgroundShells(registry=registry, **kw)
    agent = Agent(name="a", model="m", extensions=[ext, FileTools()])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 sleep=_no_sleep)
    return rt, ext


async def _jobs(client: Client, tid: str, ext: BackgroundShells) -> dict[str, Any]:
    st = fold(await client.events(tid), {ext.name: ext.reduce}, {ext.name: {}})
    jobs: dict[str, Any] = st.ext[ext.name]
    return jobs


async def _settle(ext: BackgroundShells) -> None:
    for jobs in list(ext.registry.jobs.values()):
        for job in list(jobs.values()):
            await asyncio.wait_for(job.done.wait(), 10)
            assert job.sender is not None
            await asyncio.wait_for(job.sender, 10)


def _bg(command: str) -> tuple[str, dict[str, Any]]:
    return ("Bash", {"command": command, "run_in_background": True})


async def test_an_end_notice_wakes_the_idle_task(store: SqliteStore, tmp_path: Path) -> None:
    # The job outlives the turn, so its notice wakes an idle task (an instant
    # job could finish mid-turn and ride in the same turn instead).
    provider = ScriptedProvider([reply("", _bg("sleep 0.3; echo done-here")),
                                 reply("started it"), reply("it finished")])
    rt, ext = _setup(store, provider, tmp_path)
    client = Client(store)
    tid = await client.create("a", "go", config={"cwd": str(tmp_path)})
    await rt.run_until_idle()
    assert (await client.get(tid)).waiting_on == "input"
    await _settle(ext)
    await rt.run_until_idle()
    notice = provider.requests[2].messages[-1]
    assert notice.origin == "system"
    assert notice.text.startswith("<task-notification>\n<task-id>bg-")
    assert notice.text.endswith("completed (exit code 0)</summary>\n</task-notification>"
                                "\nLast output:\ndone-here")
    [job] = (await _jobs(client, tid, ext)).values()
    assert job["status"] == "completed" and job["command"] == "sleep 0.3; echo done-here"
    assert (await client.get(tid)).waiting_on == "input"


async def test_cancelling_the_root_kills_its_jobs_silently(store: SqliteStore,
                                                           tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", _bg("sleep 30")), reply("ok")])
    rt, ext = _setup(store, provider, tmp_path)
    client = Client(store)
    tid = await client.create("a", "go", config={"cwd": str(tmp_path)})
    await rt.run_until_idle()
    [job] = ext.registry.jobs[tid].values()
    await client.cancel(tid)
    await rt.run_until_idle()
    await asyncio.wait_for(job.done.wait(), 10)
    assert job.sender is not None
    await asyncio.wait_for(job.sender, 10)
    assert job.status == "killed" and job.quiet
    inbox = await store.inbox(tid)
    assert not any("task-notification" in str(i.body) for i in inbox)
    # The ended root's jobs and logs are dropped once they are gone.
    for _ in range(100):
        if tid not in ext.registry.jobs:
            break
        await asyncio.sleep(0.02)
    assert tid not in ext.registry.jobs
    assert not Path(job.out_path).exists()


async def test_stopping_the_roots_turn_kills_its_jobs(store: SqliteStore,
                                                      tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", _bg("sleep 30"), ("Bash", {"command": "sleep 1"}))])
    rt, ext = _setup(store, provider, tmp_path)
    client = Client(store)
    tid = await client.create("a", "go", config={"cwd": str(tmp_path)})
    run = asyncio.create_task(rt.run_until_idle())
    while not ext.registry.jobs.get(tid):
        await asyncio.sleep(0.02)
    await client.cancel(tid, turn_only=True)
    await run
    await rt.run_until_idle()
    [job] = ext.registry.jobs[tid].values()
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
    assert "terminated when you give your final response" in result.text
    [job] = ext.registry.jobs[tid].values()
    await asyncio.wait_for(job.done.wait(), 10)
    assert job.status == "killed" and job.quiet


async def test_a_job_the_process_forgot_is_marked_lost(store: SqliteStore,
                                                       tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", _bg("sleep 30")), reply("ok"), reply("again")])
    rt, ext = _setup(store, provider, tmp_path)
    client = Client(store)
    tid = await client.create("a", "go", config={"cwd": str(tmp_path)})
    await rt.run_until_idle()
    # A new worker process: same agent, empty job table.
    rt2, ext2 = _setup(store, provider, tmp_path)
    await client.send(tid, "status?")
    await rt2.run_until_idle()
    [job] = (await _jobs(client, tid, ext2)).values()
    assert job["status"] == "lost"
    ext.registry.kill_where(tid)
    await _settle(ext)


async def test_a_killed_job_is_recorded_when_the_task_runs_again(store: SqliteStore,
                                                                 tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", _bg("sleep 30")), reply("ok"), reply("again")])
    rt, ext = _setup(store, provider, tmp_path)
    client = Client(store)
    tid = await client.create("a", "go", config={"cwd": str(tmp_path)})
    await rt.run_until_idle()
    ext.registry.kill_where(tid)
    await _settle(ext)
    await client.send(tid, "status?")
    await rt.run_until_idle()
    [job] = (await _jobs(client, tid, ext)).values()
    assert job["status"] == "killed"


class Delegate:
    name = "Delegate"
    description = "Hand work to a sub-agent."
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    async def run(self, input: dict[str, Any], ctx: Any) -> Any:
        from noeta.tools.base import Park, ToolOutput

        child = ctx.spawn("worker", input["prompt"], config={"cwd": input["cwd"]})
        return ToolOutput(park=Park(on="children", keys=[child]))

    async def resume(self, input: dict[str, Any], ctx: Any, outcome: Any) -> Any:
        from noeta.tools.base import text_output

        [ended] = outcome.values()
        return text_output(ended.answer)


async def test_a_sub_agents_job_notifies_the_root_after_it_ended(store: SqliteStore,
                                                                 tmp_path: Path) -> None:
    provider = ScriptedProvider([
        reply("", ("Delegate", {"prompt": "start it", "cwd": str(tmp_path)})),  # root
        reply("", _bg("sleep 0.3; echo late")),                                 # child
        reply("child started it"),                                              # child ends
        reply("ok"),                                                            # root
        reply("seen"),                                                          # root, notice
    ])
    registry = Registry(log_dir=str(tmp_path / "logs"))
    shells = BackgroundShells(registry=registry)
    root_agent = Agent(name="a", model="m", tools=[Delegate()], extensions=[shells])
    worker = Agent(name="worker", model="m", one_shot=True, extensions=[shells, FileTools()])
    rt = Runtime(store=store, agents={"a": root_agent, "worker": worker},
                 provider_for=lambda _m: provider, sleep=_no_sleep)
    client = Client(store)
    tid = await client.create("a", "go", config={"cwd": str(tmp_path)})
    await rt.run_until_idle()
    [child] = await client.tasks(parent_id=tid)
    assert child.status == "ended" and child.task_id in registry.ended
    await _settle(shells)
    await rt.run_until_idle()
    notice = provider.requests[-1].messages[-1]
    assert notice.text.startswith("<task-notification>") and notice.text.endswith("late")
