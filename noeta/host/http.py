"""``noeta serve``: the client API over HTTP, so a TUI, a web page or another
service can drive tasks on another machine. Events stream as server-sent
events; when this process also runs workers, live model output streams too.

    POST /tasks                       {agent, input?, config?, queue?, dedupe_key?,
                                      history_from?, history_through?} -> {task_id}
    GET  /tasks?parent_id=&status=&limit=
    GET  /tasks/{id}
    GET  /tasks/{id}/events?after=N
    GET  /tasks/{id}/pending          approvals and questions waiting on a person, sub-agents'
                                      included: task_id, request_id, kind, tool, agent, title
                                      ("Bash command · from the Explore agent"), choices
    GET  /tasks/{id}/watch?after=N    text/event-stream: "event" (a recorded event) and
                                      "delta" (live output) messages
    POST /tasks/{id}/messages         {input, when?, dedupe_key?} — a "/name args" text
                                      runs the task agent's slash command of that name; a
                                      local command answers {output} and sends nothing
    POST /tasks/{id}/answers          {request_id, decision?, always?, data?} — data
                                      {"mode": m} picks a card's mode choice
    POST /tasks/{id}/signals          {name, data?, dedupe_key?} — a signal: ``permission_mode``
                                      {mode} switches the permission mode, ``config``
                                      {model?, effort?, max_turns?} changes the task's config
    POST /tasks/{id}/cancel           {reason?, turn_only?}
    GET  /tasks/{id}/usage            the task tree's tokens, model calls and cost by model
    GET  /tasks/{id}/context          what fills the next request (workers in this process)
    GET  /tasks/{id}/rewind           the person's prompts, each with the files changed since
    POST /tasks/{id}/rewind           {index, conversation?, files?} -> {task_id, prompt, files}
    GET  /commands?agent=&task_id=    slash commands an agent's plugins offer
    GET  /agents                      the agents this server runs
    GET  /models                      the model catalog and its aliases
    GET  /openapi.json                this API as an OpenAPI 3.1 document
    WS   /stream-json                 Claude Code's headless protocol, one JSON message per
                                      frame (``noeta.host.stream_json``); query: resume, fork,
                                      agent, model, cwd, permission_mode, max_turns,
                                      include_partial_messages, replay_user_messages
    POST /stream-json                 the same, one-way: the request body's JSON lines in,
                                      the reply's JSON lines out; permission prompts refused

A bearer token is required when one is configured (``NOETA_SERVER_TOKEN``;
``?token=`` for a WebSocket from a browser). A request from a web page of
another origin is refused, and so is one whose ``Host`` is not in
``allowed_hosts`` (DNS rebinding). No request starts or switches a task into
``bypassPermissions`` unless ``allow_bypass``, or runs it outside ``cwd`` and
the ``roots`` (a relative ``cwd`` is taken from ``cwd``). Needs the ``server`` extra.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hmac
import json
import logging
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

import msgspec
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from noeta.core import models
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.commands import list_commands, run_typed
from noeta.core.errors import UnknownTask
from noeta.core.extension import CommandContext
from noeta.core.loop.state import CONFIG_SIGNAL
from noeta.core.pending import card_title, choices, pending_tree
from noeta.core.types import Delta, Message
from noeta.host.rewind import rewind, rewind_points
from noeta.host.usage import context_breakdown, tree_usage
from noeta.toolkit.permission_modes import MODE_SIGNAL

_enc = msgspec.json.Encoder()
log = logging.getLogger("noeta.host.http")


def _json(value: Any, status: int = 200) -> Response:
    return Response(_enc.encode(value), status_code=status, media_type="application/json")


def _input(value: Any) -> Any:
    if value is None or isinstance(value, str):
        return value
    return msgspec.convert(value, Message)


@dataclasses.dataclass
class _Front:
    """What a stream-json conversation runs on when this server has no
    ``LocalApp`` of its own (API only): the client, and deltas if any."""

    client: Any
    deltas: Any = None
    runtime: Any = None

    async def adopt(self, task_id: str) -> None:
        return None


class Refused(Exception):
    """A request asked for what this server does not allow."""


_BYPASS = "bypassPermissions"


class _GuardedClient:
    """``client`` refusing what a request may not ask for: a task in
    ``bypassPermissions`` (unless allowed) or with a ``cwd`` outside the
    server's directory and its other ``roots``. A relative ``cwd`` is taken
    from the server's directory (not the process's) and passed on
    absolute, so every worker resolves it the same."""

    def __init__(self, client: Any, *, allow_bypass: bool, cwd: str | None,
                 roots: Sequence[str] = ()) -> None:
        self._client = client
        self._allow_bypass = allow_bypass
        self._cwd = os.path.realpath(cwd) if cwd else None
        self._roots = [r for r in (self._cwd, *(os.path.realpath(r) for r in roots)) if r]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def _mode(self, mode: Any) -> None:
        if mode == _BYPASS and not self._allow_bypass:
            raise Refused("this server does not allow bypassPermissions "
                          "(start it with --allow-bypass-permissions)")

    def check_cwd(self, cwd: Any) -> Any:
        """``cwd`` made absolute; raises :class:`Refused` when it lies
        outside the server's directories."""
        if not cwd or self._cwd is None:
            return cwd
        real = os.path.realpath(os.path.join(self._cwd, str(cwd)))
        if not any(real == r or real.startswith(r.rstrip("/") + "/") for r in self._roots):
            where = ", ".join(self._roots)
            raise Refused(f"cwd {cwd!r} is outside this server's directories ({where}); "
                          "start it there or add the directory with --allow-root")
        return real

    def check_config(self, config: Any) -> Any:
        """``config`` with its ``cwd`` absolute; raises :class:`Refused` for
        what a request may not ask for."""
        if not isinstance(config, Mapping):
            return config
        self._mode(config.get("permission_mode"))
        if not config.get("cwd"):
            return config
        return {**config, "cwd": self.check_cwd(config["cwd"])}

    async def create(self, *args: Any, **kw: Any) -> Any:
        if "config" in kw:
            kw["config"] = self.check_config(kw["config"])
        return await self._client.create(*args, **kw)

    async def answer(self, task_id: str, request_id: str, decision: Any = None,
                     **kw: Any) -> Any:
        data = kw.get("data")
        if isinstance(data, Mapping):
            self._mode(data.get("mode"))
        return await self._client.answer(task_id, request_id, decision, **kw)

    async def signal(self, task_id: str, name: str, data: Any = None, **kw: Any) -> Any:
        if name == MODE_SIGNAL and isinstance(data, Mapping):
            self._mode(data.get("mode"))
        if name == CONFIG_SIGNAL:
            data = self.check_config(data)
        return await self._client.signal(task_id, name, data, **kw)


class _GuardedFront:
    """A stream-json front whose ``client`` is guarded."""

    def __init__(self, front: Any, client: _GuardedClient) -> None:
        self._front = front
        self.client = client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._front, name)


#: ``Host`` names a server on a loopback address answers to.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _hostname(value: str) -> str:
    """The name in a ``Host`` header or an origin's authority (no port)."""
    if value.startswith("["):
        return value[1:].partition("]")[0]
    return value.rpartition(":")[0] if value.count(":") == 1 else value


def create_app(client: Client, *, token: str | None = None,
               deltas: Any = None, agents: Mapping[str, Agent] | None = None,
               app: Any = None, allowed_hosts: Sequence[str] | None = None,
               origins: Sequence[str] = (), allow_bypass: bool = False,
               cwd: str | None = None, roots: Sequence[str] = ()) -> Starlette:
    """``deltas`` is ``LocalApp.deltas`` when workers run in this process;
    ``agents`` (by name) lets a message run its task agent's slash commands;
    ``app`` (the ``LocalApp`` whose workers run here) previews requests
    (``/context``) and serves new stream-json conversations' trees.

    ``allowed_hosts``: the ``Host`` names served (any when ``None``);
    ``origins``: web origins (``https://host:port``) allowed besides the
    server's own; ``allow_bypass``: requests may ask for
    ``bypassPermissions``; ``cwd``: tasks a request creates run in it or
    below it, or below one of ``roots`` (anywhere when ``None``)."""
    agents = agents if agents is not None else getattr(getattr(app, "runtime", None),
                                                       "agents", None)
    guarded = _GuardedClient(client, allow_bypass=allow_bypass, cwd=cwd, roots=roots)
    front = _GuardedFront(app if app is not None else _Front(client=client, deltas=deltas),
                          guarded)
    hosts = None if allowed_hosts is None else {h.lower() for h in allowed_hosts}
    allowed_origins = {o.rstrip("/").lower() for o in origins}

    def authorized(headers: Any, query: Any) -> bool:
        if not token:
            return True
        got = headers.get("authorization", "").removeprefix("Bearer ").strip() \
            or query.get("token", "")
        return hmac.compare_digest(got, token)

    def same_site(headers: Any) -> bool:
        """Whether the request comes from an allowed ``Host`` and, when a
        browser sent it, from this server's own page or an allowed one."""
        host = headers.get("host", "")
        if hosts is not None and _hostname(host).lower() not in hosts:
            return False
        origin = headers.get("origin")
        if origin is None:
            return True
        origin = origin.rstrip("/").lower()
        return origin in allowed_origins or origin.partition("://")[2] == host.lower()

    def guard(handler: Any) -> Any:
        async def wrapped(request: Request) -> Response:
            if not same_site(request.headers):
                return JSONResponse({"error": "cross-site request refused"}, status_code=403)
            if not authorized(request.headers, request.query_params):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            try:
                result: Response = await handler(request)
                return result
            except UnknownTask as exc:
                return JSONResponse({"error": f"unknown task {exc}"}, status_code=404)
            except Refused as exc:
                return JSONResponse({"error": str(exc)}, status_code=403)
            except (msgspec.ValidationError, ValueError, KeyError, IndexError) as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
        return wrapped

    async def create(request: Request) -> Response:
        body = await request.json()
        task_id = await guarded.create(
            body["agent"], _input(body.get("input")), config=body.get("config"),
            queue=body.get("queue", "default"), dedupe_key=body.get("dedupe_key"),
            history_from=body.get("history_from"), history_through=body.get("history_through"))
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
                typed = await run_typed(agent.all_extensions(),
                                        CommandContext(cwd="", task_id=task_id), value)
                if typed is not None and typed.local:
                    return _json({"output": typed.text})
                value = typed.text if typed is not None else value
        seq = await client.send(task_id, value,
                                when=body.get("when", "next_turn"),
                                dedupe_key=body.get("dedupe_key"))
        return _json({"inbox_seq": seq})

    async def answer(request: Request) -> Response:
        body = await request.json()
        seq = await guarded.answer(request.path_params["task_id"], body["request_id"],
                                  body.get("decision"), always=bool(body.get("always")),
                                  data=body.get("data"))
        return _json({"ok": True, "inbox_seq": seq})

    async def signal(request: Request) -> Response:
        body = await request.json()
        name = body["name"]
        if not isinstance(name, str) or not name:
            raise ValueError("a signal needs a non-empty 'name'")
        seq = await guarded.signal(request.path_params["task_id"], name, body.get("data"),
                                  dedupe_key=body.get("dedupe_key"))
        return _json({"ok": True, "inbox_seq": seq})

    async def cancel(request: Request) -> Response:
        body = await request.json() if await request.body() else {}
        await client.cancel(request.path_params["task_id"], body.get("reason", ""),
                            turn_only=bool(body.get("turn_only")))
        return _json({"ok": True})

    async def usage(request: Request) -> Response:
        task_id = request.path_params["task_id"]
        await client.get(task_id)
        total = await tree_usage(client, task_id)
        return _json({"tasks": total.tasks, "cost_usd": total.cost_usd,
                      "api_seconds": total.api_seconds, "usage": total.usage,
                      "by_model": {name: {"usage": m.usage, "calls": m.calls,
                                          "cost_usd": m.cost_usd, "api_seconds": m.api_seconds}
                                   for name, m in total.by_model.items()}})

    async def context(request: Request) -> Response:
        runtime = getattr(app, "runtime", None)
        if runtime is None:
            return _json({"error": "no workers run in this server process"}, 501)
        task_id = request.path_params["task_id"]
        await client.get(task_id)
        preview = await runtime.preview(task_id)
        if preview is None:
            return _json({"error": "this server does not run the task's agent"}, 501)
        return _json(context_breakdown(preview))

    async def rewind_list(request: Request) -> Response:
        task_id = request.path_params["task_id"]
        await client.get(task_id)
        return _json([{"index": i, "task_id": p.task_id, "seq": p.seq, "at": p.at,
                       "text": p.text, "files": list(p.files)}
                      for i, p in enumerate(await rewind_points(client, task_id))])

    async def rewind_post(request: Request) -> Response:
        task_id = request.path_params["task_id"]
        body = await request.json()
        points = await rewind_points(client, task_id)
        point = points[int(body["index"])]
        done = await rewind(client, task_id, point,
                            conversation=bool(body.get("conversation", True)),
                            files=bool(body.get("files", True)))
        if done.task_id != task_id and app is not None:
            await app.adopt(done.task_id)
        return _json({"task_id": done.task_id, "prompt": done.prompt, "files": list(done.files)})

    async def commands(request: Request) -> Response:
        q = request.query_params
        name = q.get("agent") or "main"
        if q.get("task_id"):
            name = (await client.get(q["task_id"])).agent
        agent = (agents or {}).get(name)
        if agent is None:
            return _json([])
        listed = await list_commands(agent, CommandContext(cwd="", task_id=q.get("task_id")))
        return _json([{"name": c.name, "description": c.description,
                       "argument_hint": c.argument_hint, "local": c.local} for c in listed])

    async def list_agents(request: Request) -> Response:
        return _json([{"name": a.name, "description": a.description, "model": a.model,
                       "one_shot": a.one_shot} for a in (agents or {}).values()])

    async def list_models(request: Request) -> Response:
        aliases: dict[str, list[str]] = {}
        for alias, real in models.ALIASES.items():
            aliases.setdefault(real, []).append(alias)
        return _json([{"id": name, "context_window": spec.context_window,
                       "max_output_tokens": spec.max_output_tokens,
                       "aliases": aliases.get(name, [])}
                      for name, spec in sorted(models.catalog_models().items())])

    async def openapi(request: Request) -> Response:
        return _json(OPENAPI)

    async def stream_post(request: Request) -> Response:
        from noeta.host.stream_json import StreamJson, drive

        options = _options(request.query_params, ask_client=False)
        guarded.check_config({"permission_mode": options.permission_mode})
        options.cwd = guarded.check_cwd(options.cwd)
        lines = [json.loads(x) for x in (await request.body()).decode().splitlines()
                 if x.strip()]
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        async def emit(message: dict[str, Any]) -> None:
            queue.put_nowait(json.dumps(message, ensure_ascii=False).encode() + b"\n")

        async def given() -> AsyncIterator[dict[str, Any]]:
            for line in lines:
                yield line

        async def run() -> None:
            try:
                await drive(StreamJson(front, options, emit), lines=given())
            finally:
                queue.put_nowait(None)

        async def body() -> AsyncIterator[bytes]:
            running = asyncio.create_task(run())
            try:
                while (chunk := await queue.get()) is not None:
                    yield chunk
            finally:
                running.cancel()
                with contextlib.suppress(BaseException):
                    await running

        return StreamingResponse(body(), media_type="application/x-ndjson")

    async def stream_socket(ws: WebSocket) -> None:
        from noeta.host.stream_json import StreamJson, drive

        if not same_site(ws.headers):
            await ws.close(code=4403)
            return
        if not authorized(ws.headers, ws.query_params):
            await ws.close(code=4401)
            return
        options = _options(ws.query_params, ask_client=True)
        try:
            guarded.check_config({"permission_mode": options.permission_mode})
            options.cwd = guarded.check_cwd(options.cwd)
        except Refused:
            await ws.close(code=4403)
            return
        await ws.accept()

        async def emit(message: dict[str, Any]) -> None:
            await ws.send_text(json.dumps(message, ensure_ascii=False))

        async def lines() -> AsyncIterator[dict[str, Any]]:
            with contextlib.suppress(WebSocketDisconnect):
                while True:
                    value = json.loads(await ws.receive_text())
                    if isinstance(value, dict):
                        yield value

        try:
            await drive(StreamJson(front, options, emit), lines=lines())
        except UnknownTask as exc:
            await ws.send_text(json.dumps({"type": "error", "error": f"unknown task {exc}"}))
        with contextlib.suppress(Exception):
            await ws.close()

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
        Route("/tasks/{task_id}/usage", guard(usage), methods=["GET"]),
        Route("/tasks/{task_id}/context", guard(context), methods=["GET"]),
        Route("/tasks/{task_id}/rewind", guard(rewind_list), methods=["GET"]),
        Route("/tasks/{task_id}/rewind", guard(rewind_post), methods=["POST"]),
        Route("/commands", guard(commands), methods=["GET"]),
        Route("/agents", guard(list_agents), methods=["GET"]),
        Route("/models", guard(list_models), methods=["GET"]),
        Route("/openapi.json", guard(openapi), methods=["GET"]),
        Route("/stream-json", guard(stream_post), methods=["POST"]),
        WebSocketRoute("/stream-json", stream_socket),
    ])


def _options(query: Any, *, ask_client: bool) -> Any:
    from noeta.host.stream_json import HeadlessOptions

    def flag(name: str) -> bool:
        return str(query.get(name, "")).lower() in ("1", "true", "yes")

    max_turns = query.get("max_turns")
    return HeadlessOptions(
        agent=query.get("agent") or "main", model=query.get("model") or "",
        cwd=query.get("cwd") or "", permission_mode=query.get("permission_mode") or None,
        max_turns=int(max_turns) if max_turns else None,
        include_partial_messages=flag("include_partial_messages"),
        replay_user_messages=flag("replay_user_messages"),
        ask_client=ask_client, resume=query.get("resume") or None, fork=flag("fork"))


def _op(summary: str, *, body: dict[str, Any] | None = None, params: tuple[str, ...] = (),
        query: tuple[str, ...] = ()) -> dict[str, Any]:
    op: dict[str, Any] = {"summary": summary, "responses": {
        "200": {"description": "OK", "content": {"application/json": {"schema": {}}}},
        "401": {"description": "Missing or wrong bearer token"},
        "404": {"description": "Unknown task"}}}
    parameters = [{"name": p, "in": "path", "required": True, "schema": {"type": "string"}}
                  for p in params]
    parameters += [{"name": q, "in": "query", "required": False, "schema": {"type": "string"}}
                   for q in query]
    if parameters:
        op["parameters"] = parameters
    if body is not None:
        op["requestBody"] = {"required": True, "content": {"application/json": {
            "schema": {"type": "object", **body}}}}
    return op


def _props(*required: str, **optional: str) -> dict[str, Any]:
    props: dict[str, Any] = {name: {"type": "string"} for name in required}
    props.update({name: {"type": kind} for name, kind in optional.items()})
    return {"properties": props, "required": list(required)}


_TASK = ("task_id",)
#: The API above as an OpenAPI 3.1 document (``GET /openapi.json``).
OPENAPI: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "noeta", "version": "1",
             "description": "Durable AI agents: every task is an append-only event log."},
    "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
    "security": [{"bearer": []}],
    "paths": {
        "/tasks": {
            "post": _op("Create a task", body=_props(
                "agent", input="string", config="object", queue="string",
                dedupe_key="string", history_from="string", history_through="integer")),
            "get": _op("List tasks", query=("parent_id", "status", "limit"))},
        "/tasks/{task_id}": {"get": _op("A task's row", params=_TASK)},
        "/tasks/{task_id}/events": {"get": _op("A task's events", params=_TASK,
                                               query=("after",))},
        "/tasks/{task_id}/pending": {"get": _op("Approvals and questions waiting on a person",
                                                params=_TASK)},
        "/tasks/{task_id}/watch": {"get": _op("Events and live output as server-sent events",
                                              params=_TASK, query=("after",))},
        "/tasks/{task_id}/messages": {"post": _op(
            "Send a message (or run a slash command)", params=_TASK,
            body=_props("input", when="string", dedupe_key="string"))},
        "/tasks/{task_id}/answers": {"post": _op(
            "Answer an approval or a question", params=_TASK,
            body=_props("request_id", decision="string", always="boolean", data="object"))},
        "/tasks/{task_id}/signals": {"post": _op(
            "Post a signal (permission_mode, config)", params=_TASK,
            body=_props("name", data="object", dedupe_key="string"))},
        "/tasks/{task_id}/cancel": {"post": _op(
            "End the task, or with turn_only stop the turn", params=_TASK,
            body=_props(reason="string", turn_only="boolean"))},
        "/tasks/{task_id}/usage": {"get": _op("Tokens and cost of the task tree",
                                              params=_TASK)},
        "/tasks/{task_id}/context": {"get": _op("What fills the next request", params=_TASK)},
        "/tasks/{task_id}/rewind": {
            "get": _op("The prompts a rewind can go back to", params=_TASK),
            "post": _op("Rewind the conversation and/or files to before a prompt",
                        params=_TASK,
                        body={"properties": {"index": {"type": "integer"},
                                             "conversation": {"type": "boolean"},
                                             "files": {"type": "boolean"}},
                              "required": ["index"]})},
        "/commands": {"get": _op("Slash commands an agent offers", query=("agent", "task_id"))},
        "/agents": {"get": _op("The agents this server runs")},
        "/models": {"get": _op("The model catalog")},
        "/openapi.json": {"get": _op("This document")},
        "/stream-json": {"post": _op(
            "Claude Code's headless protocol, one-way (JSON lines in the body and the "
            "reply); the same over a WebSocket at this path is two-way",
            query=("resume", "fork", "agent", "model", "cwd", "permission_mode", "max_turns",
                   "include_partial_messages", "replay_user_messages"))},
    },
}
