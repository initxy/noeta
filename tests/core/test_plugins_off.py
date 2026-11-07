"""A task's ``plugins_off`` config: a plugin named there keeps folding its
records but offers and is asked nothing, from the next turn on, on whichever
worker runs the task; a child task starts with its parent's."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.extension import Extension, Reminder, TaskView, child_task
from noeta.core.loop.state import CONFIG_SIGNAL, PLUGINS_OFF, TaskState, fold
from noeta.core.runner import Runtime, TaskRun
from noeta.core.store import Claim
from noeta.core.tool import Tool, ToolContext, ToolOutput, text_output
from noeta.testing import ScriptedProvider, reply


class Ping:
    name = "Ping"
    description = "Answers pong."
    input_schema = {"type": "object"}
    read_only = True
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return text_output("pong")


class SwitchOff:
    """Switches ``extra`` off for its own task while the turn runs."""

    name = "SwitchOff"
    description = "Switches a plugin off."
    input_schema = {"type": "object"}
    read_only = True
    clearable = False

    def __init__(self, client: Client) -> None:
        self.client = client

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        await self.client.signal(ctx.task_id, CONFIG_SIGNAL, {PLUGINS_OFF: ["extra"]})
        return text_output("switched")


class Extra(Extension):
    """Offers ``Ping`` and records each time it is asked for reminders."""

    name = "extra"

    def __init__(self) -> None:
        self.asked = 0

    def initial_state(self) -> int:
        return 0

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        return state + 1 if kind == "seen" else state

    def tools(self, view: TaskView) -> Sequence[Tool]:
        return [Ping()]

    async def reminders(self, view: TaskView) -> Sequence[Reminder]:
        self.asked += 1
        view.record(self.name, "seen")
        return ()


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


def runtime(store: SqliteStore, provider: ScriptedProvider, *exts: Extension,
            tools: Sequence[Any] = ()) -> Runtime:
    agent = Agent(name="a", model="m", extensions=exts, tools=list(tools))
    return Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,  # type: ignore[arg-type,return-value]
                   env_for=lambda _s: None)  # type: ignore[arg-type,return-value]


def tool_names(provider: ScriptedProvider) -> list[list[str]]:
    return [[t.name for t in r.tools] for r in provider.requests]


async def loaded(rt: Runtime, task_id: str) -> TaskRun:
    run = TaskRun(rt, Claim(task_id=task_id, worker=rt.worker, epoch=0, lease_until=0.0))
    await run.load()
    return run


async def test_a_plugin_switched_off_offers_nothing_but_keeps_its_state(
        store: SqliteStore) -> None:
    extra = Extra()
    provider = ScriptedProvider([reply("one"), reply("two"), reply("three")])
    rt = runtime(store, provider, extra)
    client = Client(store)
    tid = await client.create("a", "hi")
    await rt.run_until_idle()
    before = await loaded(rt, tid)
    version = before.fold_version()
    await client.signal(tid, CONFIG_SIGNAL, {PLUGINS_OFF: ["extra"]})
    await client.send(tid, "again")
    await rt.run_until_idle()
    after = await loaded(rt, tid)
    assert tool_names(provider) == [["Ping"], []]
    assert extra.asked == 1
    assert after.state.ext["extra"] == 1  # still folded, never asked again
    assert after.fold_version() == version
    assert [e.name for e in after.exts] == [] and [e.name for e in after.all_exts] == ["extra"]
    await client.signal(tid, CONFIG_SIGNAL, {PLUGINS_OFF: None})
    await client.send(tid, "back")
    await rt.run_until_idle()
    assert tool_names(provider)[-1] == ["Ping"] and extra.asked == 2


async def test_a_switch_sent_mid_turn_applies_from_the_next_turn(store: SqliteStore) -> None:
    client = Client(store)
    provider = ScriptedProvider([reply("", ("SwitchOff", {})), reply("done"), reply("later")])
    rt = runtime(store, provider, Extra(), tools=[SwitchOff(client)])
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    await client.send(tid, "next")
    await rt.run_until_idle()
    assert tool_names(provider) == [["Ping", "SwitchOff"], ["Ping", "SwitchOff"],
                                    ["SwitchOff"]]


async def test_another_worker_runs_the_task_without_the_plugin(store: SqliteStore) -> None:
    """The switch is in the log: a second worker (its own agent and plugin
    instances) runs the next turn without the plugin, and still has it
    pinned, so it never counts as missing."""
    client = Client(store)
    first, second = ScriptedProvider([reply("one")]), ScriptedProvider([reply("two")])
    on_first, on_second = Extra(), Extra()
    tid = await client.create("a", "hi")
    await runtime(store, first, on_first).run_until_idle()
    await client.signal(tid, CONFIG_SIGNAL, {PLUGINS_OFF: ["extra"]})
    await client.send(tid, "again")
    rt2 = runtime(store, second, on_second)
    await rt2.run_until_idle()
    assert tool_names(first) == [["Ping"]] and tool_names(second) == [[]]
    assert on_second.asked == 0
    assert (await loaded(rt2, tid)).state.ext["extra"] == 1
    assert (await client.get(tid)).status == "waiting"


def test_a_child_starts_with_its_parents_switches() -> None:
    state = TaskState(task_id="p", config={PLUGINS_OFF: ["web", "todos"]})
    created = child_task("p", state, [], "a", {PLUGINS_OFF: ["memory"], "model": "m"})
    assert created.config[PLUGINS_OFF] == ["memory", "todos", "web"]
    assert child_task("p", TaskState(task_id="p"), [], "a", None).config == {}


async def test_the_switch_folds_from_the_created_config(store: SqliteStore) -> None:
    client = Client(store)
    tid = await client.create("a", "hi", config={PLUGINS_OFF: ["extra", 3, ""]})
    state = fold(await client.events(tid), {})
    assert state.plugins_off == ["extra"]
    provider = ScriptedProvider([reply("one")])
    await runtime(store, provider, Extra()).run_until_idle()
    assert tool_names(provider) == [[]]


async def test_a_continued_conversation_runs_without_what_its_source_runs_without_now(
        store: SqliteStore) -> None:
    """A fork, a rewind or a resume-as-new takes the source's switches as
    they stand now (not as it was created), unless its config names its
    own."""
    client = Client(store)
    provider = ScriptedProvider([reply("one"), reply("two"), reply("three")])
    rt = runtime(store, provider, Extra())
    src = await client.create("a", "hi", config={PLUGINS_OFF: ["web"]})
    await rt.run_until_idle()
    await client.signal(src, CONFIG_SIGNAL, {PLUGINS_OFF: ["extra"]})
    fork = await client.create("a", "go on", history_from=src)
    created = (await client.events(fork))[0].event
    assert created.config[PLUGINS_OFF] == ["extra"]  # type: ignore[attr-defined]
    await rt.run_until_idle()
    assert tool_names(provider)[-1] == []
    own = await client.create("a", None, history_from=src, config={PLUGINS_OFF: []})
    assert (await client.events(own))[0].event.config[PLUGINS_OFF] == []  # type: ignore[attr-defined]

