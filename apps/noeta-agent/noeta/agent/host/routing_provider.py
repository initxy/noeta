"""An LLM provider that routes to multiple gateways by model (the
multi-gateway seam).

Deep module: toward the noeta runtime it exposes only the standard provider
protocol (``complete`` / ``complete_with_headers`` / ``complete_streaming``);
the "model id → gateway" mapping, each gateway's sub-provider, and each
gateway's per-request header rewrite all hide inside.

The runtime's dispatch (`noeta/runtime/llm.py::_call_provider`) probes
StreamingProvider / HeaderAwareProvider via ``isinstance`` — this class
implements all three methods, so it is recognized as both, and
`request_headers` (from HostConfig.provider_headers) is passed in on every
call.

Why not "one Client per gateway": the Client is built at process startup and
bound to a single ``provider``; per turn only the model string changes via
``model_selector`` — same provider, same base_url. Pointing different models
at different base_urls/auth therefore has to happen at the provider layer,
dispatching on ``request.model``. With only one gateway registered this class
is equivalent to calling that sub-provider directly (one dict lookup of
overhead), so single-gateway deployments behave unchanged.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from noeta.sdk import LLMRequest, LLMResponse, StreamDelta

logger = logging.getLogger(__name__)

# A gateway's rewrite of per-request headers: receives the request_headers the
# runtime passed in and returns the rewritten headers
HeaderTransform = Callable[[Optional[dict[str, str]]], Optional[dict[str, str]]]


class RoutingProvider:
    """Dispatches LLM calls to per-gateway sub-providers by ``request.model``.

    ``routes``: ``{gateway_name: (provider, header_transform | None)}``.
    ``default_gateway``: where unregistered models land (e.g. internal
    title/handoff calls if they ever go through this class).
    The model→gateway mapping is filled by build_provider from models.json via
    :meth:`register_model`.
    """

    def __init__(
        self,
        routes: dict[str, tuple[Any, Optional[HeaderTransform]]],
        default_gateway: str,
    ) -> None:
        if default_gateway not in routes:
            raise ValueError(f"default_gateway {default_gateway!r} not in routes")
        self._routes = routes
        self._default = default_gateway
        self._model_to_gateway: dict[str, str] = {}

    def register_model(self, model_id: str, gateway: str) -> None:
        """Record which gateway a model uses; an unregistered gateway falls
        back to the default (with a warning)."""
        if gateway not in self._routes:
            logger.warning(
                "model %s declares unconfigured gateway %s, falling back to %s",
                model_id, gateway, self._default,
            )
            gateway = self._default
        self._model_to_gateway[model_id] = gateway

    def _route(self, model: str) -> tuple[Any, Optional[HeaderTransform]]:
        gateway = self._model_to_gateway.get(model, self._default)
        return self._routes[gateway]

    # -- LLMProvider / HeaderAwareProvider / StreamingProvider ----------
    def complete(self, request: LLMRequest) -> LLMResponse:
        provider, _ = self._route(request.model)
        return provider.complete(request)

    def complete_with_headers(
        self,
        request: LLMRequest,
        request_headers: Optional[dict[str, str]],
    ) -> LLMResponse:
        provider, transform = self._route(request.model)
        headers = transform(request_headers) if transform else request_headers
        # A sub-provider may implement only the minimal complete — degrade by
        # capability instead of assuming everything is header-aware.
        if hasattr(provider, "complete_with_headers"):
            return provider.complete_with_headers(request, headers)
        return provider.complete(request)

    def complete_streaming(
        self,
        request: LLMRequest,
        on_delta: Callable[[StreamDelta], None],
        request_headers: Optional[dict[str, str]] = None,
    ) -> LLMResponse:
        provider, transform = self._route(request.model)
        headers = transform(request_headers) if transform else request_headers
        # This class declares complete_streaming → the runtime takes the
        # streaming path; if a sub-provider does not support streaming,
        # degrade step by step to header-aware / minimal complete (returning
        # the same LLMResponse shape).
        if hasattr(provider, "complete_streaming"):
            return provider.complete_streaming(request, on_delta, headers)
        if hasattr(provider, "complete_with_headers"):
            return provider.complete_with_headers(request, headers)
        return provider.complete(request)
