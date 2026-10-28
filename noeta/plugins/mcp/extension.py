"""The ``Mcp`` extension: tools from MCP servers, as ordinary tools.

How a task's MCP tool set is made and kept:

* **Listed at the start of each turn, recorded, then fixed.** When a turn
  opens (and again when a runner picks a task up at that point), every
  configured server is asked for ``tools/list`` and the result is recorded as
  one ``listed`` entry — only when it differs from the last one. ``tools()``
  is derived from that record alone, so the tool list, and the request
  prefix with it, is byte-stable for the whole turn, and a server-side change
  shows at the next turn.
* **A resumed task keeps its tools.** The tool list comes from the log, not
  from process memory: a task resumed in a fresh process (a restart, another
  worker, an approval pending across a deploy) offers the same tools and its
  calls reconnect on demand. The server list itself comes from the ``servers``
  callable, evaluated on the task each time a runner picks it up — so it must
  be derivable from the task (its config, its root id), never from state only
  the creating process had.
* **Credentials never reach the log.** The record holds aliases and tool
  schemas only; commands, urls, env and headers live in the ``McpServer``
  values the host's callable returns.
* **A dead server is dropped for the turn** and reported once per outage
  (``server_down`` / ``server_up`` records, for front ends).
* **A server's notice that its tools changed** (``list_changed``) is acted
  on after the call that brought it: the server is listed again and the new
  set recorded with the call's result, so the next step offers it.
* **A server that needs a sign-in** offers ``authenticate`` /
  ``complete_authentication`` in place of its tools
  (:mod:`noeta.plugins.mcp.oauth`); the sign-in runs in the worker that
  started it, and the server's tools replace them once it succeeds.
* **A server's question (elicitation) parks the call**: the call is kept
  running in this worker (the task tree held here) while the person
  answers the question it was turned into; the answer goes back to the
  server and the call finishes. A second question in the same call, or a
  question that cannot be put to anyone, is declined. A call whose worker
  died while it waited ends "outcome unknown".
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import urllib.parse
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Union

from noeta.core.events import Recorded, TaskEnded, Waiting
from noeta.core.extension import (
    Command,
    CommandContext,
    Extension,
    TaskView,
)
from noeta.core.questions import ask
from noeta.core.tool import Park, Tool, ToolContext, ToolOutput, text_output
from noeta.plugins.mcp.admin import McpAdmin
from noeta.plugins.mcp.client import McpClient, RequestContext
from noeta.plugins.mcp.config import (
    McpAuthRequired,
    McpConfigError,
    McpError,
    McpServer,
    describe_tool,
    map_server_names,
    tool_timeout_s,
)
from noeta.plugins.mcp.elicitation import elicitation_questions, elicitation_result
from noeta.plugins.mcp.oauth import SignIn, store_for
from noeta.plugins.mcp.pool import McpPool, default_pool
from noeta.plugins.mcp.prompts import McpPrompt, expand_prompt, list_prompts
from noeta.plugins.mcp.results import render_result
from noeta.plugins.mcp.settings import read_servers, servers_from
from noeta.plugins.mcp.tools import (
    ListMcpResourcesTool,
    McpAuthTool,
    McpTool,
    ReadMcpResourceDirTool,
    ReadMcpResourceTool,
    auth_stub_entries,
)

__all__ = ["Mcp", "ServersFn"]

log = logging.getLogger("noeta.plugins.mcp")

#: Servers as objects, or in Claude Code's ``mcpServers`` shape (name ->
#: ``{"command", "args", "env"}`` or ``{"type": "http", "url", "headers"}``).
Servers = Union[Sequence[McpServer], Mapping[str, Any]]
ServersFn = Callable[[TaskView], Union[Servers, Awaitable[Servers]]]
ScopeFn = Callable[[TaskView], Union[str, None]]


#: A sign-in started within this long is handed out again rather than
#: restarted.
SIGN_IN_REUSE_S = 180.0


def _as_list(servers: Servers) -> list[McpServer]:
    if isinstance(servers, Mapping):
        return servers_from({"mcpServers": servers}, "servers")
    return list(servers)


@dataclass
class _Call:
    """A ``tools/call`` in flight, and the server questions it brought."""

    job: asyncio.Task[tuple[dict[str, Any], list[dict[str, Any]] | None]] | None = None
    questions: asyncio.Queue[tuple[dict[str, Any], asyncio.Future[dict[str, Any]]]] = field(
        default_factory=asyncio.Queue)

    async def elicit(self, params: dict[str, Any]) -> dict[str, Any]:
        answer: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        await self.questions.put((params, answer))
        return await answer


@dataclass
class _Parked:
    call: _Call
    params: dict[str, Any]
    answer: asyncio.Future[dict[str, Any]]
    asked: Park
    release: Callable[[], Awaitable[None]] | None


@dataclass
class _SignInRun:
    sign_in: SignIn
    url: str
    started: float
    done: asyncio.Task[None]


class Mcp(Extension):
    """Tools from MCP servers.

    ``servers``: the servers a task may use — a fixed list (or a mapping in
    Claude Code's ``mcpServers`` shape), or a function of the task returning
    one (sync or async), evaluated each time a runner picks the task up. It
    replaces 0.6's ``Options.mcp_servers`` and
    ``HostConfig.mcp_server_resolver``; derive it from the task (its config,
    ``state.root_id`` for a child) so any worker gets the same answer.

    ``scope``: partitions shared connections (0.6's ``mcp_scope_resolver``):
    a string, or ``TaskView -> str | None``. Tasks naming the same server in
    the same scope share one connection per worker process; give each tenant
    its own scope so a stateful server (a browser, a login) never carries one
    tenant's state into another's task. ``None`` = one shared partition.

    ``pool``: the connection pool (default: one per worker process and event
    loop, idle connections closed after 30 minutes).

    ``settings_files``: settings files whose ``mcpServers`` are added, in
    Claude Code's format (:mod:`noeta.plugins.mcp.settings`), read on the
    worker each pick-up; with ``project`` also the task's ``<cwd>/.noeta/mcp.json``
    servers its own settings files consent to. ``project`` is ``True`` (the
    host trusts every task's working directory) or a function of the
    directory saying whether it is trusted; an untrusted directory's servers
    never start. ``servers`` wins over both on an alias.

    ``agent_servers``: an agent file's own ``mcpServers`` (Claude Code's
    shape), for the tasks of that agent only; they win over every other
    source on an alias.
    """

    name = "mcp"
    #: What ``noeta mcp`` asks of the plugin (checks, sign-ins).
    admin = McpAdmin

    def __init__(self, servers: Servers | ServersFn = (), *,
                 scope: str | ScopeFn | None = None, pool: McpPool | None = None,
                 settings_files: Sequence[str] = (),
                 project: bool | Callable[[str], bool] = False,
                 agent_servers: Mapping[str, Any] | None = None) -> None:
        self._servers = servers
        self._agent_servers = _as_list(agent_servers) if agent_servers else []
        self._settings_files = tuple(settings_files)
        self._project = project
        self._scope = scope
        self._pool = pool
        #: task id -> {alias: server}, resolved at pick-up (memory only).
        self._known: dict[str, dict[str, McpServer]] = {}
        self._scopes: dict[str, str | None] = {}
        self._tools: dict[str, tuple[Any, list[Tool]]] = {}
        self._marks: dict[str, int] = {}
        #: Command name -> (server, scope, prompt), as last listed.
        self._prompts: dict[str, tuple[McpServer, str | None, McpPrompt]] = {}
        #: task id -> the workspace directories ``roots/list`` answers.
        self._roots: dict[str, list[str]] = {}
        #: (task id, call id) -> a call waiting on the person's answer.
        self._parked: dict[tuple[str, str], _Parked] = {}
        #: (scope, server identity) -> the sign-in in progress.
        self._sign_ins: dict[tuple[object, ...], _SignInRun] = {}

    # ------------------------------------------------------------ state

    def initial_state(self) -> Any:
        return {"servers": None, "down": {}}

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        st = dict(state or self.initial_state())
        if kind == "listed":
            st["servers"] = data["servers"]
        elif kind == "server_down":
            st["down"] = {**st["down"], data["alias"]: data["reason"]}
        elif kind == "server_up":
            st["down"] = {k: v for k, v in st["down"].items() if k != data["alias"]}
        return st

    # ------------------------------------------------------------ tools

    def pool(self) -> McpPool:
        return self._pool if self._pool is not None else default_pool()

    def tools(self, view: TaskView) -> Sequence[Tool]:
        st = view.ext_state(self.name)
        if not st or st.get("servers") is None:
            return ()
        cached = self._tools.get(view.task_id)
        if cached is not None and cached[0] is st:
            return cached[1]
        tools: list[Tool] = []
        for server in st["servers"]:
            deferred = server.get("deferred", True)
            for t in server["tools"]:
                if t.get("stub"):
                    tools.append(McpAuthTool(name=t["name"], alias=server["alias"],
                                             stub=t["stub"], description=t["description"],
                                             input_schema=t["input_schema"],
                                             sign_in=self._sign_in, deferred=deferred))
                    continue
                tools.append(McpTool(name=t["name"], alias=server["alias"], remote=t["remote"],
                                     description=t["description"],
                                     input_schema=t["input_schema"], invoke=self._invoke,
                                     deferred=deferred, resume=self._resume))
        if (st["servers"] or st.get("down")) and view.state.parent_id is None:
            # Offered whenever a server is configured, even while it is down:
            # an outage must not move the stable prefix. Sub-agents get the
            # servers' tools only, as in Claude Code.
            tools += [ListMcpResourcesTool(self._servers_of, self._use),
                      ReadMcpResourceTool(self._servers_of, self._use),
                      ReadMcpResourceDirTool(self._servers_of, self._use)]
        self._tools[view.task_id] = (st, tools)
        return tools

    def _servers_of(self, ctx: ToolContext) -> dict[str, McpServer]:
        return dict(self._known.get(ctx.task_id, {}))

    def _context(self, task_id: str, call: _Call | None = None) -> RequestContext:
        return RequestContext(roots=self._roots.get(task_id, ()),
                              elicit=call.elicit if call is not None else None)

    async def _use(self, ctx: ToolContext, server: McpServer, request: Any) -> Any:
        return await self.pool().use(server, self._scopes.get(ctx.task_id), request, retry=True,
                                     context=self._context(ctx.task_id))

    async def _invoke(self, tool: McpTool, args: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        server = self._known.get(ctx.task_id, {}).get(tool.alias)
        if server is None:
            return text_output(f"{tool.name}: MCP server {tool.alias!r} is not configured "
                               "for this task", is_error=True)
        scope = self._scopes.get(ctx.task_id)
        call = _Call()

        async def run(c: McpClient) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
            result = await c.call_tool(tool.remote, args,
                                       timeout=tool_timeout_s(server.call_timeout))
            if not c.tools_changed:
                return result, None
            c.tools_changed = False
            try:
                return result, await c.list_tools()
            except McpError:
                return result, None  # the next turn lists it anyway

        # Only the runner's own calls can park on the person's answer.
        context = self._context(ctx.task_id, call if ctx.by_model else None)
        call.job = asyncio.create_task(self.pool().use(server, scope, run, context=context))
        # A call let go of (its task ended) fails unread; that is no news.
        call.job.add_done_callback(lambda job: job.cancelled() or job.exception())
        return await self._finish_call(tool, server, call, ctx, may_park=ctx.by_model)

    async def _finish_call(self, tool: McpTool, server: McpServer | None, call: _Call,
                           ctx: ToolContext, *, may_park: bool) -> ToolOutput:
        """Wait for the call; park it on the first question the server asks
        (``may_park``), decline any other."""
        job = call.job
        assert job is not None
        try:
            while True:
                asked = asyncio.ensure_future(call.questions.get())
                await asyncio.wait({job, asked}, return_when=asyncio.FIRST_COMPLETED)
                if not asked.done():
                    asked.cancel()
                    break
                params, answer = asked.result()
                if not may_park:
                    answer.set_result({"action": "decline"})
                    ctx.notify(f"The MCP server {tool.alias!r} asked a further question "
                               "during one call; it was declined.")
                    continue
                park = ask(*elicitation_questions(tool.alias, params))
                release = await ctx.hold() if ctx.hold is not None else None
                self._parked[(ctx.task_id, ctx.call_id)] = _Parked(call, params, answer,
                                                                    park, release)
                return ToolOutput(park=park)
        except asyncio.CancelledError:
            job.cancel()
            raise
        try:
            result, listed = job.result()
        except McpAuthRequired:
            return text_output(f"{tool.name}: the MCP server {tool.alias!r} requires the user "
                               "to sign in again; its sign-in tools are offered from their "
                               "next message.", is_error=True)
        except McpError as exc:
            return text_output(f"{tool.name}: {exc}", is_error=True)
        if listed is not None and server is not None:
            self._record_server(ctx, _server_entry(server, listed))
        return await render_result(result, ctx)

    async def _resume(self, tool: McpTool, args: dict[str, Any], ctx: ToolContext,
                      outcome: Any) -> ToolOutput:
        parked = self._parked.pop((ctx.task_id, ctx.call_id), None)
        if parked is None:
            return text_output(
                f"{tool.name}: the MCP server's question was answered after the call was lost "
                "(the worker running it stopped); the call's outcome is unknown.",
                is_error=True)
        try:
            if not parked.answer.done():
                parked.answer.set_result(elicitation_result(parked.params, outcome,
                                                            parked.asked))
            if outcome is None:
                if parked.call.job is not None:
                    parked.call.job.cancel()
                return text_output(f"{tool.name}: stopped while the MCP server waited for "
                                   "the user's answer.", is_error=True)
            server = self._known.get(ctx.task_id, {}).get(tool.alias)
            return await self._finish_call(tool, server, parked.call, ctx, may_park=False)
        finally:
            if parked.release is not None:
                await parked.release()

    def _record_server(self, ctx: ToolContext, entry: dict[str, Any]) -> None:
        """Record ``entry`` in place of its server's in the task's tool set
        (with the call's result; the next step offers it)."""
        st = ctx.state.get(self.name) or {}
        servers = [entry if s["alias"] == entry["alias"] else s
                   for s in st.get("servers") or []]
        if entry["alias"] not in {s["alias"] for s in servers}:
            servers = sorted([*servers, entry], key=lambda s: str(s["alias"]))
        others = {t["name"] for s in servers if s["alias"] != entry["alias"]
                  for t in s["tools"]}
        if any(t["name"] in others for t in entry["tools"]):
            return  # a name clash: the next turn's listing reports it
        if servers != st.get("servers"):
            ctx.record(self.name, "listed", {"servers": servers})

    # ------------------------------------------------------------ sign-in

    async def _sign_in(self, tool: McpAuthTool, args: dict[str, Any],
                       ctx: ToolContext) -> ToolOutput:
        server = self._known.get(ctx.task_id, {}).get(tool.alias)
        if server is None:
            return text_output(f"{tool.name}: MCP server {tool.alias!r} is not configured "
                               "for this task", is_error=True)
        scope = self._scopes.get(ctx.task_id)
        if tool.stub == "authenticate":
            return await self._start_sign_in(tool, server, scope)
        return await self._complete_sign_in(tool, server, scope, args, ctx)

    async def _start_sign_in(self, tool: McpAuthTool, server: McpServer,
                             scope: str | None) -> ToolOutput:
        key = self.pool().key(server, scope)
        complete = tool.name.removesuffix("authenticate") + "complete_authentication"
        run = self._sign_ins.get(key)
        reused = run is not None and not run.done.done() \
            and time.monotonic() - run.started < SIGN_IN_REUSE_S
        if not reused:
            if run is not None:
                await run.sign_in.close()
            sign_in = SignIn(server, store_for(server, scope), http=self.pool().http)
            try:
                url = await sign_in.start()
            except McpError as exc:
                await sign_in.close()
                return text_output(f"Could not start the sign-in to {server.alias!r}: {exc}. "
                                   "Tell the user it is not available right now.",
                                   is_error=True)
            run = _SignInRun(sign_in, url, time.monotonic(),
                             asyncio.create_task(self._await_sign_in(server, scope, sign_in)))
            self._sign_ins[key] = run
        assert run is not None
        earlier = ("A sign-in started earlier is still waiting, so this is its link. "
                   if reused else "")
        return text_output(
            f"{earlier}Ask the user to open this URL in their browser to authorize the "
            f"{server.alias!r} MCP server:\n{run.url}\n\nOnce they have, its tools become "
            "available from their next message. If the browser shows a connection error on "
            "the page it is sent back to, ask them for the full URL in the address bar and "
            f"call `{complete}` with it.")

    async def _await_sign_in(self, server: McpServer, scope: str | None,
                             sign_in: SignIn) -> None:
        try:
            await sign_in.finish()
        except McpError as exc:
            log.warning("mcp: the sign-in to %s failed: %s", server.alias, exc)
            raise
        await self.pool().forget(server, scope)

    async def _complete_sign_in(self, tool: McpAuthTool, server: McpServer, scope: str | None,
                                args: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        start = tool.name.removesuffix("complete_authentication") + "authenticate"
        run = self._sign_ins.get(self.pool().key(server, scope))
        if run is None or (run.done.done() and run.done.exception() is not None):
            return text_output(f"No sign-in to {server.alias!r} is in progress. Call `{start}` "
                               "first, then retry with the callback URL.", is_error=True)
        url = str(args.get("callback_url") or "")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        if "code" not in query and "error" not in query:
            return text_output("That URL carries no authorization code. Ask the user for the "
                               "full address from the browser's address bar, including the "
                               "`?code=...&state=...` part.", is_error=True)
        if not run.done.done() and not run.sign_in.submit(url):
            return text_output(f"That URL belongs to another sign-in attempt to "
                               f"{server.alias!r}; the current one is still waiting. Ask the "
                               "user for the URL from the page this sign-in opened.",
                               is_error=True)
        try:
            await asyncio.shield(run.done)
        except McpError as exc:
            return text_output(f"The sign-in to {server.alias!r} failed: {exc}", is_error=True)
        try:
            raw = await self.pool().use(server, scope, lambda c: c.list_tools(),
                                        context=self._context(ctx.task_id))
        except McpError:
            return text_output(f"Authentication complete for {server.alias!r}. Its tools "
                               "become available from the user's next message.")
        self._record_server(ctx, _server_entry(server, raw))
        return text_output(f"Authentication complete for {server.alias!r}. Its tools are "
                           "available now.")

    # ------------------------------------------------------------ lifecycle

    async def on_start(self, view: TaskView) -> None:
        if not await self._resolve(view):
            return
        st = view.ext_state(self.name) or {}
        if self._turn_start(view) or (st.get("servers") is None and view.state.turn_open):
            await self._refresh_once(view)

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        if isinstance(recorded.event, TaskEnded):
            for key in [k for k in self._parked if k[0] == view.task_id]:
                parked = self._parked.pop(key)
                if parked.call.job is not None:
                    parked.call.job.cancel()
                if parked.release is not None:
                    await parked.release()
        if isinstance(recorded.event, (Waiting, TaskEnded)):
            # The runner lets go of the task; the next pick-up resolves again.
            for cache in (self._known, self._scopes, self._tools, self._marks, self._roots):
                cache.pop(view.task_id, None)
        elif self._turn_start(view) and view.task_id in self._known:
            # A turn opens on taken input, or on input queued behind the
            # turn that just ended.
            await self._refresh_once(view)

    @staticmethod
    def _turn_start(view: TaskView) -> bool:
        s = view.state
        return s.turn_open and s.steps_in_turn == 0 and not s.calls

    async def _refresh_once(self, view: TaskView) -> None:
        """Refresh once per turn opening: a new turn always adds a message."""
        mark = len(view.state.messages)
        if self._marks.get(view.task_id) != mark:
            self._marks[view.task_id] = mark
            await self.refresh(view)

    async def _resolve(self, view: TaskView) -> bool:
        servers = self._servers
        try:
            if callable(servers):
                got = servers(view)
                servers = await got if inspect.isawaitable(got) else got
            servers = _as_list(servers)
            scope = self._scope
            self._scopes[view.task_id] = scope(view) if callable(scope) else scope
            self._roots[view.task_id] = [view.cwd] if view.cwd else []
            servers = [*self._agent_servers, *servers, *await self._from_files(view)]
        except Exception:
            # Keep the recorded tool set; its calls fail until a pick-up
            # resolves the servers again.
            log.exception("mcp: resolving the servers of task %s failed", view.task_id)
            return False
        known: dict[str, McpServer] = {}
        for s in servers:
            known.setdefault(s.alias, s)  # a duplicate alias is ignored
        self._known[view.task_id] = known
        return True

    async def _from_files(self, view: TaskView) -> list[McpServer]:
        if not self._settings_files and not self._project:
            return []
        cwd = view.cwd
        dirs = [cwd] if cwd and self._trusts(cwd) else []
        return await asyncio.to_thread(read_servers, self._settings_files, dirs)

    def _trusts(self, directory: str) -> bool:
        project = self._project
        if callable(project):
            try:
                return bool(project(directory))
            except Exception:
                log.warning("mcp: the trust check for %s failed", directory, exc_info=True)
                return False
        return bool(project)

    # ------------------------------------------------------------ commands

    async def _command_servers(self, ctx: CommandContext) -> list[McpServer]:
        """The task's servers once a runner resolved them; before that the
        fixed ones and the settings files'."""
        if ctx.task_id and ctx.task_id in self._known:
            return list(self._known[ctx.task_id].values())
        given = [] if callable(self._servers) else _as_list(self._servers)
        files: list[McpServer] = []
        if self._settings_files or self._project:
            files = await asyncio.to_thread(read_servers, self._settings_files,
                                            [ctx.cwd] if ctx.cwd and self._trusts(ctx.cwd)
                                            else [])
        known: dict[str, McpServer] = {}
        for server in [*self._agent_servers, *given, *files]:
            known.setdefault(server.alias, server)
        return list(known.values())

    async def commands(self, ctx: CommandContext) -> Sequence[Command]:
        """Each server prompt as ``/mcp__<alias>__<prompt>``; arguments are
        typed after it, separated by spaces (the last takes the rest)."""
        scope = self._scopes.get(ctx.task_id or "") if ctx.task_id else (
            self._scope if isinstance(self._scope, str) else None)
        out: list[Command] = []
        for server in await self._command_servers(ctx):
            try:
                prompts = await list_prompts([server], scope=scope, pool=self.pool())
            except Exception as exc:  # noqa: BLE001 — one server down hides only its prompts
                log.warning("mcp: listing the prompts of %s failed: %s", server.alias, exc)
                continue
            for p in prompts:
                self._prompts[p.command_name] = (server, scope, p)
                hint = " ".join(f"<{a['name']}>" if a["required"] else f"[{a['name']}]"
                                for a in p.arguments)
                out.append(Command(name=p.command_name, description=p.description,
                                   argument_hint=hint))
        return out

    async def run_command(self, ctx: CommandContext, name: str, args: str) -> str:
        if name not in self._prompts:
            await self.commands(ctx)
        if name not in self._prompts:
            raise McpError(f"no MCP prompt {name!r}")
        server, scope, prompt = self._prompts[name]
        names = [a["name"] for a in prompt.arguments]
        values = args.split(None, max(len(names) - 1, 0)) if names and args else []
        return await expand_prompt(server, prompt.name, dict(zip(names, values, strict=False)),
                                   scope=scope, pool=self.pool())

    async def refresh(self, view: TaskView) -> None:
        """List every server now and record the tool set if it changed."""
        known = self._known.get(view.task_id, {})
        scope = self._scopes.get(view.task_id)
        servers = sorted(known.values(), key=lambda s: s.alias)
        context = self._context(view.task_id)
        listed = await asyncio.gather(*(self._list(s, scope, context) for s in servers))
        entries: list[dict[str, Any]] = []
        owner: dict[str, str] = {}
        failures: dict[str, str] = {}
        for server, (entry, error) in zip(servers, listed, strict=True):
            if entry is not None:
                clash = next((t["name"] for t in entry["tools"] if t["name"] in owner), None)
                if clash is not None:
                    error = (f"MCP tool name {clash!r} is produced by both server "
                             f"{owner[clash]!r} and server {server.alias!r}; rename one alias")
                    entry = None
            if entry is None:
                failures[server.alias] = error or "unavailable"
                continue
            for t in entry["tools"]:
                owner[t["name"]] = server.alias
            entries.append(entry)
        data = {"servers": entries}
        st = view.ext_state(self.name) or self.initial_state()
        if st.get("servers") != entries:
            view.record(self.name, "listed", data)
        down = st.get("down") or {}
        for alias, reason in failures.items():
            if alias not in down:
                view.record(self.name, "server_down", {"alias": alias, "reason": reason})
        for alias in down:
            if alias not in failures:
                view.record(self.name, "server_up", {"alias": alias})

    async def _list(self, server: McpServer, scope: str | None, context: RequestContext
                    ) -> tuple[dict[str, Any] | None, str | None]:
        try:
            raw = await self.pool().use(server, scope, lambda c: c.list_tools(), retry=True,
                                        context=context)
            return _server_entry(server, raw), None
        except McpAuthRequired:
            return {"alias": server.alias, "deferred": server.deferred,
                    "tools": auth_stub_entries(server)}, None
        except (McpError, McpConfigError) as exc:
            return None, str(exc)


def _server_entry(server: McpServer, raw: list[dict[str, Any]]) -> dict[str, Any]:
    """The recorded tool set of one server: subset-filtered, named, sorted
    by offered name (so the order never depends on the server's)."""
    allow = set(server.tools) if server.tools is not None else None
    kept = [r for r in raw if allow is None or r.get("name") in allow]
    names = map_server_names(server.alias, [r.get("name") for r in kept])
    tools: dict[str, dict[str, Any]] = {}
    for r in kept:
        remote = str(r.get("name"))
        name = names[remote]
        if name in tools:
            continue  # the same raw name advertised twice: keep the first
        schema = r.get("inputSchema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "additionalProperties": True}
        tools[name] = {"name": name, "remote": remote,
                       "description": describe_tool(r.get("description")),
                       "input_schema": schema}
    return {"alias": server.alias, "deferred": server.deferred,
            "tools": [tools[n] for n in sorted(tools)]}
