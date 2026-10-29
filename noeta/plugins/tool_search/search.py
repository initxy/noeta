"""``ToolSearch``: loads deferred tools by exact name or by keywords; its
result is one ``ToolReference`` per match."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from noeta.core.tool import Tool, ToolContext, ToolOutput, text_output
from noeta.core.types import ToolReference

TOOL_SEARCH = "ToolSearch"
#: Matches a keyword search returns when the model gives no count.
DEFAULT_MAX_RESULTS = 5
#: A ``select:`` query reads at most this many names.
MAX_SELECT_NAMES = 64
#: A keyword query reads at most this many words.
MAX_KEYWORDS = 32

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

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
        description (whole words), after ``+word`` keeps only the tools
        whose name holds ``word``."""
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
            if not all(any(r in p for p in (*parts, *coarse)) for r in required):
                continue
            score = 0
            for t in terms:
                term = 0
                if t in parts:
                    term += 12 if mcp else 10
                elif any(t in p for p in parts):
                    term += 6 if mcp else 5
                if t in coarse:
                    term += 12 if mcp else 10
                elif any(t in c for c in coarse):
                    term += 4 if mcp else 3
                # Only in the whole name (across a ``__`` boundary): this
                # term's own score decides, not what earlier terms scored.
                if term == 0 and t in name.lower():
                    term += 3
                if whole[t].search(text):
                    term += 2
                score += term
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
        except (TypeError, ValueError, OverflowError):  # OverflowError: an infinity
            max_results = DEFAULT_MAX_RESULTS
        found = self.search(query, max_results)
        if not found:
            return text_output(f"No deferred tool matches {query.strip()!r}.")
        return ToolOutput(content=[ToolReference(tool_name=n) for n in found])
