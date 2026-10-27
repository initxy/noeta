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
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Union

from noeta.core.events import Recorded, TaskEnded, Waiting
from noeta.core.extension import Deny, Extension, Gate, Rewrite, TaskView
from noeta.core.tool import Tool, ToolContext, ToolOutput, text_output
from noeta.core.types import ToolUse
from noeta.plugins.mcp.config import (
    McpConfigError,
    McpError,
    McpServer,
    describe_tool,
    map_server_names,
)
from noeta.plugins.mcp.pool import McpPool, default_pool
from noeta.plugins.mcp.results import render_result
from noeta.plugins.mcp.tools import MCP_CALL_TOOL, McpCallTool, McpTool, ToolSearchTool, route_call

__all__ = ["Mcp", "ServersFn"]

log = logging.getLogger("noeta.plugins.mcp")

Servers = Sequence[McpServer]
ServersFn = Callable[[TaskView], Union[Servers, Awaitable[Servers]]]
ScopeFn = Callable[[TaskView], Union[str, None]]


class Mcp(Extension):
    """Tools from MCP servers.

    ``servers``: the servers a task may use — a fixed list, or a function of
    the task (``TaskView -> [McpServer]``, sync or async) evaluated each time a
    runner picks the task up. It replaces 0.6's ``Options.mcp_servers`` and
    ``HostConfig.mcp_server_resolver``; derive it from the task (its config,
    ``state.root_id`` for a child) so any worker gets the same answer.

    ``scope``: partitions shared connections (0.6's ``mcp_scope_resolver``):
    a string, or ``TaskView -> str | None``. Tasks naming the same server in
    the same scope share one connection per worker process; give each tenant
    its own scope so a stateful server (a browser, a login) never carries one
    tenant's state into another's task. ``None`` = one shared partition.

    ``pool``: the connection pool (default: one per worker process and event
    loop, idle connections closed after 30 minutes).
    """

    name = "mcp"

    def __init__(self, servers: Servers | ServersFn = (), *,
                 scope: str | ScopeFn | None = None, pool: McpPool | None = None) -> None:
        self._servers = servers
        self._scope = scope
        self._pool = pool
        #: task id -> {alias: server}, resolved at pick-up (memory only).
        self._known: dict[str, dict[str, McpServer]] = {}
        self._scopes: dict[str, str | None] = {}
        self._tools: dict[str, tuple[Any, list[Tool]]] = {}
        self._marks: dict[str, int] = {}

    # ------------------------------------------------------------ state

    def initial_state(self) -> Any:
        return {"servers": None, "deferred": False, "down": {}}

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        st = dict(state or self.initial_state())
        if kind == "listed":
            st["servers"], st["deferred"] = data["servers"], data["deferred"]
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
        deferred: dict[str, McpTool] = {}
        for server in st["servers"]:
            for t in server["tools"]:
                tool = McpTool(name=t["name"], alias=server["alias"], remote=t["remote"],
                               description=t["description"], input_schema=t["input_schema"],
                               invoke=self._invoke, advertised=not server["deferred"])
                tools.append(tool)
                if server["deferred"]:
                    deferred[tool.name] = tool
        if st["deferred"]:
            # Offered whenever a deferred server is configured, even while it
            # is down: an outage must not move the stable prefix.
            tools += [ToolSearchTool(deferred), McpCallTool(deferred)]
        self._tools[view.task_id] = (st, tools)
        return tools

    def _deferred(self, view: TaskView) -> dict[str, McpTool]:
        return {t.name: t for t in self.tools(view)
                if isinstance(t, McpTool) and not t.advertised}

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        if call.name != MCP_CALL_TOOL:
            return None
        try:
            target, args = route_call(call.input, self._deferred(view))
        except ValueError as exc:
            return Deny(reason=str(exc))
        return Rewrite(name=target, input=args)

    async def _invoke(self, tool: McpTool, args: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        server = self._known.get(ctx.task_id, {}).get(tool.alias)
        if server is None:
            return text_output(f"{tool.name}: MCP server {tool.alias!r} is not configured "
                               "for this task", is_error=True)
        scope = self._scopes.get(ctx.task_id)
        try:
            result = await self.pool().use(
                server, scope,
                lambda c: c.call_tool(tool.remote, args, timeout=server.call_timeout))
        except McpError as exc:
            return text_output(f"{tool.name}: {exc}", is_error=True)
        return await render_result(result, ctx)

    # ------------------------------------------------------------ lifecycle

    async def on_start(self, view: TaskView) -> None:
        if not await self._resolve(view):
            return
        st = view.ext_state(self.name) or {}
        if self._turn_start(view) or (st.get("servers") is None and view.state.turn_open):
            await self._refresh_once(view)

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        if isinstance(recorded.event, (Waiting, TaskEnded)):
            # The runner lets go of the task; the next pick-up resolves again.
            for cache in (self._known, self._scopes, self._tools, self._marks):
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
            scope = self._scope
            self._scopes[view.task_id] = scope(view) if callable(scope) else scope
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

    async def refresh(self, view: TaskView) -> None:
        """List every server now and record the tool set if it changed."""
        known = self._known.get(view.task_id, {})
        scope = self._scopes.get(view.task_id)
        servers = sorted(known.values(), key=lambda s: s.alias)
        listed = await asyncio.gather(*(self._list(s, scope) for s in servers))
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
        data = {"servers": entries, "deferred": any(s.deferred for s in servers)}
        st = view.ext_state(self.name) or self.initial_state()
        if st.get("servers") != entries or st.get("deferred") != data["deferred"]:
            view.record(self.name, "listed", data)
        down = st.get("down") or {}
        for alias, reason in failures.items():
            if alias not in down:
                view.record(self.name, "server_down", {"alias": alias, "reason": reason})
        for alias in down:
            if alias not in failures:
                view.record(self.name, "server_up", {"alias": alias})

    async def _list(self, server: McpServer, scope: str | None
                    ) -> tuple[dict[str, Any] | None, str | None]:
        try:
            raw = await self.pool().use(server, scope, lambda c: c.list_tools(), retry=True)
            return _server_entry(server, raw), None
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
                       "description": describe_tool(server.alias, r.get("description")),
                       "input_schema": schema}
    return {"alias": server.alias, "deferred": server.deferred,
            "tools": [tools[n] for n in sorted(tools)]}
