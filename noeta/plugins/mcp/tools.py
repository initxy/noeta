"""The tools the MCP extension offers: one per server tool, the two
resource tools ``ListMcpResourcesTool`` / ``ReadMcpResourceTool``, and for a
server that needs a sign-in, ``mcp__<alias>__authenticate`` and
``mcp__<alias>__complete_authentication`` in place of its tools.

Every one of them is deferred (the ``tool_search`` plugin): the conversation
names them, the model loads the ones it needs with ``ToolSearch`` and then
calls them directly. Server tools are listed after the built-in tools.
"""

from __future__ import annotations

import base64
import binascii
import json
import mimetypes
import posixpath
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import Any

from noeta.core.mcp_names import named_in
from noeta.core.tool import ToolContext, ToolFlags, ToolOutput, text_output
from noeta.plugins.mcp.config import (
    McpError,
    McpServer,
    McpServerError,
    make_tool_name,
    terminal_tools,
)
from noeta.toolkit.tools import format_size, load_description, save_spilled, spill_path

__all__ = [
    "AUTH_STUBS",
    "LIST_RESOURCES_TOOL",
    "READ_RESOURCE_TOOL",
    "ListMcpResourcesTool",
    "McpAuthTool",
    "McpTool",
    "ReadMcpResourceDirTool",
    "ReadMcpResourceTool",
    "auth_stub_entries",
]

LIST_RESOURCES_TOOL = "ListMcpResourcesTool"
READ_RESOURCE_TOOL = "ReadMcpResourceTool"
READ_RESOURCE_DIR_TOOL = "ReadMcpResourceDirTool"
#: The MIME type of a child that is itself a directory.
DIRECTORY_MIME = "inode/directory"
_SCHEMA = "https://json-schema.org/draft/2020-12/schema"

#: ``(tool, arguments, ctx) -> output``: the extension's way to reach the
#: tool's server.
Invoke = Callable[["McpTool", dict[str, Any], ToolContext], Awaitable[ToolOutput]]
#: ``(tool, arguments, ctx, outcome) -> output``: a call that parked on the
#: server's question, resumed with the person's answer.
Resume = Callable[["McpTool", dict[str, Any], ToolContext, Any], Awaitable[ToolOutput]]
#: ``(tool, arguments, ctx) -> output`` for a sign-in tool.
SignInFn = Callable[["McpAuthTool", dict[str, Any], ToolContext], Awaitable[ToolOutput]]
#: ``(ctx, servers) -> {alias: server}``: the task's configured servers.
ServersOf = Callable[[ToolContext], Mapping[str, McpServer]]
#: ``(ctx, server, request) -> result``: one request on a server's connection.
Use = Callable[[ToolContext, McpServer, Callable[[Any], Awaitable[Any]]], Awaitable[Any]]


class McpTool:
    """One server tool. Its result is not clearable: calling it again may
    not give the same answer (ADR-066)."""

    read_only = False
    clearable = False
    def __init__(self, *, name: str, alias: str, remote: str, description: str,
                 input_schema: dict[str, Any], invoke: Invoke, deferred: bool = True,
                 resume: Resume | None = None, loaded_description: str | None = None,
                 search_hint: str = "") -> None:
        self.name = name
        self.alias = alias
        self.remote = remote
        self.description = description
        self.input_schema = input_schema
        # The server checks its own input (Claude Code passes it on as sent).
        self._flags = ToolFlags(unchecked_input=True, listed_last=True, deferred=deferred,
                                search_hint=search_hint,
                                loaded_description=loaded_description or description)
        self._invoke = invoke
        self._resume = resume

    @property
    def flags(self) -> ToolFlags:
        """Its flags; ``silent_reply_after="success"`` for a tool named in
        ``NOETA_TERMINAL_MCP_TOOLS``: its result is meant to end the turn."""
        if named_in(terminal_tools(), self.name, "NOETA_TERMINAL_MCP_TOOLS"):
            return replace(self._flags, silent_reply_after="success")
        return self._flags

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return await self._invoke(self, dict(input), ctx)

    async def resume(self, input: dict[str, Any], ctx: ToolContext,
                     outcome: Any) -> ToolOutput:
        if self._resume is None:
            return text_output(f"{self.name}: the call could not finish", is_error=True)
        return await self._resume(self, dict(input), ctx, outcome)


#: The sign-in tools, by their raw name.
AUTH_STUBS = ("authenticate", "complete_authentication")
_EMPTY_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object", "properties": {}, "additionalProperties": False}
_CALLBACK_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"callback_url": {
        "type": "string",
        "description": "The whole URL from the browser's address bar after authorizing, "
                       "e.g. http://localhost:<port>/callback?code=...&state=..."}},
    "required": ["callback_url"],
    "additionalProperties": False}


def auth_stub_entries(server: McpServer) -> list[dict[str, Any]]:
    """The recorded tool entries standing in for a server that needs a
    sign-in."""
    names = {stub: make_tool_name(server.alias, stub) for stub in AUTH_STUBS}
    fill = {"server": server.alias, "transport": server.transport,
            "authenticate": names["authenticate"]}
    return [{"name": names[stub], "remote": stub, "stub": stub,
             "description": load_description("noeta.plugins.mcp", stub).format(**fill),
             "input_schema": _EMPTY_SCHEMA if stub == "authenticate" else _CALLBACK_SCHEMA}
            for stub in AUTH_STUBS]


class McpAuthTool:
    """``authenticate`` / ``complete_authentication`` for one server."""

    read_only = False
    clearable = False

    def __init__(self, *, name: str, alias: str, stub: str, description: str,
                 input_schema: dict[str, Any], sign_in: SignInFn, deferred: bool = True) -> None:
        self.name = name
        self.alias = alias
        self.stub = stub
        self.description = description
        self.input_schema = input_schema
        self.flags = ToolFlags(listed_last=True, deferred=deferred)
        self._sign_in = sign_in

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return await self._sign_in(self, dict(input), ctx)


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class ListMcpResourcesTool:
    name = LIST_RESOURCES_TOOL
    read_only = True
    flags = ToolFlags(
        retry_safe=True,
        deferred=True,
        search_hint="list the resources of connected MCP servers",
    )
    clearable = True
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
        return text_output(_compact(out))


class ReadMcpResourceTool:
    name = READ_RESOURCE_TOOL
    read_only = True
    flags = ToolFlags(
        retry_safe=True,
        deferred=True,
        search_hint="read one MCP resource by its URI",
    )
    clearable = True
    description = """
Reads one resource from an MCP server, given the server's name and a resource's URI.

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
        if isinstance(result, dict) and isinstance(result.get("contents"), list):
            result = {**result, "contents": [
                await _save_blob(ctx, server.alias, entry, i) if isinstance(entry, dict)
                and isinstance(entry.get("blob"), str) else entry
                for i, entry in enumerate(result["contents"])]}
        return text_output(_compact(result))


async def _save_blob(ctx: ToolContext, alias: str, entry: dict[str, Any],
                     index: int) -> dict[str, Any]:
    """A binary resource entry with its bytes saved under the spill
    directory (for ``Read``) in place of the base64 (Claude Code's shape)."""
    mime = entry.get("mimeType") if isinstance(entry.get("mimeType"), str) else None
    out = {k: v for k, v in entry.items() if k != "blob"}
    try:
        raw = base64.b64decode(entry["blob"], validate=True)
    except (binascii.Error, ValueError):
        return {**out, "text": "Binary content could not be saved: it is not valid base64"}
    if not ctx.spill_dir:
        return {**out, "text": f"Binary content ({mime or 'unknown type'}, "
                               f"{format_size(len(raw))}) could not be saved: this host "
                               "keeps no files for tool output"}
    ext = (mimetypes.guess_extension(mime) if mime else None) or ".bin"
    base = posixpath.splitext(spill_path(ctx.spill_dir, ctx.task_id, ctx.call_id))[0]
    path = f"{base}-resource-{index}{ext}"
    if not await save_spilled(ctx, path, raw):
        return {**out, "text": "The binary data was not saved; it is not available here."}
    return {**out, "blobSavedTo": path,
            "text": f"[Resource from {alias} at {entry.get('uri')}] Binary content "
                    f"({mime or 'unknown type'}, {format_size(len(raw))}) saved to {path}"}


class ReadMcpResourceDirTool:
    name = READ_RESOURCE_DIR_TOOL
    read_only = True
    flags = ToolFlags(
        retry_safe=True,
        deferred=True,
        search_hint="list what an MCP directory resource contains",
    )
    clearable = True
    description = """
Shows what one directory resource of an MCP server holds, one level down \
(`resources/directory/read`); it does not descend into subdirectories.

Parameters:
- server (required): the name of the MCP server, as configured
- uri (required): the URI of the directory resource

Each child has its own URI; a subdirectory is shown with a trailing "/" (MIME type \
"inode/directory"): call this tool again on its URI to go deeper. Read a file resource with \
ReadMcpResourceTool. Works only with servers that support directory listing; others say \
so.
"""
    input_schema: dict[str, Any] = {
        "$schema": _SCHEMA,
        "type": "object",
        "properties": {
            "server": {"type": "string", "description": "The MCP server's name."},
            "uri": {"type": "string", "description": "The directory resource's URI."},
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
            return text_output(f"{self.name}: `uri` must be a non-empty string.", is_error=True)
        server = servers.get(alias) if isinstance(alias, str) else None
        if server is None:
            return text_output(f"No MCP server named {alias!r}. Configured: "
                               f"{', '.join(sorted(servers)) or 'none'}.", is_error=True)
        try:
            children = await self._use(ctx, server, lambda c: c.read_directory(uri))
        except McpServerError as exc:
            # The server is up and said no: an answer, not a failed call
            # (Claude Code's "not a directory" / "no directory listing").
            return text_output(f"{alias} did not list {uri}: {exc.detail}. If it is a file "
                               "resource, read it with ReadMcpResourceTool; a server without "
                               "directory listing answers this way too.")
        except McpError as exc:
            return text_output(f"{self.name}: {alias} could not be reached to list {uri} "
                               f"({exc}).", is_error=True)
        return text_output(directory_listing(children))


def directory_listing(children: list[dict[str, Any]]) -> str:
    """Claude Code's shape: a count line and one name per line (a trailing
    ``/`` for a subdirectory), or ``Directory is empty.``; then the
    children as compact JSON (URI, name, MIME type)."""
    entries = [{"uri": str(c.get("uri") or ""), "name": str(c.get("name") or c.get("uri") or ""),
                **({"mimeType": str(c["mimeType"])} if c.get("mimeType") is not None else {})}
               for c in children]
    names = [e["name"] + ("/" if e.get("mimeType") == DIRECTORY_MIME else "") for e in entries]
    head = (f"Directory listing ({len(names)} {'entry' if len(names) == 1 else 'entries'}):\n"
            + "\n".join(names)) if names else "Directory is empty."
    return f"{head}\n{_compact({'resources': entries})}"
