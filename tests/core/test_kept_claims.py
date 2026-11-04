"""A claim kept over a wait (``ToolContext.keep``): the task waits, its
worker renews the lease and takes the task's inbox itself, so no other
worker runs it; the run goes on in the background, taking a slot only
while a wake has it stepping, and gives the claim back once nothing keeps
it. The claim leaving the worker (lost, or the worker stopping) loses what
kept it."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import External, InputTaken, Waiting
from noeta.core.runner import Runtime
from noeta.core.store import Post
from noeta.core.tool import KeptClaim, ToolContext, ToolOutput, text_output
from noeta.testing import ScriptedProvider, reply


class Keep:
    """Keeps the task's claim, as a background job's start does."""

    name = "Keep"
    description = "Keep the claim."
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    def __init__(self) -> None:
        self.kept: list[KeptClaim] = []
        self.lost: list[KeptClaim] = []

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        assert ctx.keep is not None
        kept = ctx.keep()
        kept.on_lost(lambda: self.lost.append(kept))
        self.kept.append(kept)
        return text_output("kept")


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def _no_sleep(_: float) -> None:
    return None


def _runtime(store: SqliteStore, provider: ScriptedProvider, tool: Keep,
             **kw: Any) -> Runtime:
    agent = Agent(name="a", model="m", tools=[tool])
    return Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                   sleep=_no_sleep, **kw)


async def _until(check: Any, timeout: float = 10.0) -> None:
    async def loop() -> None:
        while not await check():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(loop(), timeout)


async def test_a_kept_wait_takes_its_own_inbox_and_gives_the_claim_back_at_the_end(
        store: SqliteStore) -> None:
    tool = Keep()
    provider = ScriptedProvider([reply("", ("Keep", {})), reply("waiting"), reply("answered")])
    rt = _runtime(store, provider, tool)
    other = _runtime(store, provider, Keep())
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()  # returns: the kept run goes on in the background
    row = await client.get(tid)
    assert row is not None and row.status == "waiting" and row.owner == rt.worker
    assert len(rt._kept_runs) == 1
    await client.send(tid, "still there?")
    assert not await other.run_once()  # the task stays with its worker

    async def answered() -> bool:
        return len(provider.requests) == 3

    await _until(answered)
    [kept] = tool.kept
    kept.release()
    await asyncio.wait_for(next(iter(rt._kept_runs)), 10)
    row = await client.get(tid)
    assert row is not None and row.status == "waiting" and row.owner is None
    assert tool.lost == []


async def test_a_worker_letting_go_loses_what_kept_its_claims(store: SqliteStore) -> None:
    tool = Keep()
    provider = ScriptedProvider([reply("", ("Keep", {})), reply("waiting"), reply("again")])
    rt = _runtime(store, provider, tool)
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    await rt.let_go()
    assert tool.lost == tool.kept and rt._kept_runs == set()
    row = await client.get(tid)
    assert row is not None and row.status == "waiting" and row.owner is None
    await client.send(tid, "back")
    assert await _runtime(store, provider, Keep()).run_once()


async def test_a_lost_claim_loses_what_kept_it(store: SqliteStore) -> None:
    tool = Keep()
    provider = ScriptedProvider([reply("", ("Keep", {})), reply("waiting")])
    rt = _runtime(store, provider, tool, lease_seconds=0.15)
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()

    def stolen(conn: Any) -> None:  # another worker took the task over
        conn.execute("UPDATE tasks SET owner_epoch = owner_epoch + 1")

    await store._run(stolen)
    await asyncio.wait_for(next(iter(rt._kept_runs)), 10)
    assert tool.lost == tool.kept and len(tool.kept) == 1
    assert (await client.get(tid)).owner == rt.worker  # never given back by the old run


async def test_a_kept_wait_takes_no_slot(store: SqliteStore) -> None:
    tool = Keep()
    provider = ScriptedProvider([reply("", ("Keep", {})), reply("waiting"), reply("other")])
    rt = _runtime(store, provider, tool, concurrency=1)
    client = Client(store)
    first = await client.create("a", "go")
    stop = asyncio.Event()
    serving = asyncio.create_task(rt.serve(stop))
    try:
        async def kept() -> bool:
            return bool(rt._kept_runs)

        await _until(kept)
        second = await client.create("a", "next")

        async def done() -> bool:
            row = await client.get(second)
            return row is not None and row.status == "waiting"

        await _until(done)
    finally:
        stop.set()
        await serving
    assert tool.lost == tool.kept  # the worker stopping let go of the first
    assert (await client.get(first)).owner is None


async def test_a_kept_wait_leaves_a_post_that_does_not_wake_for_the_next_wake(
        store: SqliteStore) -> None:
    tool = Keep()
    provider = ScriptedProvider([reply("", ("Keep", {})), reply("waiting"), reply("answered")])
    rt = _runtime(store, provider, tool, poll_interval=0.02)
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    await store.post(Post(task_id=tid, body=External(name="news"), wake=False))
    await asyncio.sleep(0.3)
    assert isinstance((await client.events(tid))[-1].event, Waiting)  # nothing taken
    await client.send(tid, "and now?")

    async def answered() -> bool:
        return len(provider.requests) == 3

    await _until(answered)
    [taken] = [r.event for r in await client.events(tid) if isinstance(r.event, InputTaken)][-1:]
    assert [type(i.body).__name__ for i in taken.items] == ["External", "Say"]
    tool.kept[0].release()
    await asyncio.wait_for(next(iter(rt._kept_runs)), 10)



class Block:
    """Runs until let through."""

    name = "Block"
    description = "Block."
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.go = asyncio.Event()

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        self.started.set()
        await self.go.wait()
        return text_output("through")


async def test_a_woken_kept_run_takes_a_slot_while_it_steps(store: SqliteStore) -> None:
    tool, block = Keep(), Block()
    kept_provider = ScriptedProvider([reply("", ("Keep", {})), reply("waiting"),
                                      reply("answered")])
    block_provider = ScriptedProvider([reply("", ("Block", {})), reply("done")])
    agents = {"a": Agent(name="a", model="m", tools=[tool]),
              "b": Agent(name="b", model="n", tools=[block])}
    rt = Runtime(store=store, agents=agents, sleep=_no_sleep, concurrency=1,
                 provider_for=lambda m: kept_provider if m == "m" else block_provider)
    client = Client(store)
    first = await client.create("a", "go")
    stop = asyncio.Event()
    serving = asyncio.create_task(rt.serve(stop))
    try:
        async def kept() -> bool:
            return bool(rt._kept_runs)

        await _until(kept)
        await client.create("b", "block")
        await asyncio.wait_for(block.started.wait(), 10)  # the one slot is taken
        await client.send(first, "still there?")
        await asyncio.sleep(0.3)
        assert len(kept_provider.requests) == 2  # the wake waits for the slot
        block.go.set()

        async def answered() -> bool:
            return len(kept_provider.requests) == 3

        await _until(answered)
    finally:
        stop.set()
        await serving
