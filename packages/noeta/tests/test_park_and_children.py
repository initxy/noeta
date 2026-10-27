"""Tools that wait: a question parks until answered; a delegation parks until
its child task ends. Stopping withdraws; cancelling cascades to children;
an extension can make room on context overflow."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.agent import Agent
from noeta.client import Client
from noeta.errors import ContextOverflow
from noeta.events import Answer, ChildEnded, Compacted, TaskEnded, ToolFinished, Waiting
from noeta.ext.base import Extension, TaskView
from noeta.runner import Runtime
from noeta.store.sqlite import SqliteStore
from noeta.testing import ScriptedProvider, reply
from noeta.tools.base import Park, ToolContext, ToolOutput, text_output


class AskTool:
    name = "Ask"
    description = "Ask the user."
    input_schema = {"type": "object"}  # noqa: RUF012
    read_only = False
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return ToolOutput(park=Park(on="question", detail={"q": input["q"]}))

    async def resume(self, input: dict[str, Any], ctx: ToolContext, outcome: Any) -> ToolOutput:
        if outcome is None:
            return text_output("Question withdrawn by user (no answer provided).", is_error=True)
        assert isinstance(outcome, Answer)
        return text_output(f"user said: {outcome.data}")


class Delegate:
    name = "Delegate"
    description = "Hand work to a sub-agent."
    input_schema = {"type": "object"}  # noqa: RUF012
    read_only = False
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        assert ctx.spawn is not None
        child = ctx.spawn("worker", input["prompt"], queue=input.get("queue", "default"))
        return ToolOutput(park=Park(on="children", keys=[child]))

    async def resume(self, input: dict[str, Any], ctx: ToolContext, outcome: Any) -> ToolOutput:
        if outcome is None:
            return text_output("stopped", is_error=True)
        [ended] = outcome.values()
        assert isinstance(ended, ChildEnded)
        return text_output(ended.answer, is_error=ended.status != "done")


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def _no_sleep(_: float) -> None:
    return None


def runtime(store: SqliteStore, provider: ScriptedProvider, *agents: Agent) -> Runtime:
    return Runtime(store=store, agents={a.name: a for a in agents},
                   provider_for=lambda _m: provider, env_for=lambda _s: None,  # type: ignore[arg-type,return-value]
                   sleep=_no_sleep)


async def test_question_parks_then_resumes_with_the_answer(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", ("Ask", {"q": "which?"})), reply("thanks")])
    rt = runtime(store, provider, Agent(name="a", model="m", tools=[AskTool()]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    row = await client.get(tid)
    assert row.status == "waiting" and row.waiting_on == "question"
    waiting = (await client.events(tid))[-1].event
    assert isinstance(waiting, Waiting)
    # A restart while parked is not a crash: the call is not "interrupted".
    await client.answer(tid, waiting.ids[0], data="the blue one")
    await rt.run_until_idle()
    results = [r.event.result for r in await client.events(tid)
               if isinstance(r.event, ToolFinished)]
    assert [r.text for r in results] == ["user said: the blue one"]
    assert (await client.get(tid)).waiting_on == "input"


async def test_stopping_withdraws_a_parked_question(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", ("Ask", {"q": "?"}))])
    rt = runtime(store, provider, Agent(name="a", model="m", tools=[AskTool()]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    await client.cancel(tid, turn_only=True)
    await rt.run_until_idle()
    results = [r.event.result for r in await client.events(tid)
               if isinstance(r.event, ToolFinished)]
    assert results[0].is_error and "withdrawn" in results[0].text
    assert (await client.get(tid)).waiting_on == "input"


async def test_delegation_parks_until_the_child_ends(store: SqliteStore) -> None:
    provider = ScriptedProvider([
        reply("", ("Delegate", {"prompt": "count to 3"})),  # parent
        reply("1 2 3"),                                     # child
        reply("the child counted"),                         # parent again
    ])
    rt = runtime(store, provider, Agent(name="a", model="m", tools=[Delegate()]),
                 Agent(name="worker", model="m", one_shot=True))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    [child] = await client.tasks(parent_id=tid)
    assert child.status == "ended" and child.root_id == tid
    results = [r.event.result for r in await client.events(tid)
               if isinstance(r.event, ToolFinished)]
    assert [r.text for r in results] == ["1 2 3"]
    assert (await client.get(tid)).waiting_on == "input"


async def test_cancel_cascades_to_live_children(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", ("Delegate", {"prompt": "slow", "queue": "kids"}))])
    parent_rt = runtime(store, provider, Agent(name="a", model="m", tools=[Delegate()]))
    client = Client(store)
    tid = await client.create("a", "go")
    await parent_rt.run_once()  # the parent parks; nobody runs the child yet
    [child] = await client.tasks(parent_id=tid)
    await client.cancel(tid)
    await parent_rt.run_once()  # the parent ends and cancels its child
    child_rt = runtime(store, ScriptedProvider(), Agent(name="worker", model="m"))
    child_rt.queues = ("kids",)
    await child_rt.run_until_idle()
    end = (await client.events(child.task_id))[-1].event
    assert isinstance(end, TaskEnded) and end.status == "cancelled"


class MakeRoom(Extension):
    name = "room"

    async def on_context_overflow(self, view: TaskView) -> bool:
        view.compacted(Compacted(summary="(summary)", through=view.state.last_seq))
        return True


async def test_overflow_lets_an_extension_make_room(store: SqliteStore) -> None:
    provider = ScriptedProvider([ContextOverflow("too long"), reply("fits now")])
    rt = runtime(store, provider, Agent(name="a", model="m", extensions=[MakeRoom()]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    second = provider.requests[1]
    assert second.messages[0].text == "(summary)"
    assert (await client.get(tid)).waiting_on == "input"
