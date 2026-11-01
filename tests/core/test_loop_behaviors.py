"""Core-loop behaviors kept from 0.6: failed turns park, empty replies are
never recorded (the model is asked once per step to go on), cut replies still run their complete calls, the step limit
ends with a report, retries are visible."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
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
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import Message, ModelResponse, Thinking, user
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


def _nudges(request: Any) -> int:
    return sum(m.text == texts.EMPTY_REPLY_NUDGE for m in request.messages)


async def test_empty_reply_is_never_recorded_and_is_asked_to_go_on(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply(""), reply("hello")])
    client, tid = await run(store, provider, Agent(name="a", model="m"))
    evs = await events(client, tid)
    replies = [e.response.message.text for e in evs if isinstance(e, ModelReplied)]
    assert replies == ["hello"]
    nudge = provider.requests[1].messages[-1]
    assert nudge.role == "user" and nudge.text == texts.EMPTY_REPLY_NUDGE
    assert [m.role for m in provider.requests[1].messages[-2:]] == ["user", "user"]
    assert isinstance(evs[-1], Waiting) and evs[-1].detail == {"answer": "hello"}


async def test_reasoning_only_reply_is_empty(store: SqliteStore) -> None:
    only = ModelResponse(message=Message(role="assistant", content=[Thinking(text="hmm")]),
                         stop_reason="end_turn")
    provider = ScriptedProvider([only, reply("hello")])
    await run(store, provider, Agent(name="a", model="m"))
    assert provider.requests[1].messages[-1].text == texts.EMPTY_REPLY_NUDGE
    assert not any(isinstance(b, Thinking) for m in provider.requests[1].messages
                   for b in m.content)


async def test_empty_again_after_the_nudge_ends_the_turn_quietly(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply(""), reply("")])
    client, tid = await run(store, provider, Agent(name="a", model="m"))
    evs = await events(client, tid)
    assert len(provider.requests) == 2
    assert not any(isinstance(e, ModelReplied) for e in evs)
    last = evs[-1]
    assert isinstance(last, Waiting) and last.detail == {"answer": ""}
    # The next message opens a turn as usual; the nudge stays in the history.
    provider.add(reply("back"))
    await client.send(tid, "again")
    await Runtime(store=store, agents={"a": Agent(name="a", model="m")},
                  provider_for=lambda _m: provider, env_for=lambda _s: None,  # type: ignore[arg-type,return-value]
                  sleep=_no_sleep).run_until_idle()
    assert _nudges(provider.requests[2]) == 1
    assert provider.requests[2].messages[-1].text == "again"


async def test_one_shot_task_ends_done_after_two_empty_replies(store: SqliteStore) -> None:
    probe = Probe()
    provider = ScriptedProvider([reply("Looking.", ("Probe", {})), reply(""), reply("")])
    client, tid = await run(store, provider,
                            Agent(name="a", model="m", tools=[probe], one_shot=True))
    end = (await events(client, tid))[-1]
    # The preamble before the call is no answer: the turn ended on nothing.
    assert isinstance(end, TaskEnded) and end.status == "done" and not end.error
    assert end.answer == ""


async def test_the_nudge_is_asked_again_after_each_step(store: SqliteStore) -> None:
    probe = Probe()
    provider = ScriptedProvider([reply("", ("Probe", {})), reply(""),
                                 reply("", ("Probe", {})), reply(""), reply("done")])
    client, tid = await run(store, provider,
                            Agent(name="a", model="m", tools=[probe], one_shot=True))
    assert probe.runs == 2
    assert [_nudges(r) for r in provider.requests] == [0, 0, 1, 1, 2]
    end = (await events(client, tid))[-1]
    assert isinstance(end, TaskEnded) and end.status == "done" and end.answer == "done"


async def test_empty_reply_with_another_stop_still_fails(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", stop="refusal")])
    client, tid = await run(store, provider, Agent(name="a", model="m"))
    evs = await events(client, tid)
    assert not any(isinstance(e, ModelReplied) for e in evs)
    assert evs[-1].detail["turn_failed"].startswith(texts.LLM_EMPTY_RESPONSE)


async def _again(store: SqliteStore, provider: ScriptedProvider, agent: Agent) -> None:
    await Runtime(store=store, agents={agent.name: agent}, provider_for=lambda _m: provider,
                  env_for=lambda _s: None,  # type: ignore[arg-type,return-value]
                  sleep=_no_sleep).run_until_idle()


async def test_cut_and_empty_replies_are_no_crash_attempts(store: SqliteStore) -> None:
    # Two reasoning-only cuts, then an empty reply: three model calls without
    # a recorded reply, none of them a crash; the nudge still goes out.
    cut = ModelResponse(message=Message(role="assistant", content=[Thinking(text="hmm")]),
                        stop_reason="max_tokens")
    provider = ScriptedProvider([cut, cut, reply(""), reply("answer")])
    client, tid = await run(store, provider, Agent(name="a", model="m"))
    assert len(provider.requests) == 4
    assert [_nudges(r) for r in provider.requests] == [0, 0, 0, 1]
    last = (await events(client, tid))[-1]
    assert isinstance(last, Waiting) and last.detail == {"answer": "answer"}


async def test_a_new_turn_opened_by_a_notice_is_nudged_again(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply(""), reply("")])
    agent = Agent(name="a", model="m")
    client, tid = await run(store, provider, agent)
    provider.add(reply(""), reply("the job is done"))
    await client.send(tid, user("Background job finished.", origin="agent"))
    await _again(store, provider, agent)
    assert [_nudges(r) for r in provider.requests] == [0, 1, 1, 2]
    last = (await events(client, tid))[-1]
    assert isinstance(last, Waiting) and last.detail == {"answer": "the job is done"}


async def test_quiet_end_then_a_prompt_then_empty_again(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply(""), reply("")])
    agent = Agent(name="a", model="m")
    client, tid = await run(store, provider, agent)
    provider.add(reply(""), reply(""))
    await client.send(tid, "and again")
    await _again(store, provider, agent)
    assert [_nudges(r) for r in provider.requests] == [0, 1, 1, 2]
    evs = await events(client, tid)
    assert not any(isinstance(e, ModelReplied) for e in evs)
    assert isinstance(evs[-1], Waiting) and evs[-1].detail == {"answer": ""}


async def test_dropped_replies_count_in_the_usage(store: SqliteStore) -> None:
    from noeta.host.usage import tree_usage

    probe = Probe()
    provider = ScriptedProvider([reply("", ("Probe", {})), reply(""), reply("")])
    client, tid = await run(store, provider,
                            Agent(name="a", model="m", tools=[probe], one_shot=True))
    total = await tree_usage(client, tid)
    assert sum(m.calls for m in total.by_model.values()) == 3
    assert total.usage.output == 15


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
    agent = Agent(name="a", model="m", tools=[probe], step_budget=2)
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
