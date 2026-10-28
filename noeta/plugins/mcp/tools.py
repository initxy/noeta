"""The tools the MCP extension offers: one per server tool, and the two
resource tools ``ListMcpResourcesTool`` / ``ReadMcpResourceTool``.

Every one of them is deferred (``noeta.core.deferred``): the conversation
names them, the model loads the ones it needs with ``ToolSearch`` and then
calls them directly. Server tools are listed after the built-in tools.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.plugins.mcp.config import McpError, McpServer, cap_injected

__all__ = [
    "LIST_RESOURCES_TOOL",
    "READ_RESOURCE_TOOL",
    "ListMcpResourcesTool",
    "McpTool",
    "ReadMcpResourceTool",
]

LIST_RESOURCES_TOOL = "ListMcpResourcesTool"
READ_RESOURCE_TOOL = "ReadMcpResourceTool"
_SCHEMA = "https://json-schema.org/draft/2020-12/schema"

#: ``(tool, arguments, ctx) -> output``: the extension's way to reach the
#: tool's server.
Invoke = Callable[["McpTool", dict[str, Any], ToolContext], Awaitable[ToolOutput]]
#: ``(ctx, servers) -> {alias: server}``: the task's configured servers.
ServersOf = Callable[[ToolContext], Mapping[str, McpServer]]
#: ``(ctx, server, request) -> result``: one request on a server's connection.
Use = Callable[[ToolContext, McpServer, Callable[[Any], Awaitable[Any]]], Awaitable[Any]]


class McpTool:
    """One server tool. Its result is not clearable: calling it again may
    not give the same answer (ADR-066)."""

    read_only = False
    clearable = False
    listed_last = True

    def __init__(self, *, name: str, alias: str, remote: str, description: str,
                 input_schema: dict[str, Any], invoke: Invoke, deferred: bool = True) -> None:
        self.name = name
        self.alias = alias
        self.remote = remote
        self.description = description
        self.input_schema = input_schema
        self.deferred = deferred
        self._invoke = invoke

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return await self._invoke(self, dict(input), ctx)


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class ListMcpResourcesTool:
    name = LIST_RESOURCES_TOOL
    read_only = True
    clearable = True
    deferred = True
    description = """
Lists the resources that the configured MCP servers offer. Each entry has the \
standard MCP resource fields and a `server` field naming the server it comes from.

Parameters:
- server (optional): list only this server's resources; leave it out to list every server's.
"""
    input_schema: dict[str, Any] = {
        "$schema": _SCHEMA,
        "type": "object",
        "properties": {
            "server": {"type": "string",
                       "description": "Only this server's resources (default: every server)."},
        },
        "additionalProperties": False,
    }

    def __init__(self, servers_of: ServersOf, use: Use) -> None:
        self._servers_of = servers_of
        self._use = use

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        servers = dict(self._servers_of(ctx))
        wanted = input.get("server")
        if wanted is not None:
            if wanted not in servers:
                return text_output(f"No MCP server named {wanted!r}. Configured: "
                                   f"{', '.join(sorted(servers)) or 'none'}.", is_error=True)
            servers = {wanted: servers[wanted]}
        out: list[dict[str, Any]] = []
        for alias in sorted(servers):
            try:
                raw = await self._use(ctx, servers[alias], lambda c: c.list_resources())
            except McpError:
                continue  # a server without resources, or one that is down
            out += [{**r, "server": alias} for r in raw if isinstance(r, dict)]
        if not out:
            return text_output("No resources found on the configured MCP servers (they may "
                               "still offer tools).")
        return text_output(cap_injected(_compact(out), kind="resource listing"))


class ReadMcpResourceTool:
    name = READ_RESOURCE_TOOL
    read_only = True
    clearable = True
    deferred = True
    description = """
Reads one resource from an MCP server, given the server's name and the resource URI.

Parameters:
- server (required): the name of the MCP server, as configured
- uri (required): the URI of the resource to read
"""
    input_schema: dict[str, Any] = {
        "$schema": _SCHEMA,
        "type": "object",
        "properties": {
            "server": {"type": "string", "description": "The MCP server's name."},
            "uri": {"type": "string", "description": "The resource's URI."},
        },
        "required": ["server", "uri"],
        "additionalProperties": False,
    }

    def __init__(self, servers_of: ServersOf, use: Use) -> None:
        self._servers_of = servers_of
        self._use = use

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        servers = self._servers_of(ctx)
        alias, uri = input.get("server"), input.get("uri")
        if not isinstance(uri, str) or not uri:
            return text_output("ReadMcpResourceTool: `uri` must be a non-empty string.",
                               is_error=True)
        server = servers.get(alias) if isinstance(alias, str) else None
        if server is None:
            return text_output(f"No MCP server named {alias!r}. Configured: "
                               f"{', '.join(sorted(servers)) or 'none'}.", is_error=True)
        try:
            result = await self._use(ctx, server, lambda c: c.read_resource(uri))
        except McpError as exc:
            return text_output(f"{READ_RESOURCE_TOOL}: {exc}", is_error=True)
        return text_output(cap_injected(_compact(result), kind="resource"))
