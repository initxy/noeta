"""Deferred MCP schemas: two small tools stand in for N large ones.

A server spec with ``deferred=True`` keeps its tools registered under their
real ``mcp__{alias}__{tool}`` names — invocable, guarded, audited, approved —
but marks them ``advertised = False``, so the composer leaves their schemas out
of the provider tool list. In their place, once for every deferred server of
the build, the model is offered:

* ``ToolSearch`` — ``query`` → the matching deferred tools with name,
  description and full input schema (an empty query lists every one by name).
* ``McpCall`` — ``tool`` + ``arguments``. The tool carries a ``route_call``
  attribute: the default policy rewrites a ``McpCall`` call into a call to the
  named real tool (same ``call_id``) before the Guard sees it, so permission
  rules, approval, audit and the ToolRuntime all see the real name. The
  recorded assistant message keeps the ``McpCall`` tool_use the provider saw,
  and its result pairs back by ``call_id``.

The tool set stays fixed for the turn: the stable prefix carries these two
schemas instead of N, and nothing is added to it mid-task.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from noeta.protocols.resources import load_markdown
from noeta.protocols.tool import ToolContext, ToolResult

from noeta.builtins.mcp.impl.tool import McpTool, cap_injected


__all__ = [
    "MCP_CALL_TOOL",
    "TOOL_SEARCH_MAX_RESULTS",
    "TOOL_SEARCH_TOOL",
    "McpCallTool",
    "ToolSearchTool",
    "deferred_access_tools",
]


TOOL_SEARCH_TOOL = "ToolSearch"
MCP_CALL_TOOL = "McpCall"

#: Most tools one non-empty query returns, best match first.
TOOL_SEARCH_MAX_RESULTS = 5

#: Cap on one line of the empty-query listing.
_LISTING_LINE_MAX_CHARS = 160

_WORD_RE = re.compile(r"[a-z0-9]+")


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def _one_line(description: str) -> str:
    line = description.strip().splitlines()[0] if description.strip() else ""
    if len(line) > _LISTING_LINE_MAX_CHARS:
        line = line[: _LISTING_LINE_MAX_CHARS - 1].rstrip() + "…"
    return line


class ToolSearchTool:
    """Look up deferred MCP tools by name or description words."""

    name = TOOL_SEARCH_TOOL
    risk_level = "low"

    def __init__(self, deferred: Mapping[str, McpTool]) -> None:
        self._deferred = dict(deferred)
        self.description = load_markdown(__package__, "tool_search")
        self.input_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Words describing the tool you need, or its exact "
                        "name. Empty lists every deferred tool."
                    ),
                },
            },
            "required": ["query"],
        }

    def search(self, query: str) -> list[McpTool]:
        """The matching deferred tools, best first, at most
        :data:`TOOL_SEARCH_MAX_RESULTS`. An exact name (Noeta-side or the
        server's raw name) wins outright; otherwise a tool scores one point
        per query word found in its name or description, ties by name."""
        q = query.strip()
        for tool in self._deferred.values():
            if q in (tool.name, tool.remote_tool_name):
                return [tool]
        wanted = _words(q)
        scored: list[tuple[int, str, McpTool]] = []
        for tool in self._deferred.values():
            hits = len(wanted & (_words(tool.name) | _words(tool.description)))
            if hits:
                scored.append((-hits, tool.name, tool))
        scored.sort(key=lambda row: (row[0], row[1]))
        return [tool for _, _, tool in scored[:TOOL_SEARCH_MAX_RESULTS]]

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = arguments.get("query", "")
        if not isinstance(query, str):
            return ToolResult(
                success=False, summary="ToolSearch: query must be a string"
            )
        if not self._deferred:
            return ToolResult(
                success=True,
                output="No deferred tools are available this turn.",
                summary="ToolSearch: no deferred tools",
            )
        if not query.strip():
            listing = "\n".join(
                f"{tool.name} — {_one_line(tool.description)}"
                for tool in self._deferred.values()
            )
            return ToolResult(
                success=True,
                output=cap_injected(listing, kind="tool listing"),
                summary=f"ToolSearch: listed {len(self._deferred)} tool(s)",
            )
        found = self.search(query)
        if not found:
            return ToolResult(
                success=True,
                output=(
                    f"No deferred tool matches {query!r}. Call ToolSearch "
                    "with an empty query to list them all."
                ),
                summary="ToolSearch: no match",
            )
        body = json.dumps(
            [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in found
            ],
            ensure_ascii=False,
        )
        return ToolResult(
            success=True,
            output=cap_injected(body, kind="tool search result"),
            summary=f"ToolSearch: {len(found)} match(es)",
        )


class McpCallTool:
    """Run a deferred MCP tool by name — through the policy's call routing.

    ``route_call`` is what makes it work: the default policy turns a call to
    this tool into a call to the named real tool before the Guard sees it.
    ``invoke`` runs only when a policy did not route, and then refuses —
    running the real tool from here would slip past the Guard."""

    name = MCP_CALL_TOOL
    risk_level = "low"

    def __init__(self, deferred: Mapping[str, McpTool]) -> None:
        self._deferred = frozenset(deferred)
        self.description = load_markdown(__package__, "mcp_call")
        self.input_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "tool": {
                    "type": "string",
                    "description": "The exact name ToolSearch gave.",
                },
                "arguments": {
                    "type": "object",
                    "description": "The tool's arguments, per its input schema.",
                },
            },
            "required": ["tool", "arguments"],
        }

    def route_call(self, arguments: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        """``(real tool name, its arguments)``, or ``ValueError`` naming what
        is wrong with the call — the policy answers the call with it."""
        target = arguments.get("tool")
        if not isinstance(target, str) or not target:
            raise ValueError("McpCall needs `tool`: the exact name ToolSearch gave.")
        if target not in self._deferred:
            if target.startswith("mcp__"):
                raise ValueError(
                    f"{target!r} is not a deferred tool. If it is in your "
                    "tool list, call it directly; otherwise find the right "
                    "name with ToolSearch."
                )
            raise ValueError(
                f"No deferred tool named {target!r}; find the right name "
                "with ToolSearch."
            )
        args = arguments.get("arguments")
        if not isinstance(args, dict):
            raise ValueError(
                f"McpCall needs `arguments`: an object for {target!r}, per "
                "the input schema ToolSearch returned."
            )
        return target, dict(args)

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(
            success=False,
            summary=(
                "McpCall: this agent's policy does not route McpCall, so the "
                "call did not run."
            ),
        )


def deferred_access_tools(
    tools: Mapping[str, McpTool],
) -> dict[str, Any]:
    """The ``ToolSearch`` / ``McpCall`` pair over the not-advertised tools of
    ``tools``, in that order."""
    deferred = {
        name: tool for name, tool in tools.items() if not tool.advertised
    }
    return {
        TOOL_SEARCH_TOOL: ToolSearchTool(deferred),
        MCP_CALL_TOOL: McpCallTool(deferred),
    }
