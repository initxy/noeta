"""A worker's holds on task trees: a reaped hold stops what it held, a dead
worker's hold is reaped by the others and tells the tree, and a worker on
its way out gives its holds up the same way."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.errors import CommitUnknown, HeldElsewhere, StoreUnavailable
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


async def test_a_tree_another_worker_holds_cannot_be_held_here(store: SqliteStore) -> None:
    here = _runtime(store, ScriptedProvider([]), lease_seconds=0.15)
    there = _runtime(store, ScriptedProvider([]), lease_seconds=0.15)
    release = await there.hold("root")
    with pytest.raises(HeldElsewhere):
        await here.hold("root")
    assert here._holds == {}
    await release()
    mine = await here.hold("root")
    assert mine.tree.worker == here.worker
    await mine()


async def test_a_tree_with_a_task_on_a_queue_not_served_here_cannot_be_held(
        store: SqliteStore) -> None:
    """A worker serving only ``gpu`` holding a tree whose root waits on
    ``default``: a Cancel to the root would wait for the hold to end."""
    tid = await Client(store).create("a", "go")
    gpu = _runtime(store, ScriptedProvider([]), queues=("gpu",))
    with pytest.raises(HeldElsewhere):
        await gpu.hold(tid)
    both = _runtime(store, ScriptedProvider([]), queues=("default", "gpu"))
    release = await both.hold(tid)
    await release()


async def test_a_silent_workers_hold_is_reaped_before_holding(store: SqliteStore) -> None:
    rt = _runtime(store, ScriptedProvider([]), lease_seconds=0.15)
    await store.hold("root", "dead-worker", 0.05)
    await asyncio.sleep(0.1)
    release = await rt.hold("root")
    assert release.tree.worker == rt.worker
    await release()


async def test_holds_taken_at_once_share_one_tree_hold(store: SqliteStore) -> None:
    """Two jobs of one tree start at once: one ``TreeHold``, renewed while
    either runs, and the store's hold stays until both are given back."""
    rt = _runtime(store, ScriptedProvider([]), lease_seconds=0.15)
    a, b = await asyncio.gather(rt.hold("r"), rt.hold("r"))
    assert a.tree is b.tree and a.tree.count == 2
    await asyncio.sleep(rt.hold_seconds + 0.1)
    assert a.tree.fresh()
    await a()
    assert await store.hold("r", rt.worker, 30, epoch=b.tree.epoch) == b.tree.epoch
    await b()
    assert await store.hold("r", rt.worker, 30, epoch=b.tree.epoch) is None
    assert rt._hold_locks == {}


async def test_a_hold_taken_while_the_last_share_is_given_back_is_kept(
        store: SqliteStore) -> None:
    rt = _runtime(store, ScriptedProvider([]), lease_seconds=0.15)
    first = await rt.hold("r")
    _, again = await asyncio.gather(first(), rt.hold("r"))
    assert not again.tree.lost and rt._holds["r"] is again.tree
    assert await store.hold("r", rt.worker, 30, epoch=again.tree.epoch) == again.tree.epoch
    await again()


class SlowHold(SqliteStore):
    """A hold whose round trip takes ``delay`` seconds; ``sent`` is when
    each epoch renewal was asked for (this process's monotonic clock)."""

    delay = 0.2

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.sent: list[float] = []

    async def hold(self, root_id: str, worker: str, seconds: float,
                   *, epoch: int | None = None,
                   queues: Sequence[str] | None = None) -> int | None:
        if epoch is not None:
            self.sent.append(time.monotonic())
        await asyncio.sleep(self.delay)
        return await super().hold(root_id, worker, seconds, epoch=epoch, queues=queues)


async def test_a_holds_local_clock_starts_when_it_was_asked_for(tmp_path: Path) -> None:
    """How long a hold is surely still this worker's counts from before the
    request was sent, not from when the answer came back."""
    store = SlowHold(str(tmp_path / "t.db"))
    rt = _runtime(store, ScriptedProvider([]), lease_seconds=0.6)
    try:
        before = time.monotonic()
        release = await rt.hold("root")
        assert release.tree.renewed < before + SlowHold.delay / 2
        while not store.sent:
            await asyncio.sleep(0.02)
        while release.tree.renewed < store.sent[0] - SlowHold.delay / 2:
            await asyncio.sleep(0.02)
        renewed = release.tree.renewed
        assert any(abs(renewed - sent) < SlowHold.delay / 2 for sent in store.sent)
        await release()
    finally:
        await rt._drop_holds()
        await store.close()


class LostCommit(SqliteStore):
    """The first ``fail`` holds raise ``CommitUnknown``: after committing
    (``committed``), or before."""

    def __init__(self, path: str, *, committed: bool, fail: int = 1) -> None:
        super().__init__(path)
        self.committed = committed
        self.fail = fail

    async def hold(self, root_id: str, worker: str, seconds: float,
                   *, epoch: int | None = None,
                   queues: Sequence[str] | None = None) -> int | None:
        if epoch is None and self.fail:
            self.fail -= 1
            if self.committed:
                await super().hold(root_id, worker, seconds, queues=queues)
            raise CommitUnknown("connection lost during COMMIT")
        return await super().hold(root_id, worker, seconds, epoch=epoch, queues=queues)


class FlakyUnhold(SqliteStore):
    """The first unhold fails as a dropped connection would."""

    fail = 1

    async def unhold(self, root_id: str, worker: str, epoch: int,
                     *, lost: bool = False) -> None:
        if self.fail:
            self.fail -= 1
            raise StoreUnavailable("connection lost")
        await super().unhold(root_id, worker, epoch, lost=lost)


async def test_a_release_whose_unhold_fails_once_still_gives_the_hold_back(
        tmp_path: Path) -> None:
    """Left in the store, the hold would run out and be reaped, telling the
    tree a job that ended normally was lost (``hold_lost``)."""
    store = FlakyUnhold(str(tmp_path / "t.db"))
    rt = _runtime(store, ScriptedProvider([]))
    try:
        release = await rt.hold("root")
        await release()
        assert await _hold_rows(store) == []
    finally:
        await rt._drop_holds()
        await store.close()


async def _hold_rows(store: SqliteStore) -> list[tuple[Any, ...]]:
    def tx(conn: Any) -> list[tuple[Any, ...]]:
        return [tuple(r) for r in conn.execute("SELECT root_id, worker, epoch FROM tree_holds")]
    rows: list[tuple[Any, ...]] = await store._write(tx)
    return rows


@pytest.mark.parametrize("committed", [True, False])
async def test_a_hold_whose_commit_is_unknown_is_read_back(tmp_path: Path,
                                                            committed: bool) -> None:
    """The connection drops while the hold commits: the worker asks again
    (taking a hold extends its own), so a hold that landed is registered,
    renewed and given back, never left to be reaped as lost."""
    store = LostCommit(str(tmp_path / "t.db"), committed=committed)
    rt = _runtime(store, ScriptedProvider([]), lease_seconds=0.15)
    try:
        release = await rt.hold("root")
        [(root, worker, epoch)] = await _hold_rows(store)
        assert (root, worker, epoch) == ("root", rt.worker, release.tree.epoch)
        await asyncio.sleep(rt.hold_seconds + 0.1)
        assert release.tree.fresh() and not release.tree.lost
        await release()
        assert await _hold_rows(store) == []
        assert await store.reap_holds() == 0
    finally:
        await rt._drop_holds()
        await store.close()


async def test_a_hold_whose_commit_stays_unknown_raises(tmp_path: Path) -> None:
    store = LostCommit(str(tmp_path / "t.db"), committed=False, fail=10)
    rt = _runtime(store, ScriptedProvider([]), lease_seconds=0.15)
    try:
        with pytest.raises(CommitUnknown):
            await rt.hold("root")
        assert rt._holds == {} and await _hold_rows(store) == []
    finally:
        await store.close()
