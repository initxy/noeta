"""A running task's config changes through the ``config`` signal (a model
switch mid-task), a turn stops at ``config["max_turns"]`` model calls, and
a task can start from another's log up to a point (a fork)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import InputTaken, Waiting
from noeta.core.loop.state import CONFIG_SIGNAL
from noeta.core.runner import Runtime
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.testing import ScriptedProvider, reply


class Noop:
    name = "Noop"
    description = "Does nothing."
    input_schema = {"type": "object"}
    read_only = True
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return text_output("done")


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def _no_sleep(_: float) -> None:
    return None


def runtime(store: SqliteStore, provider: ScriptedProvider) -> Runtime:
    agent = Agent(name="a", model="m", tools=[Noop()])
    return Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,  # type: ignore[arg-type,return-value]
                   env_for=lambda _s: None, sleep=_no_sleep)  # type: ignore[arg-type,return-value]


async def test_the_config_signal_switches_the_model_from_the_next_call(
        store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("one"), reply("two"), reply("three")])
    rt = runtime(store, provider)
    client = Client(store)
    tid = await client.create("a", "hi", config={"model": "m1", "effort": "low"})
    await rt.run_until_idle()
    await client.signal(tid, CONFIG_SIGNAL, {"model": "m2", "cwd": "/elsewhere"})
    await client.send(tid, "again")
    await rt.run_until_idle()
    await client.signal(tid, CONFIG_SIGNAL, {"effort": None})
    await client.send(tid, "once more")
    await rt.run_until_idle()
    assert [r.model for r in provider.requests] == ["m1", "m2", "m2"]
    assert [r.effort for r in provider.requests] == ["low", "low", None]


async def test_max_turns_fails_the_turn_before_another_model_call(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", ("Noop", {})), reply("", ("Noop", {})),
                                 reply("never")])
    rt = runtime(store, provider)
    client = Client(store)
    tid = await client.create("a", "go", config={"model": "m", "max_turns": 2})
    await rt.run_until_idle()
    assert len(provider.requests) == 2
    waiting = (await client.events(tid))[-1].event
    assert isinstance(waiting, Waiting) and waiting.on == "input"
    assert waiting.detail["turn_failed"] == "max_turns: Reached maximum number of turns (2)"


async def test_a_task_created_from_another_log_continues_its_conversation(
        store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("first"), reply("second"), reply("forked")])
    rt = runtime(store, provider)
    client = Client(store)
    tid = await client.create("a", "one", config={"model": "m"})
    await rt.run_until_idle()
    await client.send(tid, "two")
    await rt.run_until_idle()
    events = await client.events(tid)
    second = [r.seq for r in events if isinstance(r.event, InputTaken)][1]
    fork = await client.create("a", "instead", config={"model": "m"}, history_from=tid,
                               history_through=second - 1)
    await rt.run_until_idle()
    sent = [m.text for m in provider.requests[-1].messages]
    assert sent == ["one", "first", "instead"]
    assert (await client.get(fork)).root_id == fork
