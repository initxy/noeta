"""chat_approvals: approve a tool call from the chat. Inside a chat_bridge task, each approval
it waits on is posted to the thread; the chat app posts a press of Allow or Deny to POST /approve
with the shared secret in X-Chat-Secret. Safe on several replicas: an answer lands once."""

import asyncio
import hashlib
import hmac
from typing import Any

import httpx

from noeta import Extension, TaskView
from noeta.core.channel import Channel
from noeta.core.client import ClientAPI
from noeta.core.events import Recorded, Waiting

FIELDS = ("thread", "call_id", "sender", "verdict")


class ChatApprovals(Channel):
    """``secret`` (``{env:NAME}``; unset refuses all) proves the caller is the chat app;
    then only ``allow_senders`` (sender ids, never rooms) may answer."""

    name = "chat_approvals"

    def __init__(self, secret: str = "", allow_senders: tuple[str, ...] = (),
                 host: str = "127.0.0.1", port: int = 8789) -> None:
        self.secret, self.allowed = secret.encode(), set(allow_senders)
        self.host, self.port = host, port

    async def receive(self, client: ClientAPI, body: Any, secret: str) -> int:
        if not self.secret or not hmac.compare_digest(secret.encode(), self.secret):
            return 401
        if not isinstance(body, dict) or not all(isinstance(body.get(k), str) for k in FIELDS) \
                or body["verdict"] not in ("allow", "deny"):
            return 400
        if body["sender"] not in self.allowed:
            return 403
        task_id = "chat-" + hashlib.sha256(body["thread"].encode()).hexdigest()[:24]
        await client.answer(task_id, body["call_id"], body["verdict"])  # only chat tasks
        return 200

    async def run(self, client: ClientAPI, stop: asyncio.Event) -> None:
        import uvicorn  # the server extra
        from starlette.applications import Starlette
        from starlette.responses import Response
        from starlette.routing import Route

        async def approve(request: Any) -> Response:
            try:
                body = await request.json()
            except ValueError:
                body = None
            code = await self.receive(client, body, request.headers.get("x-chat-secret", ""))
            return Response(status_code=code)
        app = Starlette(routes=[Route("/approve", approve, methods=["POST"])])
        server = uvicorn.Server(uvicorn.Config(app, host=self.host, port=self.port))
        serving = asyncio.create_task(server.serve())
        await stop.wait()
        server.should_exit = True
        await serving


class ApprovalPrompt(Extension):
    """Posts each approval the task waits on to its thread (``call_id``, tool, input), at
    least once with an ``Idempotency-Key``. ``url`` may hold a token: it never enters the log."""

    name, add_to = "chat_approval_prompt", ("main",)

    def __init__(self, url: str = "") -> None:
        self.url = url

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        return data["seq"] if kind == "relayed" else state

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        target, event = view.state.config.get("chat_reply"), recorded.event
        if not target or not isinstance(event, Waiting) or event.on != "approval" \
                or recorded.seq <= (view.ext_state(self.name) or 0):
            return
        asks = [{"call_id": c.use.id, "tool": c.use.name, "input": c.use.input}
                for c in view.state.open_calls if c.use.id in event.ids]
        async with httpx.AsyncClient(timeout=10) as http:
            await http.post(self.url, json={"thread": target["thread"], "approvals": asks},
                            headers={"Idempotency-Key": f"{view.task_id}:{recorded.seq}"})
        view.record(self.name, "relayed", {"seq": recorded.seq})
