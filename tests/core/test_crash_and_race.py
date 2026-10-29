"""Phase-1 gates: a crash at every write boundary recovers without re-running
a finished tool, and racing runners never double-write."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.errors import StoreUnavailable
from noeta.core.events import Event, ToolFinished, ToolStarted
from noeta.core.runner import INTERRUPTED_CALL_ERROR, Runtime
from noeta.core.store import Appended, Claim, NewTask, Patch, Post
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import Message, ModelRequest, ModelResponse, Text, ToolResult, ToolUse, Usage


class Crash(BaseException):
    """Stands in for the process dying."""


class CrashingStore(SqliteStore):
    """Dies at the ``crash_at``-th append, before or after it commits."""

    def __init__(self, path: str, crash_at: int, after_commit: bool) -> None:
        super().__init__(path)
        self.crash_at = crash_at
        self.after_commit = after_commit
        self.appends = 0

    async def append(self, claim: Claim, events: Sequence[Event], *, expected_seq: int,
                     patch: Patch = Patch(), posts: Sequence[Post] = (),
                     creates: Sequence[NewTask] = ()) -> Appended:
        self.appends += 1
        if self.appends == self.crash_at and not self.after_commit:
            raise Crash()
        done = await super().append(claim, events, expected_seq=expected_seq, patch=patch,
                                   posts=posts, creates=creates)
        if self.appends == self.crash_at:
            raise Crash()
        return done


class Journal:
    """A side effect we can count: every run of the tool is written down."""

    def __init__(self) -> None:
        self.runs: list[str] = []


class Effect:
    name = "Effect"
    description = "Do a side effect."
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    def __init__(self, journal: Journal) -> None:
        self.journal = journal

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        self.journal.runs.append(input["key"])
        return text_output(f"did {input['key']}")


class ByProgress:
    """Answers from the request alone, so a re-sent request after a crash
    gets the same answer: call Effect a, then b, then finish."""

    family = "scripted"

    async def complete(self, request: ModelRequest, on_delta: Any | None = None) -> ModelResponse:
        done = sum(1 for m in request.messages for b in m.content if isinstance(b, ToolResult))
        if done < 2:
            key = "ab"[done]
            msg = Message(role="assistant", content=[ToolUse(id=f"c{key}", name="Effect",
                                                             input={"key": key})])
            return ModelResponse(message=msg, stop_reason="tool_use", usage=Usage(output=1))
        return ModelResponse(message=Message(role="assistant", content=[Text(text="finished")]),
                             stop_reason="end_turn", usage=Usage(output=1))


async def _no_sleep(_: float) -> None:
    return None


def _runtime(store: SqliteStore, journal: Journal, lease: float = 30.0) -> Runtime:
    agent = Agent(name="a", model="m", tools=[Effect(journal)])
    return Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: ByProgress(),
                   env_for=lambda _s: None, lease_seconds=lease, sleep=_no_sleep)  # type: ignore[arg-type,return-value]


async def _expire(store: SqliteStore, task_id: str) -> None:
    def tx(conn: Any) -> None:
        conn.execute("UPDATE tasks SET lease_until = 0 WHERE task_id = ? AND status = 'running'",
                     (task_id,))

    await store._run(tx)


#: Appends in a clean run: the prompt overrides and plugins, InputTaken, then
#: (ModelCalled, ModelReplied, ToolStarted, ToolFinished) x2, ModelCalled,
#: ModelReplied, Waiting.
CLEAN_APPENDS = 13


@pytest.mark.parametrize("after_commit", [False, True])
@pytest.mark.parametrize("crash_at", range(1, CLEAN_APPENDS + 2))
async def test_crash_at_every_append_boundary(tmp_path: Path, crash_at: int,
                                              after_commit: bool) -> None:
    path = str(tmp_path / "t.db")
    journal = Journal()
    crashing = CrashingStore(path, crash_at, after_commit)
    tid = await Client(crashing).create("a", "go")
    with pytest.raises(Crash) if crash_at <= CLEAN_APPENDS else _nothing():
        await _runtime(crashing, journal).run_until_idle()
    await crashing.close()

    store = SqliteStore(path)
    await _expire(store, tid)
    await _runtime(store, journal).run_until_idle()

    assert journal.runs.count("a") <= 1 and journal.runs.count("b") <= 1
    events = [r.event for r in await store.read(tid)]
    started = [e.call.id for e in events if isinstance(e, ToolStarted)]
    finished = [e.result.call_id for e in events if isinstance(e, ToolFinished)]
    assert sorted(started) == sorted(set(started))
    assert set(started) <= set(finished)
    for e in events:
        if isinstance(e, ToolFinished) and e.result.text == INTERRUPTED_CALL_ERROR:
            assert e.result.call_id in started
    row = await store.get(tid)
    assert row is not None and row.status == "waiting" and row.inbox_read_seq == row.inbox_seq
    await store.close()


class _nothing:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *a: Any) -> bool:
        return False


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_racing_runtimes_never_double_run(tmp_path: Path, backend: str,
                                                request: pytest.FixtureRequest) -> None:
    journal = Journal()
    if backend == "postgres":
        pg = request.getfixturevalue("pg")
        stores = [pg.store() for _ in range(4)]
    else:
        path = str(tmp_path / "t.db")
        stores = [SqliteStore(path) for _ in range(4)]
    client = Client(stores[0])
    ids = [await client.create("a", f"task {i}") for i in range(12)]
    stop = asyncio.Event()
    serving = [asyncio.create_task(_runtime(s, journal, lease=0.3).serve(stop)) for s in stores]

    async def all_waiting() -> None:
        while True:
            rows = [await client.get(t) for t in ids]
            if all(r.status == "waiting" for r in rows):
                return
            await asyncio.sleep(0.05)

    await asyncio.wait_for(all_waiting(), 30)
    stop.set()
    await asyncio.gather(*serving)
    assert len(journal.runs) == 24  # a and b once per task, never twice
    for t in ids:
        events = [r.event for r in await client.events(t)]
        assert sum(isinstance(e, ToolStarted) for e in events) == 2
    for s in stores:
        await s.close()


class RenewFails(SqliteStore):
    async def renew(self, claim: Claim, lease_seconds: float) -> Claim | None:
        raise StoreUnavailable("the store is unreachable")


async def test_a_lease_that_cannot_be_renewed_is_given_up_by_the_local_clock(
        tmp_path: Path) -> None:
    """``lease_until`` is the store's clock: a store clock running ahead must
    not keep a runner going past its lease when it cannot renew it."""
    from noeta.core.runner.task import TaskRun

    store = RenewFails(str(tmp_path / "t.db"))
    rt = Runtime(store=store, agents={}, provider_for=lambda _m: None,  # type: ignore[arg-type,return-value]
                 lease_seconds=0.3)
    ahead = time.time() + 3600
    run = TaskRun(rt, Claim(task_id="t", worker=rt.worker, epoch=1, lease_until=ahead))
    try:
        await asyncio.wait_for(run._keep_claim(), 2.0)
        assert run._lost
    finally:
        await store.close()
