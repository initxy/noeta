"""A task whose run keeps killing its worker: every pickup after a claim
that was never given back is counted in the log (``core/reclaimed``), a
step finished after one resets the count, and past ``Runtime.restart_limit``
such restarts in a row the task ends failed instead of running again."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core import records
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import (
    ChildEnded,
    ExtensionRecorded,
    ModelReplied,
    Recorded,
    TaskCreated,
    TaskEnded,
    ToolFinished,
    ToolStarted,
    Waiting,
)
from noeta.core.extension import Extension, TaskView
from noeta.core.loop.state import TaskState, apply
from noeta.core.runner import Runtime
from noeta.core.store import NewTask
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import Message, ModelRequest, ModelResponse, Text, ToolResult, ToolUse, Usage


class Crash(BaseException):
    """Stands in for the process dying."""


class Kill:
    """A tool that kills its worker while it runs (the first ``times``)."""

    name = "Kill"
    description = "Kill the worker."
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    def __init__(self, times: int = 1_000) -> None:
        self.times = times
        self.runs = 0

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        self.runs += 1
        if self.runs <= self.times:
            raise Crash()
        return text_output("survived")


class CallsKillUntilItSurvives:
    """Calls ``Kill`` (a new call id each time) until one returns without
    an error, then answers."""

    family = "scripted"

    async def complete(self, request: ModelRequest, on_delta: Any | None = None) -> ModelResponse:
        results = [b for m in request.messages for b in m.content if isinstance(b, ToolResult)]
        if any(not r.is_error for r in results):
            return ModelResponse(message=Message(role="assistant", content=[Text(text="done")]),
                                 stop_reason="end_turn", usage=Usage(output=1))
        use = ToolUse(id=f"k{len(results)}", name="Kill", input={})
        return ModelResponse(message=Message(role="assistant", content=[use]),
                             stop_reason="tool_use", usage=Usage(output=1))


class CrashOnStart(Extension):
    """Kills the worker at every pickup, before any model call."""

    name = "crash_on_start"

    async def on_start(self, view: TaskView) -> None:
        raise Crash()


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def _no_sleep(_: float) -> None:
    return None


def _runtime(store: SqliteStore, agent: Agent, **kw: Any) -> Runtime:
    return Runtime(store=store, agents={agent.name: agent},
                   provider_for=lambda _m: CallsKillUntilItSurvives(),
                   env_for=lambda _s: None, sleep=_no_sleep, **kw)  # type: ignore[arg-type,return-value]


async def _lapse(store: SqliteStore, task_id: str) -> None:
    """The dead worker's lease runs out."""
    def tx(conn: Any) -> None:
        conn.execute("UPDATE tasks SET lease_until = 0 WHERE task_id = ?", (task_id,))

    await store._run(tx)


async def _crash_loop(store: SqliteStore, rt: Runtime, task_id: str, rounds: int) -> int:
    """Run pickups, letting each crash's lease run out; the crashes seen."""
    crashes = 0
    for _ in range(rounds):
        try:
            if not await rt.run_once():
                break
        except Crash:
            crashes += 1
            await _lapse(store, task_id)
    return crashes


def _reclaimed(recs: list[Recorded]) -> int:
    return sum(1 for r in recs if isinstance(r.event, ExtensionRecorded)
               and r.event.extension == records.CORE and r.event.kind == "reclaimed")


async def test_a_crash_before_any_model_call_ends_the_task_after_the_limit(
        store: SqliteStore) -> None:
    agent = Agent(name="a", model="m", extensions=[CrashOnStart()])
    tid = await Client(store).create("a", "go")
    crashes = await _crash_loop(store, _runtime(store, agent), tid, 10)
    assert crashes == 4
    recs = await store.read(tid)
    ended = recs[-1].event
    assert isinstance(ended, TaskEnded) and ended.status == "failed"
    assert ended.error.startswith("restart_limit:") and " 4 times in a row" in ended.error
    assert _reclaimed(recs) == 3
    row = await store.get(tid)
    assert row is not None and row.status == "ended"


async def test_a_model_that_calls_the_killing_tool_again_is_stopped(store: SqliteStore) -> None:
    agent = Agent(name="a", model="m", tools=[Kill()])
    tid = await Client(store).create("a", "go")
    crashes = await _crash_loop(store, _runtime(store, agent, restart_limit=2), tid, 10)
    assert crashes == 3
    recs = await store.read(tid)
    ended = recs[-1].event
    assert isinstance(ended, TaskEnded) and ended.status == "failed"
    assert "restart_limit" in ended.error
    # Each pickup answered the call cut short and the model called again.
    assert sum(isinstance(r.event, ToolStarted) for r in recs) == 3
    # The calls cut short were answered when the task ended.
    started = {r.event.call.id for r in recs if isinstance(r.event, ToolStarted)}
    assert started == {r.event.result.call_id for r in recs if isinstance(r.event, ToolFinished)}


async def test_a_task_that_loses_its_worker_once_finishes_and_resets(store: SqliteStore) -> None:
    kill = Kill(times=1)
    agent = Agent(name="a", model="m", tools=[kill])
    client = Client(store)
    tid = await client.create("a", "go")
    rt = _runtime(store, agent, restart_limit=1)
    assert await _crash_loop(store, rt, tid, 10) == 1
    recs = await store.read(tid)
    assert _reclaimed(recs) == 1
    assert isinstance(recs[-1].event, Waiting)
    assert (await client.get(tid)).waiting_on == "input"
    # The step after the pickup finished: a later loss starts from nothing.
    state = TaskState()
    for r in recs:
        apply(state, r, {})
    assert state.reclaims == 0 and not state.replied_since_reclaim


async def test_no_limit_runs_on(store: SqliteStore) -> None:
    agent = Agent(name="a", model="m", tools=[Kill(times=5)])
    tid = await Client(store).create("a", "go")
    rt = _runtime(store, agent, restart_limit=0)
    assert await _crash_loop(store, rt, tid, 20) == 5
    assert isinstance((await store.read(tid))[-1].event, Waiting)


async def test_a_released_claim_is_not_counted(store: SqliteStore) -> None:
    """A worker shutting down hands its task back: not a crash."""
    started = asyncio.Event()

    class Slow:
        name = "Kill"
        description = "Wait."
        input_schema = {"type": "object"}
        read_only = False
        clearable = False

        async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
            started.set()
            await asyncio.sleep(60)
            return text_output("late")

    agent = Agent(name="a", model="m", tools=[Slow()])
    tid = await Client(store).create("a", "go")
    for _ in range(4):
        started.clear()
        run = asyncio.ensure_future(_runtime(store, agent).run_once())
        await asyncio.wait_for(started.wait(), 10)
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
    recs = await store.read(tid)
    assert _reclaimed(recs) == 0
    row = await store.get(tid)
    assert row is not None and row.status == "ready"


async def test_a_sub_agents_parent_hears_the_failure(store: SqliteStore) -> None:
    child = Agent(name="c", model="m", extensions=[CrashOnStart()])
    # The parent is on a queue this worker does not serve: only the child runs.
    parent = await Client(store).create("c", "parent", queue="elsewhere")
    await store.create(NewTask(task_id="kid", created=TaskCreated(
        agent="c", parent_id=parent, root_id=parent)))
    assert await _crash_loop(store, _runtime(store, child), "kid", 10) == 4
    [ended] = [i.body for i in await store.inbox(parent) if isinstance(i.body, ChildEnded)]
    assert ended.child_id == "kid" and ended.status == "failed"
    assert ended.error.startswith("restart_limit:")


def _fold(*events: Any) -> TaskState:
    state = TaskState()
    for seq, ev in enumerate(events, 1):
        apply(state, Recorded(seq=seq, at=0.0, event=ev), {})
    return state


def _reply(*ids: str) -> ModelReplied:
    content: list[Any] = [ToolUse(id=i, name="T", input={}) for i in ids] or [Text(text="ok")]
    return ModelReplied(response=ModelResponse(
        message=Message(role="assistant", content=content),
        stop_reason="tool_use" if ids else "end_turn"))


def _done(call_id: str) -> ToolFinished:
    return ToolFinished(result=ToolResult(call_id=call_id, content=[Text(text="r")]))


RECLAIMED = records.event(records.Reclaimed())


def test_the_count_resets_only_on_a_step_started_after_the_pickup() -> None:
    # The call cut short gets its result after the pickup: not a new step.
    state = _fold(TaskCreated(agent="a"), _reply("a"), RECLAIMED, _done("a"))
    assert state.reclaims == 1
    state = _fold(TaskCreated(agent="a"), _reply("a"), RECLAIMED, _done("a"), RECLAIMED,
                  _reply("b"))
    assert state.reclaims == 2
    state = _fold(TaskCreated(agent="a"), _reply("a"), RECLAIMED, _done("a"), RECLAIMED,
                  _reply("b"), _done("b"))
    assert state.reclaims == 0
    # A reply with no calls is a finished step at once.
    assert _fold(TaskCreated(agent="a"), RECLAIMED, _reply()).reclaims == 0
    # Parking does not reset it.
    assert _fold(TaskCreated(agent="a"), RECLAIMED, Waiting(on="input")).reclaims == 1


def test_the_record_round_trips() -> None:
    assert records.decode(RECLAIMED) == records.Reclaimed()
