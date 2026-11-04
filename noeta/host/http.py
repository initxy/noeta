"""``noeta serve``: the client API over HTTP, so a TUI, a web page or another
service can drive tasks on another machine. Events stream as server-sent
events; when this process also runs workers, live model output streams too.

    POST /tasks                       {agent, input?, config?, queue?, dedupe_key?,
                                      history_from?, history_through?} -> {task_id};
                                      config takes only the keys in _http_guard's
                                      REQUEST_CONFIG (any other: 403)
    GET  /tasks?parent_id=&status=&limit=
    GET  /summaries?limit=&archived=  the root tasks, each with its title, cwd, first
                                      prompt and ``archived`` (``noeta.host.summaries``);
                                      archived=true / false keeps only those / the others
    GET  /files?q=&task_id=&cwd=&limit=  paths under a task's (or a cwd's) directory
                                      holding ``q``, for a prompt's ``@`` mentions
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
    POST /tasks/{id}/signals          {name, data?, dedupe_key?, wake?} — a signal (``wake``
                                      false: a waiting task stays waiting): ``permission_mode``
                                      {mode} switches the permission mode, ``config``
                                      {model?, effort?, max_turns?} changes the task's config;
                                      ``resumed``, ``restore_files``, ``rename`` {title} and
                                      ``archive`` {archived} as front ends send them; any
                                      other name: 403
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

With ``web_dir`` (``noeta serve --web DIR``) the other paths serve that directory's
files, the built web app (``web/``), without a token: it holds none. They go with
:data:`WEB_HEADERS`: a content security policy (the page's scripts, styles and requests
are its own server's; images also ``data:`` and ``blob:``) and no framing.

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
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import unquote_plus

from starlette.applications import Starlette
from starlette.routing import BaseRoute, Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles

from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.commands import Typed
from noeta.core.extension import Command, CommandContext
from noeta.host._http_guard import LOOPBACK_HOSTS, Guard, Refused, token_digest
from noeta.host._http_openapi import OPENAPI
from noeta.host._http_routes import Routes
from noeta.host._http_stream import StreamRoutes
from noeta.host.app import AppAPI, agent_commands, agent_run_command, archive_task
from noeta.host.summaries import root_summaries

__all__ = ["LOOPBACK_HOSTS", "OPENAPI", "WEB_HEADERS", "Refused", "create_app",
           "redact_token_logs", "token_digest"]


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

    async def summaries(self, limit: int = 50, *,
                        archived: bool | None = None) -> list[dict[str, Any]]:
        return await root_summaries(self.client, limit, inbox=self.client.store.inbox,
                                    archived=archived)

    async def archive(self, task_id: str, archived: bool = True) -> None:
        await archive_task(self.client, task_id, archived)


#: A query parameter: its separator and key, and its value.
_QUERY_PARAM = re.compile(r"([?&][^=&\s\"']*=)([^&\s\"']*)")


def _redact(text: str) -> str:
    """``text`` with the value of each ``token`` query parameter (the
    WebSocket's ``?token=``) replaced, its key matched as the server
    decodes it (``%74oken``)."""
    def one(m: re.Match[str]) -> str:
        key = unquote_plus(m.group(1)[1:-1]).lower()
        return m.group(1) + "REDACTED" if key == "token" else m.group(0)
    return _QUERY_PARAM.sub(one, text)


#: The loggers uvicorn names a request's path in.
_UVICORN_LOGGERS = ("uvicorn.access", "uvicorn.error")

#: The headers every file of ``web_dir`` is served with.
WEB_HEADERS = (
    (b"content-security-policy",
     b"default-src 'self'; img-src 'self' data: blob:; connect-src 'self'; "
     b"frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
)


class _WebFiles(StaticFiles):
    """The web app's files, each with :data:`WEB_HEADERS`."""

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        async def with_headers(message: Any) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", ()), *WEB_HEADERS]
            await send(message)
        await super().__call__(scope, receive, with_headers)


class _RedactQueryToken(logging.Filter):
    """Replaces a ``token=`` query value in a record's message and string
    arguments (uvicorn's access formatter reads the arguments one by one)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact(a) if isinstance(a, str) else a
                                for a in record.args)
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


def create_app(client: Client, *, token: str | None = None, token_sha256: str | None = None,
               auth: bool = True, deltas: Any = None, agents: Mapping[str, Agent] | None = None,
               app: Any = None, allowed_hosts: Sequence[str] | None = None,
               origins: Sequence[str] = (), allow_bypass: bool = False,
               cwd: str | None = None, roots: Sequence[str] = (),
               web_dir: str | None = None) -> Starlette:
    """``deltas`` is ``LocalApp.deltas`` when workers run in this process;
    ``agents`` (by name) lets a message run its task agent's slash commands;
    ``app`` (the ``LocalApp`` whose workers run here) previews requests
    (``/context``) and serves new stream-json conversations' trees.

    Every request needs the bearer token: ``token``, or the token whose
    SHA-256 is ``token_sha256`` (what ``noeta serve`` keeps). Serving
    without a token takes ``auth=False`` (a loopback-only development
    server). Serve it after :func:`redact_token_logs`, or uvicorn logs the
    WebSocket's ``?token=``.

    ``allowed_hosts``: the ``Host`` names served (any when ``None``);
    ``origins``: web origins (``https://host:port``) allowed besides the
    server's own; ``allow_bypass``: requests may ask for
    ``bypassPermissions``; ``cwd``: tasks a request creates run in it or
    below it, or below one of ``roots`` (anywhere when ``None``); ``web_dir``:
    a directory of static files served at every other path (a web app)."""
    if auth and not token and not token_sha256:
        raise ValueError("create_app needs a bearer token (token= or token_sha256=); pass "
                         "auth=False to serve every request without one")
    if token:
        token_sha256 = token_digest(token)
    agents = agents if agents is not None else getattr(getattr(app, "runtime", None),
                                                       "agents", None)
    serving: AppAPI = app if app is not None else _Front(client=client, agents=agents or {},
                                                          live=deltas)
    guard = Guard(client, serving, token_sha256=token_sha256, auth=auth,
                  allowed_hosts=allowed_hosts, origins=origins, allow_bypass=allow_bypass,
                  cwd=cwd, roots=roots)
    r = Routes(serving, app, agents)
    s = StreamRoutes(guard, deltas)
    routes: list[BaseRoute] = [
        Route("/tasks", guard(r.create), methods=["POST"]),
        Route("/tasks", guard(r.list_tasks), methods=["GET"]),
        Route("/summaries", guard(r.summaries), methods=["GET"]),
        Route("/files", guard(r.files), methods=["GET"]),
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
    ]
    if web_dir is not None:
        routes.append(Mount("/", _WebFiles(directory=web_dir, html=True)))
    return Starlette(routes=routes)
