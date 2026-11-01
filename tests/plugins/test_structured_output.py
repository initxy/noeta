"""Structured output as Claude Code gives it: a ``StructuredOutput`` tool
whose input schema is the task's schema; an accepted call ends the turn with
its input as the value, a refused one gets an error, a turn that ends
without one gets one reminder, and refused calls fail the turn after the
retry cap. All of it read back from the task's log."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import ExtensionRecorded, TaskEnded, ToolFinished
from noeta.core.runner import Runtime
from noeta.core.types import ModelRequest
from noeta.plugins.structured_output import (
    ENFORCE,
    ENFORCE_TAG,
    StructuredOutput,
    structured_answer,
)
from noeta.testing import ScriptedProvider, reply

SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"],
          "additionalProperties": False}


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def _no_sleep(_: float) -> None:
    return None


def _runtime(store: SqliteStore, provider: ScriptedProvider, agent: Agent) -> Runtime:
    return Runtime(store=store, agents={agent.name: agent}, provider_for=lambda _m: provider,
                   env_for=lambda _s: None, sleep=_no_sleep)  # type: ignore[arg-type,return-value]


async def run(store: SqliteStore, provider: ScriptedProvider, agent: Agent,
              config: dict[str, Any] | None = None) -> tuple[Client, str]:
    rt = _runtime(store, provider, agent)
    client = Client(store)
    tid = await client.create(agent.name, "go", config=config)
    await rt.run_until_idle()
    return client, tid


def _tools(request: ModelRequest) -> dict[str, Any]:
    return {t.name: t.input_schema for t in request.tools}


async def test_the_tool_is_offered_with_the_schema_and_a_valid_call_ends_the_turn(
        store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("here it is", ("StructuredOutput", {"a": 2}))])
    agent = Agent(name="s", model="m", extensions=[StructuredOutput()])
    client, tid = await run(store, provider, agent, {"output_schema": SCHEMA})
    [request] = provider.requests  # no model call after the accepted one
    assert _tools(request)["StructuredOutput"] == SCHEMA
    assert request.output_schema is None  # never the provider's constrained decoding
    events = await client.events(tid)
    assert structured_answer(events) == {"a": 2}
    [done] = [r.event for r in events if isinstance(r.event, ToolFinished)]
    assert not done.result.is_error and done.end_turn == '{"a":2}'
    row = await client.get(tid)
    assert row.waiting_on == "input"


async def test_no_schema_no_tool(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("plain")])
    agent = Agent(name="s", model="m", extensions=[StructuredOutput()])
    await run(store, provider, agent)
    assert "StructuredOutput" not in _tools(provider.requests[0])


async def test_the_agents_and_the_plugins_schema_are_defaults(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", ("StructuredOutput", {"a": 1}))])
    agent = Agent(name="s", model="m", output_schema=SCHEMA, extensions=[StructuredOutput()])
    await run(store, provider, agent)
    assert _tools(provider.requests[0])["StructuredOutput"] == SCHEMA
    other = {"type": "object"}
    provider = ScriptedProvider([reply("", ("StructuredOutput", {}))])
    agent = Agent(name="s", model="m", extensions=[StructuredOutput(other)])
    await run(store, provider, agent)
    assert _tools(provider.requests[0])["StructuredOutput"] == other


async def test_an_invalid_call_gets_an_error_and_the_model_goes_on(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", ("StructuredOutput", {"a": "x", "b": 1})),
                                 reply("", ("StructuredOutput", {"a": 3}))])
    agent = Agent(name="s", model="m", extensions=[StructuredOutput()])
    client, tid = await run(store, provider, agent, {"output_schema": SCHEMA})
    [first, second] = [r.event for r in await client.events(tid)
                       if isinstance(r.event, ToolFinished)]
    assert first.result.is_error and first.end_turn is None
    assert first.result.text.startswith("Not accepted, the input breaks the required schema: ")
    assert "/a: must be integer" in first.result.text
    assert "must not have additional property 'b'" in first.result.text
    assert not second.result.is_error
    assert structured_answer(await client.events(tid)) == {"a": 3}


async def test_ending_without_the_call_gets_one_reminder(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("I think a is 4"), reply("", ("StructuredOutput",
                                                                    {"a": 4}))])
    agent = Agent(name="s", model="m", extensions=[StructuredOutput()])
    client, tid = await run(store, provider, agent, {"output_schema": SCHEMA})
    last = provider.requests[1].messages[-1]
    assert last.role == "user" and last.text == ENFORCE and last.origin == "inline"
    assert ENFORCE.startswith(ENFORCE_TAG)
    assert structured_answer(await client.events(tid)) == {"a": 4}


async def test_the_reminder_is_given_once_per_prompt(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("no"), reply("still no")])
    agent = Agent(name="s", model="m", extensions=[StructuredOutput()])
    client, tid = await run(store, provider, agent, {"output_schema": SCHEMA})
    assert len(provider.requests) == 2  # the turn ended after the second reply
    assert (await client.get(tid)).waiting_on == "input"
    assert structured_answer(await client.events(tid)) is None
    provider.add(reply("again no"), reply("", ("StructuredOutput", {"a": 1})))
    await client.send(tid, "next")
    await _runtime(store, provider, agent).run_until_idle()
    assert provider.requests[-1].messages[-1].text == ENFORCE  # the next prompt's own


async def test_refused_calls_fail_the_turn_at_the_cap(store: SqliteStore) -> None:
    bad = [reply("", ("StructuredOutput", {"a": "no"})) for _ in range(3)]
    provider = ScriptedProvider(bad)
    agent = Agent(name="s", model="m", extensions=[StructuredOutput(max_retries=3)])
    client, tid = await run(store, provider, agent, {"output_schema": SCHEMA})
    assert len(provider.requests) == 3  # no call after the third refusal
    row = await client.get(tid)
    assert row.waiting_on == "input"
    events = await client.events(tid)
    [failed] = [r.event.data for r in events if isinstance(r.event, ExtensionRecorded)
                and r.event.kind == "turn_failed"]
    assert failed["reason"] == "max_structured_output_retries"
    assert failed["detail"].startswith("StructuredOutput was called 3 times without a valid "
                                       "answer; the last error: Not accepted")


async def test_a_one_shot_task_ends_with_the_value_as_its_answer(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", ("StructuredOutput", {"a": 7}))])
    agent = Agent(name="s", model="m", one_shot=True, extensions=[StructuredOutput()])
    client, tid = await run(store, provider, agent, {"output_schema": SCHEMA})
    [ended] = [r.event for r in await client.events(tid) if isinstance(r.event, TaskEnded)]
    assert ended.status == "done" and json.loads(ended.answer) == {"a": 7}


class Forgetful(StructuredOutput):
    async def on_event(self, view, recorded):  # type: ignore[no-untyped-def]
        return None


async def test_a_reminder_lost_to_a_crash_is_given_on_the_next_pickup(
        store: SqliteStore) -> None:
    from noeta.core.events import ModelReplied
    from noeta.core.extension import TaskView
    from noeta.core.loop.state import fold

    provider = ScriptedProvider([reply("no tool")])
    client, tid = await run(store, provider, Agent(name="s", model="m",
                                                   extensions=[Forgetful()]),
                            {"output_schema": SCHEMA})
    events = await client.events(tid)
    at = max(r.seq for r in events if isinstance(r.event, ModelReplied))
    plugin = StructuredOutput()
    reducers = {plugin.name: plugin.reduce}
    # The log as a crash right after the reply leaves it: the reminder is owed.
    crashed = TaskView(state=fold([r for r in events if r.seq <= at], reducers))
    await plugin.on_start(crashed)
    [(say, key, _)] = crashed.enqueued
    assert say.message.text == ENFORCE  # type: ignore[union-attr]
    assert key == f"structured_output:enforce:{crashed.task_id}:{at}"
    # The whole log: the task waited after the reply, so nothing is owed.
    waited = TaskView(state=fold(events, reducers))
    await plugin.on_start(waited)
    assert waited.enqueued == []
