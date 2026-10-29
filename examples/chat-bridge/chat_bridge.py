"""chat_bridge: a chat app posts each message to POST /chat; every thread is one
durable task. Only allowlisted senders get in; `yes <id>` / `no <id>` from
them answers an approval. Replies and approval prompts go back to the thread
from inside the task. Safe on several replicas: an inbound delivery lands
once (deliver), and only the task's one writer relays."""

from __future__ import annotations

import asyncio
import hashlib
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
    """``allow_senders``: sender ids let in (never room ids); ``agent``,
    ``host``, ``port``."""

    name = "chat_bridge"

    def __init__(self, allow_senders: Sequence[str] = (), agent: str = "main",
                 host: str = "127.0.0.1", port: int = 8788) -> None:
        self.allowed, self.agent = set(allow_senders), agent
        self.host, self.port = host, port

    async def receive(self, client: ClientAPI, body: dict[str, Any]) -> int:
        """One delivery ``{delivery_id, thread, sender, text}``: an HTTP status."""
        if body.get("sender") not in self.allowed:
            return 403
        task_id = "chat-" + hashlib.sha256(body["thread"].encode()).hexdigest()[:24]
        if verdict := VERDICT.match(body["text"]):
            await client.answer(task_id, verdict[2],
                                "allow" if verdict[1].lower() == "yes" else "deny")
            return 200
        await deliver(client, task_id=task_id, agent=self.agent,
                      message=framed("chat", body["sender"], body["text"]),
                      dedupe_key=f"chat:{body['delivery_id']}",
                      config={"chat_reply": {"thread": body["thread"]}})
        return 200

    async def run(self, client: ClientAPI, stop: asyncio.Event) -> None:
        import uvicorn
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import Response
        from starlette.routing import Route

        async def chat(request: Request) -> Response:
            return Response(status_code=await self.receive(client, await request.json()))

        server = uvicorn.Server(uvicorn.Config(Starlette(routes=[Route("/chat", chat,
                                methods=["POST"])]), host=self.host, port=self.port))
        serving = asyncio.create_task(server.serve())
        await stop.wait()
        server.should_exit = True
        await serving


class ChatReply(Extension):
    """In the task: post the answer, or an approval prompt, back to the
    thread; what was relayed is recorded, so a resumed task never repeats it.
    ``url`` (where the chat app takes replies, often holding a token) is a
    plugin argument, never task config: the task log keeps only the thread."""

    name = "chat_reply"
    add_to = ("main",)

    def __init__(self, url: str = "") -> None:
        self.url = url

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        return data["seq"] if kind == "relayed" else state

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        target, event = view.state.config.get("chat_reply"), recorded.event
        if not target or not isinstance(event, Waiting) or \
                recorded.seq <= (view.ext_state(self.name) or 0):
            return
        if event.on == "input":
            text = view.state.last_answer
        elif event.on == "approval":
            text = "\n".join(f"Allow {c.use.name} {json.dumps(c.use.input)}? "
                             f"Reply `yes {c.use.id}` or `no {c.use.id}`."
                             for c in view.state.open_calls if c.use.id in event.ids)
        else:
            return
        async with httpx.AsyncClient(timeout=10) as http:
            await http.post(self.url, json={"thread": target["thread"], "text": text})
        view.record(self.name, "relayed", {"seq": recorded.seq})
