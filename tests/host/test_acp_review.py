"""Regressions from the front-end review: ``noeta -p`` waits for its
sub-agents, ACP and the TUI follow only their own turn and show questions,
a retry drops the failed attempt's output, front ends serve only their own
tasks, and the server's event stream always ends."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("acp")
pytest.importorskip("textual")
pytest.importorskip("starlette")

import acp
from acp import schema

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.errors import Transient
from noeta.core.events import (
    Answer,
    ChildEnded,
    InputTaken,
    ToolFinished,
    Waiting,
)
from noeta.core.extension import Ask, Extension, TaskView
from noeta.core.store import Post
from noeta.core.tool import Park, ToolContext, ToolOutput, text_output
from noeta.core.types import Delta, ModelRequest, ModelResponse, ToolUse
from noeta.host.acp import NoetaAcpAgent
from noeta.host.local import LocalApp
from noeta.plugins.ask_user import AskUser
from noeta.testing import ScriptedProvider, reply


async def _no_sleep(_: float) -> None:
    return None


class Delegate:
    name = "Task"
    description = "d"
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        assert ctx.spawn is not None
        return ToolOutput(park=Park(on="children", keys=[ctx.spawn("worker", "sub job")]))

    async def resume(self, input: dict[str, Any], ctx: ToolContext, outcome: Any) -> ToolOutput:
        [ended] = outcome.values()
        return text_output(ended.answer)


class FakeEditor:
    def __init__(self) -> None:
        self.updates: list[Any] = []

    async def session_update(self, session_id: str, update: Any, **kw: Any) -> None:
        self.updates.append(update)

    async def request_permission(self, **kw: Any) -> Any:
        raise AssertionError("no approvals here")

    def texts(self) -> list[str]:
        return [u.content.text for u in self.updates
                if u.session_update == "agent_message_chunk"]


def local(tmp_path: Path, provider: Any, *agents: Agent, **kw: Any) -> LocalApp:
    return LocalApp(SqliteStore(str(tmp_path / "t.db")), list(agents), provider,
                    env_for=lambda _s: None, sleep=_no_sleep, **kw)


async def acp_session(app: LocalApp, tmp_path: Path) -> tuple[NoetaAcpAgent, FakeEditor, str]:
    agent = NoetaAcpAgent(app)
    editor = FakeEditor()
    agent.on_connect(editor)
    await agent.initialize(protocol_version=1)
    session = await agent.new_session(cwd=str(tmp_path))
    return agent, editor, session.session_id


def text_block(text: str) -> list[Any]:
    return [schema.TextContentBlock(type="text", text=text)]


# ------------------------------------------------------------ #3 ACP turn gating


class SlowFirst:
    family = "x"

    def __init__(self) -> None:
        self.n = 0

    async def complete(self, request: ModelRequest, on_delta: Any = None) -> ModelResponse:
        self.n += 1
        if self.n == 1:
            await asyncio.sleep(30)  # stopped from the editor
        r = reply(f"answer {self.n}")
        if on_delta is not None:
            on_delta(Delta(kind="text", text=r.message.text))
        return r


async def test_acp_cancel_then_prompt_at_once(tmp_path: Path) -> None:
    app = local(tmp_path, SlowFirst(), Agent(name="main", model="m"))
    agent, editor, sid = await acp_session(app, tmp_path)
    first = asyncio.create_task(agent.prompt(sid, text_block("first")))
    await asyncio.sleep(0.3)
    await agent.cancel(sid)
    second = asyncio.create_task(agent.prompt(sid, text_block("second")))
    r1, r2 = await asyncio.wait_for(asyncio.gather(first, second), 15)
    assert (r1.stop_reason, r2.stop_reason) == ("cancelled", "end_turn")
    assert editor.texts() == ["answer 2"]
    await app.stop()


async def test_acp_prompt_after_an_unrelated_inbox_item(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("the real answer")])
    app = local(tmp_path, provider, Agent(name="main", model="m"))
    agent, editor, sid = await acp_session(app, tmp_path)
    await app.store.post(Post(task_id=sid, body=ChildEnded(child_id="bg", status="done")))
    real_send = app.client.send

    async def send(*a: Any, **kw: Any) -> int:
        # Let the runner take the unrelated item and go back to waiting first.
        for _ in range(200):
            evs = await app.client.events(sid)
            if any(isinstance(r.event, InputTaken) for r in evs[2:]) and \
                    isinstance(evs[-1].event, Waiting):
                break
            await asyncio.sleep(0.01)
        return await real_send(*a, **kw)

    app.client.send = send  # type: ignore[method-assign]
    resp = await asyncio.wait_for(agent.prompt(sid, text_block("hi")), 10)
    assert resp.stop_reason == "end_turn"
    assert editor.texts() == ["the real answer"]
    await app.stop()


DB_QUESTION = {"questions": [{
    "question": "Which database?", "header": "DB", "multiSelect": False,
    "options": [{"label": "SQLite", "description": "a file"},
                {"label": "Postgres", "description": "a server"}]}]}


async def test_acp_shows_the_question_and_answers_it(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", ("AskUserQuestion", DB_QUESTION)),
                                 reply("using sqlite")])
    app = local(tmp_path, provider, Agent(name="main", model="m", extensions=(AskUser(),)))
    agent, editor, sid = await acp_session(app, tmp_path)
    resp = await asyncio.wait_for(agent.prompt(sid, text_block("set it up")), 10)
    assert resp.stop_reason == "end_turn"
    assert "Which database?" in "".join(editor.texts())
    await asyncio.wait_for(agent.prompt(sid, text_block("sqlite")), 10)
    assert editor.texts()[-1] == "using sqlite"
    answers = [i.body for i in await app.store.inbox(sid) if isinstance(i.body, Answer)]
    assert [a.data for a in answers] == [{"answers": {"0": {"selected": ["SQLite"]}}}]
    [done] = [r.event for r in await app.client.events(sid) if isinstance(r.event, ToolFinished)]
    assert not done.result.is_error
    await app.stop()


def two_questions() -> dict[str, Any]:
    second = {**DB_QUESTION["questions"][0], "question": "Which cache?", "header": "Cache"}
    return {"questions": [DB_QUESTION["questions"][0], second]}


async def test_acp_asks_several_questions_one_prompt_each(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", ("AskUserQuestion", two_questions())),
                                 reply("set up")])
    app = local(tmp_path, provider, Agent(name="main", model="m", extensions=(AskUser(),)))
    agent, editor, sid = await acp_session(app, tmp_path)
    await asyncio.wait_for(agent.prompt(sid, text_block("set it up")), 10)
    await asyncio.wait_for(agent.prompt(sid, text_block("postgres")), 10)
    assert "Which cache?" in editor.texts()[-1]
    await asyncio.wait_for(agent.prompt(sid, text_block("redis please")), 10)
    assert editor.texts()[-1] == "set up"
    [answer] = [i.body for i in await app.store.inbox(sid) if isinstance(i.body, Answer)]
    assert answer.data == {"answers": {"0": {"selected": ["Postgres"]},
                                       "1": {"other": "redis please"}}}
    await app.stop()


class Echo:
    name = "Echo"
    description = "e"
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return text_output("echoed")


class AskEcho(Extension):
    name = "asker"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Any:
        return Ask(prompt="may the worker echo?") if call.name == "Echo" else None


class AllowingEditor(FakeEditor):
    def __init__(self) -> None:
        super().__init__()
        self.asked: list[str] = []

    async def request_permission(self, **kw: Any) -> Any:
        self.asked.append(kw["tool_call"].title)
        return SimpleNamespace(outcome=SimpleNamespace(option_id="allow"))


def with_worker(*worker_ext: Extension) -> list[Agent]:
    return [Agent(name="main", model="m", tools=[Delegate()]),
            Agent(name="worker", model="m", one_shot=True, tools=[Echo()],
                  extensions=worker_ext)]


async def test_acp_puts_a_sub_agents_approval_to_the_editor(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", ("Task", {})), reply("", ("Echo", {})),
                                 reply("child done"), reply("final")])
    app = local(tmp_path, provider, *with_worker(AskEcho()))
    agent = NoetaAcpAgent(app)
    editor = AllowingEditor()
    agent.on_connect(editor)
    await agent.initialize(protocol_version=1)
    sid = (await agent.new_session(cwd=str(tmp_path))).session_id
    resp = await asyncio.wait_for(agent.prompt(sid, text_block("go")), 15)
    assert resp.stop_reason == "end_turn"
    assert editor.asked == ["Echo · from the worker agent"]
    assert editor.texts()[-1] == "final"
    await app.stop()


async def test_acp_shows_and_answers_a_sub_agents_question(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", ("Task", {})),
                                 reply("", ("AskUserQuestion", DB_QUESTION)),
                                 reply("child done"), reply("final")])
    app = local(tmp_path, provider, *with_worker(AskUser()))
    agent, editor, sid = await acp_session(app, tmp_path)
    await asyncio.wait_for(agent.prompt(sid, text_block("go")), 15)
    assert "[worker]" in editor.texts()[-1] and "Which database?" in editor.texts()[-1]
    resp = await asyncio.wait_for(agent.prompt(sid, text_block("Postgres")), 15)
    assert resp.stop_reason == "end_turn" and editor.texts()[-1] == "final"
    [child] = await app.client.tasks(parent_id=sid)
    [answer] = [i.body for i in await app.store.inbox(child.task_id)
                if isinstance(i.body, Answer)]
    assert answer.data == {"answers": {"0": {"selected": ["Postgres"]}}}
    await app.stop()


async def test_acp_load_session_reports_a_missing_agent(tmp_path: Path) -> None:
    app = local(tmp_path, ScriptedProvider([]), Agent(name="main", model="m"),
                own_tasks_only=True)
    from noeta.core.client import Client

    theirs = await Client(app.store).create("host-agent", "made by a host")  # not served here
    agent, _editor, _sid = await acp_session(app, tmp_path)
    with pytest.raises(acp.RequestError, match="host-agent"):
        await agent.load_session(cwd=str(tmp_path), session_id=theirs)
    assert (await app.client.get(theirs)).status == "ready"
    await app.stop()


class RetryOnce:
    """The first attempt streams a fragment and fails; the retry succeeds."""

    family = "x"

    def __init__(self) -> None:
        self.n = 0

    async def complete(self, request: ModelRequest, on_delta: Any = None) -> ModelResponse:
        self.n += 1
        if self.n == 1:
            if on_delta is not None:
                on_delta(Delta(kind="text", text="broken par"))
            raise Transient("stream cut")
        if on_delta is not None:
            on_delta(Delta(kind="text", text="good answer"))
        return reply("good answer")


async def test_acp_sets_a_retry_apart_from_the_failed_attempt(tmp_path: Path) -> None:
    app = local(tmp_path, RetryOnce(), Agent(name="main", model="m"))
    agent, editor, sid = await acp_session(app, tmp_path)
    await asyncio.wait_for(agent.prompt(sid, text_block("go")), 10)
    shown = "".join(editor.texts())
    assert shown.endswith("\n\ngood answer") and shown.count("good answer") == 1
    await app.stop()

