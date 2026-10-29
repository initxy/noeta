"""The request/response handlers of ``noeta serve``: tasks, their pending
cards, messages, answers, signals, usage, context, rewind, commands, agents
and models. Each runs behind :class:`~noeta.host._http_guard.Guard`, which
sets the request's tenant scope."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

import msgspec
from starlette.requests import Request
from starlette.responses import Response

from noeta.core import models
from noeta.core.agent import Agent
from noeta.core.events import DEFAULT_TENANT, TaskCreated
from noeta.core.extension import CommandContext
from noeta.core.types import Image, Message, Text
from noeta.host._http_guard import REQUEST_CONFIG, scope
from noeta.host._http_openapi import OPENAPI
from noeta.host.app import AppAPI
from noeta.host.pending import card_title, choices, pending_tree
from noeta.host.rewind import rewind, rewind_points
from noeta.host.usage import tree_usage

_enc = msgspec.json.Encoder()


def _json(value: Any, status: int = 200) -> Response:
    return Response(_enc.encode(value), status_code=status, media_type="application/json")


def _input(value: Any) -> Any:
    """A client's input: text, or a user turn of text and images, always
    the person's (no other role, origin or block: a client never writes
    the conversation's other parts)."""
    if value is None or isinstance(value, str):
        return value
    message = msgspec.convert(value, Message)
    if message.role != "user":
        raise ValueError("an input message has the role 'user'")
    if not all(isinstance(b, (Text, Image)) for b in message.content):
        raise ValueError("an input message holds only text and image blocks")
    return Message(role="user", content=message.content, origin="human")


async def _command_place(request: Request, agent: str | None, task_id: str | None,
                         cwd: str | None) -> tuple[str, CommandContext]:
    """The agent and context a slash command is listed or run in: a task's
    own agent and the ``cwd`` it was created with, else the request's
    ``cwd`` checked as a new task's would be (commands read the
    directory's skills and may start its MCP servers), for the request's
    tenant."""
    tenant = getattr(scope(request).client, "tenant", None) or DEFAULT_TENANT
    if task_id:
        mine = scope(request).client
        row = await mine.get(task_id)
        first = (await mine.events(task_id, 0))[:1]
        made = first[0].event if first else None
        where = made.config.get("cwd") if isinstance(made, TaskCreated) else None
        return row.agent, CommandContext(cwd=str(where or ""), task_id=task_id, tenant=tenant)
    where = scope(request).guarded.check_cwd(cwd) or scope(request).guarded.workspace or ""
    return agent or "main", CommandContext(cwd=str(where), tenant=tenant)


class Routes:
    """The handlers, on the front they serve: ``serving`` runs slash commands
    and previews context, ``app`` (the ``LocalApp`` whose workers run here,
    if any) adopts a rewind's new task, ``agents`` are listed."""

    def __init__(self, serving: AppAPI, app: Any, agents: Mapping[str, Agent] | None) -> None:
        self._serving = serving
        self._app = app
        self._agents = agents

    async def create(self, request: Request) -> Response:
        body = await request.json()
        task_id = await scope(request).guarded.create(
            body["agent"], _input(body.get("input")), config=body.get("config"),
            queue=body.get("queue", "default"), dedupe_key=body.get("dedupe_key"),
            history_from=body.get("history_from"), history_through=body.get("history_through"))
        return _json({"task_id": task_id}, 201)

    async def list_tasks(self, request: Request) -> Response:
        q = request.query_params
        rows = await scope(request).client.tasks(
            parent_id=q.get("parent_id"), status=q.get("status"), limit=int(q.get("limit", "100")))
        return _json(rows)

    async def get_task(self, request: Request) -> Response:
        return _json(await scope(request).client.get(request.path_params["task_id"]))

    async def events(self, request: Request) -> Response:
        after = int(request.query_params.get("after", "0"))
        return _json(await scope(request).client.events(request.path_params["task_id"], after))

    async def pending(self, request: Request) -> Response:
        task_id = request.path_params["task_id"]
        out = []
        for item in await pending_tree(scope(request).client, task_id):
            row = dataclasses.asdict(item)
            row["title"] = card_title(item, task_id)
            if item.kind == "approval":
                row["choices"] = choices(item.detail)
            out.append(row)
        return _json(out)

    async def send(self, request: Request) -> Response:
        body = await request.json()
        task_id = request.path_params["task_id"]
        value = _input(body["input"])
        if isinstance(value, str) and value.startswith("/"):
            agent, ctx = await _command_place(request, "", task_id, None)
            typed = await self._serving.run_command(agent, ctx, value)
            if typed is not None and typed.local:
                return _json({"output": typed.text})
            value = typed.text if typed is not None else value
        seq = await scope(request).guarded.send(task_id, value,
                                 when=body.get("when", "next_turn"),
                                 dedupe_key=body.get("dedupe_key"))
        return _json({"inbox_seq": seq})

    async def answer(self, request: Request) -> Response:
        body = await request.json()
        seq = await scope(request).guarded.answer(
            request.path_params["task_id"], body["request_id"], body.get("decision"),
            always=bool(body.get("always")), data=body.get("data"))
        return _json({"ok": True, "inbox_seq": seq})

    async def signal(self, request: Request) -> Response:
        body = await request.json()
        name = body["name"]
        if not isinstance(name, str) or not name:
            raise ValueError("a signal needs a non-empty 'name'")
        seq = await scope(request).guarded.signal(
            request.path_params["task_id"], name, body.get("data"),
            dedupe_key=body.get("dedupe_key"))
        return _json({"ok": True, "inbox_seq": seq})

    async def cancel(self, request: Request) -> Response:
        body = await request.json() if await request.body() else {}
        await scope(request).guarded.cancel(
            request.path_params["task_id"], body.get("reason", ""),
            turn_only=bool(body.get("turn_only")))
        return _json({"ok": True})

    async def usage(self, request: Request) -> Response:
        task_id = request.path_params["task_id"]
        await scope(request).client.get(task_id)
        total = await tree_usage(scope(request).client, task_id)
        return _json({"tasks": total.tasks, "cost_usd": total.cost_usd,
                      "api_seconds": total.api_seconds, "usage": total.usage,
                      "by_model": {name: {"usage": m.usage, "calls": m.calls,
                                          "cost_usd": m.cost_usd, "api_seconds": m.api_seconds}
                                   for name, m in total.by_model.items()}})

    async def context(self, request: Request) -> Response:
        task_id = request.path_params["task_id"]
        await scope(request).client.get(task_id)
        parts = await self._serving.context(task_id)
        if parts is None:
            return _json({"error": "no worker in this server process runs the task's agent"},
                         501)
        return _json(parts)

    async def rewind_list(self, request: Request) -> Response:
        task_id = request.path_params["task_id"]
        await scope(request).client.get(task_id)
        return _json([{"index": i, "task_id": p.task_id, "seq": p.seq, "at": p.at,
                       "text": p.text, "files": list(p.files)}
                      for i, p in enumerate(await rewind_points(scope(request).client, task_id))])

    async def rewind_post(self, request: Request) -> Response:
        task_id = request.path_params["task_id"]
        body = await request.json()
        points = await rewind_points(scope(request).client, task_id)
        point = points[int(body["index"])]
        # The new task is a request's: the guard's checks and the tenant's
        # queues, and only the config a request may set.
        done = await rewind(scope(request).guarded, task_id, point,
                            conversation=bool(body.get("conversation", True)),
                            files=bool(body.get("files", True)), config_keys=REQUEST_CONFIG)
        if done.task_id != task_id and self._app is not None:
            await self._app.adopt(done.task_id)
        return _json({"task_id": done.task_id, "prompt": done.prompt, "files": list(done.files)})

    async def commands(self, request: Request) -> Response:
        q = request.query_params
        name, ctx = await _command_place(request, q.get("agent"), q.get("task_id"),
                                         q.get("cwd"))
        listed = await self._serving.commands(name, ctx)
        return _json([{"name": c.name, "description": c.description,
                       "argument_hint": c.argument_hint, "local": c.local} for c in listed])

    async def run_command(self, request: Request) -> Response:
        body = await request.json()
        name, ctx = await _command_place(request, body.get("agent"), body.get("task_id"),
                                         body.get("cwd"))
        typed = await self._serving.run_command(name, ctx, str(body["text"]))
        return _json(None if typed is None else {"text": typed.text, "local": typed.local})

    async def list_agents(self, request: Request) -> Response:
        return _json([{"name": a.name, "description": a.description, "model": a.model,
                       "one_shot": a.one_shot} for a in (self._agents or {}).values()])

    async def list_models(self, request: Request) -> Response:
        aliases: dict[str, list[str]] = {}
        for alias, real in models.ALIASES.items():
            aliases.setdefault(real, []).append(alias)
        return _json([{"id": name, "context_window": spec.context_window,
                       "max_output_tokens": spec.max_output_tokens,
                       "aliases": aliases.get(name, [])}
                      for name, spec in sorted(models.catalog_models().items())])

    async def openapi(self, request: Request) -> Response:
        return _json(OPENAPI)
