"""Fold snapshots: a claim starts from the task's snapshot and folds only
the events after it; a snapshot of another fold version, or one that cannot
be read, is ignored. (Every load in the suite is also folded both ways and
compared: ``tests/snapshot_check.py``.)"""

from __future__ import annotations

import importlib
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import msgspec
import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core import snapshots
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import Recorded
from noeta.core.extension import Extension, TaskView
from noeta.core.loop.state import TaskState, fold, new_state
from noeta.core.runner import Runtime
from noeta.core.runner.snapshotting import SNAPSHOT_GROWTH
from noeta.core.runner.task import TaskRun
from noeta.core.store import Claim, Snapshot
from noeta.core.tool import Tool, ToolContext, ToolOutput, text_output
from noeta.plugins.shell.state import ShellState
from noeta.testing import ScriptedProvider, reply


class Counting(SqliteStore):
    """Keeps the ``after`` of every read of a task's log."""

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.reads: list[int] = []

    async def read(self, task_id: str, after: int = 0, *,
                   limit: int | None = None) -> list[Recorded]:
        self.reads.append(after)
        return await super().read(task_id, after, limit=limit)


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
    version = snapshots.version("a", good.all_exts)
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


_PLUGIN_INIT = '''
from noeta.core.extension import Extension


class Counter(Extension):
    name = "counter"

    def reduce(self, state, kind, data):
        from snapver_plugin.fold import count

        return count(state, data)
'''


def _forget_code() -> None:
    """Drop what the version caches about source files (a process sees its
    code change only across a restart)."""
    for value in vars(snapshots).values():
        clear = getattr(value, "cache_clear", None)
        if clear is not None:
            clear()


def test_the_version_follows_a_reducer_kept_in_a_helper_module(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = tmp_path / "snapver_plugin"
    package.mkdir()
    (package / "__init__.py").write_text(_PLUGIN_INIT)
    (package / "fold.py").write_text("def count(state, data):\n    return (state or 0) + 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    from snapver_plugin import Counter  # type: ignore[import-not-found]

    try:
        _forget_code()
        before = snapshots.version("a", [Counter()])
        (package / "fold.py").write_text("def count(state, data):\n    return (state or 0) + 2\n")
        _forget_code()
        assert snapshots.version("a", [Counter()]) != before
    finally:
        _forget_code()
        for name in [m for m in sys.modules if m.startswith("snapver_plugin")]:
            del sys.modules[name]


def test_the_version_stays_that_of_the_code_the_runtime_started_with(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Code upgraded on disk under a running worker: its snapshots keep the
    version of the code it runs, not the new code's."""
    package = tmp_path / "snapver_plugin"
    package.mkdir()
    (package / "__init__.py").write_text(_PLUGIN_INIT)
    old = "def count(state, data):\n    return (state or 0) + 1\n"
    (package / "fold.py").write_text(old)
    monkeypatch.syspath_prepend(str(tmp_path))
    from snapver_plugin import Counter  # type: ignore[import-not-found]

    try:
        _forget_code()  # a process starting
        Runtime(store=SqliteStore(str(tmp_path / "s.db")),
                agents={"a": Agent(name="a", model="m", extensions=[Counter()])},
                provider_for=lambda _m: ScriptedProvider([]))
        (package / "fold.py").write_text(old.replace("+ 1", "+ 2"))
        running = snapshots.version("a", [Counter()])
        (package / "fold.py").write_text(old)
        _forget_code()
        assert running == snapshots.version("a", [Counter()])
    finally:
        _forget_code()
        for name in [m for m in sys.modules if m.startswith("snapver_plugin")]:
            del sys.modules[name]


@pytest.mark.parametrize("extension, helper", [
    ("noeta.plugins.fs:FileTools", "noeta.plugins.fs.state"),
    ("noeta.plugins.shell:Shell", "noeta.plugins.shell.state"),
    ("noeta.plugins.subtasks.extension:Subtasks", "noeta.plugins.subtasks.idle"),
])
def test_the_version_follows_the_builtin_reducer_helpers(
        extension: str, helper: str, monkeypatch: pytest.MonkeyPatch) -> None:
    module, _, name = extension.partition(":")
    cls = getattr(importlib.import_module(module), name)
    real = snapshots._module_digest
    _forget_code()
    before = snapshots._code_digest(cls.__module__)
    monkeypatch.setattr(snapshots, "_module_digest",
                        lambda m: "changed" if m == helper else real(m))
    _forget_code()
    try:
        assert snapshots._code_digest(cls.__module__) != before
    finally:
        _forget_code()


class Sized(Counting):
    """Adds up the bytes of every snapshot written."""

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.written: list[int] = []

    async def put_snapshot(self, task_id: str, snapshot: Snapshot) -> None:
        self.written.append(len(snapshot.data))
        await super().put_snapshot(task_id, snapshot)


async def test_snapshot_bytes_written_stay_within_a_few_times_the_logs(
        tmp_path: Path) -> None:
    """Each snapshot rewrites the whole state: written at a fixed interval
    their bytes grow with the square of the log. The next one waits until
    the events since the last hold a share of its bytes."""
    store = Sized(str(tmp_path / "s.db"))
    try:
        provider = ScriptedProvider([reply("", ("Echo", {}))] * 300 + [reply("done")])
        agent = Agent(name="a", model="m", extensions=[Note()])
        rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                     env_for=lambda _s: None, sleep=_no_sleep,  # type: ignore[arg-type,return-value]
                     snapshot_every=4)
        tid = await Client(store).create("a", "go")
        await rt.run_until_idle()
        log = sum(len(msgspec.json.encode(r.event))
                  for r in await SqliteStore.read(store, tid))
        assert len(store.written) > 3
        bound = (1 + 1 / SNAPSHOT_GROWTH) * log
        assert sum(store.written) <= bound, (sum(store.written), log)
        stored = await store.snapshot(tid)
        assert stored is not None
        after = sum(len(msgspec.json.encode(r.event))
                    for r in await SqliteStore.read(store, tid, after=stored.seq))
        assert after <= SNAPSHOT_GROWTH * len(stored.data) + log / 100
    finally:
        await store.close()


@pytest.mark.parametrize("readable", [True, False])
async def test_a_newer_snapshot_of_another_version_leaves_this_ones(
        store: Counting, readable: bool) -> None:
    """A worker of another fold version (a rolling upgrade) writes its own
    snapshot, readable here or not; this worker still starts from the one
    of its version."""
    rt = _runtime(store, Note())
    tid = await _turns(store, rt, 3)
    mine = await store.snapshot(tid)
    assert mine is not None
    await store.put_snapshot(tid, Snapshot(seq=mine.seq + 1, version="0:other",
                                           data=mine.data if readable else b"{new shape"))
    assert (await store.snapshot(tid) or mine).version == "0:other"
    store.reads.clear()
    await _loaded(store, rt, tid)
    assert store.reads[0] == mine.seq
