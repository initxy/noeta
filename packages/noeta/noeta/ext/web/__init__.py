"""The web tools: ``WebFetch`` and ``WebSearch``.

``WebFetch`` answers its ``prompt`` with a digest call. Tools get no model
access, so the extension supplies it: it keeps, per task, the runner's
``complete`` (from ``on_start`` / ``before_tool``) and the main request's
model and headers (from ``before_model``), and hands the tool a digest
function bound to them. ``webfetch_model`` names a cheaper model for the
digest; otherwise the task's own model answers. The main request's headers
go with the digest only when it is sent to that same model (a
``webfetch_model`` of another provider must not receive the main gateway's
credentials). A host may instead pass ``digest`` (any ``ModelRequest ->
ModelResponse`` coroutine). The per-task bindings are kept for the
:data:`TASKS_KEPT` most recently active tasks.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar

from noeta.events import Recorded, TaskEnded
from noeta.ext.base import Extension, Gate, TaskView
from noeta.ext.web.fetch import (
    Digest,
    FetchTransport,
    HttpFetchTransport,
    PageCache,
    WebFetchTool,
    render_digest_prompt,
)
from noeta.ext.web.search import (
    SEARCH_API_KEY_ENV,
    HttpSearchTransport,
    SearchTransport,
    WebSearchTool,
)
from noeta.tools.base import Tool, ToolContext
from noeta.types import ModelRequest, ModelResponse, ToolUse, user

__all__ = ["Web"]

#: Ceiling on the digest answer.
DIGEST_MAX_TOKENS = 4096
#: Tasks whose bindings (model call, main model, page cache) are kept.
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
        self._complete: OrderedDict[str, Complete] = OrderedDict()
        self._main: OrderedDict[str, tuple[str, dict[str, str]]] = OrderedDict()
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

    # ------------------------------------------------------------ binding

    async def on_start(self, view: TaskView) -> None:
        if view.complete is not None:
            _keep(self._complete, view.task_id, view.complete)

    async def before_model(self, view: TaskView, request: ModelRequest) -> ModelRequest:
        _keep(self._main, view.task_id, (request.model, dict(request.headers)))
        return request

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        if call.name == self.fetch.name:
            if view.complete is not None:
                _keep(self._complete, view.task_id, view.complete)
            # A call run right after a restart has seen no model request in
            # this process: fall back to the task's configured model.
            if view.task_id not in self._main:
                _keep(self._main, view.task_id,
                      (str(view.state.config.get("model") or ""), {}))
        return None

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        if isinstance(recorded.event, TaskEnded):
            self._complete.pop(view.task_id, None)
            self._main.pop(view.task_id, None)
            self._caches.pop(view.task_id, None)

    def _cache_for(self, ctx: ToolContext) -> PageCache:
        cache = self._caches.get(ctx.task_id)
        return _keep(self._caches, ctx.task_id, cache if cache is not None else PageCache())

    def _digest_for(self, ctx: ToolContext) -> Digest | None:
        call = self._digest_call or self._complete.get(ctx.task_id)
        main_model, headers = self._main.get(ctx.task_id, ("", {}))
        model = self.webfetch_model or main_model
        if call is None or not model:
            return None
        if model != main_model:
            headers = {}
        complete: Complete = call

        async def digest(url: str, title: str, page: str, prompt: str) -> str:
            text = render_digest_prompt(url=url, title=title, page_markdown=page, prompt=prompt)
            request = ModelRequest(model=model, messages=[user(text)],
                                   max_tokens=DIGEST_MAX_TOKENS, thinking=False,
                                   headers=headers)
            response = await complete(request)
            return response.message.text.strip()

        return digest
