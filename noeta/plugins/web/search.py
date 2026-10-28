"""``WebSearch``: ranked hits as a Markdown list, through the Tavily Search
API as in 0.6. Every result opens with the provenance line."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from typing import Any, Protocol

import httpx

from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.plugins.web.render import collapse_whitespace
from noeta.toolkit.tools import SCHEMA_DIALECT, limit_output, truncate_bytes

__all__ = ["HttpSearchTransport", "SearchResult", "SearchTransport", "WebSearchTool"]

#: The 0.6 environment variable a host may still set instead of passing the
#: key to ``Web``.
SEARCH_API_KEY_ENV = "NOETA_WEB_SEARCH_API_KEY"
_INLINE_RESULT_MAX_CHARS = 100_000
_MAX_TITLE_BYTES = 200
_MAX_URL_BYTES = 512
_MAX_SNIPPET_BYTES = 1000
DEFAULT_COUNT = 5
MAX_COUNT = 20
#: Claude Code's ``maxResultSizeChars`` for WebSearch.
WEBSEARCH_RESULT_LIMIT = 100_000


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str


class SearchTransport(Protocol):
    """query -> ranked hits. Raises on transport / HTTP failure."""

    async def search(self, query: str, count: int) -> list[SearchResult]: ...


def _clamp_count(raw: Any) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool):
        return DEFAULT_COUNT
    return max(1, min(raw, MAX_COUNT))


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

    async def search(self, query: str, count: int) -> list[SearchResult]:
        client = self.client or httpx.AsyncClient(timeout=self.timeout)
        try:
            resp = await client.post(self.endpoint,
                                     headers={"Authorization": f"Bearer {self.api_key}"},
                                     json={"query": query, "max_results": count})
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
        self.description = (resources.files(__package__) / "web_search.md").read_text(
            "utf-8").rstrip("\n")
        self.input_schema: dict[str, Any] = {
            "$schema": SCHEMA_DIALECT,
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 2,
                          "description": "What to search for."},
                "count": {"type": "integer",
                          "description": "How many results to return (default 5, at most 20)."},
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        query = input.get("query")
        if not isinstance(query, str) or not query.strip():
            return text_output("WebSearch requires a non-empty 'query'", is_error=True)
        count = _clamp_count(input.get("count"))
        try:
            results = await self.transport.search(query, count)
        except Exception as exc:  # noqa: BLE001 - degrade, don't crash the step
            return text_output(f"WebSearch failed: {exc}", is_error=True)
        if not results:
            return text_output(f"No results were found for {query!r}. "
                               "Try different search terms.", hits=0)
        body = results_to_markdown(results)
        if len(body) > _INLINE_RESULT_MAX_CHARS:
            total = len(body)
            body = body[:_INLINE_RESULT_MAX_CHARS] + (
                f"\n(Results truncated: showing the first "
                f"{_INLINE_RESULT_MAX_CHARS} of {total} characters. Refine "
                "the query or lower 'count'.)")
        text = await limit_output(body, WEBSEARCH_RESULT_LIMIT, ctx)
        return text_output(text, hits=len(results))
