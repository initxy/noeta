"""The tools the MCP extension offers: one per server tool, plus the
``ToolSearch`` / ``McpCall`` pair for deferred servers.

A deferred server's tools stay registered under their real
``mcp__{alias}__{tool}`` names but carry ``advertised = False``, so the request
leaves their schemas out. The model finds them with ``ToolSearch`` and runs
them with ``McpCall``, which the extension's ``before_tool`` rewrites into a
call on the real tool with the same call id: every guard, approval and record
sees the real name. ``McpCall.run`` only runs when the call was answered
before it was rewritten (an approval decided on the ``McpCall`` itself); it
then runs the real tool the same way.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from functools import cache
from importlib import resources
from typing import Any

from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.plugins.mcp.config import MCP_PREFIX, cap_injected

__all__ = [
    "MCP_CALL_TOOL",
    "TOOL_SEARCH_MAX_RESULTS",
    "TOOL_SEARCH_TOOL",
    "McpCallTool",
    "McpTool",
    "ToolSearchTool",
    "route_call",
]

TOOL_SEARCH_TOOL = "ToolSearch"
MCP_CALL_TOOL = "McpCall"
#: Most tools one non-empty query returns, best match first.
TOOL_SEARCH_MAX_RESULTS = 5
_LISTING_LINE_MAX_CHARS = 160
_WORD_RE = re.compile(r"[a-z0-9]+")

#: ``(tool, arguments, ctx) -> output``: the extension's way to reach the
#: tool's server.
Invoke = Callable[["McpTool", dict[str, Any], ToolContext], Awaitable[ToolOutput]]


@cache
def _description(name: str) -> str:
    return (resources.files("noeta.plugins.mcp").joinpath("descriptions", f"{name}.md")
            .read_text(encoding="utf-8").strip())


class McpTool:
    """One server tool. Its result is not clearable: calling it again may
    not give the same answer (ADR-066)."""

    read_only = False
    clearable = False

    def __init__(self, *, name: str, alias: str, remote: str, description: str,
                 input_schema: dict[str, Any], invoke: Invoke, advertised: bool = True) -> None:
        self.name = name
        self.alias = alias
        self.remote = remote
        self.description = description
        self.input_schema = input_schema
        self.advertised = advertised
        self._invoke = invoke

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return await self._invoke(self, dict(input), ctx)


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def _one_line(description: str) -> str:
    line = description.strip().splitlines()[0] if description.strip() else ""
    if len(line) > _LISTING_LINE_MAX_CHARS:
        line = line[: _LISTING_LINE_MAX_CHARS - 1].rstrip() + "…"
    return line


class ToolSearchTool:
    name = TOOL_SEARCH_TOOL
    read_only = True
    clearable = False

    def __init__(self, deferred: Mapping[str, McpTool]) -> None:
        self.deferred = dict(deferred)
        self.description = _description("tool_search")
        self.input_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": ("Words describing the tool you need, or its exact "
                                    "name. Empty lists every deferred tool."),
                },
            },
            "required": ["query"],
        }

    def search(self, query: str) -> list[McpTool]:
        """An exact name (offered or the server's raw name) wins outright;
        otherwise one point per query word found in a tool's name or
        description, ties by name, at most five."""
        q = query.strip()
        for tool in self.deferred.values():
            if q in (tool.name, tool.remote):
                return [tool]
        wanted = _words(q)
        scored: list[tuple[int, str, McpTool]] = []
        for tool in self.deferred.values():
            hits = len(wanted & (_words(tool.name) | _words(tool.description)))
            if hits:
                scored.append((-hits, tool.name, tool))
        scored.sort(key=lambda row: (row[0], row[1]))
        return [tool for _, _, tool in scored[:TOOL_SEARCH_MAX_RESULTS]]

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        query = input.get("query", "")
        if not isinstance(query, str):
            return text_output("ToolSearch: query must be a string", is_error=True)
        if not self.deferred:
            return text_output("No deferred tools are available this turn.")
        if not query.strip():
            listing = "\n".join(f"{t.name} — {_one_line(t.description)}"
                                for t in self.deferred.values())
            return text_output(cap_injected(listing, kind="tool listing"))
        found = self.search(query)
        if not found:
            return text_output(f"No deferred tool matches {query!r}. Call ToolSearch "
                               "with an empty query to list them all.")
        body = json.dumps([{"name": t.name, "description": t.description,
                            "input_schema": t.input_schema} for t in found],
                          ensure_ascii=False)
        return text_output(cap_injected(body, kind="tool search result"))


def route_call(arguments: Mapping[str, Any],
               deferred: Mapping[str, McpTool]) -> tuple[str, dict[str, Any]]:
    """``(real tool name, its arguments)`` for an ``McpCall``, or
    ``ValueError`` saying what is wrong; that text answers the call alone."""
    target = arguments.get("tool")
    if not isinstance(target, str) or not target:
        raise ValueError("McpCall needs `tool`: the exact name ToolSearch gave.")
    if target not in deferred:
        if target.startswith(MCP_PREFIX):
            raise ValueError(
                f"{target!r} is not a deferred tool. If it is in your tool list, call it "
                "directly; otherwise find the right name with ToolSearch.")
        raise ValueError(f"No deferred tool named {target!r}; find the right name "
                         "with ToolSearch.")
    args = arguments.get("arguments")
    if not isinstance(args, dict):
        raise ValueError(f"McpCall needs `arguments`: an object for {target!r}, per the "
                         "input schema ToolSearch returned.")
    return target, dict(args)


class McpCallTool:
    name = MCP_CALL_TOOL
    read_only = False
    clearable = False

    def __init__(self, deferred: Mapping[str, McpTool]) -> None:
        self.deferred = dict(deferred)
        self.description = _description("mcp_call")
        self.input_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "tool": {"type": "string", "description": "The exact name ToolSearch gave."},
                "arguments": {"type": "object",
                              "description": "The tool's arguments, per its input schema."},
            },
            "required": ["tool", "arguments"],
        }

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        try:
            target, args = route_call(input, self.deferred)
        except ValueError as exc:
            return text_output(str(exc), is_error=True)
        return await self.deferred[target].run(args, ctx)
