"""Kernel pieces sub-agents rest on: a tool that ends the turn, a one-shot
task that waits for its background children, a task that continues another
task's conversation, and an agent's denied tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import TaskEnded, Waiting
from noeta.core.loop.state import CONTINUED_CALL, STARTED_CALL, Call, TaskState, continued
from noeta.core.runner import Runtime
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import Message, Text, ToolResult, ToolUse, user
from noeta.plugins.compaction.transcript import full_conversation
from noeta.testing import ScriptedProvider, reply


@pytest.fixture
async def store(tmp_path: Path) -> Any:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def _no_sleep(_: float) -> None:
    return None


def runtime(store: SqliteStore, providers: dict[str, ScriptedProvider], *agents: Agent) -> Runtime:
    return Runtime(store=store, agents={a.name: a for a in agents},
                   provider_for=lambda m: providers[m], env_for=lambda _s: None,  # type: ignore[arg-type,return-value]
                   sleep=_no_sleep)


class Finish:
    name = "Finish"
    description = "End with a report."
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return ToolOutput(content=[Text(text="delivered")], end_turn=input["report"])


class Spawn:
    """Starts a child in the background; with ``from`` it continues that task."""

    name = "Spawn"
    description = "Start a child."
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        assert ctx.spawn is not None
        child = ctx.spawn("child", input["prompt"], background=True,
                          history_from=input.get("from"), history_through=input.get("through"))
        return text_output(f"started {child}")


class Probe:
    name = "Probe"
    description = "Look."
    input_schema = {"type": "object"}
    read_only = True
    clearable = True

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return text_output("seen")


async def events(client: Client, tid: str) -> list[Any]:
    return [r.event for r in await client.events(tid)]


async def test_a_tool_that_ends_the_turn_makes_no_further_model_call(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("trailing words", ("Finish", {"report": "the report"}))])
    rt = runtime(store, {"m": p}, Agent(name="a", model="m", one_shot=True, tools=[Finish()]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    assert len(p.requests) == 1
    end = (await events(client, tid))[-1]
    assert isinstance(end, TaskEnded) and end.answer == "the report"


async def test_an_interactive_task_whose_tool_ends_the_turn_waits_for_input(
        store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", ("Finish", {"report": "r"}), ("Probe", {})), reply("next")])
    rt = runtime(store, {"m": p}, Agent(name="a", model="m", tools=[Finish(), Probe()]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    assert len(p.requests) == 1
    assert (await client.get(tid)).waiting_on == "input"
    await client.send(tid, "more")
    await rt.run_until_idle()
    assert len(p.requests) == 2


async def test_a_one_shot_task_waits_for_its_background_children(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("", ("Spawn", {"prompt": "dig"})), reply("launched")])
    c = ScriptedProvider([reply("dug")])
    rt = runtime(store, {"m": p, "c": c},
                 Agent(name="a", model="m", one_shot=True, tools=[Spawn()]),
                 Agent(name="child", model="c", one_shot=True))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    evs = await events(client, tid)
    waits = [e for e in evs if isinstance(e, Waiting)]
    assert waits and waits[0].on == "children"
    end = evs[-1]
    assert isinstance(end, TaskEnded) and end.status == "done" and end.answer == "launched"
    [child] = await client.tasks(parent_id=tid)
    assert child.end_status == "done"  # not cancelled by its parent's end


async def test_a_task_continues_another_tasks_conversation(store: SqliteStore) -> None:
    first = ScriptedProvider([reply("", ("Probe", {})), reply("first answer")])
    rt = runtime(store, {"m": first, "x": ScriptedProvider()},
                 Agent(name="child", model="m", one_shot=True, tools=[Probe()]),
                 Agent(name="a", model="x", tools=[Spawn()]))
    client = Client(store)
    old = await client.create("child", "task one")
    await rt.run_until_idle()
    parent_p = rt.provider_for("x")
    parent_p.add(reply("", ("Spawn", {"prompt": "and now this", "from": old})), reply("ok"))
    first.add(reply("second answer"))
    tid = await client.create("a", "continue it")
    await rt.run_until_idle()
    request = first.requests[-1]
    texts = [m.text for m in request.messages if m.role == "user" and m.text]
    assert texts == ["task one", "and now this"]
    assert [m.role for m in request.messages] == ["user", "assistant", "user", "assistant", "user"]
    [child] = await client.tasks(parent_id=tid)
    child_events = await events(client, child.task_id)
    end = child_events[-1]
    assert isinstance(end, TaskEnded) and end.answer == "second answer"
    # A spawn without a cutoff is pinned to the source's log as it stood.
    assert child_events[0].history_through == (await client.get(old)).last_seq


async def test_a_fork_answers_the_calls_its_source_still_had_open(store: SqliteStore) -> None:
    p = ScriptedProvider()
    c = ScriptedProvider([reply("forked work")])
    rt = runtime(store, {"m": p, "c": c},
                 Agent(name="a", model="m", tools=[Spawn(), Probe()]),
                 Agent(name="child", model="c", one_shot=True))
    client = Client(store)
    tid = await client.create("a", "go")

    def fork(request: Any) -> Any:
        return reply("", ("Probe", {}), ("Spawn", {"prompt": "you are the fork", "from": tid,
                                                    "through": 6}))

    p.add(fork, reply("done"))
    await rt.run_until_idle()
    msgs = c.requests[0].messages
    assert msgs[1].role == "assistant" and len(msgs[1].tool_uses) == 2
    results = [b for b in msgs[2].content if isinstance(b, ToolResult)]
    # The call that started the fork says so; the other is the source's.
    assert [r.text for r in results] == [CONTINUED_CALL, STARTED_CALL]
    assert [r.is_error for r in results] == [True, False]
    assert msgs[-1].text == "you are the fork"
    # The fork's saved transcript, read back from the logs, is what it saw.
    [child] = await client.tasks(parent_id=tid)
    whole = await full_conversation(store.read, child.task_id)
    assert whole[:len(msgs)] == msgs


def test_a_continuation_answers_open_calls_before_messages_held_behind_them() -> None:
    use = ToolUse(id="c1", name="Probe", input={})
    source = TaskState(calls=[Call(use=use)], held=[user("interjected")],
                       ext={"keep": {"x": 1}, "drop": {"timers": 1}})
    source._add(user("go"), 1)
    source._add(Message(role="assistant", content=[use]), 2)
    out = continued(source, {"drop": lambda _s: None})
    roles = [m.role for m in out.messages]
    assert roles == ["user", "assistant", "user", "user"]
    [result] = out.messages[2].content
    assert isinstance(result, ToolResult) and result.call_id == "c1"
    assert out.messages[3].text == "interjected"
    assert out.ext == {"keep": {"x": 1}, "drop": None}


async def test_denied_tools_are_never_offered(store: SqliteStore) -> None:
    p = ScriptedProvider([reply("hi")])
    rt = runtime(store, {"m": p}, Agent(name="a", model="m", tools=[Probe(), Finish()],
                                        denied_tools=("Finish",)))
    client = Client(store)
    await client.create("a", "go")
    await rt.run_until_idle()
    assert [t.name for t in p.requests[0].tools] == ["Probe"]


async def test_a_fork_keeps_the_history_it_was_created_with(store: SqliteStore) -> None:
    src_p = ScriptedProvider([reply("r1"), reply("r2"), reply("r3")])
    fork_p = ScriptedProvider([reply("f1"), reply("f2")])
    rt = runtime(store, {"m": src_p, "c": fork_p},
                 Agent(name="a", model="m"), Agent(name="b", model="c"))
    client = Client(store)
    src = await client.create("a", "one")
    await rt.run_until_idle()
    pinned = (await client.get(src)).last_seq
    fork = await client.create("b", history_from=src)
    first = (await events(client, fork))[0]
    assert first.history_through == pinned
    await client.send(src, "two")
    await rt.run_until_idle()
    await client.send(fork, "fork says")
    await rt.run_until_idle()
    texts = [m.text for m in fork_p.requests[0].messages if m.text]
    assert texts == ["one", "r1", "fork says"]
    # The source grows again; the fork's next request still extends its own.
    await client.send(src, "three")
    await rt.run_until_idle()
    await client.send(fork, "again")
    await rt.run_until_idle()
    before, after = fork_p.requests
    assert after.messages[:len(before.messages)] == before.messages


def test_the_persons_prompt_is_named_after_a_compaction_and_not_carried() -> None:
    """``prompt_seq``: the person's newest message (not a tool result, not
    a notice), kept when a compaction summarizes it (a checkpoint keys its
    saves by it); a continued task starts at 0, its carried messages at 0."""
    from noeta.core.events import (
        Compacted,
        InboxItem,
        InputTaken,
        ModelReplied,
        Recorded,
        Say,
        ToolFinished,
    )
    from noeta.core.loop.state import fold
    from noeta.core.types import ModelResponse

    def rec(seq: int, event: Any) -> Recorded:
        return Recorded(seq=seq, at=0.0, event=event)

    def said(seq: int, message: Message) -> Recorded:
        return rec(seq, InputTaken(items=[InboxItem(seq=seq, at=0.0, body=Say(message=message))],
                                   through=seq))

    use = ToolUse(id="c1", name="Probe", input={})
    state = fold([
        said(1, user("first")),
        rec(2, ModelReplied(response=ModelResponse(
            message=Message(role="assistant", content=[use]), stop_reason="tool_use"))),
        rec(3, ToolFinished(result=ToolResult(call_id="c1", content=[Text(text="ok")]))),
        said(4, user("a notice", origin="system")),
    ], {})
    assert state.prompt_seq == 1
    state = fold([rec(5, Compacted(summary="S", through=4))], {}, state=state)
    assert state.prompt_seq == 1 and all(s == 0 for s in state.message_seqs)
    assert continued(state).prompt_seq == 0
