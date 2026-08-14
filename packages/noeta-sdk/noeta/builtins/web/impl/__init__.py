"""``noeta.builtins.web.impl`` — the web tool pack implementation.

The ``web`` built-in plugin's body. Ships two
read-only tools that take no ``WorkspaceRoot``, both ``risk_level="low"``
with no workspace mutation:

* ``webfetch`` — fetch a URL, convert the HTML body to a compact Markdown
  rendering, and answer the caller's ``prompt`` against it with an auxiliary
  model call (Claude Code's `WebFetch` shape), offloading the full rendering
  to the ContentStore as an audit artifact. Always present; with no ``"llm"``
  backend it degrades to returning the rendering itself.
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

from typing import Optional, cast

from noeta.builtins.web.impl.digest import LLMPageDigester, PageDigester
from noeta.builtins.web.impl.fetch import (
    ContainerCurlFetchTransport,
    CrossHostRedirect,
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
from noeta.protocols.messages import LLMProvider


def _digester_from(ctx: SessionBuildContext) -> Optional[PageDigester]:
    """Bind webfetch's digester off the generic build context.

    The host populates ``backends["llm"]`` with the session's own provider
    adapter, and ``plugin_config["web"]["digest_model"]`` with the
    alias-resolved ``Options.webfetch_model`` when set — absent, the digest
    runs on the session's main model. No ``"llm"`` backend (a bare builder
    call, older hosts) ⇒ no digester, and webfetch keeps its raw-render
    behaviour.
    """
    provider = ctx.backends.get("llm")
    if provider is None:
        return None
    model = ctx.config("web").get("digest_model") or ctx.model
    return LLMPageDigester(
        provider=cast(LLMProvider, provider), model=cast(str, model)
    )


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
            for name, tool in build_web_tools(
                exec_env=ctx.exec_env, digester=_digester_from(ctx)
            ).items()
            if name in ctx.allowed_tools
        }
    )


__all__ = [
    "ContainerCurlFetchTransport",
    "ContainerCurlSearchTransport",
    "CrossHostRedirect",
    "FetchTransport",
    "HttpFetchTransport",
    "HttpSearchTransport",
    "LLMPageDigester",
    "PageDigester",
    "SearchResult",
    "SearchTransport",
    "WebFetchTool",
    "WebSearchTool",
    "build_web_session_pack",
    "build_web_tools",
]
