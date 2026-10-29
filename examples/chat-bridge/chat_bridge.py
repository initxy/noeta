"""chat_bridge: a chat app posts each message to POST /chat with a shared secret in
X-Chat-Secret; every thread is one durable task, replies go back from inside it.
Safe on several replicas: a delivery lands once, and only the task's writer relays."""

import asyncio
import hashlib
import hmac
import json
import re
from typing import Any

import httpx

from noeta import Extension, TaskView
from noeta.core.channel import Channel, deliver, framed
from noeta.core.client import ClientAPI
from noeta.core.events import Recorded, Waiting

VERDICT = re.compile(r"^\s*(yes|no)\s+(\S+)\s*$", re.I)  # `yes <id>` answers an approval


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
        fields = ("delivery_id", "thread", "sender", "text")
        if not isinstance(body, dict) or not all(isinstance(body.get(k), str) for k in fields):
            return 400
        if body["sender"] not in self.allowed:
            return 403
        task_id = "chat-" + hashlib.sha256(body["thread"].encode()).hexdigest()[:24]
        if verdict := VERDICT.match(body["text"]):
            await client.answer(task_id, verdict[2], ("deny", "allow")[verdict[1].lower() == "yes"])
        else:
            await deliver(client, task_id=task_id, agent=self.agent,
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
                body = json.loads(await request.body())
            except ValueError:
                body = None
            secret = request.headers.get("x-chat-secret", "")
            return Response(status_code=await self.receive(client, body, secret))
        app = Starlette(routes=[Route("/chat", chat, methods=["POST"])])
        server = uvicorn.Server(uvicorn.Config(app, host=self.host, port=self.port))
        serving = asyncio.create_task(server.serve())
        await stop.wait()
        server.should_exit = True
        await serving


class ChatReply(Extension):
    """Posts each answer or approval prompt to the thread, at least once (after a crash, again
    with the same ``Idempotency-Key``). ``url`` may hold a token: it never enters the log."""

    name, add_to = "chat_reply", ("main",)

    def __init__(self, url: str = "") -> None:
        self.url = url

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        return data["seq"] if kind == "relayed" else state

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        target, event = view.state.config.get("chat_reply"), recorded.event
        if not target or not isinstance(event, Waiting) or event.on not in ("input", "approval") \
                or recorded.seq <= (view.ext_state(self.name) or 0):
            return
        text = view.state.last_answer if event.on == "input" else "\n".join(
            f"Allow {c.use.name} {json.dumps(c.use.input)}? Reply `yes {c.use.id}` or "
            f"`no {c.use.id}`." for c in view.state.open_calls if c.use.id in event.ids)
        async with httpx.AsyncClient(timeout=10) as http:
            await http.post(self.url, json={"thread": target["thread"], "text": text},
                            headers={"Idempotency-Key": f"{view.task_id}:{recorded.seq}"})
        view.record(self.name, "relayed", {"seq": recorded.seq})
