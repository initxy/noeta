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


# ------------------------------------------------------------ #2 noeta -p


async def test_print_mode_waits_for_a_foreground_sub_agent(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = ScriptedProvider([reply("", ("Task", {})), reply("child done"), reply("final")])
    agents = [Agent(name="main", model="m", tools=[Delegate()]),
              Agent(name="worker", model="m", one_shot=True)]
    monkeypatch.setattr(cli, "_app", lambda s, **kw: local(tmp_path, provider, *agents, **kw))
    code = await cli._print_mode(cli.Settings(model="m"), "go", cwd=str(tmp_path),
                                 as_json=False, permission_mode=None)
    assert code == 0
    assert capsys.readouterr().out.strip() == "final"


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


# ------------------------------------------------------------ #4 #19 the TUI


def tui(tmp_path: Path, provider: Any, *tools: Any) -> NoetaApp:
    agent = Agent(name="main", model="claude-sonnet-4-5", tools=list(tools))
    return NoetaApp(local(tmp_path, provider, agent), agent="main", model="claude-sonnet-4-5",
                    cwd=str(tmp_path))


async def settle(pilot: Any, cond: Any, tries: int = 100) -> None:
    for _ in range(tries):
        await pilot.pause(0.05)
        if cond():
            return
    raise AssertionError("UI did not settle")


async def test_tui_question_card_and_stop_clear_the_pending_question(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", ("Ask", {})), reply("fresh start")])
    app = tui(tmp_path, provider, AskTool())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press(*"set it up", "enter")
        await settle(pilot, lambda: bool(app.waiting_question))
        [card] = list(app.query(QuestionCard))
        assert card.detail["questions"][0]["question"] == "Which database?"
        await pilot.press("escape")
        await settle(pilot, lambda: not app.waiting_question and not app.busy)
        await pilot.press(*"something else", "enter")
        await settle(pilot, lambda: provider.steps == [] and not app.busy)
        inbox = await app.local.store.inbox(app.task_id)
        assert not any(isinstance(i.body, Answer) for i in inbox)
        assert isinstance(inbox[-1].body, Say)
        # Re-opening replays the log; the withdrawn question stays withdrawn.
        await app.open_task(app.task_id)
        await settle(pilot, lambda: len(app.query(QuestionCard)) == 1)
        await pilot.pause(0.3)
        assert app.waiting_question == ""


async def test_tui_drops_a_failed_attempts_output(tmp_path: Path) -> None:
    app = tui(tmp_path, RetryOnce())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press(*"go", "enter")
        await settle(pilot, lambda: not app.busy and len(app.query(AssistantMessage)) >= 1)
        await pilot.pause(0.2)
        assert [w.source for w in app.query(AssistantMessage)] == ["good answer"]


# ------------------------------------------------------------ #11 own tasks only


async def test_a_front_end_serves_only_its_own_tasks(tmp_path: Path) -> None:
    path = str(tmp_path / "shared.db")
    theirs = ScriptedProvider([reply("B's answer")])
    mine = ScriptedProvider([reply("A's answer")])
    other = LocalApp(SqliteStore(path), [Agent(name="main", model="m")], theirs,
                     env_for=lambda _s: None, own_tasks_only=True)
    me = LocalApp(SqliteStore(path), [Agent(name="main", model="m")], mine,
                  env_for=lambda _s: None, own_tasks_only=True)
    await me.start()
    # A task some other process created and left for its own worker.
    left = await other.client.create("main", "not yours")
    tid = await me.client.create("main", "mine")
    row = await me.client.wait(tid, timeout=5)
    assert row.status == "waiting"
    await asyncio.sleep(0.3)
    assert (await me.client.get(left)).status == "ready"
    assert len(mine.requests) == 1 and theirs.requests == []
    # Resuming adopts it.
    await me.adopt(left)
    mine.add(reply("now mine"))
    assert (await me.client.wait(left, timeout=5)).status == "waiting"
    await me.stop()
    await other.store.close()


# ------------------------------------------------------------ #19 server / remote


class BrokenWatchClient:
    async def get(self, task_id: str) -> Any:
        return None

    async def watch(self, task_id: str, after: int = 0) -> Any:
        yield Recorded(seq=1, at=0, event=TaskCreated(agent="a"))
        raise RuntimeError("store went away")


async def test_server_ends_the_stream_when_the_follower_fails() -> None:
    app = create_app(BrokenWatchClient())  # type: ignore[arg-type]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://x") as http:
        resp = await asyncio.wait_for(http.get("/tasks/t/watch"), 5)
    assert b"event: end" in resp.content and b"store went away" in resp.content


def _sse(*recs: Recorded, end: bool) -> bytes:
    import msgspec

    out = b"".join(b"event: event\ndata: " + msgspec.json.encode(r) + b"\n\n" for r in recs)
    return out + (b"event: end\ndata: {}\n\n" if end else b"")


async def test_remote_watch_reconnects_after_a_dropped_stream() -> None:
    afters: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        after = request.url.params.get("after", "0")
        afters.append(after)
        if after == "0":  # dropped after one event, without "end"
            return httpx.Response(200, content=_sse(
                Recorded(seq=1, at=0, event=TaskCreated(agent="a")), end=False))
        return httpx.Response(200, content=_sse(
            Recorded(seq=2, at=0, event=Waiting(on="input")), end=True))

    client = RemoteClient("http://x", transport=httpx.MockTransport(handler))
    seqs = [rec.seq async for rec in client.watch("t")]
    assert seqs == [1, 2] and afters == ["0", "1"]
    await client.close()


async def test_remote_watch_raises_an_ended_streams_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'event: end\ndata: {"error": "boom"}\n\n')

    client = RemoteClient("http://x", transport=httpx.MockTransport(handler))
    with pytest.raises(NoetaError, match="boom"):
        async for _ in client.watch("t"):
            pass
    await client.close()
