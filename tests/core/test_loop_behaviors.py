"""Core-loop behaviors kept from 0.6: failed turns park, empty replies are
never recorded (the turn ends quietly unless an extension continues it), cut
replies still run their complete calls, the step limit ends with a report,
retries are visible."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core import records
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
from noeta.core.extension import Extension, TaskView
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


async def test_empty_reply_with_another_stop_still_fails(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", stop="refusal")])
    client, tid = await run(store, provider, Agent(name="a", model="m"))
    evs = await events(client, tid)
    assert not any(isinstance(e, ModelReplied) for e in evs)
    assert evs[-1].detail["turn_failed"].startswith(texts.LLM_EMPTY_RESPONSE)


def _core(evs: list[Any], kind: str) -> list[Any]:
    return [records.decode(e) for e in evs
            if isinstance(e, ExtensionRecorded) and e.extension == "core" and e.kind == kind]


async def test_without_an_extension_an_empty_reply_ends_the_turn_quietly(
        store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("")])
    client, tid = await run(store, provider, Agent(name="a", model="m"))
    evs = await events(client, tid)
    assert len(provider.requests) == 1
    assert not any(isinstance(e, ModelReplied) for e in evs)
    assert not _core(evs, "continue")
    [done] = _core(evs, "turn_done")
    assert done.reason == records.EMPTY_REPLY and done.usage is not None
    assert isinstance(evs[-1], Waiting) and evs[-1].detail == {"answer": ""}


async def test_without_an_extension_a_one_shot_task_ends_done_with_no_answer(
        store: SqliteStore) -> None:
    from noeta.host.usage import tree_usage

    probe = Probe()
    provider = ScriptedProvider([reply("Looking.", ("Probe", {})), reply("")])
    client, tid = await run(store, provider,
                            Agent(name="a", model="m", tools=[probe], one_shot=True))
    end = (await events(client, tid))[-1]
    assert isinstance(end, TaskEnded) and end.status == "done" and end.answer == ""
    total = await tree_usage(client, tid)
    assert sum(m.calls for m in total.by_model.values()) == 2  # the dropped reply counts


class GoOn(Extension):
    """Continues the turn after an empty reply with its own message, once."""

    name = "go_on"

    def __init__(self) -> None:
        self.seen: list[ModelResponse] = []

    async def on_empty_reply(self, view: TaskView, response: ModelResponse) -> Message | None:
        self.seen.append(response)
        view.record("go_on", "asked")
        return None if view.state.empty_nudged else user("go on", origin="inline")


async def test_an_extension_continues_the_turn_after_an_empty_reply(store: SqliteStore) -> None:
    ext = GoOn()
    provider = ScriptedProvider([reply(""), reply(""), reply(""), reply("hi")])
    agent = Agent(name="a", model="m", extensions=[ext])
    client, tid = await run(store, provider, agent)
    evs = await events(client, tid)
    # Asked once, then the second empty reply ends the turn.
    assert len(provider.requests) == 2 and len(ext.seen) == 2
    assert provider.requests[1].messages[-1] == user("go on", origin="inline")
    [cont] = _core(evs, "continue")
    assert cont.reason == records.EMPTY_REPLY and cont.usage is not None
    assert [d.reason for d in _core(evs, "turn_done")] == [records.EMPTY_REPLY]
    # The extension's own records made in the hook are kept.
    assert sum(isinstance(e, ExtensionRecorded) and e.extension == "go_on" for e in evs) == 2
    assert isinstance(evs[-1], Waiting) and evs[-1].detail == {"answer": ""}


def _old_empty_reply_log() -> list[Any]:
    """A log as the core wrote it while it asked again itself: an empty
    reply, its nudge, another empty reply, then a prompt."""
    from noeta.core.events import InboxItem, InputTaken, Recorded, Say, TaskCreated
    from noeta.core.types import Usage

    nudge = ("[Your previous response had no visible output. Please continue and "
             "produce a user-visible response.]")
    usage = Usage(uncached=10, output=5)
    return [Recorded(seq=i + 1, at=0.0, event=ev) for i, ev in enumerate([
        TaskCreated(agent="a", config={}),
        InputTaken(items=[InboxItem(seq=1, at=0.0, body=Say(message=user("go")))], through=1),
        records.event(records.Continue(reason=records.EMPTY_REPLY, model="m", usage=usage),
                      message=user(nudge, origin="inline")),
        records.event(records.TurnDone(reason=records.EMPTY_REPLY, model="m", usage=usage)),
    ])]


def test_an_old_empty_reply_log_replays_to_the_same_state() -> None:
    from noeta.core.loop.state import TaskState, apply
    from noeta.core.types import Usage

    log = _old_empty_reply_log()
    state = TaskState(task_id="t")
    for rec in log[:3]:
        apply(state, rec, {})
    assert state.empty_nudged and state.model_attempts == 0
    assert [(m.role, m.origin) for m in state.messages] == [("user", None), ("user", "inline")]
    apply(state, log[3], {})
    assert not state.turn_open and state.answer_parts == [] and state.last_answer == ""
    assert state.usage == Usage(uncached=20, output=10)
    assert state.messages[-1].text.startswith("[Your previous response had no visible output.")

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
    assert results[0].text == "<tool_use_error>Error: No such tool available: Nope</tool_use_error>"
    assert results[0].is_error
    assert probe.runs == 1


async def test_retries_are_recorded(store: SqliteStore) -> None:
    provider = ScriptedProvider([Transient("overloaded"), reply("ok")])
    client, tid = await run(store, provider, Agent(name="a", model="m"))
    retries = [e for e in await events(client, tid)
               if isinstance(e, ExtensionRecorded) and e.kind == "retry"]
    assert len(retries) == 1 and retries[0].data["attempt"] == 1
