"""Structured output: each turn's answer parsed as JSON (raw text when it is
not), recorded for hosts, including on a turn that parks."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.runner import Runtime
from noeta.plugins.structured_output import StructuredOutput, structured_answer
from noeta.testing import ScriptedProvider, reply

SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}}


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def _no_sleep(_: float) -> None:
    return None


async def run(store: SqliteStore, provider: ScriptedProvider, agent: Agent) -> tuple[Client, str]:
    rt = Runtime(store=store, agents={agent.name: agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: None, sleep=_no_sleep)  # type: ignore[arg-type,return-value]
    client = Client(store)
    tid = await client.create(agent.name, "go")
    await rt.run_until_idle()
    return client, tid


async def test_json_answer_is_recorded_as_a_value_on_a_parked_turn(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply('{"b": [1], "a": 2}')])
    agent = Agent(name="s", model="m", output_schema=SCHEMA, extensions=[StructuredOutput()])
    client, tid = await run(store, provider, agent)
    assert (await client.get(tid)).waiting_on == "input"
    value = structured_answer(await client.events(tid))
    assert value == {"b": [1], "a": 2}
    assert provider.requests[0].output_schema == SCHEMA


async def test_text_that_is_not_json_falls_back_to_the_raw_answer(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("not json")])
    agent = Agent(name="s", model="m", one_shot=True, extensions=[StructuredOutput(SCHEMA)])
    client, tid = await run(store, provider, agent)
    assert structured_answer(await client.events(tid)) == "not json"
    assert provider.requests[0].output_schema == SCHEMA


async def test_continued_answer_is_parsed_whole(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply('{"a": ', stop="max_tokens"), reply("1}")])
    agent = Agent(name="s", model="m", extensions=[StructuredOutput(SCHEMA)])
    client, tid = await run(store, provider, agent)
    assert structured_answer(await client.events(tid)) == {"a": 1}


class Forgetful(StructuredOutput):
    async def on_event(self, view, recorded):  # type: ignore[no-untyped-def]
        return None


async def test_an_answer_lost_to_a_crash_is_recorded_on_the_next_run(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply('{"a": 1}')])
    client, tid = await run(store, provider, Agent(name="s", model="m",
                                                    extensions=[Forgetful(SCHEMA)]))
    assert structured_answer(await client.events(tid)) is None
    provider.add(reply('{"a": 2}'))
    await client.send(tid, "again")
    agent = Agent(name="s", model="m", extensions=[StructuredOutput(SCHEMA)])
    rt = Runtime(store=store, agents={"s": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: None, sleep=_no_sleep)  # type: ignore[arg-type,return-value]
    await rt.run_until_idle()
    answers = [r.event.data["value"] for r in await client.events(tid)
               if getattr(r.event, "extension", "") == "structured_output"]
    assert answers == [{"a": 1}, {"a": 2}]
