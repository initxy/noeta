from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import ModelReplied, TaskEnded, ToolFinished, ToolStarted, Waiting
from noeta.core.extension import Ask, Deny, Extension, Gate, TaskView
from noeta.core.runner import (
    INTERRUPTED_CALL_ERROR,
    OUTPUT_LIMIT_RESUME_NUDGE,
    Runtime,
)
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import ToolUse
from noeta.testing import ScriptedProvider, reply


class Echo:
    name = "Echo"
    description = "Echo the text."
    input_schema = {"type": "object", "properties": {"text": {"type": "string"}}}
    read_only = True
    clearable = True

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.approved: list[bool] = []

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        self.calls.append(input["text"])
        self.approved.append(ctx.approved)
        ctx.record("echo", "said", input["text"])
        return text_output(f"echo: {input['text']}")


class Counter(Extension):
    name = "echo"

    def initial_state(self) -> Any:
        return 0

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        return state + 1


class Gatekeeper(Extension):
    name = "gate"

    def __init__(self, verdict: Gate | None) -> None:
        self.verdict = verdict

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        return self.verdict


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


def runtime(store: SqliteStore, provider: ScriptedProvider, *agents: Agent) -> Runtime:
    async def no_sleep(_: float) -> None:
        return None

    return Runtime(store=store, agents={a.name: a for a in agents},
                   provider_for=lambda _m: provider, env_for=lambda _s: None,  # type: ignore[arg-type,return-value]
                   sleep=no_sleep)


async def test_turn_with_a_tool_call_then_waits_for_input(store: SqliteStore) -> None:
    echo = Echo()
    provider = ScriptedProvider([reply("", ("Echo", {"text": "hi"})), reply("all done")])
    agent = Agent(name="a", model="m", system_prompt="be brief", tools=[echo],
                  extensions=[Counter()])
    rt = runtime(store, provider, agent)
    client = Client(store)
    tid = await client.create("a", "say hi")
    await rt.run_until_idle()

    row = await client.get(tid)
    assert row.status == "waiting" and row.waiting_on == "input"
    assert echo.calls == ["hi"] and echo.approved == [False]
    types = [type(r.event).__name__ for r in await client.events(tid)]
    # The second entry records the prompt override files (none here).
    assert types == ["TaskCreated", "ExtensionRecorded", "ExtensionRecorded", "InputTaken", "ModelCalled", "ModelReplied", "ToolStarted",
                     "ExtensionRecorded", "ToolFinished", "ModelCalled", "ModelReplied",
                     "Waiting"]
    second = provider.requests[1]
    assert second.system[0].text == "be brief" and second.system[-1].cache
    assert [m.role for m in second.messages] == ["user", "assistant", "user"]
    assert second.messages[2].content[0].text == "echo: hi"  # type: ignore[union-attr]


async def test_next_message_wakes_the_task(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("one"), reply("two")])
    rt = runtime(store, provider, Agent(name="a", model="m"))
    client = Client(store)
    tid = await client.create("a", "first")
    await rt.run_until_idle()
    await client.send(tid, "second")
    await rt.run_until_idle()
    assert [m.text for m in provider.requests[1].messages] == ["first", "one", "second"]
    assert (await client.get(tid)).status == "waiting"


async def test_one_shot_agent_ends_with_its_answer(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("42")])
    rt = runtime(store, provider, Agent(name="a", model="m", one_shot=True))
    client = Client(store)
    tid = await client.create("a", "answer?")
    await rt.run_until_idle()
    events = await client.events(tid)
    assert isinstance(events[-1].event, TaskEnded) and events[-1].event.answer == "42"


async def test_deny_and_ask_gates(store: SqliteStore) -> None:
    echo = Echo()
    provider = ScriptedProvider([reply("", ("Echo", {"text": "x"})), reply("ok")])
    rt = runtime(store, provider, Agent(name="a", model="m", tools=[echo],
                                        extensions=[Gatekeeper(Deny(reason="no"))]))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    assert echo.calls == []
    finished = [r.event for r in await client.events(tid) if isinstance(r.event, ToolFinished)]
    assert finished[0].result.is_error and finished[0].result.text == "no"

    echo2 = Echo()
    provider2 = ScriptedProvider([reply("", ("Echo", {"text": "y"})), reply("ok")])
    rt2 = runtime(store, provider2, Agent(name="b", model="m", tools=[echo2],
                                          extensions=[Gatekeeper(Ask(prompt="run?"))]))
    tid2 = await client.create("b", "go")
    await rt2.run_until_idle()
    row = await client.get(tid2)
    assert row.status == "waiting" and row.waiting_on == "approval" and echo2.calls == []
    waiting = (await client.events(tid2))[-1].event
    assert isinstance(waiting, Waiting)
    await client.answer(tid2, waiting.ids[0], "allow")
    await rt2.run_until_idle()
    assert echo2.calls == ["y"] and (await client.get(tid2)).waiting_on == "input"
    assert echo2.approved == [True]  # a person allowed it at its prompt


async def test_crash_after_tool_started_is_never_rerun(store: SqliteStore) -> None:
    """A runner dies between ToolStarted and ToolFinished: the next runner
    answers the call with "outcome unknown" and does not run it again."""
    echo = Echo()
    provider = ScriptedProvider([reply("", ("Echo", {"text": "once"})), reply("recovered")])
    agent = Agent(name="a", model="m", tools=[echo])
    client = Client(store)
    tid = await client.create("a", "go")

    claim = await store.claim("dead", ["default"], -1)  # simulate the dead runner's writes
    assert claim is not None
    from noeta.core.events import InputTaken, ModelCalled

    items = await store.inbox(tid)
    resp = provider.steps.pop(0)
    call = resp.message.tool_uses[0]  # type: ignore[union-attr]
    await store.append(claim, [InputTaken(items=items, through=items[-1].seq),
                               ModelCalled(model="m", request_hash="h"),
                               ModelReplied(response=resp),  # type: ignore[arg-type]
                               ToolStarted(call=call)], expected_seq=1)

    rt = runtime(store, provider, agent)
    await rt.run_until_idle()
    assert echo.calls == []
    finished = [r.event for r in await client.events(tid) if isinstance(r.event, ToolFinished)]
    assert finished[0].result.text == INTERRUPTED_CALL_ERROR
    assert (await client.get(tid)).waiting_on == "input"


async def test_output_cap_continues_and_joins_answer(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("Hello, ", stop="max_tokens"), reply("world.")])
    rt = runtime(store, provider, Agent(name="a", model="m", one_shot=True))
    client = Client(store)
    tid = await client.create("a", "greet")
    await rt.run_until_idle()
    assert provider.requests[1].messages[-1].text == OUTPUT_LIMIT_RESUME_NUDGE
    end = (await client.events(tid))[-1].event
    assert isinstance(end, TaskEnded) and end.answer == "Hello, world."


async def test_cancel_ends_and_next_turn_message_is_queued(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("a")])
    rt = runtime(store, provider, Agent(name="a", model="m"))
    client = Client(store)
    tid = await client.create("a", "x")
    await client.cancel(tid, "bye")
    await rt.run_until_idle()
    end = (await client.events(tid))[-1].event
    assert isinstance(end, TaskEnded) and end.status == "cancelled" and provider.requests == []


async def test_unknown_agent_fails_instead_of_spinning(store: SqliteStore) -> None:
    rt = runtime(store, ScriptedProvider(), Agent(name="a", model="m"))
    client = Client(store)
    tid = await client.create("nobody", "hi")
    await rt.run_until_idle()
    row = await client.get(tid)
    assert row.status == "ended" and row.end_status == "failed"


async def test_cancel_during_a_write_keeps_the_runner_in_step(store: SqliteStore) -> None:
    """Cancelling the step while its append is in flight: the write commits
    and the runner's view follows it, so the next boundary does not conflict."""
    import asyncio

    provider = ScriptedProvider([reply("one"), reply("two")])
    rt = runtime(store, provider, Agent(name="a", model="m"))
    client = Client(store)
    tid = await client.create("a", "x")
    claim = await store.claim("w", ["default"], 30)
    assert claim is not None
    from noeta.core.runner import TaskRun

    run = TaskRun(rt, claim)
    await run.load()
    from noeta.core.events import ExtensionRecorded

    task = asyncio.create_task(run.append([ExtensionRecorded(extension="x", kind="k")]))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert run.state.last_seq == (await client.get(tid)).last_seq
    await run.append([ExtensionRecorded(extension="x", kind="k2")])
