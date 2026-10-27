"""Regressions from the front-end review: ``noeta -p`` waits for its
sub-agents, ACP and the TUI follow only their own turn and show questions,
a retry drops the failed attempt's output, front ends serve only their own
tasks, and the server's event stream always ends."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("acp")
pytest.importorskip("textual")
pytest.importorskip("starlette")

import httpx  # noqa: E402
from acp import schema  # noqa: E402

import noeta.cli as cli  # noqa: E402
from noeta.acp import NoetaAcpAgent  # noqa: E402
from noeta.agent import Agent  # noqa: E402
from noeta.errors import NoetaError, Transient  # noqa: E402
from noeta.events import (  # noqa: E402
    Answer,
    ChildEnded,
    InputTaken,
    Recorded,
    Say,
    TaskCreated,
    Waiting,
)
from noeta.local import LocalApp  # noqa: E402
from noeta.remote import RemoteClient  # noqa: E402
from noeta.server import create_app  # noqa: E402
from noeta.store.base import Post  # noqa: E402
from noeta.store.sqlite import SqliteStore  # noqa: E402
from noeta.testing import ScriptedProvider, reply  # noqa: E402
from noeta.tools.base import Park, ToolContext, ToolOutput, text_output  # noqa: E402
from noeta.tui.app import NoetaApp  # noqa: E402
from noeta.tui.widgets import AssistantMessage, QuestionCard  # noqa: E402
from noeta.types import Delta, ModelRequest, ModelResponse  # noqa: E402


async def _no_sleep(_: float) -> None:
    return None


class AskTool:
    name = "Ask"
    description = "Ask the user."
    input_schema = {"type": "object"}  # noqa: RUF012
    read_only = False
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return ToolOutput(park=Park(on="question", detail={"questions": [
            {"question": "Which database?", "options": [{"label": "sqlite"}]}]}))

    async def resume(self, input: dict[str, Any], ctx: ToolContext, outcome: Any) -> ToolOutput:
        if outcome is None:
            return text_output("withdrawn", is_error=True)
        return text_output(f"user said: {outcome.data}")


class Delegate:
    name = "Task"
    description = "d"
    input_schema = {"type": "object"}  # noqa: RUF012
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


async def test_acp_shows_the_question_and_answers_it(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", ("Ask", {})), reply("using sqlite")])
    app = local(tmp_path, provider, Agent(name="main", model="m", tools=[AskTool()]))
    agent, editor, sid = await acp_session(app, tmp_path)
    resp = await asyncio.wait_for(agent.prompt(sid, text_block("set it up")), 10)
    assert resp.stop_reason == "end_turn"
    assert "Which database?" in "".join(editor.texts())
    await asyncio.wait_for(agent.prompt(sid, text_block("sqlite")), 10)
    assert editor.texts()[-1] == "using sqlite"
    answers = [i.body for i in await app.store.inbox(sid) if isinstance(i.body, Answer)]
    assert [a.data for a in answers] == ["sqlite"]
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

