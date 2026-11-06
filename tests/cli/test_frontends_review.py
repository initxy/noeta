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
pytest.importorskip("starlette")

import httpx
from acp import schema

import noeta.cli.main as cli
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.errors import NoetaError, Transient
from noeta.core.events import (
    Recorded,
    TaskCreated,
    Waiting,
)
from noeta.core.runner import Runtime
from noeta.core.tool import Park, ToolContext, ToolOutput, text_output
from noeta.core.types import Delta, ModelRequest, ModelResponse
from noeta.host.http import create_app
from noeta.host.local import LocalApp
from noeta.host.remote import RemoteClient
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


def local(tmp_path: Path, provider: Any, *agents: Agent, **kw: Any) -> LocalApp:
    return LocalApp(SqliteStore(str(tmp_path / "t.db")), list(agents), provider,
                    env_for=lambda _s: None, sleep=_no_sleep, **kw)


def text_block(text: str) -> list[Any]:
    return [schema.TextContentBlock(type="text", text=text)]


# ------------------------------------------------------------ #2 noeta -p


def test_print_mode_waits_for_a_foreground_sub_agent(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = ScriptedProvider([reply("", ("Task", {})), reply("child done"), reply("final")])
    agents = [Agent(name="main", model="m", tools=[Delegate()]),
              Agent(name="worker", model="m", one_shot=True)]
    monkeypatch.setattr(cli, "_app", lambda s, cwd=None, **kw: local(tmp_path, provider, *agents,
                                                                     own_tasks_only=True))
    with pytest.raises(SystemExit) as done:
        cli.main(["-p", "go", "--model", "m", "--cwd", str(tmp_path)])
    assert done.value.code == 0
    assert capsys.readouterr().out.strip() == "final"


# ------------------------------------------------------------ #4 #19 the TUI


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


async def test_a_store_wide_worker_never_runs_a_front_ends_task(tmp_path: Path) -> None:
    """``noeta serve`` / ``noeta worker`` on the same store would run it with
    their own settings; the front end that opens it again runs it."""
    path = str(tmp_path / "shared.db")
    served = ScriptedProvider([reply("serve's answer")])
    serve = Runtime(store=SqliteStore(path), agents={"main": Agent(name="main", model="m")},
                    provider_for=lambda _m: served, env_for=lambda _s: None)
    gone = LocalApp(SqliteStore(path), [Agent(name="main", model="m")], ScriptedProvider([]),
                    env_for=lambda _s: None, own_tasks_only=True)
    tid = await gone.client.create("main", "mine")  # its process quits before running it
    await gone.store.close()
    assert not await serve.run_once() and served.requests == []
    mine = ScriptedProvider([reply("my answer")])
    later = LocalApp(SqliteStore(path), [Agent(name="main", model="m")], mine,
                     env_for=lambda _s: None, own_tasks_only=True)
    await later.start()
    await later.adopt(tid)
    assert (await later.client.wait(tid, timeout=5)).status == "waiting"
    assert len(mine.requests) == 1
    await later.stop()
    await serve.store.close()


def _front_end(path: str, provider: Any) -> LocalApp:
    return LocalApp(SqliteStore(path), [Agent(name="main", model="m")], provider,
                    env_for=lambda _s: None, own_tasks_only=True)


def _server(path: str, provider: Any) -> tuple[LocalApp, httpx.AsyncClient]:
    app = LocalApp(SqliteStore(path), [Agent(name="main", model="m")], provider,
                   env_for=lambda _s: None)
    api = create_app(app.client, auth=False, app=app)
    return app, httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://t")


async def test_a_message_sent_through_the_server_runs_a_front_ends_task(tmp_path: Path) -> None:
    """The web app or the desktop app acting on a session the TUI made: the
    server takes the task over and runs it; until then it stays the front
    end's."""
    path = str(tmp_path / "shared.db")
    tui = _front_end(path, ScriptedProvider([reply("tui's answer")]))
    await tui.start()
    tid = await tui.client.create("main", "mine")
    assert (await tui.client.wait(tid, timeout=5)).status == "waiting"
    await tui.stop()  # the TUI quits
    served = ScriptedProvider([reply("serve's answer"), reply("after the rewind")])
    serve, http = _server(path, served)
    await serve.start()
    # A post that wakes nothing (archiving it) leaves it the front end's.
    r = await http.post(f"/tasks/{tid}/signals",
                        json={"name": "archive", "data": {"archived": True}, "wake": False})
    assert r.status_code == 200
    assert (await serve.client.get(tid)).queue == "front-end"
    r = await http.post(f"/tasks/{tid}/messages", json={"input": "again"})
    assert r.status_code == 200
    row = await serve.client.wait(tid, timeout=5)
    assert row.status == "waiting" and row.queue == "default" and len(served.requests) == 1
    # A rewind through the server makes its new task on the server's queue.
    r = await http.post(f"/tasks/{tid}/rewind", json={"index": 0, "files": False})
    assert r.status_code == 200
    assert (await serve.client.get(r.json()["task_id"])).queue == "default"
    await http.aclose()
    await serve.stop()
    # A front end opening it again still runs it.
    later = _front_end(path, ScriptedProvider([reply("tui again")]))
    await later.start()
    await later.adopt(tid)
    await later.client.send(tid, "and more")
    assert (await later.client.wait(tid, timeout=5)).status == "waiting"
    await later.stop()


class Gated:
    """The first call waits for ``gate``: the front end is mid-turn."""

    family = "x"

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest, on_delta: Any = None) -> ModelResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            await self.gate.wait()
        return reply(f"answer {len(self.requests)}")


async def test_a_server_never_takes_a_task_a_live_front_end_holds(tmp_path: Path) -> None:
    """A message through the server reaches the front end running the task,
    which answers it; the server runs nothing."""
    path = str(tmp_path / "shared.db")
    mine = Gated()
    tui = _front_end(path, mine)
    await tui.start()
    tid = await tui.client.create("main", "mine")
    for _ in range(100):
        if mine.requests:
            break
        await asyncio.sleep(0.02)
    served = ScriptedProvider([reply("serve's answer")])
    serve, http = _server(path, served)
    await serve.start()
    r = await http.post(f"/tasks/{tid}/messages", json={"input": "also this"})
    assert r.status_code == 200
    assert (await serve.client.get(tid)).queue == "front-end"
    mine.gate.set()
    for _ in range(250):
        if len(mine.requests) == 2 and (await tui.client.get(tid)).status == "waiting":
            break
        await asyncio.sleep(0.02)
    assert len(mine.requests) == 2 and served.requests == []
    await http.aclose()
    await serve.stop()
    await tui.stop()


# ------------------------------------------------------------ #19 server / remote


class BrokenWatchClient:
    async def get(self, task_id: str) -> Any:
        return None

    async def watch(self, task_id: str, after: int = 0) -> Any:
        yield Recorded(seq=1, at=0, event=TaskCreated(agent="a"))
        raise RuntimeError("store went away at /srv/private/t.db")


async def test_server_ends_the_stream_when_the_follower_fails() -> None:
    app = create_app(BrokenWatchClient(), auth=False)  # type: ignore[arg-type]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://x") as http:
        resp = await asyncio.wait_for(http.get("/tasks/t/watch"), 5)
    # The stream ends with an error; a library's text (a server path) stays
    # in the server's log.
    assert b"event: end" in resp.content and b'"error"' in resp.content
    assert b"/srv/private" not in resp.content


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
