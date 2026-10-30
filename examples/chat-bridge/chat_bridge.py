"""chat_bridge: a chat app posts each message to POST /chat with a shared secret in
X-Chat-Secret; every thread is one durable task, replies go back from inside it.
Safe on several replicas: a delivery lands once, and only the task's writer relays."""

import asyncio
import hashlib
import hmac
from typing import Any

import httpx

from noeta import Extension, Recorded, TaskView
from noeta.core.channel import Channel, deliver, framed
from noeta.core.client import ClientAPI
from noeta.core.events import Waiting

FIELDS = ("delivery_id", "thread", "sender", "text")


class ChatBridge(Channel):
    """``secret`` (``{env:NAME}``; unset refuses all) proves the caller is the chat app;
    then only ``allow_senders`` (sender ids, never rooms) get in."""

    name = "chat_bridge"

    def __init__(self, secret: str = "", allow_senders: tuple[str, ...] = (),
                 agent: str = "main", host: str = "127.0.0.1", port: int = 8788) -> None:
        self.secret, self.allowed, self.agent = secret.encode(), set(allow_senders), agent
        self.host, self.port = host, port

    async def receive(self, client: ClientAPI, body: Any, secret: str) -> int:
        if not self.secret or not hmac.compare_digest(secret.encode(), self.secret):
            return 401
        if not isinstance(body, dict) or not all(isinstance(body.get(k), str) for k in FIELDS):
            return 400
        if body["sender"] not in self.allowed:
            return 403
        await deliver(client, agent=self.agent,
                      task_id="chat-" + hashlib.sha256(body["thread"].encode()).hexdigest()[:24],
                      message=framed("chat", body["sender"], body["text"]),
                      dedupe_key=f"chat:{body['delivery_id']}",
                      config={"chat_reply": {"thread": body["thread"]}})
        return 200

    async def run(self, client: ClientAPI, stop: asyncio.Event) -> None:
        import uvicorn  # the server extra
        from starlette.applications import Starlette
        from starlette.responses import Response
        from starlette.routing import Route

        async def chat(request: Any) -> Response:
            try:
                body = await request.json()
            except ValueError:
                body = None
            code = await self.receive(client, body, request.headers.get("x-chat-secret", ""))
            return Response(status_code=code)
        app = Starlette(routes=[Route("/chat", chat, methods=["POST"])])
        server = uvicorn.Server(uvicorn.Config(app, host=self.host, port=self.port))
        serving = asyncio.create_task(server.serve())
        await stop.wait()
        server.should_exit = True
        await serving


class ChatReply(Extension):
    """Posts each answer to the thread, at least once (after a crash, again with the same
    ``Idempotency-Key``). ``url`` may hold a token: it never enters the log."""

    name, add_to = "chat_reply", ("main",)

    def __init__(self, url: str = "") -> None:
        self.url = url

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        return data["seq"] if kind == "relayed" else state

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        target, event = view.state.config.get("chat_reply"), recorded.event
        if not target or not isinstance(event, Waiting) or event.on != "input" \
                or recorded.seq <= (view.ext_state(self.name) or 0):
            return
        async with httpx.AsyncClient(timeout=10) as http:
            await http.post(self.url, json={"thread": target["thread"],
                                            "text": view.state.last_answer},
                            headers={"Idempotency-Key": f"{view.task_id}:{recorded.seq}"})
        view.record(self.name, "relayed", {"seq": recorded.seq})
