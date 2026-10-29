"""``noeta serve``: the client API over HTTP, so a TUI, a web page or another
service can drive tasks on another machine. Events stream as server-sent
events; when this process also runs workers, live model output streams too.

    POST /tasks                       {agent, input?, config?, queue?, dedupe_key?,
                                      history_from?, history_through?} -> {task_id};
                                      config takes only model, effort, max_turns, cwd,
                                      permission_mode and output_schema (any other: 403)
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
                                      {model?, effort?, max_turns?} changes the task's config;
                                      ``resumed`` and ``restore_files`` as front ends send
                                      them; any other name: 403
    POST /tasks/{id}/cancel           {reason?, turn_only?}
    GET  /tasks/{id}/usage            the task tree's tokens, model calls and cost by model
    GET  /tasks/{id}/context          what fills the next request (workers in this process)
    GET  /tasks/{id}/rewind           the person's prompts, each with the files changed since
    POST /tasks/{id}/rewind           {index, conversation?, files?} -> {task_id, prompt, files}
    GET  /commands?agent=&task_id=&cwd=  slash commands an agent's plugins offer
    POST /commands/run                {text, agent?, task_id?, cwd?} -> {text, local} or null:
                                      a "/name args" run as the agent's command, nothing sent
    GET  /agents                      the agents this server runs
    GET  /models                      the model catalog and its aliases
    GET  /openapi.json                this API as an OpenAPI 3.1 document
    WS   /stream-json                 Claude Code's headless protocol, one JSON message per
                                      frame (``noeta.host.stream_json``); query: resume, fork,
                                      agent, model, cwd, permission_mode, max_turns,
                                      include_partial_messages, replay_user_messages
    POST /stream-json                 the same, one-way: the request body's JSON lines in,
                                      the reply's JSON lines out; permission prompts refused

A bearer token is required (``NOETA_SERVER_TOKEN``; ``?token=`` for a
WebSocket from a browser) unless the host turns auth off. A request from a web page of
another origin is refused, and so is one whose ``Host`` is not in
``allowed_hosts`` (DNS rebinding). No request starts or switches a task into
``bypassPermissions`` unless ``allow_bypass``, or runs it outside ``cwd`` and
the ``roots`` (a relative ``cwd`` is taken from ``cwd``). Needs the ``server`` extra.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from starlette.applications import Starlette
from starlette.routing import Route, WebSocketRoute

from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.commands import Typed
from noeta.core.extension import Command, CommandContext
from noeta.host._http_guard import LOOPBACK_HOSTS, Guard, Refused
from noeta.host._http_openapi import OPENAPI
from noeta.host._http_routes import Routes
from noeta.host._http_stream import StreamRoutes
from noeta.host.app import AppAPI, agent_commands, agent_run_command

__all__ = ["LOOPBACK_HOSTS", "OPENAPI", "Refused", "create_app", "redact_token_logs"]


@dataclasses.dataclass
class _Front:
    """What a stream-json conversation runs on when this server has no
    ``LocalApp`` of its own (API only): the client, the agents' commands,
    and deltas if any."""

    client: Any
    agents: Mapping[str, Agent]
    live: Any = None

    def deltas(self, sink: Any) -> Any:
        return self.live(sink) if self.live is not None else (lambda: None)

    async def adopt(self, task_id: str) -> None:
        return None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def agent_names(self) -> list[str]:
        return sorted(self.agents)

    async def commands(self, agent: str, ctx: CommandContext) -> list[Command]:
        return await agent_commands(self.agents, agent, ctx)

    async def run_command(self, agent: str, ctx: CommandContext, text: str) -> Typed | None:
        return await agent_run_command(self.agents, agent, ctx, text)

    async def context(self, task_id: str) -> dict[str, Any] | None:
        return None


#: A bearer token in a query string (the WebSocket's ``?token=``).
_QUERY_TOKEN = re.compile(r"(?i)([?&]token=)[^&\s\"']*")
#: The loggers uvicorn names a request's path in.
_UVICORN_LOGGERS = ("uvicorn.access", "uvicorn.error")


class _RedactQueryToken(logging.Filter):
    """Replaces a ``token=`` query value in a record's message and string
    arguments (uvicorn's access formatter reads the arguments one by one)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _QUERY_TOKEN.sub(r"\1REDACTED", record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_QUERY_TOKEN.sub(r"\1REDACTED", a) if isinstance(a, str)
                                else a for a in record.args)
        return True


def redact_token_logs() -> None:
    """Keep ``?token=`` values out of uvicorn's access and error logs (it logs
    a WebSocket's path with its query at info). Call it after making the
    ``uvicorn.Config`` that serves :func:`create_app`; calling it again adds
    nothing."""
    for name in _UVICORN_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, _RedactQueryToken) for f in logger.filters):
            logger.addFilter(_RedactQueryToken())


def create_app(client: Client, *, token: str | None = None,
               tokens: Mapping[str, str] | Callable[[str], str | None] | None = None,
               auth: bool = True, deltas: Any = None, agents: Mapping[str, Agent] | None = None,
               app: Any = None, allowed_hosts: Sequence[str] | None = None,
               origins: Sequence[str] = (), allow_bypass: bool = False,
               cwd: str | None = None, roots: Sequence[str] = (),
               workspaces: Mapping[str, str] | None = None,
               queues: Mapping[str, Sequence[str]] | None = None) -> Starlette:
    """``deltas`` is ``LocalApp.deltas`` when workers run in this process;
    ``agents`` (by name) lets a message run its task agent's slash commands;
    ``app`` (the ``LocalApp`` whose workers run here) previews requests
    (``/context``) and serves new stream-json conversations' trees.

    Every request needs a bearer token: ``token`` is the default tenant's,
    ``tokens`` maps more tokens to their tenants (or finds a token's
    tenant). A request reaches only its tenant's tasks (``Client.scoped``);
    another tenant's task answers 404. Serving without a token takes
    ``auth=False`` (a loopback-only development server; the default
    tenant). Serve it after :func:`redact_token_logs`, or uvicorn logs the
    WebSocket's ``?token=``.

    ``allowed_hosts``: the ``Host`` names served (any when ``None``);
    ``origins``: web origins (``https://host:port``) allowed besides the
    server's own; ``allow_bypass``: requests may ask for
    ``bypassPermissions``; ``cwd``: tasks a request creates run in it or
    below it, or below one of ``roots`` (anywhere when ``None``);
    ``workspaces``: per tenant, the directory its tasks run in or below
    (instead of ``cwd`` and ``roots``), and by default; ``queues``: per
    tenant, the queues its requests may create tasks on (a tenant not
    named: ``default`` and the queue of its own name; the default tenant:
    any)."""
    if auth and not token and not tokens:
        raise ValueError("create_app needs a bearer token (token= or tokens=); pass "
                         "auth=False to serve every request without one")
    if tokens and not hasattr(client, "scoped"):
        raise ValueError("tenant tokens need a client that can be scoped to a tenant")
    agents = agents if agents is not None else getattr(getattr(app, "runtime", None),
                                                       "agents", None)
    serving: AppAPI = app if app is not None else _Front(client=client, agents=agents or {},
                                                          live=deltas)
    guard = Guard(client, serving, token=token, tokens=tokens, auth=auth,
                  allowed_hosts=allowed_hosts, origins=origins, allow_bypass=allow_bypass,
                  cwd=cwd, roots=roots, workspaces=workspaces, queues=queues)
    r = Routes(serving, app, agents)
    s = StreamRoutes(guard, deltas)
    return Starlette(routes=[
        Route("/tasks", guard(r.create), methods=["POST"]),
        Route("/tasks", guard(r.list_tasks), methods=["GET"]),
        Route("/tasks/{task_id}", guard(r.get_task), methods=["GET"]),
        Route("/tasks/{task_id}/events", guard(r.events), methods=["GET"]),
        Route("/tasks/{task_id}/pending", guard(r.pending), methods=["GET"]),
        Route("/tasks/{task_id}/watch", guard(s.watch), methods=["GET"]),
        Route("/tasks/{task_id}/messages", guard(r.send), methods=["POST"]),
        Route("/tasks/{task_id}/answers", guard(r.answer), methods=["POST"]),
        Route("/tasks/{task_id}/signals", guard(r.signal), methods=["POST"]),
        Route("/tasks/{task_id}/cancel", guard(r.cancel), methods=["POST"]),
        Route("/tasks/{task_id}/usage", guard(r.usage), methods=["GET"]),
        Route("/tasks/{task_id}/context", guard(r.context), methods=["GET"]),
        Route("/tasks/{task_id}/rewind", guard(r.rewind_list), methods=["GET"]),
        Route("/tasks/{task_id}/rewind", guard(r.rewind_post), methods=["POST"]),
        Route("/commands", guard(r.commands), methods=["GET"]),
        Route("/commands/run", guard(r.run_command), methods=["POST"]),
        Route("/agents", guard(r.list_agents), methods=["GET"]),
        Route("/models", guard(r.list_models), methods=["GET"]),
        Route("/openapi.json", guard(r.openapi), methods=["GET"]),
        Route("/stream-json", guard(s.stream_post), methods=["POST"]),
        WebSocketRoute("/stream-json", s.stream_socket),
    ])
