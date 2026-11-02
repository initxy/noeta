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
  died (or lost its hold on the tree) while it waited ends "outcome
  unknown" as soon as another worker picks the task up: the reaped hold
  wakes it, and the call is answered in the person's place.
* **Every call carries its idempotency key** in the request's ``_meta``
  (``noeta/idempotencyKey``: the same for every run of the call), for a
  server that can drop a repeat. A call a crash left unfinished is still
  answered "outcome unknown": nothing says a server keeps those keys.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Union

from noeta.core.errors import HeldElsewhere
from noeta.core.events import Answer, Recorded, TaskEnded, ToolFinished, Waiting
from noeta.core.extension import (
    Command,
    CommandContext,
    Extension,
    Reminder,
    TaskView,
)
from noeta.core.questions import ask
from noeta.core.tool import Park, Tool, ToolContext, ToolOutput, text_output
from noeta.core.types import Message, ToolReference
from noeta.plugins.mcp.admin import McpAdmin
from noeta.plugins.mcp.client import McpClient, RequestContext
from noeta.plugins.mcp.config import (
    NAME_MAX,
    McpAuthRequired,
    McpConfigError,
    McpError,
    McpServer,
    describe_tool,
    map_server_names,
    tool_timeout_s,
)
from noeta.plugins.mcp.elicitation import elicitation_questions, elicitation_result
from noeta.plugins.mcp.instructions import cap_instructions, instructions_delta
from noeta.plugins.mcp.pool import McpPool, default_pool
from noeta.plugins.mcp.prompts import McpPrompt, expand_prompt, list_prompts
from noeta.plugins.mcp.results import render_result
from noeta.plugins.mcp.settings import read_servers, servers_from
from noeta.plugins.mcp.sign_ins import SIGN_IN_REUSE_S as SIGN_IN_REUSE_S
from noeta.plugins.mcp.sign_ins import SignIns
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
#: A server's tools and its instructions, as listed.
_Listing = tuple[list[dict[str, Any]], Union[str, None]]


#: The ``tools/call`` ``_meta`` key that carries ``ToolContext.idempotency_key``.
IDEMPOTENCY_META = "noeta/idempotencyKey"
#: Tasks whose resolved servers are kept in memory at most. The runner lets
#: go of a task with ``Waiting`` or ``TaskEnded``; one that leaves this worker
#: otherwise (another worker took it, a crash) is forgotten, least recently
#: resolved first, once this many newer ones are.
TASKS_KEEP = 256
#: The servers' instructions in the joined system turn (``Reminder.order``):
#: after the deferred tools and the agent list, before the skill listing
#: (Claude Code's order).
INSTRUCTIONS_ORDER = 45


def _as_list(servers: Servers) -> list[McpServer]:
    if isinstance(servers, Mapping):
        return servers_from({"mcpServers": servers}, "servers")
    return list(servers)


@dataclass
class _Call:
    """A ``tools/call`` in flight, and the server questions it brought."""

    job: asyncio.Task[tuple[dict[str, Any], _Listing | None]] | None = None
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
    the same scope share one connection per worker process; give each user
    of a host its own scope so a stateful server (a browser, a login) never
    carries one user's state into another's task. ``None`` = one shared
    partition.

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
        #: The turn mark (``_marks``) of the last re-listing a fruitless
        #: ``ToolSearch`` asked for: at most one per turn.
        self._search_marks: dict[str, int] = {}
        #: (connection partition, command name) -> (server, prompt), as last
        #: listed in that partition: one partition's listing never names
        #: the connection another partition's command runs on.
        self._prompts: dict[tuple[str | None, str], tuple[McpServer, McpPrompt]] = {}
        #: The partitions in ``_prompts``, least recently listed first; at
        #: most :data:`TASKS_KEEP` are kept.
        self._prompt_scopes: dict[str | None, None] = {}
        #: task id -> the workspace directories ``roots/list`` answers.
        self._roots: dict[str, list[str]] = {}
        #: (task id, call id) -> a call waiting on the person's answer.
        self._parked: dict[tuple[str, str], _Parked] = {}
        self._sign_ins = SignIns(self.pool)

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
        elif kind == "instructions":
            st["told"] = dict(data["servers"])
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
                                     loaded_description=t.get("loaded_description"),
                                     input_schema=t["input_schema"], invoke=self._invoke,
                                     deferred=deferred and not t.get("always_load"),
                                     resume=self._resume,
                                     search_hint=t.get("search_hint", "")))
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

        async def run(c: McpClient) -> tuple[dict[str, Any], _Listing | None]:
            result = await c.call_tool(tool.remote, args,
                                       timeout=tool_timeout_s(server.call_timeout),
                                       meta={IDEMPOTENCY_META: ctx.idempotency_key})
            if not c.tools_changed:
                return result, None
            c.tools_changed = False
            try:
                return result, await _listing(c)
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
                try:
                    release = await ctx.hold() if ctx.hold is not None else None
                except HeldElsewhere:
                    # The open call lives in this process, and the tree's
                    # next turn would run on the worker that holds it.
                    answer.set_result({"action": "decline"})
                    ctx.notify(f"The MCP server {tool.alias!r} asked a question that could "
                               "not wait for an answer on this worker; it was declined.")
                    continue
                park = ask(*elicitation_questions(tool.alias, params))
                key = (ctx.task_id, ctx.call_id)
                self._parked[key] = _Parked(call, params, answer, park, release)
                if release is not None:
                    # The hold is gone: so is the call, wherever the task goes.
                    release.tree.on_lost(functools.partial(self._drop_parked, key))
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
            self._record_server(ctx, _server_entry(server, *listed))
        return await render_result(result, ctx)

    def _drop_parked(self, key: tuple[str, str]) -> None:
        parked = self._parked.pop(key, None)
        if parked is not None and parked.call.job is not None:
            parked.call.job.cancel()

    def _answer_lost_calls(self, view: TaskView) -> None:
        """Parked calls of this task that this process does not run (their
        worker died or let go of them) are answered in the person's place,
        so they end "outcome unknown" now instead of whenever someone
        answers a question no server waits for any more."""
        mine = {t.name for t in self.tools(view) if isinstance(t, McpTool)}
        for c in view.state.open_calls:
            use = c.ran or c.use
            if (c.parked or {}).get("on") == "question" and use.name in mine \
                    and use.id not in view.state.answers \
                    and (view.task_id, use.id) not in self._parked:
                view.enqueue(Answer(request_id=use.id), dedupe_key=f"mcp:lost:{use.id}")

    async def _resume(self, tool: McpTool, args: dict[str, Any], ctx: ToolContext,
                      outcome: Any) -> ToolOutput:
        parked = self._parked.pop((ctx.task_id, ctx.call_id), None)
        if parked is None:
            return text_output(
                f"{tool.name}: the worker running this call stopped while the MCP server "
                "waited for the user's answer; the call's outcome is unknown.", is_error=True)
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
            return await self._sign_ins.start(tool, server, scope)
        failed = await self._sign_ins.finish(tool, server, scope, args)
        if failed is not None:
            return failed
        try:
            raw = await self.pool().use(server, scope, _listing,
                                        context=self._context(ctx.task_id))
        except McpError:
            return text_output(f"Authentication complete for {server.alias!r}. Its tools "
                               "become available from the user's next message.")
        self._record_server(ctx, _server_entry(server, *raw))
        return text_output(f"Authentication complete for {server.alias!r}. Its tools are "
                           "available now.")

    # ------------------------------------------------------------ instructions

    async def reminders(self, view: TaskView) -> Sequence[Reminder]:
        """The servers' instructions, as Claude Code gives them: those of a
        server that joined (or changed them) since they were last told, and
        the names of servers gone since."""
        st = view.ext_state(self.name) or {}
        if st.get("servers") is None:
            return ()
        now = {s["alias"]: s["instructions"] for s in st["servers"] if s.get("instructions")}
        text = instructions_delta(st.get("told") or {}, now)
        if not text:
            return ()
        return (Reminder(text=text, kind="instructions", data={"servers": now},
                         role="system", order=INSTRUCTIONS_ORDER),)

    async def resident(self, view: TaskView) -> Sequence[Message]:
        """After a compaction the instructions are told again in full."""
        st = view.ext_state(self.name) or {}
        if view.keeps_tail is not None and st.get("told"):
            view.record(self.name, "instructions", {"servers": {}})
        return ()

    # ------------------------------------------------------------ lifecycle

    async def on_start(self, view: TaskView) -> None:
        self._answer_lost_calls(view)
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
            self._forget(view.task_id)
        elif self._turn_start(view) and view.task_id in self._known:
            # A turn opens on taken input, or on input queued behind the
            # turn that just ended.
            await self._refresh_once(view)
        elif isinstance(recorded.event, ToolFinished) and view.task_id in self._known:
            await self._after_search(view, recorded.event)

    async def _after_search(self, view: TaskView, event: ToolFinished) -> None:
        """A ``ToolSearch`` that loaded nothing while a server is down lists
        the servers again, once a turn, as Claude Code reconnects before it
        gives up: a server back up offers its tools from the next step."""
        if any(isinstance(b, ToolReference) for b in event.result.content):
            return
        st = view.ext_state(self.name) or {}
        mark = self._marks.get(view.task_id, -1)
        if not st.get("down") or self._search_marks.get(view.task_id) == mark:
            return
        if _call_name(view, event.result.call_id) != SEARCH_TOOL:
            return
        self._search_marks[view.task_id] = mark
        await self.refresh(view)

    def _forget(self, task_id: str) -> None:
        for cache in (self._known, self._scopes, self._tools, self._marks, self._roots,
                      self._search_marks):
            cache.pop(task_id, None)

    def _forget_old_tasks(self) -> None:
        """Keep the per-task entries of at most :data:`TASKS_KEEP` tasks
        (a task with a call waiting on the person stays)."""
        parked = {task_id for task_id, _call in self._parked}
        old = [t for t in self._known if t not in parked]
        for task_id in old[:max(0, len(self._known) - TASKS_KEEP)]:
            self._forget(task_id)
        for task_id in [t for t in self._tools if t not in self._known and t not in parked]:
            del self._tools[task_id]

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
            if view.task_id not in self._known:
                # Nothing else lets go of them: no task entry holds them.
                for cache in (self._scopes, self._roots):
                    cache.pop(view.task_id, None)
            return False
        known: dict[str, McpServer] = {}
        for s in servers:
            known.setdefault(s.alias, s)  # a duplicate alias is ignored
        self._known.pop(view.task_id, None)  # most recently resolved last
        self._known[view.task_id] = known
        self._forget_old_tasks()
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

    def _command_scope(self, ctx: CommandContext) -> str | None:
        """The connection partition a command of ``ctx`` lists and runs in."""
        if ctx.task_id:
            return self._scopes.get(ctx.task_id)
        return self._scope if isinstance(self._scope, str) else None

    async def commands(self, ctx: CommandContext) -> Sequence[Command]:
        """Each server prompt as ``/mcp__<alias>__<prompt>``; arguments are
        typed after it, separated by spaces (the last takes the rest)."""
        scope = self._command_scope(ctx)
        out: list[Command] = []
        for server in await self._command_servers(ctx):
            try:
                prompts = await list_prompts([server], scope=scope, pool=self.pool())
            except Exception as exc:  # noqa: BLE001 — one server down hides only its prompts
                log.warning("mcp: listing the prompts of %s failed: %s", server.alias, exc)
                continue
            for p in prompts:
                self._prompts[(scope, p.command_name)] = (server, p)
                hint = " ".join(f"<{a['name']}>" if a["required"] else f"[{a['name']}]"
                                for a in p.arguments)
                out.append(Command(name=p.command_name, description=p.description,
                                   argument_hint=hint))
        self._prompt_scopes.pop(scope, None)
        self._prompt_scopes[scope] = None
        while len(self._prompt_scopes) > TASKS_KEEP:
            gone = next(iter(self._prompt_scopes))
            del self._prompt_scopes[gone]
            for key in [k for k in self._prompts if k[0] == gone]:
                del self._prompts[key]
        return out

    async def run_command(self, ctx: CommandContext, name: str, args: str) -> str:
        scope = self._command_scope(ctx)
        if (scope, name) not in self._prompts:
            await self.commands(ctx)
        if (scope, name) not in self._prompts:
            raise McpError(f"no MCP prompt {name!r}")
        server, prompt = self._prompts[(scope, name)]
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
            raw = await self.pool().use(server, scope, _listing, retry=True, context=context)
            return _server_entry(server, *raw), None
        except McpAuthRequired:
            return {"alias": server.alias, "deferred": server.deferred,
                    "tools": auth_stub_entries(server)}, None
        except (McpError, McpConfigError) as exc:
            return None, str(exc)


async def _listing(c: McpClient) -> _Listing:
    """A server's tools and its instructions."""
    return await c.list_tools(), c.instructions


#: The ``tool_search`` plugin's tool, whose fruitless search re-lists the
#: servers that are down (named here, not imported: plugins stay apart).
SEARCH_TOOL = "ToolSearch"

#: A server tool's ``_meta`` keys Claude Code reads: words ``ToolSearch``
#: also matches, and "send this tool's schema from the first request".
SEARCH_HINT_META = "anthropic/searchHint"
ALWAYS_LOAD_META = "anthropic/alwaysLoad"


def _call_name(view: TaskView, call_id: str) -> str | None:
    """The tool a result answers: in the open batch, else (the batch closed
    with this result) in the latest assistant message."""
    for c in view.state.calls:
        if c.use.id == call_id:
            return c.use.name
    for message in reversed(view.state.messages):
        if message.role == "assistant":
            return next((u.name for u in message.tool_uses if u.id == call_id), None)
    return None


def _visible_to_model(meta: dict[str, Any]) -> bool:
    """Whether a tool's ``_meta.ui.visibility`` lets the model see it: no
    ``ui`` or no ``visibility`` does; a list without ``"model"``, or a value
    that cannot be read, does not."""
    ui = meta.get("ui")
    if ui is None:
        return True
    if not isinstance(ui, dict):
        return False
    visibility = ui.get("visibility")
    if visibility is None:
        return True
    return isinstance(visibility, list) and "model" in visibility


def _server_entry(server: McpServer, raw: list[dict[str, Any]],
                  instructions: str | None = None) -> dict[str, Any]:
    """The recorded tool set of one server: subset-filtered, named, sorted
    by offered name (so the order never depends on the server's), with the
    server's instructions when it gives any."""
    allow = set(server.tools) if server.tools is not None else None
    kept = [r for r in raw if allow is None or r.get("name") in allow]
    names = map_server_names(server.alias, [r.get("name") for r in kept])
    tools: dict[str, dict[str, Any]] = {}
    for r in kept:
        remote = str(r.get("name"))
        name = names[remote]
        if name in tools:
            continue  # the same raw name advertised twice: keep the first
        if len(name) > NAME_MAX:
            log.warning("mcp: %s: tool %r is not offered: its name has %d characters, the "
                        "limit is %d", server.alias, name, len(name), NAME_MAX)
            continue
        raw_meta = r.get("_meta")
        meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        if not _visible_to_model(meta):
            log.info("mcp: %s: tool %r is kept from the model (its _meta.ui.visibility "
                     "omits \"model\")", server.alias, remote)
            continue
        schema = r.get("inputSchema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "additionalProperties": True}
        listed, loaded = describe_tool(r.get("description"))
        tools[name] = {"name": name, "remote": remote, "description": listed,
                       "input_schema": schema}
        hint = meta.get(SEARCH_HINT_META)
        if isinstance(hint, str) and hint.split():
            tools[name]["search_hint"] = " ".join(hint.split())
        if meta.get(ALWAYS_LOAD_META) is True and not server.defer_all:
            tools[name]["always_load"] = True
        if loaded != listed:
            tools[name]["loaded_description"] = loaded
    entry: dict[str, Any] = {"alias": server.alias, "deferred": server.deferred,
                             "tools": [tools[n] for n in sorted(tools)]}
    if instructions:
        entry["instructions"] = cap_instructions(instructions)
    return entry
