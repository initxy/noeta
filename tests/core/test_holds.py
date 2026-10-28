"""A worker's holds on task trees: a reaped hold stops what it held, a dead
worker's hold is reaped by the others and tells the tree, and a worker on
its way out gives its holds up the same way."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import External, InputTaken
from noeta.core.runner import Runtime
from noeta.core.store import HOLD_LOST
from noeta.testing import ScriptedProvider, reply


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


def _runtime(store: SqliteStore, provider: ScriptedProvider, **kw: Any) -> Runtime:
    async def no_sleep(_: float) -> None:
        return None

    return Runtime(store=store, agents={"a": Agent(name="a", model="m")},
                   provider_for=lambda _m: provider, sleep=no_sleep, **kw)


async def _lost_taken(client: Client, tid: str) -> list[Any]:
    return [i.body.data for r in await client.events(tid) if isinstance(r.event, InputTaken)
            for i in r.event.items if isinstance(i.body, External)
            and i.body.name == HOLD_LOST]


async def test_a_dead_workers_hold_is_reaped_and_wakes_its_tree(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("waiting"), reply("told")])
    rt = _runtime(store, provider, lease_seconds=0.15)
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    epoch = await store.hold(tid, "dead-worker", 0.05)
    await client.send(tid, "more")
    assert not await rt.run_once()  # held by a worker not yet known dead
    await asyncio.sleep(0.1)
    await rt.run_until_idle()
    assert await _lost_taken(client, tid) == [{"worker": "dead-worker", "epoch": epoch}]


async def test_a_reaped_hold_stops_what_it_held(store: SqliteStore) -> None:
    rt = _runtime(store, ScriptedProvider([]), lease_seconds=0.15)
    release = await rt.hold("root")
    stopped: list[str] = []
    release.tree.on_lost(lambda: stopped.append("job"))
    assert release.tree.worker == rt.worker and release.tree.fresh()
    assert rt._renewer is not None
    rt._renewer.cancel()  # the worker froze: nothing renews its hold
    await asyncio.sleep(rt.hold_seconds + 0.05)
    assert not release.tree.fresh()
    other = _runtime(store, ScriptedProvider([]), lease_seconds=0.15)
    await other.run_once()  # reaps
    rt._renewer = asyncio.create_task(rt._renew_holds())  # the worker thaws
    await asyncio.sleep(rt.lease_seconds)
    assert stopped == ["job"] and release.tree.lost and rt._holds == {}
    await release()
    again = await rt.hold("root")
    assert again.tree.epoch != release.tree.epoch and not again.tree.lost
    await again()


async def test_a_stopping_worker_tells_the_trees_it_held(store: SqliteStore) -> None:
    rt = _runtime(store, ScriptedProvider([reply("ok")]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    release = await rt.hold(tid)
    stopped: list[str] = []
    release.tree.on_lost(lambda: stopped.append("job"))
    stop = asyncio.Event()
    serving = asyncio.create_task(rt.serve(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await serving
    assert stopped == ["job"]
    lost = External(name=HOLD_LOST, data={"worker": rt.worker, "epoch": release.tree.epoch})
    assert (await store.inbox(tid))[-1].body == lost
