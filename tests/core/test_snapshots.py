"""Fold snapshots: a claim starts from the task's snapshot and folds only
the events after it; a snapshot of another fold version, or one that cannot
be read, is ignored. (Every load in the suite is also folded both ways and
compared: ``tests/snapshot_check.py``.)"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core import snapshots
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import Recorded
from noeta.core.extension import Extension, TaskView
from noeta.core.loop.state import TaskState, fold, new_state
from noeta.core.runner import Runtime
from noeta.core.runner.task import TaskRun
from noeta.core.store import Claim, Snapshot
from noeta.core.tool import Tool, ToolContext, ToolOutput, text_output
from noeta.plugins.shell.bash import ShellState
from noeta.testing import ScriptedProvider, reply


class Counting(SqliteStore):
    """Keeps the ``after`` of every read of a task's log."""

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.reads: list[int] = []

    async def read(self, task_id: str, after: int = 0) -> list[Recorded]:
        self.reads.append(after)
        return await super().read(task_id, after)


class Note(Extension):
    """Records every tool call; its state is a tuple, a set and a dataclass."""

    name = "note"

    def initial_state(self) -> Any:
        return {"calls": (), "names": frozenset(), "shell": ShellState()}

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        return {"calls": (*state["calls"], data), "names": state["names"] | {data},
                "shell": ShellState(cwd=str(data))}

    def tools(self, view: TaskView) -> list[Tool]:
        return [Echo()]


class Echo:
    name = "Echo"
    description = "Echo."
    input_schema: dict[str, Any] = {"type": "object"}
    read_only = True
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        ctx.record("note", "call", ctx.call_id)
        return text_output("echo")


class Opaque(Extension):
    """A state the snapshot cannot carry (an object without equality)."""

    name = "opaque"

    def initial_state(self) -> Any:
        return object()


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Counting]:
    s = Counting(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def _no_sleep(_: float) -> None:
    return None


def _runtime(store: SqliteStore, *extensions: Extension, every: int = 4) -> Runtime:
    steps = [reply("", ("Echo", {})), reply("", ("Echo", {})), reply("done")] * 10
    provider = ScriptedProvider(steps)
    agent = Agent(name="a", model="m", extensions=list(extensions))
    return Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                   env_for=lambda _s: None, sleep=_no_sleep,  # type: ignore[arg-type,return-value]
                   snapshot_every=every)


async def _turns(store: SqliteStore, rt: Runtime, n: int) -> str:
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    for i in range(n - 1):
        await client.send(tid, f"again {i}")
        await rt.run_until_idle()
    return tid


async def _loaded(store: SqliteStore, rt: Runtime, tid: str) -> TaskRun:
    run = TaskRun(rt, Claim(task_id=tid, worker="w", epoch=0, lease_until=0.0))
    await run.load()
    return run


async def test_a_claim_folds_the_snapshot_and_the_events_after_it(store: Counting) -> None:
    rt = _runtime(store, Note())
    tid = await _turns(store, rt, 3)
    stored = await store.snapshot(tid)
    last = (await store.get(tid)).last_seq  # type: ignore[union-attr]
    assert stored is not None and 0 < stored.seq <= last and last - stored.seq < 4
    store.reads.clear()
    run = await _loaded(store, rt, tid)
    assert store.reads[0] == stored.seq  # the rest of the log is never read
    records = await SqliteStore.read(store, tid)
    note = Note()
    full = fold(records, {"note": note.reduce}, state=new_state({"note": note.initial_state()}))
    full.task_id = tid
    assert run.state == full
    assert len(run.state.ext["note"]["calls"]) == 6


async def test_a_snapshot_of_another_version_is_ignored(store: Counting) -> None:
    rt = _runtime(store, Note(), every=1000)
    tid = await _turns(store, rt, 2)
    assert await store.snapshot(tid) is None
    good = await _loaded(store, rt, tid)
    data = snapshots.encode(TaskState(task_id=tid, agent="a"))
    assert data is not None
    await store.put_snapshot(tid, Snapshot(seq=3, version="0:other", data=data))
    store.reads.clear()
    assert (await _loaded(store, rt, tid)).state == good.state
    assert store.reads[0] == 0


async def test_a_snapshot_that_cannot_be_read_is_ignored(store: Counting) -> None:
    rt = _runtime(store, Note(), every=1000)
    tid = await _turns(store, rt, 1)
    good = await _loaded(store, rt, tid)
    version = snapshots.version("a", good.exts)
    await store.put_snapshot(tid, Snapshot(seq=3, version=version, data=b"{not json"))
    assert (await _loaded(store, rt, tid)).state == good.state


async def test_a_state_that_cannot_be_carried_is_never_snapshotted(store: Counting) -> None:
    rt = _runtime(store, Note(), Opaque(), every=1)
    tid = await _turns(store, rt, 2)
    assert await store.snapshot(tid) is None


async def test_no_snapshots_when_turned_off(store: Counting) -> None:
    rt = _runtime(store, Note(), every=0)
    tid = await _turns(store, rt, 3)
    assert await store.snapshot(tid) is None


def test_the_version_follows_the_plugins_and_the_agent() -> None:
    assert snapshots.version("a", [Note()]) == snapshots.version("a", [Note()])
    assert snapshots.version("a", [Note()]) != snapshots.version("b", [Note()])
    assert snapshots.version("a", [Note()]) != snapshots.version("a", [Note(), Opaque()])


def test_values_come_back_with_their_types() -> None:
    state = TaskState(ext={"x": {"t": (1, "a"), "s": {1, 2}, "f": frozenset({"k"}),
                                 "d": ShellState(cwd="/w", jobs={"j": {"n": [1]}})}})
    data = snapshots.encode(state)
    assert data is not None and snapshots.decode(data) == state
    assert type(snapshots.decode(data).ext["x"]["t"]) is tuple
    from collections import OrderedDict
    assert snapshots.encode(TaskState(ext={"x": OrderedDict(a=1)})) is None
    assert snapshots.encode(TaskState(ext={"x": {1: "int key"}})) is None
