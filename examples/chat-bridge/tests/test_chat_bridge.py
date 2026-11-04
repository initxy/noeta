"""The chat bridge example: a thread becomes a task, only senders holding the
secret and on the allowlist get in, answers go back (each with an idempotency
key), and a delivery reaching two replicas lands once."""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from noeta import Agent, Runtime
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core import registry
from noeta.core.client import Client
from noeta.core.events import Say
from noeta.testing import ScriptedProvider, reply

HERE = Path(__file__).parents[1]
TOKEN = "hook-token-s3cret"
SECRET = "bridge-s3cret"
REPLY_URL = f"https://chat.example/hooks/{TOKEN}"


@respx.mock
async def test_a_chat_thread_runs_a_task_and_gets_its_answers(tmp_path: Path) -> None:
    sent = respx.post(REPLY_URL).mock(return_value=httpx.Response(200))
    registry.load_plugin_files([HERE])
    bridge = registry.load(registry.CHANNELS, "chat_bridge")(
        secret=SECRET, allow_senders=["alice"])
    relay = registry.load(registry.PLUGINS, "chat_reply")(url=REPLY_URL)
    agent = Agent(name="main", model="m", extensions=(relay,))
    provider = ScriptedProvider([reply("hello alice"), reply("deployed")])
    store = SqliteStore(str(tmp_path / "t.db"))
    rt = Runtime(store=store, agents={"main": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: None)  # type: ignore[arg-type,return-value]
    replicas = [Client(store), Client(store)]
    first: dict[str, Any] = {"delivery_id": "d1", "thread": "T", "sender": "alice",
                             "text": "hi"}
    assert await bridge.receive(replicas[0], first, "") == 401  # no secret
    assert await bridge.receive(replicas[0], first, SECRET + "x") == 401  # wrong secret
    assert await bridge.receive(replicas[0], {**first, "sender": "mallory"}, SECRET) == 403
    for missing in ("thread", "text", "delivery_id", "sender"):
        partial = {k: v for k, v in first.items() if k != missing}
        assert await bridge.receive(replicas[0], partial, SECRET) == 400, missing
    assert await bridge.receive(replicas[0], ["not", "an", "object"], SECRET) == 400
    assert await bridge.receive(replicas[0], {**first, "thread": 7}, SECRET) == 400
    assert [await bridge.receive(c, first, SECRET) for c in replicas] == [200, 200]
    [row] = await replicas[0].tasks()
    assert [i.body.message.text for i in await store.inbox(row.task_id)
            if isinstance(i.body, Say)] == ['<channel source="chat" sender="alice">\n'
                                             "hi\n</channel>"]
    await rt.run_until_idle()
    assert await bridge.receive(replicas[1], {**first, "delivery_id": "d2",
                                              "text": "deploy"}, SECRET) == 200
    await rt.run_until_idle()
    await rt.run_until_idle()
    assert [json.loads(c.request.content) for c in sent.calls] == [
        {"thread": "T", "text": "hello alice"}, {"thread": "T", "text": "deployed"}]
    keys = [c.request.headers["idempotency-key"] for c in sent.calls]
    assert len(set(keys)) == 2 and all(k.startswith(row.task_id + ":") for k in keys)
    log = [r.event for r in await replicas[0].events(row.task_id)]
    assert TOKEN not in repr(log)  # the reply URL's token never enters the task log
    await store.close()


class Crash(BaseException):
    """The worker process dies (nothing in it catches this)."""


@respx.mock
async def test_an_answer_is_posted_again_after_a_crash_before_it_was_recorded(
        tmp_path: Path) -> None:
    posts: list[httpx.Request] = []

    def chat_app(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        if len(posts) == 1:
            raise Crash  # the post reached the chat app; the worker died before recording it
        return httpx.Response(200)

    respx.post(REPLY_URL).mock(side_effect=chat_app)
    registry.load_plugin_files([HERE])
    relay = registry.load(registry.PLUGINS, "chat_reply")(url=REPLY_URL)
    agent = Agent(name="main", model="m", extensions=(relay,))
    provider = ScriptedProvider([reply("hello alice")])
    store = SqliteStore(str(tmp_path / "t.db"))

    def worker() -> Runtime:
        return Runtime(store=store, agents={"main": agent}, provider_for=lambda _m: provider,
                       env_for=lambda _s: None, lease_seconds=0.2)  # type: ignore[arg-type,return-value]

    client = Client(store)
    tid = await client.create("main", "hi", config={"chat_reply": {"thread": "T"}})
    with pytest.raises(Crash):
        await worker().run_until_idle()
    assert (await client.get(tid)).status == "running"  # claimed, its lease running out
    await asyncio.sleep(0.3)
    await worker().run_until_idle()
    assert [json.loads(p.content) for p in posts] == [{"thread": "T", "text": "hello alice"}] * 2
    assert len({p.headers["idempotency-key"] for p in posts}) == 1
    relayed = [r.event for r in await client.events(tid)
               if getattr(r.event, "kind", None) == "relayed"]
    assert len(relayed) == 1
    row = await client.get(tid)
    assert (row.status, row.waiting_on) == ("waiting", "input")
    await client.signal(tid, "unrelated")  # a wake that opens no turn
    await worker().run_until_idle()
    assert len(posts) == 2  # nothing left to deliver, and no new wait to relay
    await store.close()


async def test_the_bridge_listens_over_http(tmp_path: Path) -> None:
    pytest.importorskip("uvicorn", reason="the listener needs the server extra")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    registry.load_plugin_files([HERE])
    bridge = registry.load(registry.CHANNELS, "chat_bridge")(
        secret=SECRET, allow_senders=["alice"], port=port)
    store, stop = SqliteStore(str(tmp_path / "t.db")), asyncio.Event()
    running = asyncio.create_task(bridge.run(Client(store), stop))
    url, body = f"http://127.0.0.1:{port}/chat", {
        "delivery_id": "d1", "thread": "T", "sender": "bob", "text": "hi"}
    async with httpx.AsyncClient() as http:
        for _ in range(100):
            try:
                unsigned = await http.post(url, json=body)
                break
            except httpx.ConnectError:
                await asyncio.sleep(0.05)
        wrong = await http.post(url, json=body, headers={"X-Chat-Secret": "guess"})
        signed = await http.post(url, json=body, headers={"X-Chat-Secret": SECRET})
        garbled = await http.post(url, content=b"{not json", headers={"X-Chat-Secret": SECRET})
        partial = await http.post(url, json={"sender": "alice"},
                                  headers={"X-Chat-Secret": SECRET})
    assert (unsigned.status_code, wrong.status_code, signed.status_code) == (401, 401, 403)
    assert (garbled.status_code, partial.status_code) == (400, 400)
    stop.set()
    await asyncio.wait_for(running, 5)
    await store.close()
