"""Deferred tools: a tool that sets ``deferred = True`` is announced by name
only and sent to the model once ``ToolSearch`` has loaded it.

Any extension's tool may be deferred. While a task offers at least one, the
runner adds ``ToolSearch``; its result is a list of ``ToolReference`` blocks,
and the fold keeps every referenced name in ``TaskState.loaded_tools`` (a
compaction does not unload a tool). The request then sends each loaded
deferred tool with ``defer_loading`` and always sends
``DeferredToolPlaceholder``, a tool that is never called and keeps the
vendor's deferred-loading mode on from the first request.

Order: every sent tool sorted by name, then the tools that declare
``listed_last`` (server tools) sorted by name. The placeholder goes right
before the ``listed_last`` group; while that group is empty and no deferred
tool is loaded, it goes right before the last tool.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from noeta.core.tool import Tool, ToolContext, ToolOutput, text_output, tool_flag
from noeta.core.types import ToolReference, ToolSpec

__all__ = [
    "PLACEHOLDER",
    "TOOL_LOADED_TEXT",
    "TOOL_SEARCH",
    "ToolSearchTool",
    "deferred_names",
    "is_deferred",
    "request_tools",
    "with_tool_search",
]

TOOL_SEARCH = "ToolSearch"
PLACEHOLDER = "DeferredToolPlaceholder"
#: The text that follows a ``ToolSearch`` result in the same user message.
TOOL_LOADED_TEXT = "Tool loaded."
#: Matches a keyword search returns when the model gives no count.
DEFAULT_MAX_RESULTS = 5
#: A ``select:`` query reads at most this many names.
MAX_SELECT_NAMES = 64
#: A keyword query reads at most this many words.
MAX_KEYWORDS = 32

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

_PLACEHOLDER_DESCRIPTION = (
    "Reserved entry that only keeps deferred tool loading enabled. It does nothing; "
    "never call it.")

_SEARCH_DESCRIPTION = """\
Fetch the full definition of deferred tools so you can call them.

Some tools are deferred: only their names are listed, in a message in this conversation, and \
their parameter schemas are not loaded. Calling a deferred tool before loading it fails. Pass a \
query here; the tools it matches are loaded, and from then on you call each one directly by its \
own name, exactly like the tools defined at the start. A loaded tool stays available for the rest \
of the conversation.

The matched definitions come back in the same encoding as the tool list you started with: one \
<function>{"description": ..., "name": ..., "parameters": ...}</function> line per tool, inside \
a <functions> block.

Ways to query:
- "select:WebFetch,NotebookEdit" loads exactly the named tools
- "fetch url page" searches names and descriptions and loads up to max_results best matches
- "+mcp resource" keeps only tools whose name contains "mcp", ranked by the other words"""

_CAMEL = re.compile(r"([a-z])([A-Z])")
_MCP_SPLIT = re.compile(r"[\s_.]+")


def is_deferred(tool: Tool) -> bool:
    return tool_flag(tool, "deferred")


def _listed_last(tool: Tool) -> bool:
    return tool_flag(tool, "listed_last")


def deferred_names(tools: Iterable[Tool]) -> list[str]:
    """The names of the deferred tools among ``tools``, sorted: what the
    conversation announces (``TaskView.deferred_tool_names``)."""
    return sorted({t.name for t in tools if is_deferred(t)})


def with_tool_search(tools: Sequence[Tool]) -> list[Tool]:
    """``tools`` plus ``ToolSearch`` when any of them is deferred (and none
    is already called so): the tools a task offers."""
    out = list(tools)
    deferred = {t.name: t for t in out if is_deferred(t)}
    if deferred and all(t.name != TOOL_SEARCH for t in out):
        out.append(ToolSearchTool(deferred))
    return out


def placeholder_spec() -> ToolSpec:
    return ToolSpec(name=PLACEHOLDER, description=_PLACEHOLDER_DESCRIPTION,
                    input_schema={"type": "object", "properties": {}}, defer_loading=True)


def request_tools(tools: Sequence[Tool], loaded: Iterable[str],
                  describe: Callable[[Tool], str]) -> list[ToolSpec]:
    """The tool list of a request: every tool that is not deferred, plus each
    deferred one that is loaded, in the module's order. ``describe`` gives a
    tool's (possibly overridden) description."""
    done = set(loaded)
    any_deferred = any_loaded = False
    sent: list[Tool] = []
    for t in tools:
        if is_deferred(t):
            any_deferred = True
            if t.name not in done:
                continue
            any_loaded = True
        sent.append(t)
    def spec(t: Tool) -> ToolSpec:
        return ToolSpec(name=t.name, description=describe(t), input_schema=t.input_schema,
                        defer_loading=is_deferred(t))
    body = sorted((t for t in sent if not _listed_last(t)), key=lambda t: t.name)
    tail = sorted((t for t in sent if _listed_last(t)), key=lambda t: t.name)
    head = [spec(t) for t in body]
    rest = [spec(t) for t in tail]
    if not any_deferred:
        return head + rest
    if rest or any_loaded or not head:
        return [*head, placeholder_spec(), *rest]
    return [*head[:-1], placeholder_spec(), head[-1]]


def _mcp_tool_part(name: str) -> str | None:
    """``t`` of an ``mcp__<server>__t`` tool name, else ``None``."""
    if not name.startswith("mcp__"):
        return None
    _, sep, tool = name[len("mcp__"):].partition("__")
    return tool if sep and tool else None


def _name_parts(name: str) -> tuple[list[str], list[str], bool]:
    """A tool name's words, its coarse parts (an MCP tool's server and tool
    names, else the whole name) and whether it is an MCP tool."""
    tool = _mcp_tool_part(name)
    if tool is not None:
        server = name[len("mcp__"):-len(tool) - 2]
        coarse = [p.lower() for p in (server, tool) if p]
        return [w for c in coarse for w in _MCP_SPLIT.split(c) if w], coarse, True
    words = _CAMEL.sub(r"\1 \2", name).replace("_", " ").lower().split()
    return words, [name.lower()], False


class ToolSearchTool:
    """Loads deferred tools: answers with one ``ToolReference`` per match."""

    name = TOOL_SEARCH
    read_only = True
    retry_safe = True
    clearable = False
    description = _SEARCH_DESCRIPTION
    input_schema: dict[str, Any] = {
        "$schema": SCHEMA_DIALECT,
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": ('Which tools to load: "select:<name>[,<name>...]" for exact '
                                "names, otherwise keywords to search for."),
            },
            "max_results": {
                "type": "number",
                "default": DEFAULT_MAX_RESULTS,
                "description": "The most tools a keyword search loads (default 5).",
            },
        },
        "required": ["query", "max_results"],
        "additionalProperties": False,
    }

    def __init__(self, deferred: Mapping[str, Tool]) -> None:
        self._tools = dict(deferred)

    def search(self, query: str, max_results: int = DEFAULT_MAX_RESULTS) -> list[str]:
        q = query.strip()
        if q.lower().startswith("select:"):
            listed = q[len("select:"):].split(",")[:MAX_SELECT_NAMES]
            wanted = [n.strip() for n in listed if n.strip()]
            by_lower = {n.lower(): n for n in self._tools}
            out: list[str] = []
            for n in wanted:
                hit = n if n in self._tools else by_lower.get(n.lower())
                # An MCP tool may also be named by its own name, without the
                # ``mcp__<server>__`` prefix (every server's tool of that name).
                hits = [hit] if hit is not None else [
                    t for t in self._tools if _mcp_tool_part(t) == n]
                out.extend(h for h in hits if h not in out)
            return out
        return self._keyword_search(q, max_results)

    def _keyword_search(self, query: str, max_results: int) -> list[str]:
        """A query that is a tool's name loads that tool alone; an
        ``mcp__<server>`` prefix loads that server's tools; otherwise the
        words are scored against each tool's name parts (most) and its
        description (whole words), ``+word`` filtering first."""
        limit = max(0, max_results)
        q = " ".join(query.split()[:MAX_KEYWORDS]).lower()
        exact = next((n for n in self._tools if n.lower() == q), None)
        if exact is not None:
            return [exact]
        if q.startswith("mcp__") and len(q) > len("mcp__"):
            prefixed = [n for n in self._tools if n.lower().startswith(q)]
            if prefixed:
                return prefixed[:limit]
        words = q.split()
        required = [w[1:] for w in words if w.startswith("+") and len(w) > 1]
        plain = [w for w in words if not (w.startswith("+") and len(w) > 1)]
        terms = [*required, *plain] if required else words
        whole = {t: re.compile(rf"\b{re.escape(t)}\b") for t in terms}
        scored: list[tuple[int, str]] = []
        for name, tool in self._tools.items():
            parts, coarse, mcp = _name_parts(name)
            text = (getattr(tool, "description", "") or "").lower()
            if not all(r in parts or any(r in p for p in parts) or r in coarse
                       or any(r in c for c in coarse) or whole[r].search(text)
                       for r in required):
                continue
            score = 0
            for t in terms:
                if t in parts:
                    score += 12 if mcp else 10
                elif any(t in p for p in parts):
                    score += 6 if mcp else 5
                if t in coarse:
                    score += 12 if mcp else 10
                elif any(t in c for c in coarse):
                    score += 4 if mcp else 3
                if t in " ".join(parts) and score == 0:
                    score += 3
                if whole[t].search(text):
                    score += 2
            if score > 0:
                scored.append((score, name))
        scored.sort(key=lambda s: -s[0])  # stable: ties keep the tools' order
        return [name for _, name in scored[:limit]]

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        query = input.get("query")
        if not isinstance(query, str):
            return text_output("ToolSearch: `query` must be a string.", is_error=True)
        limit = input.get("max_results", DEFAULT_MAX_RESULTS)
        try:
            max_results = int(limit) if limit is not None else DEFAULT_MAX_RESULTS
        except (TypeError, ValueError):
            max_results = DEFAULT_MAX_RESULTS
        found = self.search(query, max_results)
        if not found:
            return text_output(f"No deferred tool matches {query.strip()!r}.")
        return ToolOutput(content=[ToolReference(tool_name=n) for n in found])
