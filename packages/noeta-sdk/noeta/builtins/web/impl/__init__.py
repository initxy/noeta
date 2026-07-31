"""``noeta.builtins.web.impl`` — the web tool pack implementation.

The ``web`` built-in plugin's body. Ships two
read-only tools that take no ``WorkspaceRoot``, both ``risk_level="low"``
with no workspace mutation:

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

This module is reached only through the plugin loader's ``ref`` resolution;
nothing imports it statically.
"""

from __future__ import annotations

from noeta.builtins.web.impl.fetch import (
    ContainerCurlFetchTransport,
    FetchTransport,
    HttpFetchTransport,
    WebFetchTool,
    build_web_tools,
)
from noeta.builtins.web.impl.search import (
    ContainerCurlSearchTransport,
    HttpSearchTransport,
    SearchResult,
    SearchTransport,
    WebSearchTool,
)
from noeta.execution.session_pack import PackContribution, SessionBuildContext


def build_web_session_pack(ctx: SessionBuildContext) -> PackContribution:
    """The web pack as a ``session_pack`` contribution.

    The manifest-declared factory (band 200) — appends directly after the fs
    pack, preserving the merged fs-then-web insertion order, and filters by
    the agent whitelist exactly as the fs base pack does. Sandbox mode routes
    webfetch / web_search egress THROUGH the container (curl via the
    ExecEnv); ``None`` keeps the host httpx path.
    """
    return PackContribution(
        tools={
            name: tool
            for name, tool in build_web_tools(exec_env=ctx.exec_env).items()
            if name in ctx.allowed_tools
        }
    )


__all__ = [
    "ContainerCurlFetchTransport",
    "ContainerCurlSearchTransport",
    "FetchTransport",
    "HttpFetchTransport",
    "HttpSearchTransport",
    "SearchResult",
    "SearchTransport",
    "WebFetchTool",
    "WebSearchTool",
    "build_web_session_pack",
    "build_web_tools",
]
