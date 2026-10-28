"""How the runner combines gates: an approving Allow, firm asks, and
``resolve_ask`` answering before a call parks."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.client import Client
from noeta.core.events import External, InputTaken, ToolFinished, Waiting
from noeta.core.extension import Allow, Ask, Deny, Extension, Gate, TaskView
from noeta.core.types import ToolUse
from noeta.testing import ScriptedProvider, reply
from tests.plugins.permissions.helpers import Fake, agent, events, runtime


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


class Fixed(Extension):
    def __init__(self, name: str, gate: Gate | None, resolve: Gate | None = None) -> None:
        self.name = name
        self.gate = gate
        self.resolve = resolve
        self.resolved: list[list[Ask]] = []

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        return self.gate

    async def resolve_ask(self, view: TaskView, call: ToolUse,
                          asks: Sequence[Ask]) -> Gate | None:
        self.resolved.append(list(asks))
        return self.resolve


async def run(store: SqliteStore, *exts: Extension) -> tuple[list[Any], Fake]:
    tool = Fake("T")
    provider = ScriptedProvider([reply("", ("T", {})), reply("done")])
    rt = runtime(store, provider, agent(exts, [tool]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    return await events(client, tid), tool


async def test_an_approving_allow_satisfies_asks_that_are_not_firm(store: SqliteStore) -> None:
    evs, tool = await run(store, Fixed("p", Ask(prompt="?")), Fixed("h", Allow(approve=True)))
    assert tool.runs == [{}]
    assert not any(isinstance(e, Waiting) and e.on == "approval" for e in evs)


async def test_a_firm_ask_still_reaches_a_person(store: SqliteStore) -> None:
    evs, tool = await run(store, Fixed("p", Ask(prompt="?", firm=True)),
                          Fixed("h", Allow(approve=True)))
    assert not tool.runs
    assert isinstance(evs[-1], Waiting) and evs[-1].on == "approval"


async def test_resolve_ask_answers_in_a_persons_place(store: SqliteStore) -> None:
    resolver = Fixed("r", None, resolve=Deny(reason="blocked by policy"))
    evs, tool = await run(store, Fixed("p", Ask(prompt="?")), resolver)
    assert not tool.runs
    [finished] = [e for e in evs if isinstance(e, ToolFinished)]
    assert finished.result.is_error and finished.result.text == "blocked by policy"
    assert [a.prompt for a in resolver.resolved[0]] == ["?"]


async def test_resolve_ask_is_not_asked_when_someone_denies(store: SqliteStore) -> None:
    resolver = Fixed("r", Deny(reason="no"), resolve=Allow())
    _, tool = await run(store, Fixed("p", Ask(prompt="?")), resolver)
    assert not tool.runs and resolver.resolved == []


async def test_a_signal_reaches_the_task_without_a_turn(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("hi")])
    rt = runtime(store, provider, agent([], []))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    await client.signal(tid, "permission_mode", {"mode": "plan"})
    await rt.run_until_idle()
    taken = [i.body for e in await events(client, tid) if isinstance(e, InputTaken)
             for i in e.items]
    assert External(name="permission_mode", data={"mode": "plan"}) in taken
    assert (await client.get(tid)).waiting_on == "input"
