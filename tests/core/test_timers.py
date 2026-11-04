"""Timers: a task waiting for input becomes claimable when its earliest timer
is due, fires it while idle, and takes the timer's text as its next
message. An extension may stage a child outside a tool call."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import ChildEnded, InputTaken, Recorded, TimerFired, Waiting
from noeta.core.extension import Extension, TaskView
from noeta.core.runner import Runtime
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.testing import ScriptedProvider, reply


class Later:
    name = "Later"
    description = "Come back later."
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        ctx.set_timer("later", time.time() + float(input["in"]), input.get("text", ""))
        return text_output("scheduled")


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


async def test_a_due_timer_wakes_the_idle_task_with_its_text(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", ("Later", {"in": 0, "text": "check again"})),
                                 reply("ok, later"), reply("checked")])
    rt = runtime(store, provider, Agent(name="a", model="m", tools=[Later()]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    events = [r.event for r in await client.events(tid)]
    fired = [i.body for e in events if isinstance(e, InputTaken) for i in e.items
             if isinstance(i.body, TimerFired)]
    assert fired == [TimerFired(name="later")]
    assert provider.requests[-1].messages[-1].text == "check again"
    waiting = events[-1]
    assert isinstance(waiting, Waiting) and waiting.on == "input" and waiting.until is None


async def test_a_future_timer_sets_ready_at_and_fires_once_due(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", ("Later", {"in": 3600, "text": "wake"})),
                                 reply("waiting"), reply("woke")])
    rt = runtime(store, provider, Agent(name="a", model="m", tools=[Later()]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    row = await client.get(tid)
    assert row is not None and row.status == "waiting" and row.ready_at is not None
    waiting = (await client.events(tid))[-1].event
    assert isinstance(waiting, Waiting) and waiting.until == row.ready_at
    assert not await rt.run_once()  # not due yet: no worker claims it
    assert await store.claim("w2", ["default"], 30) is None
    # A message wakes it early; the timer stays set and is announced again.
    await client.send(tid, "anything new?")
    await rt.run_until_idle()
    assert len(provider.requests) == 3
    row = await client.get(tid)
    assert row is not None and row.ready_at == waiting.until


class Fixed:
    """Sets the timer ``again`` for one fixed, past time."""

    name = "Fixed"
    description = "Come back at a fixed time."
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        ctx.set_timer("again", 1000.0, "tick")
        return text_output("scheduled")


async def test_a_timer_set_again_for_the_same_time_fires_again(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", ("Fixed", {})), reply("set"),
                                 reply("", ("Fixed", {})), reply("set again"),
                                 reply("done")])
    rt = runtime(store, provider, Agent(name="a", model="m", tools=[Fixed()]))
    client = Client(store)
    tid = await client.create("a", "go")
    await asyncio.wait_for(rt.run_until_idle(), 5)
    events = [r.event for r in await client.events(tid)]
    fired = [i.body for e in events if isinstance(e, InputTaken) for i in e.items
             if isinstance(i.body, TimerFired)]
    assert fired == [TimerFired(name="again"), TimerFired(name="again")]
    assert len(provider.requests) == 5


class Fanout(Extension):
    """Spawns one child from ``on_event`` when the turn's first reply lands."""

    name = "fanout"

    def __init__(self) -> None:
        self.spawned: list[str] = []
        self.ended: list[ChildEnded] = []

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        from noeta.core.events import ModelReplied

        ev = recorded.event
        if isinstance(ev, ModelReplied) and not self.spawned and not view.state.parent_id:
            self.spawned.append(view.spawn("worker", "do it", key="w1"))
        if isinstance(ev, InputTaken):
            self.ended += [i.body for i in ev.items if isinstance(i.body, ChildEnded)]


async def test_an_extension_spawns_a_tracked_child(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("parent done"), reply("child done")])
    fan = Fanout()
    rt = runtime(store, provider, Agent(name="a", model="m", extensions=[fan], one_shot=True),
                 Agent(name="worker", model="m"))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    assert fan.spawned == [f"{tid}.w1"]
    assert [e.answer for e in fan.ended] == ["child done"]
    row = await client.get(tid)
    assert row is not None and row.status == "ended"  # it waited for its child


class TwoSpawns(Extension):
    """Spawns a child without a key from ``on_event`` on each of the first
    two replies, and one from ``after_tool``."""

    name = "two"

    def __init__(self) -> None:
        self.spawned: list[str] = []

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        from noeta.core.events import ModelReplied

        if isinstance(recorded.event, ModelReplied) and not view.state.parent_id \
                and len(self.spawned) < 3:
            self.spawned.append(view.spawn("worker", "do it"))

    async def after_tool(self, view: TaskView, use: Any, output: ToolOutput) -> ToolOutput:
        if not view.state.parent_id:
            self.spawned.append(view.spawn("worker", "after the tool"))
        return output


async def test_spawns_without_a_key_each_create_their_own_child(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", ("Later", {"in": 3600})), reply("parent done"),
                                 reply("child done"), reply("child done"),
                                 reply("child done")])
    ext = TwoSpawns()
    rt = runtime(store, provider, Agent(name="a", model="m", tools=[Later()], extensions=[ext],
                                        one_shot=True),
                 Agent(name="worker", model="m"))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    assert len(ext.spawned) == 3 and len(set(ext.spawned)) == 3
    children = await client.tasks(parent_id=tid)
    assert sorted(c.task_id for c in children) == sorted(ext.spawned)
    assert all(c.end_status == "done" for c in children)


class AskFirst(Extension):
    """Every ``Danger`` call waits for a person's approval."""

    name = "ask_first"

    async def before_tool(self, view: TaskView, call: Any) -> Any:
        from noeta.core.extension import Ask

        return Ask(prompt="sure?") if call.name == "Danger" else None


class Danger(Later):
    name = "Danger"

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return text_output("done the dangerous thing")


async def test_a_timer_due_while_parked_fires_once_the_turn_ends(store: SqliteStore) -> None:
    """As Claude Code fires a scheduled prompt only once it is idle, a
    timer that falls due while a call waits for approval fires (once) when
    the turn has ended, not before."""
    provider = ScriptedProvider([reply("", ("Later", {"in": 0, "text": "tick"})),
                                 reply("", ("Danger", {})), reply("did it"), reply("ticked")])
    rt = runtime(store, provider, Agent(name="a", model="m", tools=[Later(), Danger()],
                                        extensions=[AskFirst()]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    waiting = (await client.events(tid))[-1].event
    assert isinstance(waiting, Waiting) and waiting.on == "approval"
    assert len(provider.requests) == 2  # due, but the turn is still open
    await client.answer(tid, waiting.ids[0], "allow")
    await rt.run_until_idle()
    events = [r.event for r in await client.events(tid)]
    fired = [i for e in events if isinstance(e, InputTaken) for i in e.items
             if isinstance(i.body, TimerFired)]
    assert len(fired) == 1 and len(provider.requests) == 4
    assert provider.requests[3].messages[-1].text == "tick"


class _Lagging:
    """``time`` as seen by a worker whose clock is behind the store's."""

    def __init__(self, behind: float) -> None:
        self.behind = behind

    def time(self) -> float:
        return time.time() - self.behind


@pytest.fixture(params=["sqlite", "postgres"])
async def any_store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Any]:
    if request.param == "sqlite":
        s: Any = SqliteStore(str(tmp_path / "t.db"))
    else:
        s = request.getfixturevalue("pg").store()
    yield s
    await s.close()


async def test_a_worker_clock_behind_the_store_fires_the_due_timer(
        any_store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # The store wakes the task by its own clock; a worker an hour behind
    # must fire the timer then, not append the same Waiting again (the task
    # would be claimable at once, again and again).
    from noeta.core.runner import timers

    monkeypatch.setattr(timers, "time", _Lagging(3600.0))
    provider = ScriptedProvider([reply("", ("Later", {"in": 0.05, "text": "wake"})),
                                 reply("waiting"), reply("woke")])
    rt = runtime(any_store, provider, Agent(name="a", model="m", tools=[Later()]))
    client = Client(any_store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    await asyncio.sleep(0.1)
    for _ in range(5):
        await rt.run_once()
    events = [r.event for r in await client.events(tid)]
    fired = [i.body for e in events if isinstance(e, InputTaken) for i in e.items
             if isinstance(i.body, TimerFired)]
    assert fired == [TimerFired(name="later")]
    assert sum(isinstance(e, Waiting) for e in events) == 2
    assert provider.requests[-1].messages[-1].text == "wake"


class _Relays(Extension):
    """Records each wait it relays, as the chat bridge does: the record
    lands after the ``Waiting``."""

    name = "relays"

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        if isinstance(recorded.event, Waiting):
            view.record(self.name, "relayed", {"seq": recorded.seq})


async def test_a_worker_clock_behind_fires_the_due_timer_past_a_plugins_record(
        store: SqliteStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from noeta.core.runner import timers

    monkeypatch.setattr(timers, "time", _Lagging(3600.0))
    provider = ScriptedProvider([reply("", ("Later", {"in": 0.05, "text": "wake"})),
                                 reply("waiting"), reply("woke")])
    rt = runtime(store, provider, Agent(name="a", model="m", tools=[Later()],
                                        extensions=(_Relays(),)))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    await asyncio.sleep(0.1)
    for _ in range(5):
        await rt.run_once()
    events = [r.event for r in await client.events(tid)]
    fired = [i.body for e in events if isinstance(e, InputTaken) for i in e.items
             if isinstance(i.body, TimerFired)]
    assert fired == [TimerFired(name="later")]
    assert provider.requests[-1].messages[-1].text == "wake"
