"""``noeta serve``: the client API over HTTP, so a TUI, a web page or another
service can drive tasks on another machine. Events stream as server-sent
events; when this process also runs workers, live model output streams too.

    POST /tasks                       {agent, input?, config?, queue?, dedupe_key?} -> {task_id}
    GET  /tasks?parent_id=&status=&limit=
    GET  /tasks/{id}
    GET  /tasks/{id}/events?after=N
    GET  /tasks/{id}/pending          approvals and questions waiting on a person, sub-agents'
                                      included: task_id, request_id, kind, tool, agent, title
                                      ("Bash command · from the Explore agent"), choices
    GET  /tasks/{id}/watch?after=N    text/event-stream: "event" (a recorded event) and
                                      "delta" (live output) messages
    POST /tasks/{id}/messages         {input, when?, dedupe_key?} — a "/name args" text
                                      runs the task agent's slash command of that name
    POST /tasks/{id}/answers          {request_id, decision?, always?, data?} — data
                                      {"mode": m} picks a card's mode choice
    POST /tasks/{id}/signals          {name, data?, dedupe_key?} — a signal a plugin reads
                                      (``permission_mode`` {mode}: switch the permission mode)
    POST /tasks/{id}/cancel           {reason?, turn_only?}

A bearer token is required when one is configured (``NOETA_SERVER_TOKEN``).
Needs the ``server`` extra.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hmac
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any

import msgspec
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.commands import expand_command
from noeta.core.errors import UnknownTask
from noeta.core.extension import CommandContext
from noeta.core.pending import card_title, choices, pending_tree
from noeta.core.types import Delta, Message

_enc = msgspec.json.Encoder()
log = logging.getLogger("noeta.host.http")


def _json(value: Any, status: int = 200) -> Response:
    return Response(_enc.encode(value), status_code=status, media_type="application/json")


def _input(value: Any) -> Any:
    if value is None or isinstance(value, str):
        return value
    return msgspec.convert(value, Message)


def create_app(client: Client, *, token: str | None = None,
               deltas: Any = None, agents: Mapping[str, Agent] | None = None) -> Starlette:
    """``deltas`` is ``LocalApp.deltas`` when workers run in this process;
    ``agents`` (by name) lets a message run its task agent's slash commands."""

    def authorized(request: Request) -> bool:
        if not token:
            return True
        got = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        return hmac.compare_digest(got, token)

    def guard(handler: Any) -> Any:
        async def wrapped(request: Request) -> Response:
            if not authorized(request):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            try:
                result: Response = await handler(request)
                return result
            except UnknownTask as exc:
                return JSONResponse({"error": f"unknown task {exc}"}, status_code=404)
            except (msgspec.ValidationError, ValueError, KeyError) as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
        return wrapped

    async def create(request: Request) -> Response:
        body = await request.json()
        task_id = await client.create(
            body["agent"], _input(body.get("input")), config=body.get("config"),
            queue=body.get("queue", "default"), dedupe_key=body.get("dedupe_key"))
        return _json({"task_id": task_id}, 201)

    async def list_tasks(request: Request) -> Response:
        q = request.query_params
        rows = await client.tasks(parent_id=q.get("parent_id"), status=q.get("status"),  # type: ignore[arg-type]
                                  limit=int(q.get("limit", "100")))
        return _json(rows)

    async def get_task(request: Request) -> Response:
        return _json(await client.get(request.path_params["task_id"]))

    async def events(request: Request) -> Response:
        after = int(request.query_params.get("after", "0"))
        return _json(await client.events(request.path_params["task_id"], after))

    async def pending(request: Request) -> Response:
        task_id = request.path_params["task_id"]
        out = []
        for item in await pending_tree(client, task_id):
            row = dataclasses.asdict(item)
            row["title"] = card_title(item, task_id)
            if item.kind == "approval":
                row["choices"] = choices(item.detail)
            out.append(row)
        return _json(out)

    async def send(request: Request) -> Response:
        body = await request.json()
        task_id = request.path_params["task_id"]
        value = _input(body["input"])
        if isinstance(value, str) and value.startswith("/") and agents:
            row = await client.get(task_id)
            agent = agents.get(row.agent)
            if agent is not None:
                expanded = await expand_command(agent, CommandContext(cwd="", task_id=task_id),
                                                value)
                value = expanded if expanded is not None else value
        seq = await client.send(task_id, value,
                                when=body.get("when", "next_turn"),
                                dedupe_key=body.get("dedupe_key"))
        return _json({"inbox_seq": seq})

    async def answer(request: Request) -> Response:
        body = await request.json()
        seq = await client.answer(request.path_params["task_id"], body["request_id"],
                                  body.get("decision"), always=bool(body.get("always")),
                                  data=body.get("data"))
        return _json({"ok": True, "inbox_seq": seq})

    async def signal(request: Request) -> Response:
        body = await request.json()
        name = body["name"]
        if not isinstance(name, str) or not name:
            raise ValueError("a signal needs a non-empty 'name'")
        seq = await client.signal(request.path_params["task_id"], name, body.get("data"),
                                  dedupe_key=body.get("dedupe_key"))
        return _json({"ok": True, "inbox_seq": seq})

    async def cancel(request: Request) -> Response:
        body = await request.json() if await request.body() else {}
        await client.cancel(request.path_params["task_id"], body.get("reason", ""),
                            turn_only=bool(body.get("turn_only")))
        return _json({"ok": True})

    async def watch(request: Request) -> Response:
        task_id = request.path_params["task_id"]
        await client.get(task_id)
        after = int(request.query_params.get("after", "0"))
        queue: asyncio.Queue[bytes] = asyncio.Queue()

        def on_delta(tid: str, delta: Delta) -> None:
            if tid == task_id:
                queue.put_nowait(b"event: delta\ndata: " + _enc.encode(delta) + b"\n\n")

        async def follow() -> None:
            end: dict[str, Any] = {}
            try:
                async for rec in client.watch(task_id, after):
                    queue.put_nowait(b"event: event\ndata: " + _enc.encode(rec) + b"\n\n")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the stream must end, not hang
                log.warning("watch %s failed: %s", task_id, exc)
                end = {"error": f"{type(exc).__name__}: {exc}"}
            queue.put_nowait(b"event: end\ndata: " + _enc.encode(end) + b"\n\n")

        async def stream() -> AsyncIterator[bytes]:
            unsubscribe = deltas(on_delta) if deltas is not None else None
            following = asyncio.create_task(follow())
            try:
                while True:
                    try:
                        chunk = await asyncio.wait_for(queue.get(), 15)
                    except TimeoutError:
                        yield b": keep-alive\n\n"
                        continue
                    yield chunk
                    if chunk.startswith(b"event: end"):
                        return
            finally:
                following.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await following
                if unsubscribe is not None:
                    unsubscribe()

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"cache-control": "no-cache"})

    return Starlette(routes=[
        Route("/tasks", guard(create), methods=["POST"]),
        Route("/tasks", guard(list_tasks), methods=["GET"]),
        Route("/tasks/{task_id}", guard(get_task), methods=["GET"]),
        Route("/tasks/{task_id}/events", guard(events), methods=["GET"]),
        Route("/tasks/{task_id}/pending", guard(pending), methods=["GET"]),
        Route("/tasks/{task_id}/watch", guard(watch), methods=["GET"]),
        Route("/tasks/{task_id}/messages", guard(send), methods=["POST"]),
        Route("/tasks/{task_id}/answers", guard(answer), methods=["POST"]),
        Route("/tasks/{task_id}/signals", guard(signal), methods=["POST"]),
        Route("/tasks/{task_id}/cancel", guard(cancel), methods=["POST"]),
    ])

