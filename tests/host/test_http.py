"""The HTTP server and the remote client against each other over a real
socket: create, watch (events and live deltas), answer an approval, cancel,
auth."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("starlette")
uvicorn = pytest.importorskip("uvicorn")

import httpx  # noqa: E402

from noeta.backends.stores.sqlite import SqliteStore  # noqa: E402
from noeta.core.agent import Agent  # noqa: E402
from noeta.core.events import ModelReplied, ToolFinished, Waiting  # noqa: E402
from noeta.core.extension import Ask, Extension, TaskView  # noqa: E402
from noeta.core.tool import ToolContext, ToolOutput, text_output  # noqa: E402
from noeta.core.types import Delta, ToolUse  # noqa: E402
from noeta.host.http import create_app  # noqa: E402
from noeta.host.local import LocalApp  # noqa: E402
from noeta.host.remote import RemoteClient  # noqa: E402
from noeta.testing import ScriptedProvider, reply  # noqa: E402


class Look:
    name = "Read"
    description = "Read."
    input_schema = {"type": "object"}
    read_only = True
    clearable = True

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return text_output("body")


class AskAll(Extension):
    name = "ask"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Any:
        return Ask(prompt="ok?")


@pytest.fixture
async def served(tmp_path: Path) -> AsyncIterator[tuple[str, ScriptedProvider]]:
    provider = ScriptedProvider()
    agent = Agent(name="main", model="m", tools=[Look()], extensions=[AskAll()])
    local = LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], provider,
                     env_for=lambda _s: None)
    await local.start()
    app = create_app(local.client, token="secret", deltas=local.deltas)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", provider
    server.should_exit = True
    await serving
    await local.stop()


async def test_round_trip_with_approval(served: tuple[str, ScriptedProvider]) -> None:
    url, provider = served
    provider.add(reply("checking", ("Read", {"file_path": "x"})), reply("all good"))
    client = RemoteClient(url, token="secret")
    deltas: list[Delta] = []
    client.on_delta = lambda _t, d: deltas.append(d)
    tid = await client.create("main", "look at x")
    seen: list[Any] = []
    async for rec in client.watch(tid):
        seen.append(rec.event)
        ev = rec.event
        if isinstance(ev, Waiting) and ev.on == "approval":
            await client.answer(tid, ev.ids[0], "allow")
        if isinstance(ev, Waiting) and ev.on == "input":
            break
    assert any(isinstance(e, ToolFinished) and not e.result.is_error for e in seen)
    assert [e.response.message.text for e in seen if isinstance(e, ModelReplied)] == \
        ["checking", "all good"]
    assert [d.text for d in deltas] == ["checking", "all good"]
    row = await client.get(tid)
    assert row.status == "waiting" and [r.task_id for r in await client.tasks()] == [tid]
    await client.cancel(tid)
    await client.close()


async def test_pending_lists_the_card(served: tuple[str, ScriptedProvider]) -> None:
    url, provider = served
    provider.add(reply("", ("Read", {"file_path": "x"})), reply("ok"))
    client = RemoteClient(url, token="secret")
    tid = await client.create("main", "look")
    async with httpx.AsyncClient(base_url=url,
                                 headers={"authorization": "Bearer secret"}) as http:
        for _ in range(200):
            rows = (await http.get(f"/tasks/{tid}/pending")).json()
            if rows:
                break
            await asyncio.sleep(0.02)
    [row] = rows
    assert row["kind"] == "approval" and row["title"] == "Read"
    assert [c["id"] for c in row["choices"]] == ["allow", "always", "deny"]
    await client.cancel(tid)
    await client.close()


async def test_token_is_required(served: tuple[str, ScriptedProvider]) -> None:
    url, _ = served
    async with httpx.AsyncClient(base_url=url) as http:
        assert (await http.get("/tasks")).status_code == 401
        resp = await http.get("/tasks/nope", headers={"authorization": "Bearer secret"})
        assert resp.status_code == 404
