"""The web tools: ``WebFetch`` and ``WebSearch``.

``WebFetch`` answers its ``prompt`` with a digest call through the task's
model (``ToolContext.complete``), or ``webfetch_model``, a cheaper one. A
host may instead pass ``digest`` (any ``ModelRequest -> ModelResponse``
coroutine). The page cache is kept for the :data:`TASKS_KEPT` most recently
active tasks.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar

from noeta.core.events import Recorded, TaskEnded
from noeta.core.extension import Extension, TaskView
from noeta.core.tool import Tool, ToolContext
from noeta.core.types import ModelRequest, ModelResponse, user
from noeta.plugins.web.fetch import (
    DIGEST_INSTRUCTIONS,
    DOCS_DIGEST_INSTRUCTIONS,
    Digest,
    FetchTransport,
    HttpFetchTransport,
    PageCache,
    WebFetchTool,
    preapproved,
    render_digest_prompt,
)
from noeta.plugins.web.search import (
    SEARCH_API_KEY_ENV,
    HttpSearchTransport,
    SearchTransport,
    WebSearchTool,
)
from noeta.toolkit.side_requests import side_request

__all__ = ["Web"]

#: Tasks whose page cache is kept.
TASKS_KEPT = 256

Complete = Callable[[ModelRequest], Awaitable[ModelResponse]]
_V = TypeVar("_V")


def _keep(table: OrderedDict[str, _V], key: str, value: _V) -> _V:
    table[key] = value
    table.move_to_end(key)
    while len(table) > TASKS_KEPT:
        table.popitem(last=False)
    return value


class Web(Extension):
    """``WebFetch`` always; ``WebSearch`` when a search key (or transport)
    is given.

    Arguments a host sets:

    * ``webfetch_model`` — the model that digests fetched pages (0.6's
      ``Options.webfetch_model``); ``None`` uses the task's model.
    * ``search_api_key`` — the Tavily key; ``None`` falls back to
      ``NOETA_WEB_SEARCH_API_KEY``, and with neither ``WebSearch`` is not
      offered.
    * ``digest`` — a model call for the digest instead of the runner's.
    * ``fetch_transport`` / ``search_transport`` — replace the httpx
      transports (tests, a proxy, a sandbox egress).
    * ``digest_timeout`` — seconds before a digest is given up (raw page
      returned), 60 by default.
    """

    name = "web"

    def __init__(self, *, webfetch_model: str | None = None,
                 search_api_key: str | None = None, digest: Complete | None = None,
                 fetch_transport: FetchTransport | None = None,
                 search_transport: SearchTransport | None = None,
                 digest_timeout: float = 60.0) -> None:
        self.webfetch_model = webfetch_model
        self._digest_call = digest
        self._caches: OrderedDict[str, PageCache] = OrderedDict()
        self.fetch = WebFetchTool(fetch_transport or HttpFetchTransport(),
                                  digest_for=self._digest_for, cache_for=self._cache_for,
                                  digest_timeout=digest_timeout)
        self._tools: list[Tool] = [self.fetch]
        key = search_api_key if search_api_key is not None else os.environ.get(
            SEARCH_API_KEY_ENV, "").strip()
        if search_transport is None and key:
            search_transport = HttpSearchTransport(key)
        if search_transport is not None:
            self._tools.append(WebSearchTool(search_transport))

    def tools(self, view: TaskView) -> Sequence[Tool]:
        return self._tools

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        if isinstance(recorded.event, TaskEnded):
            self._caches.pop(view.task_id, None)

    def _cache_for(self, ctx: ToolContext) -> PageCache:
        cache = self._caches.get(ctx.task_id)
        return _keep(self._caches, ctx.task_id, cache if cache is not None else PageCache())

    def _digest_for(self, ctx: ToolContext) -> Digest | None:
        call = self._digest_call or ctx.complete
        model = self.webfetch_model or ctx.model
        if call is None or not model:
            return None
        complete: Complete = call

        async def digest(url: str, title: str, page: str, prompt: str) -> str:
            instructions = (ctx.prompt("web/fetch_digest_docs", DOCS_DIGEST_INSTRUCTIONS)
                            if preapproved(url)
                            else ctx.prompt("web/fetch_digest", DIGEST_INSTRUCTIONS))
            text = render_digest_prompt(page_markdown=page, prompt=prompt,
                                        instructions=instructions)
            response = await complete(side_request(
                model=model, identity=ctx.identity, messages=[user(text)], effort=ctx.effort))
            return response.message.text.strip()

        return digest
