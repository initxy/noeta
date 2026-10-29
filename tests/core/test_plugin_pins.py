"""A task records the plugins it runs with when it starts; a worker that
lacks one of them leaves the task to a worker that has it (a rolling
install), and after a few claims in a row fails it (``plugin_missing``)
instead of running it without; a changed plugin set is recorded again."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from noeta.agents.agent_files import build_agents, parse_agent_file
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core import records, registry
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import TaskEnded
from noeta.core.extension import Extension
from noeta.core.runner import Runtime
from noeta.testing import ScriptedProvider, reply


class Marker(Extension):
    name = "marker"


def runtime(store: SqliteStore, agent: Agent, provider: ScriptedProvider,
            **kw: Any) -> Runtime:
    return Runtime(store=store, agents={agent.name: agent}, provider_for=lambda _m: provider,
                   env_for=lambda _s: None, **kw)  # type: ignore[arg-type,return-value]


async def pins(client: Client, task_id: str) -> list[list[dict[str, str]]]:
    return [core.pins for r in await client.events(task_id)
            if isinstance(core := records.decode(r.event), records.Plugins)]


@pytest.fixture
async def store(tmp_path: Path) -> Any:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def test_a_task_records_its_plugins_when_it_starts(store: SqliteStore) -> None:
    [agent] = build_agents([parse_agent_file(
        "---\nname: a\nplugins:\n  - title: {model: small}\n---\nhi\n")], model="m",
        plugins={"tool_search": False})
    agent = Agent(name="a", model="m", extensions=(*agent.extensions, Marker()))
    provider = ScriptedProvider([reply("one"), reply("two")])
    client = Client(store)
    tid = await client.create("a", "go")
    await runtime(store, agent, provider).run_until_idle()
    [[title, marker]] = await pins(client, tid)
    assert title["name"] == "title" and title["source"] == "package noeta"
    assert title["version"] and len(title["arguments"]) == 16
    assert marker == {"name": "marker", "source": f"class {__name__}.Marker", "version": "",
                      "arguments": ""}
    await client.send(tid, "again")
    await runtime(store, agent, provider).run_until_idle()
    assert len(await pins(client, tid)) == 1  # the same plugins: nothing new recorded


async def test_a_worker_without_a_pinned_plugin_leaves_the_task_to_one_with_it(
        store: SqliteStore) -> None:
    """A worker not yet upgraded in a rolling install claims a task an
    upgraded one pinned the new plugin into: it lets the lease run out with
    nothing written, and the upgraded worker goes on with the task."""
    client = Client(store)
    tid = await client.create("a", "go")
    with_marker = Agent(name="a", model="m", extensions=(Marker(),))
    await runtime(store, with_marker, ScriptedProvider([reply("one")])).run_until_idle()
    await client.send(tid, "again")
    seq = (await client.get(tid)).last_seq
    old = ScriptedProvider()
    assert await runtime(store, Agent(name="a", model="m"), old,
                         lease_seconds=0.1).run_once()
    row = await client.get(tid)
    assert row.status != "ended" and row.last_seq == seq and old.requests == []
    await asyncio.sleep(0.15)  # the old worker's lease runs out
    await runtime(store, with_marker, ScriptedProvider([reply("two")])).run_until_idle()
    row = await client.get(tid)
    assert (row.status, row.waiting_on) == ("waiting", "input")


async def test_a_worker_without_a_pinned_plugin_fails_the_task_after_a_few_claims(
        store: SqliteStore) -> None:
    from noeta.core.runner.task import LOAD_ATTEMPTS

    client = Client(store)
    tid = await client.create("a", "go")
    with_marker = Agent(name="a", model="m", extensions=(Marker(),))
    await runtime(store, with_marker, ScriptedProvider([reply("one")])).run_until_idle()
    await client.send(tid, "again")
    provider = ScriptedProvider()
    rt = runtime(store, Agent(name="a", model="m"), provider, lease_seconds=0.1)
    for attempt in range(LOAD_ATTEMPTS):
        assert (await client.get(tid)).status != "ended", attempt
        assert await rt.run_once()
        await asyncio.sleep(0.15)  # the last claim's lease runs out
    end = (await client.events(tid))[-1].event
    assert isinstance(end, TaskEnded) and end.status == "failed"
    assert end.error.startswith("plugin_missing: this task runs with marker,")
    assert provider.requests == []  # it never ran without the plugin


async def test_a_changed_plugin_set_is_recorded_again(store: SqliteStore) -> None:
    client = Client(store)
    tid = await client.create("a", "go")
    provider = ScriptedProvider([reply("one"), reply("two")])
    await runtime(store, Agent(name="a", model="m"), provider).run_until_idle()
    await client.send(tid, "again")
    await runtime(store, Agent(name="a", model="m", extensions=(Marker(),)),
                  provider).run_until_idle()
    first, second = await pins(client, tid)
    assert first == [] and [p["name"] for p in second] == ["marker"]
    assert (await client.get(tid)).status == "waiting"


def test_the_arguments_hash_is_the_same_in_every_process() -> None:
    def resolver() -> None: ...

    a, b = Marker(), Marker()
    registry.note_arguments(a, {"x": 1, "fn": resolver, "path": Path("/p")})
    registry.note_arguments(b, {"path": Path("/p"), "fn": resolver, "x": 1})
    assert registry.pin(a) == registry.pin(b)
    registry.note_arguments(b, {"x": 2})
    assert registry.pin(a)["arguments"] != registry.pin(b)["arguments"]
