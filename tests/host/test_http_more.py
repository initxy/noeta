"""The HTTP host's newer doors: Claude Code's headless protocol over a
WebSocket and one-way over POST, commands, agents, models, usage, context,
rewind and the OpenAPI document — over a real socket."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("starlette")
uvicorn = pytest.importorskip("uvicorn")
websockets = pytest.importorskip("websockets")

import httpx  # noqa: E402

from noeta.backends.stores.sqlite import SqliteStore  # noqa: E402
from noeta.core.agent import Agent  # noqa: E402
from noeta.core.extension import Ask, Command, CommandContext, Extension, TaskView  # noqa: E402
from noeta.core.tool import ToolContext, ToolOutput, text_output  # noqa: E402
from noeta.core.types import ToolUse  # noqa: E402
from noeta.host.http import create_app  # noqa: E402
from noeta.host.local import LocalApp  # noqa: E402
from noeta.testing import ScriptedProvider, reply  # noqa: E402

MODEL = "claude-opus-5-5"


class Echo:
    name = "Echo"
    description = "e"
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return text_output("echoed")


class Gate(Extension):
    name = "gate"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Any:
        return Ask(prompt="echo?")

    async def commands(self, ctx: CommandContext) -> Sequence[Command]:
        return [Command(name="hi", description="Say hi", template="hi {args}"),
                Command(name="where", description="Where", local=True)]

    async def run_command(self, ctx: CommandContext, name: str, args: str) -> str:
        return "here"


@pytest.fixture
async def served(tmp_path: Path) -> AsyncIterator[tuple[str, ScriptedProvider]]:
    provider = ScriptedProvider()
    agent = Agent(name="main", model=MODEL, tools=[Echo()], extensions=[Gate()],
                  description="The main agent.")
    local = LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], provider,
                     env_for=lambda _s: None)
    await local.start()
    app = create_app(local.client, token="secret", deltas=local.deltas, app=local)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"127.0.0.1:{port}", provider
    server.should_exit = True
    await serving
    await local.stop()


H = {"authorization": "Bearer secret"}


async def test_stream_json_over_a_websocket(served: tuple[str, ScriptedProvider]) -> None:
    host, provider = served
    provider.add(reply("", ("Echo", {})), reply("echo done"))
    url = f"ws://{host}/stream-json?token=secret&model={MODEL}"
    async with websockets.connect(url) as ws:
        init = json.loads(await ws.recv())
        assert init["subtype"] == "init" and init["session_id"]
        await ws.send(json.dumps({"type": "user", "message": {"role": "user",
                                                              "content": "echo"}}))
        while True:
            message = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if message["type"] == "control_request":
                assert message["request"]["subtype"] == "can_use_tool"
                await ws.send(json.dumps({"type": "control_response", "response": {
                    "subtype": "success", "request_id": message["request_id"],
                    "response": {"behavior": "allow", "updatedInput": {}}}}))
            if message["type"] == "result":
                break
        assert message["result"] == "echo done" and not message["is_error"]
    with pytest.raises(websockets.InvalidStatus, match="403"):
        async with websockets.connect(f"ws://{host}/stream-json"):
            pass  # no token


async def test_stream_json_one_way_and_the_rest(served: tuple[str, ScriptedProvider]) -> None:
    host, provider = served
    provider.add(reply("", ("Echo", {})), reply("no echo then"))
    base = f"http://{host}"
    async with httpx.AsyncClient(base_url=base, headers=H, timeout=10) as http:
        body = "\n".join(json.dumps(x) for x in [
            {"type": "user", "message": {"role": "user", "content": "echo"}}])
        resp = await http.post(f"/stream-json?model={MODEL}", content=body)
        out = [json.loads(x) for x in resp.text.splitlines()]
        result = out[-1]
        assert result["type"] == "result" and result["result"] == "no echo then"
        assert result["permission_denials"][0]["tool_name"] == "Echo"
        task_id = out[0]["session_id"]

        assert [c["name"] for c in (await http.get("/commands")).json()] == ["hi", "where"]
        local = await http.post(f"/tasks/{task_id}/messages", json={"input": "/where"})
        assert local.json() == {"output": "here"}
        [agent] = (await http.get("/agents")).json()
        assert agent == {"name": "main", "description": "The main agent.", "model": MODEL,
                         "one_shot": False}
        listed = {m["id"]: m for m in (await http.get("/models")).json()}
        assert "opus" in listed[MODEL]["aliases"]
        usage = (await http.get(f"/tasks/{task_id}/usage")).json()
        assert usage["tasks"] == 1 and usage["by_model"][MODEL]["calls"] == 2
        context = (await http.get(f"/tasks/{task_id}/context")).json()
        assert context["categories"]["Messages"] > 0
        points = (await http.get(f"/tasks/{task_id}/rewind")).json()
        assert [p["text"] for p in points] == ["echo"]
        fork = (await http.post(f"/tasks/{task_id}/rewind", json={"index": 0})).json()
        assert fork["prompt"] == "echo" and fork["task_id"] != task_id
        assert (await http.get(f"/tasks/{fork['task_id']}")).status_code == 200
        spec = (await http.get("/openapi.json")).json()
        assert spec["openapi"].startswith("3.1") and "/tasks/{task_id}/rewind" in spec["paths"]
        signal = await http.post(f"/tasks/{task_id}/signals",
                                 json={"name": "config", "data": {"model": "opus"}})
        assert signal.status_code == 200
        assert (await http.get("/models", headers={"authorization": "x"})).status_code == 401
