"""chat_bridge: a chat app posts each message to POST /chat with a shared secret in
X-Chat-Secret; every thread is one durable task, replies go back from inside it.
Safe on several replicas: a delivery lands once, and only the task's writer relays."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from collections.abc import Sequence
from typing import Any

import httpx

from noeta import Extension, TaskView
from noeta.core.channel import Channel, deliver, framed
from noeta.core.client import ClientAPI
from noeta.core.events import Recorded, Waiting

VERDICT = re.compile(r"^\s*(yes|no)\s+(\S+)\s*$", re.I)


class ChatBridge(Channel):
    """``secret`` (as ``{env:NAME}``; unset refuses all) proves the caller is the
    chat app; then ``allow_senders`` (sender ids, never rooms) are let in."""

    name = "chat_bridge"

    def __init__(self, secret: str = "", allow_senders: Sequence[str] = (), agent: str = "main",
                 host: str = "127.0.0.1", port: int = 8788) -> None:
        self.secret, self.allowed, self.agent = secret.encode(), set(allow_senders), agent
        self.host, self.port = host, port

    async def receive(self, client: ClientAPI, body: dict[str, Any], secret: str) -> int:
        """One delivery ``{delivery_id, thread, sender, text}``: an HTTP status."""
        if not self.secret or not hmac.compare_digest(secret.encode(), self.secret):
            return 401
        if body.get("sender") not in self.allowed:
            return 403
        task_id = "chat-" + hashlib.sha256(body["thread"].encode()).hexdigest()[:24]
        if verdict := VERDICT.match(body["text"]):  # `yes <id>` / `no <id>` answers an approval
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
        from starlette.requests import Request
        from starlette.responses import Response
        from starlette.routing import Route

        async def chat(request: Request) -> Response:
            return Response(status_code=await self.receive(
                client, await request.json(), request.headers.get("x-chat-secret", "")))
        app = Starlette(routes=[Route("/chat", chat, methods=["POST"])])
        server = uvicorn.Server(uvicorn.Config(app, host=self.host, port=self.port))
        serving = asyncio.create_task(server.serve())
        await stop.wait()
        server.should_exit = True
        await serving


class ChatReply(Extension):
    """Posts the answer or an approval prompt back to the thread, once. ``url``
    (often holding a token) is a plugin argument, never in the task's log."""

    name = "chat_reply"
    add_to = ("main",)

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
            f"Allow {c.use.name} {json.dumps(c.use.input)}? "
            f"Reply `yes {c.use.id}` or `no {c.use.id}`."
            for c in view.state.open_calls if c.use.id in event.ids)
        async with httpx.AsyncClient(timeout=10) as http:
            await http.post(self.url, json={"thread": target["thread"], "text": text})
        view.record(self.name, "relayed", {"seq": recorded.seq})
