"""``WebSearch``: ranked hits through the Tavily Search API as in 0.6, in
Claude Code's result shape: the query, the hits' titles and URLs as a JSON
list, then each hit with its snippet, and a reminder to cite the sources.

Claude Code's parameters: ``allowed_domains`` / ``blocked_domains`` filter
the hits (passed to a transport that takes them, and applied to what comes
back either way) and ``mode`` picks a quick search or a thorough one.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.plugins.web.render import collapse_whitespace
from noeta.toolkit.tools import SCHEMA_DIALECT, limit_output, load_description, truncate_bytes

__all__ = ["HttpSearchTransport", "SearchResult", "SearchTransport", "WebSearchTool"]

#: The 0.6 environment variable a host may still set instead of passing the
#: key to ``Web``.
SEARCH_API_KEY_ENV = "NOETA_WEB_SEARCH_API_KEY"
_INLINE_RESULT_MAX_CHARS = 100_000
_MAX_TITLE_BYTES = 200
_MAX_URL_BYTES = 512
_MAX_SNIPPET_BYTES = 1000
DEFAULT_COUNT = 5
#: Hits asked for in ``extended`` mode.
EXTENDED_COUNT = 10
MODES = ("standard", "extended")
#: Claude Code's ``maxResultSizeChars`` for WebSearch (1e5) under its 50,000 ceiling.
WEBSEARCH_RESULT_LIMIT = 50_000
#: The line closing every result that has hits.
CITE_REMINDER = "Reminder: cite the sources above in your reply, as Markdown links."


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str


class SearchTransport(Protocol):
    """query -> ranked hits. Raises on transport / HTTP failure. A transport
    may also take the keyword arguments ``allowed_domains``,
    ``blocked_domains`` and ``extended`` (see :class:`HttpSearchTransport`)."""

    async def search(self, query: str, count: int) -> list[SearchResult]: ...


def _domains(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [d.strip().lower().lstrip(".") for d in raw if isinstance(d, str) and d.strip()]


def _host_in(url: str, domains: Sequence[str]) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def _takes_filters(transport: SearchTransport) -> bool:
    try:
        params = inspect.signature(transport.search).parameters
    except (TypeError, ValueError):
        return False
    return {"allowed_domains", "blocked_domains", "extended"} <= set(params) or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def results_to_markdown(results: list[SearchResult]) -> str:
    blocks: list[str] = []
    for i, r in enumerate(results, start=1):
        title = r.title.strip() or r.url.strip()
        line = f"{i}. [{title}]({r.url.strip()})"
        snippet = r.snippet.strip()
        if snippet:
            line += f"\n   {snippet}"
        blocks.append(line)
    return "\n\n".join(blocks)


def parse_tavily(payload: dict[str, Any]) -> list[SearchResult]:
    return [
        SearchResult(
            title=truncate_bytes(collapse_whitespace(str(item.get("title", ""))),
                                 _MAX_TITLE_BYTES),
            url=truncate_bytes(collapse_whitespace(str(item.get("url", ""))), _MAX_URL_BYTES),
            # Tavily names the per-hit excerpt ``content``.
            snippet=truncate_bytes(collapse_whitespace(str(item.get("content", ""))),
                                   _MAX_SNIPPET_BYTES),
        )
        for item in payload.get("results", []) or []
    ]


class HttpSearchTransport:
    """``POST https://api.tavily.com/search`` with a Bearer key."""

    def __init__(self, api_key: str, *, endpoint: str = "https://api.tavily.com/search",
                 timeout: float = 10.0, client: httpx.AsyncClient | None = None) -> None:
        self.api_key = api_key
        self.endpoint = endpoint
        self.timeout = timeout
        self.client = client

    async def search(self, query: str, count: int, *, allowed_domains: Sequence[str] = (),
                     blocked_domains: Sequence[str] = (), extended: bool = False
                     ) -> list[SearchResult]:
        body: dict[str, Any] = {"query": query, "max_results": count,
                                "search_depth": "advanced" if extended else "basic"}
        if allowed_domains:
            body["include_domains"] = list(allowed_domains)
        if blocked_domains:
            body["exclude_domains"] = list(blocked_domains)
        client = self.client or httpx.AsyncClient(timeout=self.timeout)
        try:
            resp = await client.post(self.endpoint,
                                     headers={"Authorization": f"Bearer {self.api_key}"},
                                     json=body)
            resp.raise_for_status()
            payload = resp.json()
        finally:
            if self.client is None:
                await client.aclose()
        return parse_tavily(payload if isinstance(payload, dict) else {})


class WebSearchTool:
    name = "WebSearch"
    read_only = True
    retry_safe = True
    clearable = True
    deferred = True

    def __init__(self, transport: SearchTransport) -> None:
        self.transport = transport
        self.description = load_description("noeta.plugins.web", "web_search")
        self.input_schema: dict[str, Any] = {
            "$schema": SCHEMA_DIALECT,
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 2,
                          "description": "What to search for."},
                "allowed_domains": {"type": "array", "items": {"type": "string"},
                                    "description": "Keep only results from these domains."},
                "blocked_domains": {"type": "array", "items": {"type": "string"},
                                    "description": "Leave out results from these domains."},
                "mode": {"type": "string", "enum": list(MODES), "description": (
                    "\"standard\": a quick, cheap search, right for plain lookups (facts, "
                    "official pages, documentation, well-known topics) and follow-ups. "
                    "\"extended\": a thorough, fresh search that costs several times more "
                    "and takes longer.")},
            },
            "required": ["query", "mode"],
            "additionalProperties": False,
        }

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        query = input.get("query")
        if not isinstance(query, str) or not query.strip():
            return text_output("WebSearch requires a non-empty 'query'", is_error=True)
        allowed, blocked = _domains(input.get("allowed_domains")), \
            _domains(input.get("blocked_domains"))
        if allowed and blocked:
            return text_output("WebSearch takes allowed_domains or blocked_domains, not both "
                               "in one search", is_error=True)
        extended = input.get("mode") == "extended"
        count = EXTENDED_COUNT if extended else DEFAULT_COUNT
        try:
            if _takes_filters(self.transport):
                results = await self.transport.search(  # type: ignore[call-arg]
                    query, count, allowed_domains=allowed, blocked_domains=blocked,
                    extended=extended)
            else:
                results = await self.transport.search(query, count)
        except Exception as exc:  # noqa: BLE001 - degrade, don't crash the step
            return text_output(f"WebSearch failed: {exc}", is_error=True)
        results = [r for r in results if (not allowed or _host_in(r.url, allowed))
                   and not _host_in(r.url, blocked)]
        header = f'Web search results for query: "{query}"\n\n'
        if not results:
            return text_output(header + "No links found.", hits=0)
        links = json.dumps([{"title": r.title.strip() or r.url.strip(), "url": r.url.strip()}
                            for r in results], ensure_ascii=False)
        body = f"{header}Links: {links}\n\n{results_to_markdown(results)}"
        if len(body) > _INLINE_RESULT_MAX_CHARS:
            total = len(body)
            body = body[:_INLINE_RESULT_MAX_CHARS] + (
                f"\n(Results truncated: showing the first "
                f"{_INLINE_RESULT_MAX_CHARS} of {total} characters. Refine "
                "the query.)")
        text = await limit_output(body, WEBSEARCH_RESULT_LIMIT, ctx)
        return text_output(f"{text}\n\n{CITE_REMINDER}", hits=len(results))
