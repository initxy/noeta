"""Several providers behind one ``provider_for(model)``: each model id is
sent to the provider that serves it.

A model routes to the endpoint that names it (``explicit``), else to the
first endpoint whose model listing has it (the default endpoint first, then
the others in order), else to the default endpoint. Only the model id
decides, so a task resumed on another machine with the same configuration
routes the same way. A model the catalog does not know gets its context
window from its endpoint's listing, when the listing publishes one.

Listings are read only when a decision needs one, from the endpoint itself,
and kept on disk for :data:`LISTING_MAX_AGE` seconds; a failed read falls
back to an older copy, else to no listing (one attempt per process).
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
from typing import Any

import httpx

from noeta.backends.providers.listing import ListedModel
from noeta.core import models
from noeta.core.errors import Fatal
from noeta.core.provider import OnDelta, Provider
from noeta.core.types import ModelRequest, ModelResponse

__all__ = ["LISTING_MAX_AGE", "Endpoint", "ProviderRouter"]

log = logging.getLogger("noeta.providers.routing")

#: How long a listing kept on disk is used without reading it again.
LISTING_MAX_AGE = 24 * 3600.0


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


class ProviderRouter:
    """``provider_for(model)`` over several endpoints, the first the default.
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
        #: Per endpoint: its listing once read (or found missing).
        self._listings: dict[str, list[ListedModel]] = {}
        self._lock = threading.RLock()

    @property
    def family(self) -> str:
        return self.default.family

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
            if name in self._listings and not refresh:
                return self._listings[name]
            path = self._cache_file(endpoint)
            cached = self._read_cache(path)
            if cached is not None and not refresh and self._clock() - cached[0] < self._max_age:
                self._listings[name] = cached[1]
                return cached[1]
            try:
                rows = endpoint.listing()
            except httpx.HTTPStatusError as exc:
                # The endpoint answered: it has no listing to read (kept, so
                # it is not asked again for a while).
                log.info("provider %s has no model listing: %s", name, exc)
                rows = cached[1] if cached is not None else []
                self._write_cache(path, rows)
            except Exception as exc:  # noqa: BLE001 — a listing is a hint, never required
                log.warning("reading the model listing of provider %s failed: %s", name, exc)
                rows = cached[1] if cached is not None else []
            else:
                self._write_cache(path, rows)
            self._listings[name] = rows
            return rows

    def _listed(self, name: str, model: str) -> ListedModel | None:
        return next((m for m in self.listing(name) if m.id == model), None)

    # -- routing ---------------------------------------------------------

    def _known_route(self, model: str) -> Endpoint | None:
        """The route when no listing must be read for it, else ``None``."""
        real = models.resolve_alias(model)
        if real in self._explicit:
            return self.endpoints[self._explicit[real]]
        if len(self.endpoints) == 1:
            return self.default
        with self._lock:
            for name in self.endpoints:
                if self.endpoints[name].listing is None:
                    continue
                if name not in self._listings:
                    return None
                if any(m.id == real for m in self._listings[name]):
                    return self.endpoints[name]
        return self.default

    def route(self, model: str) -> Endpoint:
        """The endpoint ``model`` is sent to (reads listings when needed)."""
        real = models.resolve_alias(model)
        if real in self._explicit:
            return self.endpoints[self._explicit[real]]
        if len(self.endpoints) > 1:
            for name in self.endpoints:
                if self._listed(name, real) is not None:
                    return self.endpoints[name]
        return self.default

    def prepare(self, model: str) -> Endpoint:
        """Route ``model`` and, when the catalog does not know it, take its
        limits from its endpoint's listing. Blocks while listings are read."""
        endpoint = self.route(model)
        real = models.resolve_alias(model)
        if models.find_spec(real) is None:
            listed = self._listed(endpoint.name, real)
            if listed is not None and listed.context_window:
                spec = models.ModelSpec(
                    real_model_id=real, context_window=listed.context_window,
                    max_output_tokens=listed.max_output_tokens
                    or models.UNKNOWN_MODEL_MAX_OUTPUT_TOKENS,
                    # Unknown modalities: let images through for the provider to judge.
                    supports_vision=listed.supports_vision is not False)
                with contextlib.suppress(ValueError):  # registered meanwhile
                    models.register_models({real: spec})
        return endpoint

    def available(self, *, refresh: bool = False) -> list[tuple[str, str]]:
        """``(model, endpoint name)`` for every model an endpoint names or
        lists, each model under the endpoint it routes to. Blocks while
        listings are read."""
        seen: dict[str, str] = {}
        for model, name in self._explicit.items():
            seen[model] = name
        for name in self.endpoints:
            for listed in self.listing(name, refresh=refresh):
                seen.setdefault(listed.id, name)
        return sorted(seen.items(), key=lambda item: (list(self.endpoints).index(item[1]),
                                                      item[0]))

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

    def provider_for(self, model: str) -> Provider:
        """The adapter ``model``'s requests go to: the endpoint's own when
        its route and limits are settled, else one that settles them (off
        the event loop) on its first call."""
        endpoint = self._known_route(model)
        if endpoint is not None and (models.find_spec(model) is not None
                                     or endpoint.listing is None
                                     or endpoint.name in self._listings):
            return self.provider(endpoint.name)
        return _Routed(self, model)

    async def aclose(self) -> None:
        for provider in list(self._built.values()):
            close = getattr(provider, "aclose", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()


class _Routed:
    """A provider for one model whose route is settled on its first call."""

    def __init__(self, router: ProviderRouter, model: str) -> None:
        self._router = router
        self._model = model
        self.family = router.family

    async def _inner(self) -> Provider:
        endpoint = await asyncio.to_thread(self._router.prepare, self._model)
        self.family = endpoint.family
        return self._router.provider(endpoint.name)

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
