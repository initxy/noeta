"""Core-loop behaviors kept from 0.6: failed turns park, empty replies are
never recorded, cut replies still run their complete calls, the step limit
ends with a report, retries are visible."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.errors import Fatal, Transient
from noeta.core.events import (
    ExtensionRecorded,
    ModelReplied,
    TaskEnded,
    ToolFinished,
    Waiting,
)
from noeta.core.runner import OUTPUT_LIMIT_THINKING_NUDGE, Runtime, texts
from noeta.core.store.sqlite import SqliteStore
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import Message, ModelResponse, Thinking
from noeta.testing import ScriptedProvider, reply


class Probe:
    name = "Probe"
    description = "Look."
    input_schema = {"type": "object"}
    read_only = True
    clearable = True

    def __init__(self) -> None:
        self.live = 0
        self.peak = 0
        self.runs = 0

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        self.runs += 1
        self.live += 1
        self.peak = max(self.peak, self.live)
        await asyncio.sleep(0.01)
        self.live -= 1
        return text_output("seen")


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def _no_sleep(_: float) -> None:
    return None


async def run(store: SqliteStore, provider: ScriptedProvider, agent: Agent,
              goal: str = "go") -> tuple[Client, str]:
    rt = Runtime(store=store, agents={agent.name: agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: None, sleep=_no_sleep)  # type: ignore[arg-type,return-value]
    client = Client(store)
    tid = await client.create(agent.name, goal)
    await rt.run_until_idle()
    return client, tid


async def events(client: Client, tid: str) -> list[Any]:
    return [r.event for r in await client.events(tid)]


async def test_model_error_parks_an_interactive_task(store: SqliteStore) -> None:
    provider = ScriptedProvider([Fatal("bad key", status=401, body="invalid x-api-key")])
    client, tid = await run(store, provider, Agent(name="a", model="m"))
    row = await client.get(tid)
    assert row.status == "waiting"
    last = (await events(client, tid))[-1]
    assert isinstance(last, Waiting)
    assert last.detail["turn_failed"].startswith("llm_error: bad key")
    assert "invalid x-api-key" in last.detail["turn_failed"]
    provider.add(reply("fine now"))
    await client.send(tid, "again")
    await Runtime(store=store, agents={"a": Agent(name="a", model="m")},
                  provider_for=lambda _m: provider, env_for=lambda _s: None,  # type: ignore[arg-type,return-value]
                  sleep=_no_sleep).run_until_idle()
    last = (await events(client, tid))[-1]
    assert isinstance(last, Waiting) and last.detail == {"answer": "fine now"}


async def test_model_error_ends_a_one_shot_task(store: SqliteStore) -> None:
    provider = ScriptedProvider([Fatal("nope")])
    client, tid = await run(store, provider, Agent(name="a", model="m", one_shot=True))
    end = (await events(client, tid))[-1]
    assert isinstance(end, TaskEnded) and end.status == "failed"
    assert end.error.startswith(texts.LLM_ERROR)


async def test_empty_reply_is_never_recorded(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("")])
    client, tid = await run(store, provider, Agent(name="a", model="m"))
    evs = await events(client, tid)
    assert not any(isinstance(e, ModelReplied) for e in evs)
    assert evs[-1].detail["turn_failed"].startswith(texts.LLM_EMPTY_RESPONSE)


async def test_thinking_only_cut_is_not_recorded_and_nudges(store: SqliteStore) -> None:
    cut = ModelResponse(message=Message(role="assistant", content=[Thinking(text="hmm")]),
                        stop_reason="max_tokens")
    provider = ScriptedProvider([cut, reply("answer")])
    client, tid = await run(store, provider, Agent(name="a", model="m"))
    replies = [e for e in await events(client, tid) if isinstance(e, ModelReplied)]
    assert [r.response.message.text for r in replies] == ["answer"]
    assert provider.requests[1].messages[-1].text == OUTPUT_LIMIT_THINKING_NUDGE


async def test_cut_reply_still_runs_its_complete_calls(store: SqliteStore) -> None:
    probe = Probe()
    provider = ScriptedProvider([reply("", ("Probe", {}), stop="max_tokens"), reply("ok")])
    client, tid = await run(store, provider, Agent(name="a", model="m", tools=[probe]))
    assert probe.runs == 1
    assert (await client.get(tid)).waiting_on == "input"


async def test_step_limit_ends_with_a_report(store: SqliteStore) -> None:
    probe = Probe()
    provider = ScriptedProvider([reply("", ("Probe", {})), reply("", ("Probe", {})),
                                 reply("here is where it stands", ("Probe", {}))])
    agent = Agent(name="a", model="m", tools=[probe], max_steps_per_turn=2)
    client, tid = await run(store, provider, agent)
    final = provider.requests[-1]
    assert final.tool_choice == "none" and final.tools
    assert final.messages[-1].text == texts.STEP_LIMIT_PROMPT.format(n=2)
    last = (await events(client, tid))[-1]
    assert isinstance(last, Waiting)
    assert last.detail["answer"] == ("Stopped at the 2-step limit before finishing.\n\n"
                                     "here is where it stands")
    assert probe.runs == 2  # the report round's tool call was dropped


async def test_parallel_read_only_calls_are_capped_at_ten(store: SqliteStore) -> None:
    probe = Probe()
    provider = ScriptedProvider([reply("", *[("Probe", {})] * 14), reply("done")])
    await run(store, provider, Agent(name="a", model="m", tools=[probe]))
    assert probe.runs == 14 and probe.peak == 10


async def test_unknown_tool_answers_only_that_call(store: SqliteStore) -> None:
    probe = Probe()
    provider = ScriptedProvider([reply("", ("Nope", {}), ("Probe", {})), reply("done")])
    client, tid = await run(store, provider, Agent(name="a", model="m", tools=[probe]))
    results = [e.result for e in await events(client, tid) if isinstance(e, ToolFinished)]
    assert results[0].text == texts.UNKNOWN_TOOL.format(name="Nope") and results[0].is_error
    assert probe.runs == 1


async def test_retries_are_recorded(store: SqliteStore) -> None:
    provider = ScriptedProvider([Transient("overloaded"), reply("ok")])
    client, tid = await run(store, provider, Agent(name="a", model="m"))
    retries = [e for e in await events(client, tid)
               if isinstance(e, ExtensionRecorded) and e.kind == "retry"]
    assert len(retries) == 1 and retries[0].data["attempt"] == 1
