"""Tools that wait: a question parks until answered; a delegation parks until
its child task ends. Stopping withdraws; cancelling cascades to children;
an extension can make room on context overflow."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.errors import ContextOverflow
from noeta.core.events import Answer, ChildEnded, Compacted, TaskEnded, ToolFinished, Waiting
from noeta.core.extension import Extension, TaskView
from noeta.core.runner import Runtime
from noeta.core.tool import Park, ToolContext, ToolOutput, text_output
from noeta.testing import ScriptedProvider, reply


class AskTool:
    name = "Ask"
    description = "Ask the user."
    input_schema = {"type": "object"}
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
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        assert ctx.spawn is not None
        kw = {k: input[k] for k in ("queue", "history_from") if k in input}
        child = ctx.spawn("worker", input["prompt"], **kw)
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


@pytest.mark.parametrize(("asked", "lands"), [({}, "gpu"), ({"queue": "kids"}, "kids")])
async def test_a_child_is_created_on_its_parents_queue_unless_one_is_named(
        store: SqliteStore, asked: dict[str, str], lands: str) -> None:
    """A child the worker serving its parent's queue can run: on another
    queue, a held tree would wait for a worker that may never come."""
    provider = ScriptedProvider([reply("", ("Delegate", {"prompt": "go", **asked}))])
    rt = runtime(store, provider, Agent(name="a", model="m", tools=[Delegate()]))
    rt.queues = ("gpu",)
    client = Client(store)
    tid = await client.create("a", "go", queue="gpu")
    await rt.run_once()
    [child] = await client.tasks(parent_id=tid)
    assert child.queue == lands


async def test_a_child_on_a_queue_this_worker_does_not_serve_is_refused_while_it_holds_the_tree(
        store: SqliteStore) -> None:
    """A held tree's tasks run only on its holder: a child on a queue the
    holder does not serve would wait, unclaimed, until the hold ends."""
    provider = ScriptedProvider([reply("", ("Delegate", {"prompt": "go", "queue": "kids"})),
                                 reply("ok")])
    rt = runtime(store, provider, Agent(name="a", model="m", tools=[Delegate()]))
    client = Client(store)
    tid = await client.create("a", "go")
    release = await rt.hold(tid)  # as a background job does
    try:
        await rt.run_until_idle()
    finally:
        await release()
    assert await client.tasks(parent_id=tid) == []
    [result] = [r.event.result for r in await client.events(tid)
                if isinstance(r.event, ToolFinished)]
    assert result.is_error and "kids" in result.text


class SpawnOnQueue(Extension):
    """Spawns a background child on ``queue`` before the first model call."""

    name = "spawner"

    def __init__(self, queue: str) -> None:
        self.queue = queue
        self.refused: list[str] = []

    async def before_model(self, view: TaskView, request: Any) -> Any:
        if not view.state.children and not self.refused:
            try:
                view.spawn("a", "go", queue=self.queue)
            except ValueError as e:
                self.refused.append(str(e))
        return request


@pytest.mark.parametrize("queue", ["kids", "default"])
async def test_an_extension_spawn_on_a_queue_this_worker_does_not_serve_is_refused_while_it_holds(
        store: SqliteStore, queue: str) -> None:
    """``TaskView.spawn`` keeps the held tree's children on queues its
    holder serves, as a tool call's spawn does."""
    spawner = SpawnOnQueue(queue)
    rt = runtime(store, ScriptedProvider([reply("ok")] * 3),
                 Agent(name="a", model="m", extensions=[spawner]))
    client = Client(store)
    tid = await client.create("a", "go")
    release = await rt.hold(tid)
    try:
        await rt.run_until_idle()
    finally:
        await release()
    children = await client.tasks(parent_id=tid)
    if queue == "default":
        assert len(children) == 1 and spawner.refused == []
        return
    assert children == []
    [refusal] = spawner.refused
    assert "kids" in refusal


@pytest.mark.parametrize("source_tenant", ["acme", "beta"])
async def test_a_child_continues_only_its_own_tenants_conversation(
        store: SqliteStore, source_tenant: str) -> None:
    """``spawn(history_from=)`` reads another task's conversation: another
    tenant's is not there for it, as for ``Client.create``."""
    source = await Client(store).scoped(source_tenant).create("a")
    provider = ScriptedProvider([
        reply("", ("Delegate", {"prompt": "go", "history_from": source})), reply("ok")])
    rt = runtime(store, provider, Agent(name="a", model="m", tools=[Delegate()]))
    acme = Client(store).scoped("acme")
    tid = await acme.create("a", "go")
    await rt.run_until_idle()
    children = await acme.tasks(parent_id=tid)
    if source_tenant == "acme":
        assert len(children) == 1
        return
    assert children == []
    [result] = [r.event.result for r in await acme.events(tid)
                if isinstance(r.event, ToolFinished)]
    assert result.is_error and "UnknownTask" in result.text


async def test_a_sub_agent_ends_after_its_turn_without_one_shot(store: SqliteStore) -> None:
    # A Claude Code agent file has no one_shot; its sub-agent used to wait
    # for a next message that never comes, and the parent waited forever.
    provider = ScriptedProvider([
        reply("", ("Delegate", {"prompt": "count to 3"})),
        reply("1 2 3"),
        reply("the child counted"),
    ])
    rt = runtime(store, provider, Agent(name="a", model="m", tools=[Delegate()]),
                 Agent(name="worker", model="m"))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    [child] = await client.tasks(parent_id=tid)
    assert (child.status, child.end_status) == ("ended", "done")
    parent = await client.get(tid)
    assert (parent.status, parent.waiting_on) == ("waiting", "input")  # the main agent talks on


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
