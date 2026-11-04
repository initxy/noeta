"""Several providers behind one ``provider_for``: each model call is sent to
the provider that serves its model.

**A task's chosen model routes by its log.** When a front end starts a task
or switches its model, :meth:`ProviderRouter.route_record` settles the model
once — its provider, and the limits a listing gave it — and that record is
kept in the task's config (``MODEL_ROUTE``) beside the bare model id. Every
worker then sends that model to the recorded provider
(:meth:`ProviderRouter.task_provider`) whatever its own listings say, and
fails the call when the provider is not configured there.

**Any other model resolves here**, the same way the record was made: the
endpoint that names it (``explicit``, from the configuration), else the
first endpoint whose model listing has it (the default first), else the
default endpoint. Only the configuration's part of that is the same on
every machine; a listing may differ or fail.

Listings are read only when a decision needs one, from the endpoint itself,
and kept on disk for :data:`LISTING_MAX_AGE` seconds. A 404 / 405 means the
endpoint has none (kept as an empty listing); any other failure keeps the
older copy as it was and is tried again after a growing pause.

With several endpoints, reasoning a provider signed is replayed only to that
provider: its ``Thinking.family`` is tagged ``<family>@<endpoint>`` on the
way in and restored only for that endpoint on the way out, so an adapter
drops it as foreign everywhere else.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import msgspec

from noeta.backends.providers.listing import ListedModel
from noeta.core import models
from noeta.core.errors import Fatal
from noeta.core.loop.state import MODEL_ROUTE
from noeta.core.provider import OnDelta, Provider
from noeta.core.types import Message, ModelRequest, ModelResponse, Thinking

if TYPE_CHECKING:
    from noeta.core.loop.state import TaskState

__all__ = ["LISTING_MAX_AGE", "Endpoint", "ProviderRouter"]

log = logging.getLogger("noeta.providers.routing")

#: How long a listing kept on disk is used without reading it again.
LISTING_MAX_AGE = 24 * 3600.0
#: The first pause before a failed listing read is tried again (it doubles,
#: up to :data:`LISTING_MAX_AGE`).
LISTING_RETRY_AFTER = 60.0
#: Status codes that mean the endpoint has no listing to read.
_NO_LISTING = frozenset({404, 405})
#: The limits a route record may carry.
_LIMITS = ("context_window", "max_output_tokens", "supports_vision")


@dataclass(frozen=True)
class Endpoint:
    """One configured provider. ``make`` builds its adapter on first use;
    ``listing`` reads its model listing (``None``: it has none)."""

    name: str
    family: str
    make: Callable[[], Provider]
    listing: Callable[[], list[ListedModel]] | None = None
    #: Part of the listing's cache key, so a changed endpoint is read anew.
    base_url: str | None = None


@dataclass
class _Listing:
    rows: list[ListedModel]
    #: When to read it again; ``None``: it is settled for this process.
    retry_at: float | None = None
    pause: float = LISTING_RETRY_AFTER


class ProviderRouter:
    """Model calls over several endpoints, the first the default.
    ``explicit``: model id -> endpoint name, from the configuration.
    ``cache_dir``: where listings are kept (``None``: in memory only)."""

    def __init__(self, endpoints: Sequence[Endpoint], *,
                 explicit: Mapping[str, str] | None = None,
                 cache_dir: Path | None = None,
                 max_age: float = LISTING_MAX_AGE,
                 clock: Callable[[], float] = time.time) -> None:
        if not endpoints:
            raise ValueError("a router needs at least one endpoint")
        names = [e.name for e in endpoints]
        if len(set(names)) != len(names):
            raise ValueError(f"endpoint names repeat: {names}")
        self.endpoints: dict[str, Endpoint] = {e.name: e for e in endpoints}
        self.default = endpoints[0]
        unknown = {n for n in (explicit or {}).values() if n not in self.endpoints}
        if unknown:
            raise ValueError(f"models name providers that are not configured: {sorted(unknown)}")
        self._explicit = {models.resolve_alias(m): n for m, n in (explicit or {}).items()}
        self._cache_dir = cache_dir
        self._max_age = max_age
        self._clock = clock
        self._built: dict[str, Provider] = {}
        self._tagged: dict[str, Provider] = {}
        self._listings: dict[str, _Listing] = {}
        #: Limits a listing gave, by model id (what a route record carries).
        self._listed_limits: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    @property
    def family(self) -> str:
        return self.default.family

    @property
    def several(self) -> bool:
        return len(self.endpoints) > 1

    # -- listings --------------------------------------------------------

    def _cache_file(self, endpoint: Endpoint) -> Path | None:
        if self._cache_dir is None:
            return None
        key = hashlib.sha256(f"{endpoint.family}\0{endpoint.base_url or ''}".encode())
        return self._cache_dir / f"{endpoint.name}-{key.hexdigest()[:12]}.json"

    def _read_cache(self, path: Path | None) -> tuple[float, list[ListedModel]] | None:
        if path is None:
            return None
        try:
            data = json.loads(path.read_text("utf-8"))
            rows = [ListedModel(**row) for row in data["models"]]
            return float(data["fetched_at"]), rows
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write_cache(self, path: Path | None, rows: list[ListedModel]) -> None:
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"fetched_at": self._clock(),
                                       "models": [dataclasses.asdict(r) for r in rows]}),
                           "utf-8")
            tmp.replace(path)
        except OSError as exc:
            log.debug("keeping the model listing at %s failed: %s", path, exc)

    def listing(self, name: str, *, refresh: bool = False) -> list[ListedModel]:
        """Endpoint ``name``'s models (empty when it has no listing or it
        cannot be read). Blocks while it is read; call it off the event loop."""
        endpoint = self.endpoints[name]
        if endpoint.listing is None:
            return []
        with self._lock:
            held = self._listings.get(name)
            now = self._clock()
            if held is not None and not refresh and (held.retry_at is None
                                                     or now < held.retry_at):
                return held.rows
            path = self._cache_file(endpoint)
            cached = self._read_cache(path)
            if cached is not None and not refresh and now - cached[0] < self._max_age:
                self._listings[name] = _Listing(cached[1])
                return cached[1]
            try:
                rows = endpoint.listing()
            except Exception as exc:  # noqa: BLE001 — a listing is a hint, never required
                status = exc.response.status_code \
                    if isinstance(exc, httpx.HTTPStatusError) else None
                if status in _NO_LISTING:
                    log.info("provider %s has no model listing (%s)", name, status)
                    self._write_cache(path, [])
                    self._listings[name] = _Listing([])
                    return []
                # Transient: the older copy stays as it was; ask again later.
                pause = min(held.pause * 2, self._max_age) if held is not None \
                    else LISTING_RETRY_AFTER
                log.warning("reading the model listing of provider %s failed (again in "
                            "%.0f s): %s", name, pause, exc)
                rows = cached[1] if cached is not None else (held.rows if held else [])
                self._listings[name] = _Listing(rows, retry_at=now + pause, pause=pause)
                return rows
            self._write_cache(path, rows)
            self._listings[name] = _Listing(rows)
            return rows

    def _listed(self, name: str, model: str) -> ListedModel | None:
        return next((m for m in self.listing(name) if m.id == model), None)

    def _settled(self, name: str) -> bool:
        held = self._listings.get(name)
        return held is not None and held.retry_at is None

    # -- resolving a model -------------------------------------------------

    def _known_route(self, model: str) -> Endpoint | None:
        """The route when no listing must be read for it, else ``None``."""
        real = models.resolve_alias(model)
        if real in self._explicit:
            return self.endpoints[self._explicit[real]]
        if not self.several:
            return self.default
        with self._lock:
            for name, endpoint in self.endpoints.items():
                if endpoint.listing is None:
                    continue
                if not self._settled(name):
                    return None
                if any(m.id == real for m in self._listings[name].rows):
                    return endpoint
        return self.default

    def route(self, model: str) -> Endpoint:
        """The endpoint ``model`` resolves to (reads listings when needed,
        one endpoint at a time, stopping at the first that has it)."""
        real = models.resolve_alias(model)
        if real in self._explicit:
            return self.endpoints[self._explicit[real]]
        if self.several:
            for name in self.endpoints:
                if self._listed(name, real) is not None:
                    return self.endpoints[name]
        return self.default

    def _register(self, model: str, limits: Mapping[str, Any]) -> None:
        """A catalog row for ``model`` from ``limits`` (a listing's or a
        route record's), when the catalog does not know it."""
        window = limits.get("context_window")
        if models.find_spec(model) is not None or not isinstance(window, int) or window <= 0:
            return
        output = limits.get("max_output_tokens")
        spec = models.ModelSpec(
            real_model_id=model, context_window=window,
            max_output_tokens=output if isinstance(output, int) and output > 0
            else models.UNKNOWN_MODEL_MAX_OUTPUT_TOKENS,
            # Unknown modalities: let images through for the provider to judge.
            supports_vision=limits.get("supports_vision") is not False)
        with contextlib.suppress(ValueError):  # registered meanwhile
            models.register_models({model: spec})

    def prepare(self, model: str) -> Endpoint:
        """Resolve ``model`` and, when the catalog does not know it, take its
        limits from its endpoint's listing. Blocks while listings are read."""
        endpoint = self.route(model)
        real = models.resolve_alias(model)
        if models.find_spec(real) is None:
            listed = self._listed(endpoint.name, real)
            if listed is not None and listed.context_window:
                limits: dict[str, Any] = {"context_window": listed.context_window}
                if listed.max_output_tokens:
                    limits["max_output_tokens"] = listed.max_output_tokens
                if listed.supports_vision is not None:
                    limits["supports_vision"] = listed.supports_vision
                self._listed_limits[real] = limits
                self._register(real, limits)
        return endpoint

    def route_record(self, model: str) -> dict[str, Any] | None:
        """What a task's config records with ``model`` when it is chosen
        (``MODEL_ROUTE``): its provider, and the limits a listing gave it;
        ``None`` when there is nothing a worker could decide differently
        (one endpoint, limits the catalog or configuration give). Blocks."""
        endpoint = self.prepare(model)
        real = models.resolve_alias(model)
        limits = self._listed_limits.get(real, {})
        if not self.several and not limits:
            return None
        return {"model": real, "provider": endpoint.name, **limits}

    def adopt_route(self, route: Any) -> None:
        """Make a recorded route's limits known here (a task loaded on this
        worker), before its first request is sized."""
        if isinstance(route, Mapping) and isinstance(route.get("model"), str):
            self._register(route["model"], {k: route[k] for k in _LIMITS if k in route})

    # -- providers -------------------------------------------------------

    def provider(self, name: str) -> Provider:
        """Endpoint ``name``'s adapter, built once. A failure to build it
        (no key, a bad setting) is a :class:`Fatal` model error."""
        with self._lock:
            found = self._built.get(name)
            if found is None:
                try:
                    found = self.endpoints[name].make()
                except Exception as exc:
                    raise Fatal(f"provider {name!r} cannot be used: {exc}") from exc
                self._built[name] = found
            return found

    def adopt(self, name: str, provider: Provider) -> None:
        """Use ``provider``, built already, as endpoint ``name``'s adapter."""
        with self._lock:
            self._built[name] = provider
            self._tagged.pop(name, None)

    def endpoint_provider(self, name: str) -> Provider:
        """What calls to endpoint ``name`` go through: its adapter, or with
        several endpoints one that keeps its signed reasoning its own."""
        if not self.several:
            return self.provider(name)
        with self._lock:
            found = self._tagged.get(name)
            if found is None:
                found = self._tagged[name] = _Tagged(self.endpoints[name], self.provider(name))
            return found

    def settled(self, model: str) -> bool:
        """Whether ``model``'s route and limits are known without reading
        a listing (what reading one could not change)."""
        endpoint = self._known_route(model)
        return endpoint is not None and (models.find_spec(model) is not None
                                          or endpoint.listing is None
                                          or self._settled(endpoint.name))

    def provider_for(self, model: str) -> Provider:
        """The provider ``model``'s calls go to, resolved here: the
        endpoint's when its route and limits are settled, else one that
        settles them (off the event loop) on its first call."""
        if self.settled(model):
            endpoint = self._known_route(model)
            assert endpoint is not None
            return self.endpoint_provider(endpoint.name)
        return _Routed(self, model)

    def task_provider(self, state: TaskState, model: str) -> Provider:
        """``Runtime.task_provider``: ``model`` goes where the task's
        recorded route says when the route is of that model, else as
        :meth:`provider_for` resolves it."""
        route = state.config.get(MODEL_ROUTE)
        if isinstance(route, Mapping) and route.get("model") == models.resolve_alias(model):
            name = route.get("provider")
            if name not in self.endpoints:
                raise Fatal(f"this task runs {model!r} on provider {name!r}, which is not "
                            f"configured here (providers: {', '.join(self.endpoints)})")
            self.adopt_route(route)
            return self.endpoint_provider(str(name))
        return self.provider_for(model)

    def available(self, *, refresh: bool = False) -> list[tuple[str, str]]:
        """``(model, endpoint name)`` for every model an endpoint names or
        lists, each under the endpoint it resolves to. Blocks while listings
        are read."""
        seen: dict[str, str] = dict(self._explicit)
        for name in self.endpoints:
            for listed in self.listing(name, refresh=refresh):
                seen.setdefault(listed.id, name)
        order = list(self.endpoints)
        return sorted(seen.items(), key=lambda item: (order.index(item[1]), item[0]))

    async def aclose(self) -> None:
        for provider in list(self._built.values()):
            close = getattr(provider, "aclose", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()


def _retag(messages: Sequence[Message], change: Callable[[Thinking], Thinking | None]
           ) -> list[Message] | None:
    """``messages`` with each ``Thinking`` block changed (``None``: kept as
    it is), or ``None`` when none changed."""
    out, changed = [], False
    for message in messages:
        content = []
        for block in message.content:
            new = change(block) if isinstance(block, Thinking) else None
            content.append(block if new is None else new)
            changed = changed or new is not None
        out.append(msgspec.structs.replace(message, content=content)
                   if any(a is not b for a, b in zip(content, message.content, strict=True))
                   else message)
    return out if changed else None


class _Tagged:
    """Endpoint ``endpoint``'s adapter, its signed reasoning kept its own
    (one of several endpoints)."""

    def __init__(self, endpoint: Endpoint, inner: Provider) -> None:
        self.family = endpoint.family
        self._inner = inner
        self._name = endpoint.name
        if hasattr(inner, "complete_whole"):
            self.complete_whole = self._complete_whole
        if hasattr(inner, "count_tokens"):
            self.count_tokens = inner.count_tokens

    def _tag(self, family: str | None) -> str:
        return f"{family}@{self._name}"

    def _outgoing(self, request: ModelRequest) -> ModelRequest:
        def own(block: Thinking) -> Thinking | None:
            # Ours goes back as the adapter's own; another endpoint's stays
            # tagged, so the adapter drops it as foreign.
            if block.family and block.family.endswith(f"@{self._name}"):
                return msgspec.structs.replace(block, family=block.family.rsplit("@", 1)[0])
            return None

        messages = _retag(request.messages, own)
        return request if messages is None else msgspec.structs.replace(request,
                                                                        messages=messages)

    def _incoming(self, response: ModelResponse) -> ModelResponse:
        def tag(block: Thinking) -> Thinking | None:
            if block.family is None or "@" in block.family:
                return None
            return msgspec.structs.replace(block, family=self._tag(block.family))

        messages = _retag([response.message], tag)
        return response if messages is None else msgspec.structs.replace(
            response, message=messages[0])

    async def complete(self, request: ModelRequest,
                       on_delta: OnDelta | None = None) -> ModelResponse:
        return self._incoming(await self._inner.complete(self._outgoing(request), on_delta))

    async def _complete_whole(self, request: ModelRequest) -> ModelResponse:
        response: ModelResponse = await self._inner.complete_whole(  # type: ignore[attr-defined]
            self._outgoing(request))
        return self._incoming(response)


class _Routed:
    """A provider for one model whose route is settled on its first call."""

    def __init__(self, router: ProviderRouter, model: str) -> None:
        self._router = router
        self._model = model
        self.family = router.family

    async def _inner(self) -> Provider:
        endpoint = await asyncio.to_thread(self._router.prepare, self._model)
        self.family = endpoint.family
        return self._router.endpoint_provider(endpoint.name)

    async def complete(self, request: ModelRequest,
                       on_delta: OnDelta | None = None) -> ModelResponse:
        return await (await self._inner()).complete(request, on_delta)

    async def complete_whole(self, request: ModelRequest) -> ModelResponse:
        inner = await self._inner()
        whole = getattr(inner, "complete_whole", None)
        if whole is None:
            return await inner.complete(request)
        response: ModelResponse = await whole(request)
        return response

    async def count_tokens(self, model: str, text: str) -> Any:
        count = getattr(await self._inner(), "count_tokens", None)
        return None if count is None else await count(model, text)
