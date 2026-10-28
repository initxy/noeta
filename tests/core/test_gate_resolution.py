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


async def test_a_declined_call_stops_the_turn(store: SqliteStore) -> None:
    """Claude Code stops the turn when the person declines: no further model
    call; the results carry the marker and the next prompt joins them."""
    from noeta.backends.providers.anthropic import AnthropicProvider
    from noeta.core.runner.texts import REJECTED_BY_USER, REJECTED_MARKER
    from noeta.core.types import ModelRequest

    tool = Fake("T")
    provider = ScriptedProvider([reply("", ("T", {})), reply("after")])
    rt = runtime(store, provider, agent([Fixed("p", Ask(prompt="?"))], [tool]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    [rid] = [e for e in await events(client, tid) if isinstance(e, Waiting)][-1].ids
    await client.answer(tid, rid, "deny", data={"message": "not now"})
    await rt.run_until_idle()
    assert not tool.runs and len(provider.requests) == 1
    assert (await client.get(tid)).waiting_on == "input"
    [finished] = [e for e in await events(client, tid) if isinstance(e, ToolFinished)]
    assert finished.result.text.startswith(REJECTED_BY_USER) and "not now" in finished.result.text
    await client.send(tid, "next")
    await rt.run_until_idle()
    request = provider.requests[-1]
    body = AnthropicProvider(api_key="k").build_body(
        ModelRequest(model="m", messages=request.messages, max_tokens=1))
    last = body["messages"][-1]["content"]
    assert [b["type"] for b in last] == ["tool_result", "text", "text"]
    assert last[1]["text"] == REJECTED_MARKER and last[2]["text"] == "next"
