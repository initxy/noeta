"""The ``empty_reply`` plugin: a reply with no text and no tool call is
asked to go on, once per step, as Claude Code asks; without the plugin the
turn ends quietly on the first one."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.agents import DEFAULT_PLUGINS, builtin_agent_files, main_agent
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core import records
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import ExtensionRecorded, ModelReplied, TaskEnded, Waiting
from noeta.core.loop.state import TaskState, apply
from noeta.core.runner import Runtime
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import Message, ModelResponse, Thinking, user
from noeta.plugins.empty_reply import EmptyReply
from noeta.testing import ScriptedProvider, reply
from noeta.toolkit.empty_reply import EMPTY_REPLY_NUDGE


class Probe:
    name = "Probe"
    description = "Look."
    input_schema = {"type": "object"}
    read_only = True
    clearable = True

    def __init__(self) -> None:
        self.runs = 0

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        self.runs += 1
        await asyncio.sleep(0)
        return text_output("seen")


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def _no_sleep(_: float) -> None:
    return None


def _agent(**kw: Any) -> Agent:
    return Agent(name="a", model="m", extensions=[EmptyReply()], **kw)


async def run(store: SqliteStore, provider: ScriptedProvider, agent: Agent,
              goal: str = "go") -> tuple[Client, str]:
    client = Client(store)
    tid = await client.create(agent.name, goal)
    await _again(store, provider, agent)
    return client, tid


async def _again(store: SqliteStore, provider: ScriptedProvider, agent: Agent) -> None:
    await Runtime(store=store, agents={agent.name: agent}, provider_for=lambda _m: provider,
                  env_for=lambda _s: None,  # type: ignore[arg-type,return-value]
                  sleep=_no_sleep).run_until_idle()



async def events(client: Client, tid: str) -> list[Any]:
    return [r.event for r in await client.events(tid)]


def _nudges(request: Any) -> int:
    return sum(m.text == EMPTY_REPLY_NUDGE for m in request.messages)


async def test_empty_reply_is_never_recorded_and_is_asked_to_go_on(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply(""), reply("hello")])
    client, tid = await run(store, provider, _agent())
    evs = await events(client, tid)
    replies = [e.response.message.text for e in evs if isinstance(e, ModelReplied)]
    assert replies == ["hello"]
    nudge = provider.requests[1].messages[-1]
    assert nudge.role == "user" and nudge.text == EMPTY_REPLY_NUDGE
    assert [m.role for m in provider.requests[1].messages[-2:]] == ["user", "user"]
    assert isinstance(evs[-1], Waiting) and evs[-1].detail == {"answer": "hello"}


async def test_reasoning_only_reply_is_empty(store: SqliteStore) -> None:
    only = ModelResponse(message=Message(role="assistant", content=[Thinking(text="hmm")]),
                         stop_reason="end_turn")
    provider = ScriptedProvider([only, reply("hello")])
    await run(store, provider, _agent())
    assert provider.requests[1].messages[-1].text == EMPTY_REPLY_NUDGE
    assert not any(isinstance(b, Thinking) for m in provider.requests[1].messages
                   for b in m.content)


async def test_empty_again_after_the_nudge_ends_the_turn_quietly(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply(""), reply("")])
    client, tid = await run(store, provider, _agent())
    evs = await events(client, tid)
    assert len(provider.requests) == 2
    assert not any(isinstance(e, ModelReplied) for e in evs)
    last = evs[-1]
    assert isinstance(last, Waiting) and last.detail == {"answer": ""}
    # The next message opens a turn as usual; the nudge stays in the history.
    provider.add(reply("back"))
    await client.send(tid, "again")
    await _again(store, provider, _agent())
    assert _nudges(provider.requests[2]) == 1
    assert provider.requests[2].messages[-1].text == "again"


async def test_one_shot_task_ends_done_after_two_empty_replies(store: SqliteStore) -> None:
    probe = Probe()
    provider = ScriptedProvider([reply("Looking.", ("Probe", {})), reply(""), reply("")])
    client, tid = await run(store, provider,
                            _agent(tools=[probe], one_shot=True))
    end = (await events(client, tid))[-1]
    # The preamble before the call is no answer: the turn ended on nothing.
    assert isinstance(end, TaskEnded) and end.status == "done" and not end.error
    assert end.answer == ""


async def test_the_nudge_is_asked_again_after_each_step(store: SqliteStore) -> None:
    probe = Probe()
    provider = ScriptedProvider([reply("", ("Probe", {})), reply(""),
                                 reply("", ("Probe", {})), reply(""), reply("done")])
    client, tid = await run(store, provider,
                            _agent(tools=[probe], one_shot=True))
    assert probe.runs == 2
    assert [_nudges(r) for r in provider.requests] == [0, 0, 1, 1, 2]
    end = (await events(client, tid))[-1]
    assert isinstance(end, TaskEnded) and end.status == "done" and end.answer == "done"


async def test_cut_and_empty_replies_are_no_crash_attempts(store: SqliteStore) -> None:
    # Two reasoning-only cuts, then an empty reply: three model calls without
    # a recorded reply, none of them a crash; the nudge still goes out.
    cut = ModelResponse(message=Message(role="assistant", content=[Thinking(text="hmm")]),
                        stop_reason="max_tokens")
    provider = ScriptedProvider([cut, cut, reply(""), reply("answer")])
    client, tid = await run(store, provider, _agent())
    assert len(provider.requests) == 4
    assert [_nudges(r) for r in provider.requests] == [0, 0, 0, 1]
    last = (await events(client, tid))[-1]
    assert isinstance(last, Waiting) and last.detail == {"answer": "answer"}


async def test_a_new_turn_opened_by_a_notice_is_nudged_again(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply(""), reply("")])
    agent = _agent()
    client, tid = await run(store, provider, agent)
    provider.add(reply(""), reply("the job is done"))
    await client.send(tid, user("Background job finished.", origin="agent"))
    await _again(store, provider, agent)
    assert [_nudges(r) for r in provider.requests] == [0, 1, 1, 2]
    last = (await events(client, tid))[-1]
    assert isinstance(last, Waiting) and last.detail == {"answer": "the job is done"}


async def test_quiet_end_then_a_prompt_then_empty_again(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply(""), reply("")])
    agent = _agent()
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
                            _agent(tools=[probe], one_shot=True))
    total = await tree_usage(client, tid)
    assert sum(m.calls for m in total.by_model.values()) == 3
    assert total.usage.output == 15


class Ender:
    """A tool whose successful result ends the turn (Claude Code's terminal
    MCP tools): no reply need follow it."""

    name = "Ender"
    description = "Finish."
    input_schema = {"type": "object"}
    read_only = True
    clearable = False
    silent_reply_after = "success"

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return text_output("done", is_error=bool(input.get("fail")))


async def test_an_empty_reply_after_a_turn_ending_tool_ends_the_turn(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", ("Ender", {})), reply("")])
    client, tid = await run(store, provider, _agent(tools=[Ender()]))
    assert len(provider.requests) == 2
    last = (await events(client, tid))[-1]
    assert isinstance(last, Waiting) and last.detail == {"answer": ""}


async def test_a_failed_turn_ending_call_still_gets_the_nudge(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", ("Ender", {"fail": True})), reply(""), reply("ok")])
    await run(store, provider, _agent(tools=[Ender()]))
    assert _nudges(provider.requests[2]) == 1



def test_it_is_on_by_default_and_can_be_switched_off() -> None:
    assert ("empty_reply", {}) in DEFAULT_PLUGINS
    for f in builtin_agent_files():
        assert "empty_reply" in [name for name, _ in f.plugins or ()], f.name
    assert any(isinstance(e, EmptyReply) for e in main_agent("m").extensions)
    off = main_agent("m", plugins={"empty_reply": False})
    assert not any(isinstance(e, EmptyReply) for e in off.extensions)


async def test_switched_off_the_first_empty_reply_ends_the_turn(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("")])
    client, tid = await run(store, provider, Agent(name="a", model="m"))
    assert len(provider.requests) == 1
    evs = await events(client, tid)
    assert isinstance(evs[-1], Waiting) and evs[-1].detail == {"answer": ""}


def _fold(evs: list[Any]) -> TaskState:
    from noeta.core.events import Recorded

    state = TaskState(task_id="t")
    for i, ev in enumerate(evs):
        apply(state, Recorded(seq=i + 1, at=0.0, event=ev), {})
    return state


async def test_its_log_is_the_one_the_core_wrote_before(store: SqliteStore) -> None:
    # The core records the plugin's continuation as it recorded its own
    # nudge: an old log and a new one fold to the same conversation.
    from noeta.core.events import InboxItem, InputTaken, Say, TaskCreated
    from noeta.core.types import Usage

    provider = ScriptedProvider([reply(""), reply("")])
    client, tid = await run(store, provider, _agent())
    new = [e for e in await events(client, tid)
           if isinstance(e, ExtensionRecorded) and e.extension == "core"
           and e.kind in ("continue", "turn_done")]
    usage = Usage(uncached=10, output=5)
    old = [records.event(records.Continue(reason=records.EMPTY_REPLY, model="m", usage=usage),
                         message=user(EMPTY_REPLY_NUDGE, origin="inline")),
           records.event(records.TurnDone(reason=records.EMPTY_REPLY, model="m", usage=usage))]
    assert [(e.kind, e.data, e.message) for e in new] == \
        [(e.kind, e.data, e.message) for e in old]
    opening = [TaskCreated(agent="a", config={}),
               InputTaken(items=[InboxItem(seq=1, at=0.0, body=Say(message=user("go")))],
                          through=1)]
    a, b = _fold([*opening, *old]), _fold([*opening, *new])
    assert len(a.messages) == 2 and a.messages[-1].text == EMPTY_REPLY_NUDGE
    assert (a.messages, a.empty_nudged, a.answer_parts, a.usage, a.turn_open) == \
        (b.messages, b.empty_nudged, b.answer_parts, b.usage, b.turn_open)
