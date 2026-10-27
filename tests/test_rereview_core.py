"""Regressions from the re-review (core side): a message held behind a batch
reopens a turn that closes by stop, a rewrite to the same call is no
rewrite, a failing ``after_tool`` during a resume cannot wedge the task,
and the host helper lists only real questions."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.agent import Agent
from noeta.client import Client
from noeta.events import ToolFinished
from noeta.ext.ask_user import AskUser
from noeta.ext.base import Ask, Extension, Rewrite, TaskView
from noeta.ext.subtasks.host import pending_requests
from noeta.runner import Runtime
from noeta.store.sqlite import SqliteStore
from noeta.testing import ScriptedProvider, reply
from noeta.tools.base import Park, ToolContext, ToolOutput, text_output
from noeta.types import ToolUse, user


async def _no_sleep(_: float) -> None:
    return None


def runtime(store: Any, provider: Any, *agents: Agent) -> Runtime:
    return Runtime(store=store, agents={a.name: a for a in agents},
                   provider_for=lambda _m: provider, env_for=lambda _s: None, sleep=_no_sleep)


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


class Echo:
    name = "Echo"
    description = "e"
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    def __init__(self) -> None:
        self.inputs: list[dict[str, Any]] = []

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        self.inputs.append(input)
        return text_output("ok")


class AskEcho(Extension):
    name = "asker"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Any:
        return Ask(prompt="ok?") if call.name == "Echo" else None


# ------------------------------------------------------------ P3-5 held messages


async def test_a_held_message_reopens_a_stopped_turn(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", ("Echo", {})), reply("answered the held message")])
    rt = runtime(store, provider, Agent(name="a", model="m", tools=[Echo()],
                                        extensions=(AskEcho(),)))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    assert (await client.get(tid)).waiting_on == "approval"
    await client.send(tid, user("also this"), when="now")
    await rt.run_until_idle()
    await client.cancel(tid, "stop", turn_only=True)
    await rt.run_until_idle()
    assert len(provider.requests) == 2
    assert provider.requests[1].messages[-1].text == "also this"
    assert (await client.get(tid)).waiting_on == "input"


# ------------------------------------------------------------ P3-6 idempotent rewrite


class Normalize(Extension):
    """Always rewrites to the canonical input — the same call once it is."""

    name = "normalize"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Any:
        return Rewrite(input={"text": str(call.input.get("text", "")).strip()})


async def test_a_rewrite_to_the_same_call_is_no_rewrite(store: SqliteStore) -> None:
    echo = Echo()
    provider = ScriptedProvider([reply("", ("Echo", {"text": "  hi "})), reply("done")])
    rt = runtime(store, provider, Agent(name="a", model="m", tools=[echo],
                                        extensions=(Normalize(),)))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    assert echo.inputs == [{"text": "hi"}]
    [done] = [r.event for r in await client.events(tid) if isinstance(r.event, ToolFinished)]
    assert not done.result.is_error


# ------------------------------------------------------------ P3-8 after_tool in a resume


class BrokenAfter(Extension):
    name = "broken_after"

    async def after_tool(self, view: TaskView, call: ToolUse, output: ToolOutput) -> ToolOutput:
        raise RuntimeError("after_tool bug")


async def test_a_failing_after_tool_during_a_resume_keeps_the_output(
        store: SqliteStore) -> None:
    q = {"questions": [{"question": "Which db?", "header": "DB", "multiSelect": False,
                        "options": [{"label": "sqlite", "description": "a"},
                                    {"label": "pg", "description": "b"}]}]}
    provider = ScriptedProvider([reply("", ("AskUserQuestion", q)), reply("done")])
    rt = runtime(store, provider, Agent(name="a", model="m",
                                        extensions=(AskUser(), BrokenAfter())))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    row = await client.get(tid)
    assert row.waiting_on == "question"
    [asked] = (await client.events(tid))[-1].event.ids
    await client.answer(tid, asked, data={"0": {"selected": ["pg"]}})
    await rt.run_until_idle()
    row = await client.get(tid)
    assert row.status == "waiting" and row.waiting_on == "input"
    [done] = [r.event for r in await client.events(tid) if isinstance(r.event, ToolFinished)]
    assert not done.result.is_error and '"pg"' in done.result.text


# ------------------------------------------------------------ P3-7 host helper


class AskAndDelegate:
    """``mode=ask`` parks on a question; ``mode=spawn`` on a child."""

    name = "Both"
    description = "b"
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        if input.get("mode") == "spawn":
            assert ctx.spawn is not None
            return ToolOutput(park=Park(on="children", keys=[ctx.spawn("worker", "job")]))
        return ToolOutput(park=Park(on="question", detail={"questions": [
            {"id": "0", "question": "Which?", "options": [{"label": "x"}]}]}))

    async def resume(self, input: dict[str, Any], ctx: ToolContext, outcome: Any) -> ToolOutput:
        return text_output("ok")


async def test_host_pending_requests_lists_only_questions(store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("", ("Both", {"mode": "ask"}),
                                       ("Both", {"mode": "spawn"})),
                                 reply("", ("Echo", {}))])
    parent = Agent(name="main", model="m", tools=[AskAndDelegate()])
    worker = Agent(name="worker", model="m", one_shot=True, tools=[Echo()],
                   extensions=(AskEcho(),))
    rt = runtime(store, provider, parent, worker)
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    [child] = await client.tasks(parent_id=tid)
    assert (await client.get(tid)).waiting_on == "question"
    found = await pending_requests(client, tid)
    assert sorted((p.task_id, p.on) for p in found) == sorted(
        [(tid, "question"), (child.task_id, "approval")])
    assert all(p.request_id != child.task_id for p in found)
