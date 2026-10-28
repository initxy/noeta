"""The chat bridge example: a thread becomes a task, only allowlisted senders
get in, an approval is asked and answered through the chat, replies go back
once, and a delivery reaching two replicas lands once."""

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
from noeta.core.events import Say, ToolFinished
from noeta.core.extension import Ask, Gate, ToolSet
from noeta.core.types import ToolUse
from noeta.testing import ScriptedProvider, reply

HERE = Path(__file__).parents[1]
REPLY_URL = "https://chat.example/reply"


@function_tool
def Deploy(env: str) -> str:
    """Deploy to an environment."""
    return f"deployed to {env}"


class AskFirst(Extension):
    name = "ask_first"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        return Ask(prompt="deploy?")


@respx.mock
async def test_a_chat_thread_runs_a_task_with_approvals_through_the_chat(tmp_path: Path) -> None:
    sent = respx.post(REPLY_URL).mock(return_value=httpx.Response(200))
    registry.load_plugin_files([HERE])
    bridge = registry.load(registry.CHANNELS, "chat_bridge")(
        allow_senders=["alice"], reply_url=REPLY_URL)
    relay = registry.load(registry.PLUGINS, "chat_reply")()
    agent = Agent(name="main", model="m", extensions=(relay, AskFirst(), ToolSet([Deploy])))
    provider = ScriptedProvider([reply("", ("Deploy", {"env": "prod"})), reply("done: prod")])
    store = SqliteStore(str(tmp_path / "t.db"))
    rt = Runtime(store=store, agents={"main": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: None)  # type: ignore[arg-type,return-value]
    replicas = [Client(store), Client(store)]
    first: dict[str, Any] = {"delivery_id": "d1", "thread": "T", "sender": "alice",
                             "text": "deploy prod"}
    assert await bridge.receive(replicas[0], {**first, "sender": "mallory"}) == 403
    assert [await bridge.receive(c, first) for c in replicas] == [200, 200]
    [row] = await replicas[0].tasks()
    assert [i.body.message.text for i in await store.inbox(row.task_id)
            if isinstance(i.body, Say)] == ['<channel source="chat" sender="alice">\n'
                                             "deploy prod\n</channel>"]
    await rt.run_until_idle()
    [asked] = [json.loads(c.request.content) for c in sent.calls]
    call_id = asked["text"].rsplit("`no ", 1)[1].rstrip("`.")
    assert asked == {"thread": "T", "text": f'Allow Deploy {{"env": "prod"}}? '
                                            f"Reply `yes {call_id}` or `no {call_id}`."}
    assert await bridge.receive(replicas[1], {"delivery_id": "d2", "thread": "T",
                                              "sender": "alice", "text": f"yes {call_id}"}) == 200
    await rt.run_until_idle()
    finished = [r.event.result.text for r in await replicas[0].events(row.task_id)
                if isinstance(r.event, ToolFinished)]
    assert finished == ["deployed to prod"]
    assert [json.loads(c.request.content)["text"] for c in sent.calls][1:] == ["done: prod"]
    await rt.run_until_idle()
    assert len(sent.calls) == 2  # nothing relayed twice
    await store.close()


async def test_the_bridge_listens_over_http(tmp_path: Path) -> None:
    pytest.importorskip("uvicorn", reason="the listener needs the server extra")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    registry.load_plugin_files([HERE])
    bridge = registry.load(registry.CHANNELS, "chat_bridge")(allow_senders=["alice"], port=port)
    store, stop = SqliteStore(str(tmp_path / "t.db")), asyncio.Event()
    running = asyncio.create_task(bridge.run(Client(store), stop))
    async with httpx.AsyncClient() as http:
        for _ in range(100):
            try:
                answer = await http.post(f"http://127.0.0.1:{port}/chat", json={
                    "delivery_id": "d1", "thread": "T", "sender": "bob", "text": "hi"})
                break
            except httpx.ConnectError:
                await asyncio.sleep(0.05)
    assert answer.status_code == 403
    stop.set()
    await asyncio.wait_for(running, 5)
    await store.close()
