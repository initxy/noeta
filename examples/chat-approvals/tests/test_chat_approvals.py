"""The chat approvals example next to chat_bridge: an approval the task waits on
is posted to its thread (with an idempotency key), a press from an allowlisted
sender holding the secret answers it, the same press on two replicas lands once."""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from noeta import Agent, Extension, Runtime, TaskView, function_tool
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core import registry
from noeta.core.client import Client
from noeta.core.events import ToolFinished
from noeta.core.extension import Ask, Gate, ToolSet
from noeta.core.types import ToolUse
from noeta.testing import ScriptedProvider, reply

HERE = Path(__file__).parents[1]
BRIDGE = HERE.parent / "chat-bridge"
SECRET = "bridge-s3cret"
TOKEN = "hook-token-s3cret"
REPLY_URL = f"https://chat.example/hooks/{TOKEN}"


@function_tool
def Deploy(env: str) -> str:
    """Deploy to an environment."""
    return f"deployed to {env}"


class AskFirst(Extension):
    name = "ask_first"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        return Ask(prompt="deploy?")


@respx.mock
async def test_an_approval_is_asked_and_answered_through_the_chat(tmp_path: Path) -> None:
    sent = respx.post(REPLY_URL).mock(return_value=httpx.Response(200))
    registry.load_plugin_files([BRIDGE, HERE])
    bridge = registry.load(registry.CHANNELS, "chat_bridge")(
        secret=SECRET, allow_senders=["alice"])
    approvals = registry.load(registry.CHANNELS, "chat_approvals")(
        secret=SECRET, allow_senders=["alice"])
    extensions = (registry.load(registry.PLUGINS, "chat_reply")(url=REPLY_URL),
                  registry.load(registry.PLUGINS, "chat_approval_prompt")(url=REPLY_URL),
                  AskFirst(), ToolSet([Deploy]))
    agent = Agent(name="main", model="m", extensions=extensions)
    provider = ScriptedProvider([reply("", ("Deploy", {"env": "prod"})), reply("done: prod")])
    store = SqliteStore(str(tmp_path / "t.db"))
    rt = Runtime(store=store, agents={"main": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: None)  # type: ignore[arg-type,return-value]
    replicas = [Client(store), Client(store)]
    assert await bridge.receive(replicas[0], {"delivery_id": "d1", "thread": "T",
                                              "sender": "alice", "text": "deploy prod"},
                                SECRET) == 200
    await rt.run_until_idle()
    [asked] = [json.loads(c.request.content) for c in sent.calls]
    [prompt] = asked["approvals"]
    assert asked["thread"] == "T"
    assert {k: prompt[k] for k in ("tool", "input")} == {"tool": "Deploy",
                                                         "input": {"env": "prod"}}
    press: dict[str, Any] = {"thread": "T", "call_id": prompt["call_id"], "sender": "alice",
                             "verdict": "allow"}
    assert await approvals.receive(replicas[0], press, "") == 401
    assert await approvals.receive(replicas[0], {**press, "sender": "mallory"}, SECRET) == 403
    assert await approvals.receive(replicas[0], {**press, "verdict": "maybe"}, SECRET) == 400
    assert await approvals.receive(replicas[0], {**press, "call_id": 7}, SECRET) == 400
    assert [await approvals.receive(c, press, SECRET) for c in replicas] == [200, 200]
    await rt.run_until_idle()
    [row] = await replicas[0].tasks()
    finished = [r.event.result.text for r in await replicas[0].events(row.task_id)
                if isinstance(r.event, ToolFinished)]
    assert finished == ["deployed to prod"]
    assert [json.loads(c.request.content).get("text") for c in sent.calls] == [None, "done: prod"]
    keys = [c.request.headers["idempotency-key"] for c in sent.calls]
    assert len(set(keys)) == 2 and all(k.startswith(row.task_id + ":") for k in keys)
    assert TOKEN not in repr([r.event for r in await replicas[0].events(row.task_id)])
    await store.close()


async def test_the_approvals_listen_over_http(tmp_path: Path) -> None:
    pytest.importorskip("uvicorn", reason="the listener needs the server extra")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    registry.load_plugin_files([HERE])
    approvals = registry.load(registry.CHANNELS, "chat_approvals")(
        secret=SECRET, allow_senders=["alice"], port=port)
    store, stop = SqliteStore(str(tmp_path / "t.db")), asyncio.Event()
    running = asyncio.create_task(approvals.run(Client(store), stop))
    url, body = f"http://127.0.0.1:{port}/approve", {
        "thread": "T", "call_id": "c1", "sender": "bob", "verdict": "allow"}
    async with httpx.AsyncClient() as http:
        for _ in range(100):
            try:
                unsigned = await http.post(url, json=body)
                break
            except httpx.ConnectError:
                await asyncio.sleep(0.05)
        signed = await http.post(url, json=body, headers={"X-Chat-Secret": SECRET})
        garbled = await http.post(url, content=b"{not json", headers={"X-Chat-Secret": SECRET})
    assert (unsigned.status_code, signed.status_code, garbled.status_code) == (401, 403, 400)
    stop.set()
    await asyncio.wait_for(running, 5)
    await store.close()
