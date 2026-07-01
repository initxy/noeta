"""`noeta.tools.web` — the web tool pack (second wave).

Ships two read-only tools that take no ``WorkspaceRoot`` (so they live here, not
in ``noeta.tools.fs``), both ``risk_level="low"`` with no workspace mutation:

* ``webfetch`` — fetch a public URL, convert the HTML body to a compact Markdown
  rendering, and return it (offloading the full body to the ContentStore when it
  exceeds the inline byte budget). Always present.
* ``web_search`` — run a web search and return ranked hits as Markdown. Present
  only when ``NOETA_WEB_SEARCH_API_KEY`` is set (no key ⇒ omitted, like a failed
  MCP server), since its backend is otherwise unreachable.

Each tool's HTTP transport is an injected seam (:class:`FetchTransport` /
:class:`SearchTransport`) so tests drive it with a fake (no live network) while
production wires the real httpx-backed transport. Private / authenticated URLs
and auth / quota failures surface at the transport (HTTP 401/403 or a connection
error) and degrade to a clear ``ToolResult(success=False, ...)`` — these
limitations are documented in the tools' description resources.
"""

from __future__ import annotations

from noeta.tools.web.fetch import (
    FetchTransport,
    HttpFetchTransport,
    WebFetchTool,
    build_web_tools,
)
from noeta.tools.web.search import (
    HttpSearchTransport,
    SearchResult,
    SearchTransport,
    WebSearchTool,
)


__all__ = [
    "FetchTransport",
    "HttpFetchTransport",
    "HttpSearchTransport",
    "SearchResult",
    "SearchTransport",
    "WebFetchTool",
    "WebSearchTool",
    "build_web_tools",
]
